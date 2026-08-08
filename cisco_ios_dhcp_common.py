"""
Common helper functions shared by the migrate and monitor scripts.

I pulled all the boring plumbing (env var handling, ssh, screen scraping the
IOS output) into this one file so the two actual scripts stay short and
readable. Nothing clever going on in here and thats deliberate - parsing CLI
output is fragile enough without getting fancy about it.
"""

import ipaddress
import logging
import os
import re
import sys

from netmiko import ConnectHandler

logger = logging.getLogger(__name__)

# these are the tags the netbox schema (netbox_dhcp_schema.yaml) sets up for us,
# if you change them here you need to change them there too
POOL_TAG_SLUG = "cisco-ios-dhcp-pool"
LEASE_TAG_SLUG = "cisco-ios-dhcp-lease"

# custom fields we expect to exist on the ipam.prefix object type, again these
# come from netbox_dhcp_schema.yaml
PREFIX_CUSTOM_FIELDS = [
    "ios_dhcp_pool_name",
    "ios_dhcp_source_router",
    "ios_dhcp_default_routers",
    "ios_dhcp_dns_servers",
    "ios_dhcp_domain_name",
    "ios_dhcp_ntp_servers",
    "ios_dhcp_lease_time",
    "ios_dhcp_options",
    "ios_dhcp_excluded_ranges",
    "ios_dhcp_total_addresses",
    "ios_dhcp_leased_addresses",
    "ios_dhcp_excluded_addresses",
]

REQUIRED_ENV_VARS = [
    "NETBOX_URL",
    "NETBOX_TOKEN",
    "ROUTER_IP_ADDRESS_FILE_PATH",
    "ROUTER_USERNAME",
]


def get_env_config():
    """Read everything we need out of the environment and sanity check it."""
    missing = []
    for var_name in REQUIRED_ENV_VARS:
        if not os.environ.get(var_name):
            missing.append(var_name)
    if missing:
        print("Missing required environment variables: " + ", ".join(missing))
        print("Have a read of the README, all of these need setting before we can do anything useful.")
        sys.exit(1)

    config = {
        "netbox_url": os.environ.get("NETBOX_URL"),
        "netbox_token": os.environ.get("NETBOX_TOKEN"),
        "router_file": os.environ.get("ROUTER_IP_ADDRESS_FILE_PATH"),
        "ssh_key_file": os.environ.get("ROUTER_SSH_ID_FILE_PATH"),
        "username": os.environ.get("ROUTER_USERNAME"),
        "password": os.environ.get("ROUTER_PASSWORD"),
    }

    if not config["ssh_key_file"] and not config["password"]:
        print("You need ROUTER_SSH_ID_FILE_PATH or ROUTER_PASSWORD set.")
        print("(pubkey auth is the strongly recommended option here, see the README)")
        sys.exit(1)

    if config["ssh_key_file"]:
        # expand ~ so people can just say ~/.ssh/id_rsa like a normal human
        config["ssh_key_file"] = os.path.expanduser(config["ssh_key_file"])

    return config


def read_router_list(file_path):
    """One router management address per line, blank lines and # comments are fine."""
    routers = []
    with open(file_path) as router_file:
        for line in router_file:
            line = line.strip()
            if line and not line.startswith("#"):
                routers.append(line)
    if not routers:
        print("Router list file " + file_path + " is empty, nothing to do")
        sys.exit(1)
    return routers


def connect_to_router(router_ip, config):
    """SSH to the router, netmiko does all the heavy lifting for us here."""
    device = {
        "device_type": "cisco_xe",
        "host": router_ip,
        "username": config["username"],
    }
    if config["ssh_key_file"]:
        device["use_keys"] = True
        device["key_file"] = config["ssh_key_file"]
    if config["password"]:
        device["password"] = config["password"]
    return ConnectHandler(**device)


def normalize_ip(ip_string):
    """IOS likes to print v6 addresses in SHOUTY UPPERCASE while netbox stores
    them compressed lowercase, so squash everything through the ipaddress
    module before comparing anything to anything."""
    try:
        return str(ipaddress.ip_address(ip_string))
    except ValueError:
        return ip_string


def classful_prefix_length(address):
    """If you dont give IOS a mask on the network statement it falls back to
    good old classful rules. Yes its 2026 and yes we are still writing code
    that cares about class A/B/C boundaries, IOS never forgets."""
    first_octet = int(address.split(".")[0])
    if first_octet < 128:
        return 8
    if first_octet < 192:
        return 16
    return 24


def new_pool(name, address_family):
    """Fresh empty pool dict, both the v4 and v6 config parsers fill these in."""
    return {
        "name": name,
        "address_family": address_family,
        "prefixes": [],
        "default_routers": [],
        "dns_servers": [],
        "domain_name": "",
        "ntp_servers": [],
        "lease_time": "",
        "extra_options": [],
        "excluded_ranges": [],
        "is_host_pool": False,
        "total_addresses": None,
        "leased_addresses": None,
        "excluded_addresses": None,
    }


# ---------------------------------------------------------------------------
# v4 pool config parsing (from show running-config | section ip dhcp)
# ---------------------------------------------------------------------------

V4_POOL_RE = re.compile(r"^ip dhcp pool (\S+)")
V4_EXCLUDED_RE = re.compile(
    r"^ip dhcp excluded-address (?:vrf \S+ )?(\d[\d.]*)(?:\s+(\d[\d.]*))?"
)


def parse_dhcp_pool_configs(config_output):
    """Parse the ip dhcp pool blocks out of the running config.

    Sadly there is no show command that gives you the pool options (dns-server,
    default-router and friends), they only live in the config, so scraping the
    running config it is. Returns the list of pools plus the global
    excluded-address ranges (which IOS configures globally rather than per
    pool, because of course it does).
    """
    pools = []
    excluded_ranges = []
    current_pool = None

    for line in config_output.splitlines():
        if not line.strip():
            continue

        excluded_match = V4_EXCLUDED_RE.match(line)
        if excluded_match:
            start = excluded_match.group(1)
            end = excluded_match.group(2) or start
            excluded_ranges.append((start, end))
            current_pool = None
            continue

        pool_match = V4_POOL_RE.match(line)
        if pool_match:
            current_pool = new_pool(pool_match.group(1), 4)
            pools.append(current_pool)
            continue

        if not line.startswith(" "):
            # some other top level config line snuck in, not interested
            current_pool = None
            continue

        if current_pool is not None:
            parse_v4_pool_line(line.strip(), current_pool)

    return pools, excluded_ranges


def parse_v4_pool_line(line, pool):
    """Deal with a single config line inside an ip dhcp pool block."""
    parts = line.split()
    keyword = parts[0]

    if keyword == "network" and len(parts) >= 2:
        try:
            network_address = parts[1]
            if len(parts) >= 3 and parts[2] != "secondary":
                mask = parts[2]
                if mask.startswith("/"):
                    prefix = ipaddress.ip_network(network_address + mask, strict=False)
                else:
                    prefix = ipaddress.ip_network(network_address + "/" + mask, strict=False)
            else:
                # no mask given, IOS assumes classful (see comment on the helper)
                prefix_length = classful_prefix_length(network_address)
                prefix = ipaddress.ip_network(
                    network_address + "/" + str(prefix_length), strict=False
                )
            pool["prefixes"].append(str(prefix))
        except ValueError as error:
            logger.warning("Couldnt make sense of network line '%s': %s", line, error)
    elif keyword == "default-router":
        pool["default_routers"].extend(parts[1:])
    elif keyword == "dns-server":
        pool["dns_servers"].extend(parts[1:])
    elif keyword == "domain-name" and len(parts) >= 2:
        pool["domain_name"] = parts[1]
    elif keyword == "lease":
        # lease <days> [hours [minutes]] or lease infinite, keep it verbatim
        pool["lease_time"] = " ".join(parts[1:])
    elif keyword == "option" and len(parts) >= 4 and parts[1] == "42" and parts[2] == "ip":
        # option 42 is NTP, pull it out specially seeing as people actually
        # care about that one - everything else lands in extra_options below
        pool["ntp_servers"].extend(parts[3:])
    elif keyword in ("host", "client-identifier", "hardware-address"):
        # a manual binding pool, not really a pool at all - flag it so the
        # migrate script can skip it rather than creating a bogus prefix
        pool["is_host_pool"] = True
        pool["extra_options"].append(line)
    else:
        # anything we dont specifically understand gets kept verbatim so at
        # least you can SEE it in netbox and migrate it by hand. this covers
        # option statements, netbios-name-server (hello WINS my old friend),
        # bootfile, next-server, vrf and whatever else is lurking in there
        pool["extra_options"].append(line)


V4_POOL_NAME_RE = re.compile(r"^Pool (\S+) :")
V4_POOL_STAT_RE = re.compile(r"^\s*(Total|Leased|Excluded) addresses\s*:\s*(\d+)")


def parse_dhcp_pool_stats(show_output):
    """Scrape the utilization counters out of 'show ip dhcp pool', keyed by
    pool name so the caller can merge them into the config-parsed pools."""
    stats = {}
    current_name = None
    for line in show_output.splitlines():
        name_match = V4_POOL_NAME_RE.match(line)
        if name_match:
            current_name = name_match.group(1)
            stats[current_name] = {}
            continue
        if current_name is None:
            continue
        stat_match = V4_POOL_STAT_RE.match(line)
        if stat_match:
            stat_key = stat_match.group(1).lower() + "_addresses"
            stats[current_name][stat_key] = int(stat_match.group(2))
    return stats


# ---------------------------------------------------------------------------
# v6 pool config parsing (from show running-config | section ipv6 dhcp)
# ---------------------------------------------------------------------------

V6_POOL_RE = re.compile(r"^ipv6 dhcp pool (\S+)")


def parse_dhcpv6_pool_configs(config_output):
    """Same idea as the v4 version but for ipv6 dhcp pool blocks.

    Note there is no default-router equivalent here - v6 clients learn their
    gateway from RAs, not DHCP, so dont go looking for one.
    """
    pools = []
    current_pool = None

    for line in config_output.splitlines():
        if not line.strip():
            continue

        pool_match = V6_POOL_RE.match(line)
        if pool_match:
            current_pool = new_pool(pool_match.group(1), 6)
            pools.append(current_pool)
            continue

        if not line.startswith(" "):
            current_pool = None
            continue

        if current_pool is None:
            continue

        parts = line.strip().split()
        keyword = parts[0]

        if keyword == "address" and len(parts) >= 3 and parts[1] == "prefix":
            try:
                current_pool["prefixes"].append(str(ipaddress.ip_network(parts[2], strict=False)))
            except ValueError as error:
                logger.warning("Couldnt make sense of address prefix line '%s': %s", line, error)
            if "lifetime" in parts:
                lifetime_index = parts.index("lifetime")
                current_pool["lease_time"] = " ".join(parts[lifetime_index:])
        elif keyword == "dns-server":
            current_pool["dns_servers"].extend(parts[1:])
        elif keyword == "domain-name" and len(parts) >= 2:
            current_pool["domain_name"] = parts[1]
        elif keyword == "sntp" and len(parts) >= 3 and parts[1] == "address":
            current_pool["ntp_servers"].extend(parts[2:])
        else:
            # prefix-delegation and anything else we dont handle gets kept
            # verbatim, same deal as the v4 side
            current_pool["extra_options"].append(line.strip())

    return pools


V6_SHOW_POOL_NAME_RE = re.compile(r"^DHCPv6 pool:\s+(\S+)")
V6_ACTIVE_CLIENTS_RE = re.compile(r"^\s+Active clients:\s+(\d+)")


def parse_dhcpv6_pool_stats(show_output):
    """Grab the active client count per pool from 'show ipv6 dhcp pool'."""
    stats = {}
    current_name = None
    for line in show_output.splitlines():
        name_match = V6_SHOW_POOL_NAME_RE.match(line)
        if name_match:
            current_name = name_match.group(1)
            stats[current_name] = {}
            continue
        if current_name is None:
            continue
        clients_match = V6_ACTIVE_CLIENTS_RE.match(line)
        if clients_match:
            stats[current_name]["leased_addresses"] = int(clients_match.group(1))
    return stats


def attach_excluded_ranges(pools, excluded_ranges):
    """Work out which of the globally configured excluded-address ranges live
    inside each pools prefix(es) and record them against the pool. IOS
    excluding addresses globally instead of per pool makes migrating them a
    bit of a pain, this at least puts them next to the right prefix."""
    for pool in pools:
        for prefix_str in pool["prefixes"]:
            network = ipaddress.ip_network(prefix_str)
            for start, end in excluded_ranges:
                try:
                    start_ip = ipaddress.ip_address(start)
                except ValueError:
                    continue
                if start_ip in network:
                    if start == end:
                        pool["excluded_ranges"].append(start)
                    else:
                        pool["excluded_ranges"].append(start + "-" + end)


# ---------------------------------------------------------------------------
# binding (lease) parsing
# ---------------------------------------------------------------------------

# the lease expiration column has spaces in it ("Mar 08 2026 10:30 AM") which
# is why the middle group is non greedy, and the state/interface columns only
# exist on newer IOS XE so they are optional
V4_BINDING_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})\s+"          # the leased ip
    r"(\S+)\s+"                                # first chunk of the client id
    r"(.+?)\s+"                                # lease expiration (or Infinite)
    r"(Automatic|Manual|Static|Relay)"         # binding type
    r"(?:\s+(\S+))?"                           # state (newer IOS XE only)
    r"(?:\s+(\S+))?"                           # interface (newer again)
)
# long client ids wrap onto indented continuation lines of pure hex
V4_CONTINUATION_RE = re.compile(r"^\s+([0-9a-fA-F.]+)\s*$")


def parse_dhcp_bindings(show_output):
    """Screen scrape 'show ip dhcp binding' into a list of lease dicts."""
    bindings = []
    current = None
    for line in show_output.splitlines():
        line_match = V4_BINDING_RE.match(line)
        if line_match:
            current = {
                "ip": line_match.group(1),
                "client_id": line_match.group(2),
                "lease_expiration": line_match.group(3).strip(),
                "type": line_match.group(4),
                "state": line_match.group(5) or "Active",
                "family": 4,
            }
            bindings.append(current)
            continue
        if current is not None:
            continuation_match = V4_CONTINUATION_RE.match(line)
            if continuation_match:
                current["client_id"] += continuation_match.group(1)
    return bindings


V6_CLIENT_RE = re.compile(r"^Client:\s+(\S+)")
V6_DUID_RE = re.compile(r"^\s+DUID:\s+([0-9A-Fa-f]+)")
V6_ADDRESS_RE = re.compile(r"^\s+Address:\s+([0-9A-Fa-f:]+)")
V6_EXPIRES_RE = re.compile(r"^\s+expires at\s+(.+?)(?:\s+\(|$)")


def parse_dhcpv6_bindings(show_output):
    """Screen scrape 'show ipv6 dhcp binding'. Very different beast to the v4
    output - each client is a multi line block with the DUID on one line and
    the IA NA address(es) further down.

    Note we only pick up IA NA addresses here. IA PD (prefix delegation) is a
    whole different can of worms and doesnt map to a single ip address in
    netbox anyway.
    """
    bindings = []
    current_duid = ""
    for line in show_output.splitlines():
        if V6_CLIENT_RE.match(line):
            current_duid = ""
            continue
        duid_match = V6_DUID_RE.match(line)
        if duid_match:
            current_duid = duid_match.group(1)
            continue
        address_match = V6_ADDRESS_RE.match(line)
        if address_match:
            bindings.append({
                "ip": normalize_ip(address_match.group(1)),
                "client_id": current_duid,
                "lease_expiration": "",
                "type": "Automatic",
                "state": "Active",
                "family": 6,
            })
            continue
        expires_match = V6_EXPIRES_RE.match(line)
        if expires_match and bindings and not bindings[-1]["lease_expiration"]:
            # the expires line belongs to the Address: line just above it
            bindings[-1]["lease_expiration"] = expires_match.group(1).strip()
    return bindings


# ---------------------------------------------------------------------------
# client id / DUID to mac address wrangling
# ---------------------------------------------------------------------------

def format_mac(hex_digits):
    """12 hex chars in, aa:bb:cc:dd:ee:ff out."""
    octets = []
    for i in range(0, 12, 2):
        octets.append(hex_digits[i:i + 2])
    return ":".join(octets).lower()


def client_id_to_mac(client_id):
    """Try to turn a v4 dhcp client id into a normal mac address.

    Cisco mostly builds the client id as 01 + the mac (01 being the htype for
    ethernet per RFC 2132) so 14 hex digits is really just a mac with 01 stuck
    on the front, and 12 hex digits is the mac as-is. Anything else (some
    vendors shove whole ascii strings in there) we give up and return None.
    """
    hex_digits = client_id.replace(".", "")
    if not re.fullmatch(r"[0-9a-fA-F]+", hex_digits):
        return None
    if len(hex_digits) == 14 and hex_digits.startswith("01"):
        hex_digits = hex_digits[2:]
    if len(hex_digits) != 12:
        return None
    return format_mac(hex_digits)


def duid_to_mac(duid):
    """Try to dig the mac address out of a DHCPv6 DUID.

    DUID-LLT (type 0001) is type + hwtype + a timestamp + the mac, and
    DUID-LL (type 0003) is type + hwtype + the mac. Anything else (DUID-EN,
    DUID-UUID) simply doesnt contain a mac so we return None and move on.
    """
    hex_digits = re.sub(r"[^0-9a-fA-F]", "", duid)
    if len(hex_digits) < 4:
        return None
    duid_type = hex_digits[:4]
    if duid_type == "0001" and len(hex_digits) >= 28:
        return format_mac(hex_digits[16:28])
    if duid_type == "0003" and len(hex_digits) >= 20:
        return format_mac(hex_digits[8:20])
    return None


# ---------------------------------------------------------------------------
# netbox schema sanity checks
# ---------------------------------------------------------------------------

def get_netbox_tag(nb, slug):
    """Fetch one of our tags out of netbox, bail with a friendly message if
    the schema hasnt been provisioned yet."""
    tag = nb.extras.tags.get(slug=slug)
    if tag is None:
        print("Cant find the '" + slug + "' tag in netbox.")
        print("Looks like the schema hasnt been provisioned yet - see the README,")
        print("netbox_dhcp_schema.yaml has everything you need.")
        sys.exit(1)
    return tag


def check_prefix_custom_fields(nb):
    """Make sure all the custom fields we write to actually exist, otherwise
    netbox will reject our updates with a fairly cryptic error."""
    existing_names = []
    for custom_field in nb.extras.custom_fields.all():
        existing_names.append(custom_field.name)
    missing = []
    for field_name in PREFIX_CUSTOM_FIELDS:
        if field_name not in existing_names:
            missing.append(field_name)
    if missing:
        print("Netbox is missing these custom fields: " + ", ".join(missing))
        print("The schema needs provisioning first - see the README,")
        print("netbox_dhcp_schema.yaml has the lot.")
        sys.exit(1)
