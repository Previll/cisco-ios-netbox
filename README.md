# cisco-ios-netbox
These python scripts allow you to synchronize data from Cisco IOS XE routers built-in DHCP servers for two use cases:

## Use Case #1: Migrating from Cisco IOS XE DHCP Pools to Netbox
Import the existing DHCP Pool configuration from IOS XE Devices for the purpose of migrating to Netbox 

## Use Case #2: Visibility for Router-Assigned DHCP Clients (Dynamic Leases)
Ensure DHCP Clients served by IOS DHCP Servers are correctly populated in the NetBox Prefix Utilization and IP Address views/layouts.

## Requirements
### Python

`pip install -r requirements.txt`

### Netbox
## Prerequisites & NetBox Configuration

Before running this automation, your NetBox deployment must be provisioned with the standard schema for Cisco DHCP tracking. Instead of creating individual tags and custom fields manually, you can apply the unified schema files included in this repository.

Choose one of the two deployment methods below to configure your instance.

### Option A: Automated Provisioning via Ansible (Recommended)
If you manage your infrastructure as code, use the official `netbox.netbox` collection to ingest the unified schema file. Pass the schema parameters directly into your playbook execution:

```bash
# Apply the tags and custom field schemas from the repo in one step
ansible-playbook playbooks/provision_netbox_schema.yml -e @ios_dhcp_schema.yaml
```

### Option B: Manual Setup via NetBox Web UI
If you do not use Ansible, you can import these structures directly into the NetBox administration panel using the native data import engine:

1. **Import Tags**: 
   * Navigate to **Operations > Tags** in the NetBox sidebar.
   * Click the **Import** button at the top right.
   * Paste the contents of your `tags` block from `netbox_dhcp_schema.yaml` into the window and submit.
2. **Import Custom Fields**: 
   * Navigate to **Operations > Custom Fields** (or **Customization > Custom Fields** depending on your NetBox version).
   * Click the **Import** button.
   * Paste the contents of your `custom_fields` block, ensuring the targeted object type is mapped correctly to **`IPAM > Prefix`**.

## Environment Variable
* `NETBOX_URL`: URL of the Netbox instance
* `NETBOX_TOKEN`: Netbox API Token
* `ROUTER_IP_ADDRESS_FILE_PATH`: File path to a list of IPv4/IPv6 addresses
* `ROUTER_SSH_ID_FILE_PATH`: Local path to the private SSH key file (e.g., `~/.ssh/id_rsa`) used for public-key authentication to the router.
* `ROUTER_USERNAME`: Username able to login to the provided list of routers and has permission to run required show dhcp commands (script avoids use of show run) 
* `ROUTER_PASSWORD`: (Optional) We strongly recommend the use of an ssh ID file with pubkey authentication, but this option is also available. 
* `ROUTER_IP_ADDRESS_FILE_PATH`: File path to a list of IPv4/IPv6 addresses

## Scripts

`migrate_cisco_ios_dhcp_pools.py` - used for use case #1
`monitor_cisco_ios_dhcp_leases.py` - runs looped and interrogates IOS devices for new leases every IOS_DHCP_DISCOVERY_INTERVAL, default value is 240 minutes (4 hours) 

