from netbox.plugins import PluginConfig

from .version import __version__


class NetBoxPowerdnsSyncConfig(PluginConfig):
    name = "netbox_powerdns_sync"
    verbose_name = "NetBox PowerDNS sync"
    version = __version__
    description = "Sync DNS records in PowerDNS with NetBox"
    author = "Matej Vadnjal"
    author_email = "matej.vadnjal@arnes.si"
    base_url = "powerdns-sync"
    min_version = "3.6.0"
    default_settings = {
        "ttl_custom_field": None,
        "powerdns_managed_record_comment": "netbox-powerdns-sync",
        "post_save_enabled": False,
        "custom_domain_field": None,
        "default_rnds_value": 'not.configured.dns.server'
    }

    def ready(self):
        super().ready()


config = NetBoxPowerdnsSyncConfig
