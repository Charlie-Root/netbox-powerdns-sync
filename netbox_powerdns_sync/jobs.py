import logging
import traceback
from datetime import timedelta

from core.choices import JobStatusChoices
from core.models import Job
from dcim.models import Device, Interface
from django.db.models import Q
from extras.choices import LogLevelChoices
from ipam.models import FHRPGroup, IPAddress
from netaddr import IPNetwork
from virtualization.models import VirtualMachine, VMInterface

from netbox_powerdns_sync.constants import FAMILY_TYPES, PTR_TYPE

from .exceptions import PowerdnsSyncNoServers, PowerdnsSyncServerZoneMissing
from .models import ApiServer, Zone
from .naming import generate_fqdn
from .record import DnsRecord
from .utils import (
    get_custom_domain,
    get_ip_ttl,
    make_canonical,
    make_dns_label,
    set_dns_name,
    get_default_rdns
)

logger = logging.getLogger("netbox.netbox_powerdns_sync.jobs")


class JobLoggingMixin:
    def log(self, level: str, msg: str) -> None:
        data = self.job.data or {}
        logs = data.get("log", [])
        logs.append(
            {
                "message": msg,
                "status": level,
            }
        )
        data["log"] = logs
        self.job.data = data

    def log_debug(self, msg: str) -> None:
        logger.debug(msg)
        self.log(LogLevelChoices.LOG_DEFAULT, msg)

    def log_success(self, msg: str) -> None:
        logger.info(msg)
        self.log(LogLevelChoices.LOG_SUCCESS, msg)

    def log_info(self, msg: str) -> None:
        logger.info(msg)
        self.log(LogLevelChoices.LOG_INFO, msg)

    def log_warning(self, msg: str) -> None:
        logger.warning(msg)
        self.log(LogLevelChoices.LOG_WARNING, msg)

    def log_failure(self, msg: str) -> None:
        logger.error(msg)
        self.log(LogLevelChoices.LOG_FAILURE, msg)


class PowerdnsTask(JobLoggingMixin):
    def __init__(self, job: Job) -> None:
        self.job = job
        self.init_attrs()

    def init_attrs(self):
        self.fqdn: str = ""
        self.forward_zone: Zone = None
        self.reverse_zone: Zone = None
        self.make_fqdn_ran: bool = False

    def get_pdns_servers_for_zone(self, zone_name: str) -> list[ApiServer]:
        if not zone_name:
            return []
        zone = Zone.objects.get(name=zone_name)
        return zone.api_servers.enabled().all()

    def add_to_output(self, row):
        if not self.job.data:
            self.job.data = dict()
        if "output" not in self.job.data:
            self.job.data["output"] = []
        self.job.data["output"].append(row)

    def make_name_from_interface(
        self, interface: Interface | VMInterface, host: Device | VirtualMachine
    ) -> str:
        name = host.name
        name = ".".join(map(make_dns_label, name.split(".")))
        if self.ip != host.primary_ip4 and self.ip != host.primary_ip6:
            name = make_dns_label(interface.name) + "." + name
        return name

    def make_fqdn(self) -> str:
        """Determines FQDN and sets forward zone"""
        if self.make_fqdn_ran:
            return self.fqdn
        self.make_fqdn_ran = True
        if self.determine_forward_zone():
            self.fqdn = generate_fqdn(self.ip, self.forward_zone)
        return self.fqdn

    def determine_forward_zone(self):
        # determine zone from any FQDN names
        name = None
        if self.ip.dns_name:
            name = self.ip.dns_name
        elif isinstance(self.ip.assigned_object, Interface):
            name = self.ip.assigned_object.device.name
        elif isinstance(self.ip.assigned_object, VMInterface):
            name = self.ip.assigned_object.virtual_machine.name
        elif isinstance(self.ip.assigned_object, FHRPGroup):
            name = self.ip.assigned_object.name
        if name:
            self.forward_zone = Zone.get_best_zone(name)
        # determine zone by matching tags or roles
        if not self.forward_zone:
            self.forward_zone = Zone.match_ip(self.ip).first()

        if not self.forward_zone:
            self.forward_zone = get_custom_domain(self.ip)

        return self.forward_zone

    def make_reverse_domain(self) -> str | None:
        """Returns reverse domain name"""
        self.log_debug(f"Making reverse domain for {self.ip}")
        return make_canonical(self.ip.address.ip.reverse_dns)

    def create_record(self, dns_record: DnsRecord) -> None:
        servers = self.get_pdns_servers_for_zone(dns_record.zone_name)

        if not servers:
            raise PowerdnsSyncNoServers(
                f"No valid servers found for zone {dns_record.zone_name}"
            )

        for api_server in self.get_pdns_servers_for_zone(dns_record.zone_name):
            zone = api_server.api.get_zone(dns_record.zone_name)
            if not zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {dns_record.zone_name} not found on server {api_server}"
                )
            self.add_to_output(
                {
                    "action": "CREATE",
                    "rr": str(dns_record),
                    "zone": str(zone),
                    "server": str(api_server),
                }
            )
            zone.create_records([dns_record.as_rrset()])

    def delete_record(self, dns_record: DnsRecord) -> None:
        servers = self.get_pdns_servers_for_zone(dns_record.zone_name)
        if not servers:
            raise PowerdnsSyncNoServers(
                f"No valid servers found for zone {dns_record.zone_name}"
            )
        for api_server in self.get_pdns_servers_for_zone(dns_record.zone_name):
            zone = api_server.api.get_zone(dns_record.zone_name)
            if not zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {dns_record.zone_name} not found on server {api_server}"
                )
            self.add_to_output(
                {
                    "action": "DELETE",
                    "rr": str(dns_record),
                    "zone": str(zone),
                    "server": str(api_server),
                }
            )
            zone.delete_records([dns_record.as_rrset()])


class PowerdnsTaskIP(PowerdnsTask):
    def __init__(self, job: Job) -> None:
        super().__init__(job)
        self.ip: IPAddress = job.object
        self.log_debug(f"IP: {self.ip}")

    @classmethod
    def run_update_ip(cls, job: Job, *args, **kwargs) -> None:
        task = cls(job)
        if job.object_id and not job.object:
            task.job.start()
            task.log_warning(
                "No IP Address object given. IP was probably removed or DB transaction aborted, nothing to do."
            )
            task.job.terminate(status=JobStatusChoices.STATUS_COMPLETED)
            return
        try:
            task.log_debug("Starting task")
            task.job.start()
            task.log_debug("Creating forward record")
            task.create_forward()
            task.log_debug("Creating reverse record")
            task.create_reverse()
            task.log_success("Finished")
            task.job.terminate()
        except Exception as e:
            task.log_failure(f"error {e}")
            task.job.data = task.job.data or dict()
            task.job.data["exception"] = str(e)
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
            raise e

    def create_forward(self) -> None:
        self.make_fqdn()

        if not self.forward_zone:
            self.log_info(f"No matching forward zone found for IP:{self.ip}. Skipping")
        else:
            self.log_info(f"Found matching forward zone to be {self.forward_zone}")

        if not self.fqdn:
            self.log_info(
                f"No FQDN could be determined for IP:{self.ip} (zone:{self.forward_zone}). Skipping"
            )

        reverse_fqdn = self.make_reverse_domain()
        self.log_debug(f"Reverse FQDN: {reverse_fqdn}")
        self.reverse_zone = Zone.get_best_zone(str(reverse_fqdn))
        if not self.reverse_zone:
            self.log_info(
                f"No matching reverse zone for {self.ip} ({self.fqdn}). Skipping"
            )

        self.log_debug(
            f"Reverse zone found for IP:{self.ip} (zone:{self.reverse_zone})"
        )
        name = self.fqdn.replace(self.forward_zone.name, "").rstrip(".")
        fqdn = generate_fqdn(self.ip, self.reverse_zone)
        custom_domain = get_custom_domain(self.ip)

        dns_record = DnsRecord(
            name=name,
            dns_type=FAMILY_TYPES[self.ip.family],
            data=str(self.ip.address.ip),
            ttl=get_ip_ttl(self.ip) or self.reverse_zone.default_ttl,
            zone_name=self.forward_zone.name,
        )
        self.log_info(f"Forward record: {dns_record}")
        self.create_record(dns_record)
        self.log_info(f"Forward record created")

    def create_reverse(self) -> None:
        self.make_fqdn()

        if not self.forward_zone:
            self.log_info(f"No matching forward zone found for IP:{ip}. Skipping")
        else:
            self.log_info(f"Found matching forward zone to be {self.forward_zone}")

        if not self.fqdn:
            self.log_info(
                f"No FQDN could be determined for IP:{ip} (zone:{self.forward_zone}). Skipping"
            )

        reverse_fqdn = self.make_reverse_domain()
        self.reverse_zone = Zone.get_best_zone(reverse_fqdn)

        if not self.reverse_zone:
            self.log_warning(
                f"No reverse zone for IP:{self.ip} fqdn:{self.fqdn} Skipping"
            )
            return

        name = reverse_fqdn.replace(self.reverse_zone.name, "").rstrip(".")
        fqdn = generate_fqdn(self.ip, self.reverse_zone)
        custom_domain = get_custom_domain(self.ip)
        dns_record = DnsRecord(
            name=name,
            dns_type=PTR_TYPE,
            data=f"{fqdn or ''}{custom_domain or ''}.",
            ttl=get_ip_ttl(self.ip) or self.reverse_zone.default_ttl,
            zone_name=self.reverse_zone.name,
        )

        self.log_info(f"Reverse record {dns_record}")
        self.create_record(dns_record)
        set_dns_name(self.ip, dns_record.data)
        self.log_success("Reverse record created")


class PowerdnsTaskFullSync(PowerdnsTask):
    def __init__(self, job: Job) -> None:
        super().__init__(job)
        self.zone: Zone = job.object

    @classmethod
    def run_full_sync(cls, job: Job, *args, **kwargs) -> None:
        task = cls(job)

        try:
            task.log_debug(f"Starting sync for zone {task.zone}")
            task.job.start()
            if not task.zone.enabled:
                task.log_warning(
                    f"Zone {task.zone} is disabled for updates, not syncing"
                )
                task.job.terminate()
                return
            task.log_debug("Loading Netbox records")
            netbox_records = task.load_netbox_records()
            task.log_debug("Loading Powerdns records")
            pdns_records = task.load_pdns_records()
            task.log_info(
                f"Found record count: netbox:{len(netbox_records)} pdns:{len(pdns_records)}"
            )
            to_delete = pdns_records - netbox_records
            to_create = netbox_records - pdns_records
            task.log_info(
                f"Record change count: to_delete:{len(to_delete)} to_create:{len(to_create)}"
            )
            for record in to_delete:
                task.delete_record(record)
            for record in to_create:
                task.create_record(record)
            task.log_success("Finished")
            task.job.terminate()
        except PowerdnsSyncNoServers as e:
            task.log_failure(str(e))
            task.job.data = task.job.data or dict()
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)
        except Exception as e:
            stacktrace = traceback.format_exc()
            task.log_failure(
                f"An exception occurred: `{type(e).__name__}: {e}`\n```\n{stacktrace}\n```"
            )
            task.job.data = task.job.data or dict()
            task.job.terminate(status=JobStatusChoices.STATUS_ERRORED)

        # Schedule the next job if an interval has been set
        if job.interval:
            new_scheduled_time = job.scheduled + timedelta(minutes=job.interval)
            Job.enqueue(
                cls.run_full_sync,
                instance=job.object,
                name=job.name,
                user=job.user,
                schedule_at=new_scheduled_time,
                interval=job.interval,
            )

    def get_addresses(self):
        """Get IPAddress objects that could have DNS records"""
        zone_canonical = self.zone.name
        zone_domain = self.zone.name.rstrip(".")
        self.log_debug(f"Zone canonical: {zone_canonical}")
        self.log_debug(f"Zone domain: {zone_domain}")

        # filter for FQDN names (ip.dns_name, Device, VM, FHRPGroup)
        query_zone = Q(dns_name__endswith=zone_canonical) | Q(
            dns_name__endswith=zone_domain
        )
        query_zone |= Q(interface__device__name__endswith=zone_canonical) | Q(
            interface__device__name__endswith=zone_domain
        )
        query_zone |= Q(
            vminterface__virtual_machine__name__endswith=zone_canonical
        ) | Q(vminterface__virtual_machine__name__endswith=zone_domain)
        query_zone |= Q(fhrpgroup__name__endswith=zone_canonical) | Q(
            fhrpgroup__name__endswith=zone_domain
        )

        self.log_debug("Checking if this is a rdns zone")
        parts = zone_domain.split(".")

        network_cidr = None
        if (
            len(parts) >= 3
            and parts[-2] == "in-addr"
            and parts[-1] == "arpa"
            or len(parts) > 3
            and parts[-1] == "ip6"
        ):
            self.log_debug(f"Zone is reverse zone, looking for prefixes")

            if len(parts) >= 3 and parts[-2] == "in-addr" and parts[-1] == "arpa":
                if len(parts) == 5:  # e.g., 0.72.10.in-addr.arpa -> 10.72.0.0/24
                    base_ip = f"{parts[2]}.{parts[1]}.{parts[0]}.0"
                    network_cidr = IPNetwork(f"{base_ip}/24")
                elif len(parts) == 4:  # e.g., 72.10.in-addr.arpa -> 10.72.0.0/16
                    base_ip = f"{parts[1]}.{parts[0]}.0.0"
                    network_cidr = IPNetwork(f"{base_ip}/16")
                elif len(parts) == 3:  # e.g., 10.in-addr.arpa -> 10.0.0.0/8
                    base_ip = f"{parts[0]}.0.0.0"
                    network_cidr = IPNetwork(f"{base_ip}/8")

                else:
                    network_cidr = None
        elif len(parts) > 3 and parts[-2] == "ip6" and parts[-1] == "arpa":
            self.log_debug(f"Zone is IPv6 reverse zone, looking for prefixes")
            # IPv6 reverse DNS is in nibbles, e.g., 1.0.0.0.2.ip6.arpa -> 2000::/32
            reversed_nibbles = parts[:-2]
            reversed_nibbles.reverse()

            # Join nibbles and separate into groups of 4 hex digits
            ipv6_nibbles = "".join(reversed_nibbles)
            ipv6_address_parts = [
                ipv6_nibbles[i : i + 4] for i in range(0, len(ipv6_nibbles), 4)
            ]

            # Calculate the network prefix length
            network_length = len(reversed_nibbles) * 4

            # Ensure the address has a valid IPv6 format
            try:
                # Join parts with ':' to form valid IPv6 address notation
                ipv6_address = ":".join(ipv6_address_parts)
                ipv6_network = IPNetwork(f"{ipv6_address}::/{network_length}")
                network_cidr = ipv6_network
            except Exception as e:
                self.log_debug(f"Invalid IPv6 address constructed: {ipv6_address}")
                self.log_debug(f"AddrFormatError: {e}")
                network_cidr = None

        if network_cidr:
            self.log_debug(
                f"Prefix found, going to check for hosts between {network_cidr.network} and {network_cidr.broadcast}"
            )
            # Query any address within the CIDR range
            query_zone |= Q(address__net_host_contained=network_cidr)
        else:
            self.log_debug("No rDNS zone found.")

        # filter for matchers (tags & roles)
        query_zone |= Q(tags__in=self.zone.match_ipaddress_tags.all())
        query_zone |= Q(interface__tags__in=self.zone.match_interface_tags.all())
        query_zone |= Q(vminterface__tags__in=self.zone.match_interface_tags.all())
        query_zone |= Q(interface__device__tags__in=self.zone.match_device_tags.all())
        query_zone |= Q(
            vminterface__virtual_machine__tags__in=self.zone.match_device_tags.all()
        )
        query_zone |= Q(fhrpgroup__tags__in=self.zone.match_fhrpgroup_tags.all())
        query_zone |= Q(interface__device__role__in=self.zone.match_device_roles.all())
        query_zone |= Q(
            vminterface__virtual_machine__role__in=self.zone.match_device_roles.all()
        )
        results = IPAddress.objects.filter(query_zone)
        if self.zone.match_interface_mgmt_only:
            results = results.filter(interface__mgmt_only=True)
        return results

    def load_netbox_records(self) -> set[DnsRecord]:
        records = set()
        ip: IPAddress
        ip_addresses = self.get_addresses()

        self.log_info(f"Found {ip_addresses.count()} matching addresses to check")
        for ip in ip_addresses:
            self.log_debug(f"Checking IP: {ip}")
            self.init_attrs()
            self.ip = ip
            self.make_fqdn()
            if not self.forward_zone:
                self.log_info(f"No matching forward zone found for IP: {ip}. Skipping")
            else:
                self.log_info(f"Found matching forward zone to be {self.forward_zone}")

            if not self.fqdn:
                self.log_info(
                    f"No FQDN could be determined for IP: {ip} (zone:{self.forward_zone}). Skipping"
                )

            self.log_debug(f"Forward FQDN: {self.fqdn}")
            self.log_debug(f"Self zone: {self.zone}")

            if self.forward_zone and self.forward_zone == self.zone:
                name = self.fqdn.replace(self.forward_zone.name, "").rstrip(".")
                self.log_info(
                    f"Forward zone is matching self.zone, creating forward record for {name}"
                )
                records.add(
                    DnsRecord(
                        name=name,
                        data=str(ip.address.ip),
                        dns_type=FAMILY_TYPES.get(ip.family),
                        zone_name=self.forward_zone.name,
                        ttl=get_ip_ttl(ip) or self.forward_zone.default_ttl,
                    )
                )

            if self.zone.is_reverse:
                reverse_fqdn = self.make_reverse_domain()
                self.log_debug(f"Reverse FQDN: {reverse_fqdn}")
                self.reverse_zone = Zone.get_best_zone(reverse_fqdn)
                self.log_debug(
                    f"Reverse zone found for IP:{ip} (zone:{self.reverse_zone})"
                )

                if not self.reverse_zone:
                    self.log_info(
                        f"No matching reverse zone for {ip} ({self.fqdn}). Skipping"
                    )
                    continue

                if self.reverse_zone == self.zone:
                    name = reverse_fqdn.replace(self.reverse_zone.name, "").rstrip(".")
                    
                    fqdn = generate_fqdn(self.ip, self.reverse_zone)
                    custom_domain = get_custom_domain(self.ip)

                    self.log_debug(
                        f"Reverse name: {name} - {fqdn} - {self.reverse_zone.name} - {custom_domain}"
                    )
                    
                    if fqdn == "":
                        fqdn = get_default_rdns()
                        
                    records.add(
                        DnsRecord(
                            name=name,
                            dns_type=PTR_TYPE,
                            data=f"{fqdn or ''}{custom_domain or ''}.",
                            ttl=get_ip_ttl(self.ip) or self.reverse_zone.default_ttl,
                            zone_name=self.reverse_zone.name,
                        )
                    )

                    set_dns_name(self.ip, f"{fqdn or ''}{custom_domain or ''}.")

        return records

    def load_pdns_records(self) -> set[DnsRecord]:
        flat_records = set()
        checked_types = [PTR_TYPE] + list(FAMILY_TYPES.values())
        servers = self.get_pdns_servers_for_zone(self.zone.name)
        if not servers:
            raise PowerdnsSyncNoServers(f"No valid servers found for zone {self.zone}")
        for api_server in servers:
            pdns_zone = api_server.api.get_zone(self.zone.name)
            if not pdns_zone:
                raise PowerdnsSyncServerZoneMissing(
                    f"Zone {self.zone.name} not found on server {api_server}"
                )
            for record in pdns_zone.records:
                if record["type"] not in checked_types:
                    self.log_debug(
                        f"Skipping record {record['name']} because of type {record['type']}"
                    )
                    continue
                else:
                    self.log_debug(f"Processing record {record['name']}")

                flat_records.update(DnsRecord.from_pdns_record(record, pdns_zone))
        return flat_records
