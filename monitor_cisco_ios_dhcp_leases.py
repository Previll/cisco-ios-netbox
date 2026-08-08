#!/usr/bin/env python3
"""
monitor_cisco_ios_dhcp_leases.py

Use case #2 - runs looped, and every IOS_DHCP_DISCOVERY_INTERVAL minutes
(default 240, i.e. 4 hours) it sshes to each router in the list, grabs the
active dhcp bindings (v4 and v6) and makes sure netbox has a matching ip
address object for each one, status set to DHCP. That way your prefix
utilization view actually reflects reality instead of ancient history.

Leases that have disappeared off the routers get cleaned out of netbox too,
BUT only ones carrying our tag - if an address already existed in netbox
without the tag we assume a human put it there and we keep our hands off it.

Unlike the migrate script this one sticks to show commands only, no config
reading required.
"""

import logging
import os
import sys
import time

import pynetbox

from cisco_ios_dhcp_common import (
    LEASE_TAG_SLUG,
    client_id_to_mac,
    connect_to_router,
    duid_to_mac,
    get_env_config,
    get_netbox_tag,
    normalize_ip,
    parse_dhcp_bindings,
    parse_dhcpv6_bindings,
    read_router_list,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 240


def collect_bindings_from_router(connection):
    """Grab the v4 and v6 bindings off a router in one hit. The read timeouts
    are generous on purpose, a busy branch router can take a while to dump a
    big binding table."""
    bindings = parse_dhcp_bindings(
        connection.send_command("show ip dhcp binding", read_timeout=120)
    )
    # boxes with no v6 dhcp just return nothing here, which is fine
    bindings.extend(parse_dhcpv6_bindings(
        connection.send_command("show ipv6 dhcp binding", read_timeout=120)
    ))
    return bindings


def get_prefix_length_for_ip(nb, lease_ip, family):
    """Find the most specific netbox prefix containing this ip so we can give
    the address a sensible mask instead of slapping /32 on everything. Falls
    back to /32 (or /128) if netbox has never heard of the subnet."""
    best_length = None
    for prefix in nb.ipam.prefixes.filter(contains=lease_ip):
        length = int(str(prefix.prefix).split("/")[1])
        if best_length is None or length > best_length:
            best_length = length
    if best_length is not None:
        return best_length
    if family == 6:
        return 128
    return 32


def build_lease_description(binding, router_ip):
    """Human readable description for the netbox ip address object."""
    if binding["family"] == 6:
        mac = duid_to_mac(binding["client_id"])
        fallback_label = "duid "
    else:
        mac = client_id_to_mac(binding["client_id"])
        fallback_label = "client-id "

    if mac:
        client = "mac " + mac
    else:
        client = fallback_label + binding["client_id"]

    description = "DHCP lease from " + router_ip + " (" + client
    if binding["lease_expiration"]:
        description += ", expires " + binding["lease_expiration"]
    return description + ")"


def sync_lease_to_netbox(nb, binding, router_ip, lease_tag):
    """Create or refresh a single lease in netbox. Returns what happened so
    the caller can keep score."""
    address = binding["ip"] + "/" + str(
        get_prefix_length_for_ip(nb, binding["ip"], binding["family"])
    )
    description = build_lease_description(binding, router_ip)

    # filter rather than get because the same ip can exist more than once in
    # netbox (different vrfs etc) and get() throws a wobbly if theres several
    existing = list(nb.ipam.ip_addresses.filter(address=binding["ip"]))
    if not existing:
        nb.ipam.ip_addresses.create(
            address=address,
            status="dhcp",
            description=description,
            tags=[lease_tag.id],
        )
        logger.info("Created lease %s", address)
        return "created"

    nb_ip = existing[0]
    tag_slugs = []
    for existing_tag in nb_ip.tags:
        tag_slugs.append(existing_tag.slug)
    if LEASE_TAG_SLUG not in tag_slugs:
        # somebody created this address by hand, its not ours to fiddle with
        logger.info("Leaving %s alone, it exists in netbox without our tag", binding["ip"])
        return "skipped"

    changed = nb_ip.update({
        "address": address,
        "status": "dhcp",
        "description": description,
    })
    if changed:
        logger.info("Updated lease %s", address)
        return "updated"
    return "unchanged"


def prune_stale_leases(nb, active_ips, lease_tag_slug):
    """Anything in netbox wearing our lease tag that the routers no longer
    know about is stale, so out it goes."""
    deleted = 0
    for nb_ip in nb.ipam.ip_addresses.filter(tag=lease_tag_slug):
        plain_ip = normalize_ip(str(nb_ip.address).split("/")[0])
        if plain_ip not in active_ips:
            logger.info("Removing stale lease %s", nb_ip.address)
            nb_ip.delete()
            deleted += 1
    return deleted


def run_discovery_cycle(nb, config, routers, lease_tag):
    """One full pass over all the routers."""
    active_ips = set()
    failed_routers = []
    counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0}

    for router_ip in routers:
        logger.info("Polling %s", router_ip)
        try:
            connection = connect_to_router(router_ip, config)
            bindings = collect_bindings_from_router(connection)
            connection.disconnect()
        except Exception as error:
            logger.error("Problem talking to %s: %s", router_ip, error)
            failed_routers.append(router_ip)
            continue

        logger.info("%s has %s binding(s)", router_ip, len(bindings))
        for binding in bindings:
            # only active leases count, syncing expired ones would just
            # pollute netbox with clients that arent there anymore
            if binding["state"] != "Active":
                continue
            binding["ip"] = normalize_ip(binding["ip"])
            active_ips.add(binding["ip"])
            result = sync_lease_to_netbox(nb, binding, router_ip, lease_tag)
            counts[result] += 1

    if failed_routers:
        # if a router didnt answer we have no idea whether its leases are
        # still valid, so pruning now would throw the baby out with the
        # bathwater - skip the cleanup this cycle and try again next time
        logger.warning(
            "Skipping stale lease cleanup, %s router(s) didnt answer: %s",
            len(failed_routers), ", ".join(failed_routers),
        )
    else:
        counts["deleted"] = prune_stale_leases(nb, active_ips, LEASE_TAG_SLUG)

    logger.info(
        "Cycle done: %s created, %s updated, %s unchanged, %s skipped, %s deleted",
        counts["created"], counts["updated"], counts["unchanged"],
        counts["skipped"], counts["deleted"],
    )


def main():
    config = get_env_config()
    interval_minutes = int(
        os.environ.get("IOS_DHCP_DISCOVERY_INTERVAL", DEFAULT_INTERVAL_MINUTES)
    )
    nb = pynetbox.api(config["netbox_url"], token=config["netbox_token"])
    lease_tag = get_netbox_tag(nb, LEASE_TAG_SLUG)
    routers = read_router_list(config["router_file"])

    logger.info(
        "Starting lease discovery loop, %s router(s), interval %s minutes",
        len(routers), interval_minutes,
    )

    while True:
        try:
            run_discovery_cycle(nb, config, routers, lease_tag)
        except Exception as error:
            # belt and braces - a netbox hiccup shouldnt kill a long running
            # monitor, log it and have another crack next cycle
            logger.error("Discovery cycle blew up: %s", error)
        logger.info("Sleeping for %s minutes", interval_minutes)
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCaught ctrl-c, bye")
        sys.exit(0)
