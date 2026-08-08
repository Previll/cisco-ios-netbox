#!/usr/bin/env python3
"""
migrate_cisco_ios_dhcp_pools.py

Use case #1 - point this at your IOS XE routers and it pulls the full DHCP
pool setup off each one (v4 AND v6) and creates matching prefixes in netbox,
tagged and loaded up with custom fields covering everything you need to
rebuild the pool somewhere else: dns servers, default gateways, domain name,
ntp servers, lease timers, excluded ranges and any raw options we didnt
specifically understand.

Heads up: the pool options only exist in the config, no show ip dhcp command
will give them to you, so this script DOES read the running config (via
show running-config | section). It is what it is.

Run it as many times as you like, its idempotent - existing prefixes just get
their custom fields refreshed rather than duplicated, and tags that other
people have put on the prefix are left alone.
"""

import logging
import sys

import pynetbox

from cisco_ios_dhcp_common import (
    POOL_TAG_SLUG,
    attach_excluded_ranges,
    check_prefix_custom_fields,
    connect_to_router,
    get_env_config,
    get_netbox_tag,
    parse_dhcp_pool_configs,
    parse_dhcp_pool_stats,
    parse_dhcpv6_pool_configs,
    parse_dhcpv6_pool_stats,
    read_router_list,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def collect_pools_from_router(connection):
    """Run the show commands and glue the results together into one list of
    pool dicts covering both address families."""
    # v4 first - config for the pool details, show command for the counters
    v4_config = connection.send_command(
        "show running-config | section ip dhcp", read_timeout=60
    )
    v4_pools, excluded_ranges = parse_dhcp_pool_configs(v4_config)
    attach_excluded_ranges(v4_pools, excluded_ranges)

    v4_stats = parse_dhcp_pool_stats(
        connection.send_command("show ip dhcp pool", read_timeout=60)
    )
    for pool in v4_pools:
        pool.update(v4_stats.get(pool["name"], {}))

    # now the v6 side, same deal. on a box with no v6 dhcp configured these
    # commands just come back empty which suits us fine
    v6_config = connection.send_command(
        "show running-config | section ipv6 dhcp", read_timeout=60
    )
    v6_pools = parse_dhcpv6_pool_configs(v6_config)

    v6_stats = parse_dhcpv6_pool_stats(
        connection.send_command("show ipv6 dhcp pool", read_timeout=60)
    )
    for pool in v6_pools:
        pool.update(v6_stats.get(pool["name"], {}))

    return v4_pools + v6_pools


def build_custom_fields(pool, router_ip):
    """Flatten a pool dict into the custom field values netbox expects."""
    return {
        "ios_dhcp_pool_name": pool["name"],
        "ios_dhcp_source_router": router_ip,
        "ios_dhcp_default_routers": ", ".join(pool["default_routers"]),
        "ios_dhcp_dns_servers": ", ".join(pool["dns_servers"]),
        "ios_dhcp_domain_name": pool["domain_name"],
        "ios_dhcp_ntp_servers": ", ".join(pool["ntp_servers"]),
        "ios_dhcp_lease_time": pool["lease_time"],
        "ios_dhcp_options": "\n".join(pool["extra_options"]),
        "ios_dhcp_excluded_ranges": ", ".join(pool["excluded_ranges"]),
        "ios_dhcp_total_addresses": pool["total_addresses"],
        "ios_dhcp_leased_addresses": pool["leased_addresses"],
        "ios_dhcp_excluded_addresses": pool["excluded_addresses"],
    }


def sync_pool_to_netbox(nb, pool, router_ip, pool_tag):
    """Create (or refresh) a netbox prefix for each subnet in a dhcp pool.
    Returns a (created, updated) tuple for the summary at the end."""
    created = 0
    updated = 0

    if pool["is_host_pool"]:
        # manual binding pools dont describe a subnet so theres no sensible
        # prefix to create for them - migrate those by hand
        logger.warning(
            "Skipping pool %s on %s - its a manual binding (host) pool",
            pool["name"], router_ip,
        )
        return created, updated

    if not pool["prefixes"]:
        logger.warning(
            "Pool %s on %s has no network/prefix statement, skipping it",
            pool["name"], router_ip,
        )
        return created, updated

    custom_fields = build_custom_fields(pool, router_ip)
    description = "IOS DHCPv" + str(pool["address_family"]) + " pool " + pool["name"] + " from " + router_ip

    for prefix_str in pool["prefixes"]:
        nb_prefix = nb.ipam.prefixes.get(prefix=prefix_str)
        if nb_prefix is None:
            nb.ipam.prefixes.create(
                prefix=prefix_str,
                status="active",
                description=description,
                tags=[pool_tag.id],
                custom_fields=custom_fields,
            )
            logger.info("Created prefix %s for pool %s", prefix_str, pool["name"])
            created += 1
        else:
            # dont clobber tags somebody else has put on it, just make sure
            # ours is in there
            tag_ids = []
            for existing_tag in nb_prefix.tags:
                tag_ids.append(existing_tag.id)
            if pool_tag.id not in tag_ids:
                tag_ids.append(pool_tag.id)
            nb_prefix.update({
                "description": description,
                "tags": tag_ids,
                "custom_fields": custom_fields,
            })
            logger.info("Refreshed prefix %s for pool %s", prefix_str, pool["name"])
            updated += 1

    return created, updated


def main():
    config = get_env_config()
    nb = pynetbox.api(config["netbox_url"], token=config["netbox_token"])

    # check the schema is in place BEFORE we start talking to routers,
    # nothing worse than getting halfway through and falling over
    pool_tag = get_netbox_tag(nb, POOL_TAG_SLUG)
    check_prefix_custom_fields(nb)

    routers = read_router_list(config["router_file"])
    total_created = 0
    total_updated = 0
    failed_routers = []

    for router_ip in routers:
        logger.info("Connecting to %s", router_ip)
        try:
            connection = connect_to_router(router_ip, config)
            pools = collect_pools_from_router(connection)
            connection.disconnect()
        except Exception as error:
            logger.error("Problem talking to %s: %s", router_ip, error)
            failed_routers.append(router_ip)
            continue

        if not pools:
            logger.info("No dhcp pools found on %s, moving on", router_ip)
            continue

        logger.info("Found %s pool(s) on %s", len(pools), router_ip)
        for pool in pools:
            created, updated = sync_pool_to_netbox(nb, pool, router_ip, pool_tag)
            total_created += created
            total_updated += updated

    logger.info(
        "All done: %s prefix(es) created, %s refreshed, %s router(s) unreachable",
        total_created, total_updated, len(failed_routers),
    )
    if failed_routers:
        logger.warning("Unreachable: %s", ", ".join(failed_routers))
        sys.exit(1)


if __name__ == "__main__":
    main()
