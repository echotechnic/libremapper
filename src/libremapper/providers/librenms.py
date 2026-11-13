from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import Engine, bindparam, inspect, text

from libremapper.config import SourceConfig
from libremapper.models import Device, Interface, Link, LinkEndpoint, Topology
from libremapper.providers.base import TopologyProvider
from libremapper.providers.file import select_topology


def _decode_ip(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return str(ipaddress.ip_address(bytes(value)))
        except ValueError:
            return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def _normalize_mac(value: Any) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^0-9a-f]", "", str(value).lower())
    return normalized if len(normalized) == 12 else None


def _endpoint_pair(
    left: tuple[int, int], right: tuple[int, int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    return (left, right) if left <= right else (right, left)


class LibreNMSProvider(TopologyProvider):
    def __init__(self, name: str, engine: Engine, settings: dict[str, Any]) -> None:
        super().__init__(name)
        self.engine = engine
        self.include_mac_adjacency = bool(settings.get("include_mac_adjacency", True))
        self.include_ip_adjacency = bool(settings.get("include_ip_adjacency", False))
        self.include_routing_adjacency = bool(settings.get("include_routing_adjacency", False))
        self.include_external_bgp_peers = bool(settings.get("include_external_bgp_peers", False))

    def load(self, source: SourceConfig) -> Topology:
        with self.engine.connect() as connection:
            device_rows = connection.execute(
                text(
                    """
                    SELECT d.device_id, d.hostname, d.sysName, d.ip, d.os,
                           d.hardware, d.type, l.location
                    FROM devices d
                    LEFT JOIN locations l ON l.id = d.location_id
                    """
                )
            ).all()
            group_rows = connection.execute(
                text(
                    """
                    SELECT dgd.device_id, dg.id AS group_id, dg.name AS group_name
                    FROM device_group_device dgd
                    JOIN device_groups dg ON dg.id = dgd.device_group_id
                    """
                )
            ).all()

            groups: dict[int, list[str]] = defaultdict(list)
            for row in group_rows:
                groups[int(row.device_id)].extend([str(row.group_id), str(row.group_name)])

            all_devices = [
                Device(
                    source_id=str(row.device_id),
                    hostname=row.hostname,
                    fqdn=row.hostname if row.hostname and "." in row.hostname else None,
                    management_ip=_decode_ip(row.ip),
                    librenms_device_id=int(row.device_id),
                    attributes={
                        "sys_name": row.sysName,
                        "location": row.location,
                        "groups": groups[int(row.device_id)],
                        "os": row.os,
                        "hardware": row.hardware,
                        "type": row.type,
                    },
                )
                for row in device_rows
            ]
            preliminary = select_topology(Topology(provider=self.name, devices=all_devices), source)
            selected_ids = {int(device.source_id) for device in preliminary.devices}
            if not selected_ids:
                return preliminary

            ports_statement = text(
                """
                SELECT port_id, device_id, ifName, ifDescr, ifAlias, ifIndex,
                       ifPhysAddress, ifType, ifSpeed
                FROM ports
                WHERE device_id IN :device_ids AND deleted = 0
                """
            ).bindparams(bindparam("device_ids", expanding=True))
            port_rows = connection.execute(
                ports_statement, {"device_ids": sorted(selected_ids)}
            ).all()
            port_by_id: dict[int, Any] = {}
            device_interfaces: dict[int, list[Interface]] = defaultdict(list)
            for row in port_rows:
                port_by_id[int(row.port_id)] = row
                device_interfaces[int(row.device_id)].append(
                    Interface(
                        source_id=str(row.port_id),
                        device_source_id=str(row.device_id),
                        name=row.ifName or row.ifDescr,
                        if_index=int(row.ifIndex) if row.ifIndex is not None else None,
                        mac=row.ifPhysAddress,
                        librenms_port_id=int(row.port_id),
                        attributes={
                            "if_descr": row.ifDescr,
                            "if_alias": row.ifAlias,
                            "if_type": row.ifType,
                            "if_speed": row.ifSpeed,
                        },
                    )
                )
            for device in preliminary.devices:
                device.interfaces = device_interfaces[int(device.source_id)]

            links = self._load_links(connection, selected_ids, port_by_id, source.interface_types)
            address_index: dict[tuple[str, str], list[tuple[int, int | None]]] = defaultdict(list)
            direct_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
            if self.include_ip_adjacency or self.include_routing_adjacency:
                address_index, ip_links, direct_pairs = self._load_ip_links(
                    connection,
                    preliminary.devices,
                    selected_ids,
                    port_by_id,
                    source.interface_types,
                )
                if self.include_ip_adjacency:
                    links.extend(ip_links)
            if self.include_mac_adjacency:
                links.extend(
                    self._load_mac_links(
                        connection, selected_ids, port_by_id, source.interface_types
                    )
                )
            external_devices: list[Device] = []
            warnings = list(preliminary.warnings)
            if self.include_routing_adjacency:
                routing_links, external_devices, routing_warnings = self._load_routing_links(
                    connection,
                    preliminary.devices,
                    selected_ids,
                    port_by_id,
                    address_index,
                    direct_pairs,
                )
                links.extend(routing_links)
                warnings.extend(routing_warnings)

            devices_by_id = {int(device.source_id): device for device in preliminary.devices}
            self._corroborate_with_port_text(links, port_by_id, devices_by_id)

        protocols = {item.lower() for item in source.protocols}
        if protocols:
            links = [link for link in links if (link.protocol or "").lower() in protocols]
        referenced = {endpoint for link in links for endpoint in (link.a.device, link.b.device)}
        devices = list(preliminary.devices)
        devices.extend(device for device in external_devices if device.source_id in referenced)

        topology = Topology(
            provider=self.name,
            devices=devices,
            links=links,
            warnings=warnings,
        )
        return topology

    def _load_links(
        self,
        connection: Any,
        selected_ids: set[int],
        port_by_id: dict[int, Any],
        interface_types: list[str],
    ) -> list[Link]:
        statement = text(
            """
            SELECT id, local_device_id, local_port_id, remote_device_id,
                   remote_port_id, remote_port, protocol
            FROM links
            WHERE active = 1
              AND local_device_id IN :device_ids
              AND remote_device_id IN :device_ids
            """
        ).bindparams(bindparam("device_ids", expanding=True))
        rows = connection.execute(statement, {"device_ids": sorted(selected_ids)}).all()
        allowed = set(interface_types)
        links: list[Link] = []
        for row in rows:
            local_port = port_by_id.get(int(row.local_port_id or 0))
            remote_port = port_by_id.get(int(row.remote_port_id or 0))
            if allowed and (
                not local_port
                or local_port.ifType not in allowed
                or (remote_port and remote_port.ifType not in allowed)
            ):
                continue
            links.append(
                Link(
                    source_id=f"link:{row.id}",
                    a=LinkEndpoint(
                        device=str(row.local_device_id),
                        interface=str(row.local_port_id) if row.local_port_id else None,
                    ),
                    b=LinkEndpoint(
                        device=str(row.remote_device_id),
                        interface=str(row.remote_port_id) if row.remote_port_id else None,
                    ),
                    protocol=row.protocol,
                    attributes={
                        "remote_port": row.remote_port,
                        "relationship_kind": "direct",
                        "evidence": ["native-links"],
                    },
                )
            )
        return links

    def _load_mac_links(
        self,
        connection: Any,
        selected_ids: set[int],
        port_by_id: dict[int, Any],
        interface_types: list[str],
    ) -> list[Link]:
        if not inspect(connection).has_table("ipv4_mac"):
            return []
        statement = text(
            """
            SELECT m.id, p.device_id AS local_device_id,
                   p.port_id AS local_port_id, m.mac_address
            FROM ipv4_mac m
            JOIN ports p ON p.port_id = m.port_id
            WHERE p.device_id IN :device_ids
            """
        ).bindparams(bindparam("device_ids", expanding=True))
        rows = connection.execute(statement, {"device_ids": sorted(selected_ids)}).all()
        allowed = set(interface_types)
        ports_by_mac: dict[str, list[Any]] = defaultdict(list)
        for port in port_by_id.values():
            normalized = _normalize_mac(port.ifPhysAddress)
            if normalized and (not allowed or port.ifType in allowed):
                ports_by_mac[normalized].append(port)
        links: list[Link] = []
        for row in rows:
            local_port = port_by_id.get(int(row.local_port_id))
            candidates = [
                port
                for port in ports_by_mac.get(_normalize_mac(row.mac_address) or "", [])
                if int(port.device_id) != int(row.local_device_id)
            ]
            if len(candidates) != 1:
                continue
            remote_port = candidates[0]
            if allowed and (
                not local_port
                or not remote_port
                or local_port.ifType not in allowed
                or remote_port.ifType not in allowed
            ):
                continue
            links.append(
                Link(
                    source_id=f"mac:{row.id}:{row.local_port_id}:{remote_port.port_id}",
                    a=LinkEndpoint(
                        device=str(row.local_device_id),
                        interface=str(row.local_port_id),
                    ),
                    b=LinkEndpoint(
                        device=str(remote_port.device_id),
                        interface=str(remote_port.port_id),
                    ),
                    protocol="mac",
                    confidence=0.5,
                    attributes={
                        "relationship_kind": "logical",
                        "evidence": ["ipv4-mac"],
                    },
                )
            )
        return links

    def _load_ip_links(
        self,
        connection: Any,
        devices: list[Device],
        selected_ids: set[int],
        port_by_id: dict[int, Any],
        interface_types: list[str],
    ) -> tuple[
        dict[tuple[str, str], list[tuple[int, int | None]]],
        list[Link],
        set[tuple[tuple[int, int], tuple[int, int]]],
    ]:
        address_index: dict[tuple[str, str], list[tuple[int, int | None]]] = defaultdict(list)
        for device in devices:
            if device.management_ip:
                address_index[("", device.management_ip)].append((int(device.source_id), None))

        allowed = set(interface_types)
        networks: dict[tuple[str, str], set[tuple[int, int]]] = defaultdict(set)
        statements = (
            (
                "ipv4_addresses",
                """
                SELECT a.ipv4_address AS address, NULL AS compressed,
                       a.ipv4_prefixlen AS prefixlen, a.context_name,
                       p.device_id, p.port_id
                FROM ipv4_addresses a
                JOIN ports p ON p.port_id = a.port_id
                WHERE p.device_id IN :device_ids AND p.deleted = 0
                """,
            ),
            (
                "ipv6_addresses",
                """
                SELECT a.ipv6_address AS address, a.ipv6_compressed AS compressed,
                       a.ipv6_prefixlen AS prefixlen, a.context_name,
                       p.device_id, p.port_id
                FROM ipv6_addresses a
                JOIN ports p ON p.port_id = a.port_id
                WHERE p.device_id IN :device_ids AND p.deleted = 0
                """,
            ),
        )
        for table_name, query in statements:
            if not inspect(connection).has_table(table_name):
                continue
            statement = text(query).bindparams(bindparam("device_ids", expanding=True))
            rows = connection.execute(statement, {"device_ids": sorted(selected_ids)}).all()
            for row in rows:
                port = port_by_id.get(int(row.port_id))
                if not port or (allowed and port.ifType not in allowed):
                    continue
                address = _decode_ip(row.compressed) or _decode_ip(row.address)
                if not address:
                    continue
                context = str(row.context_name or "")
                endpoint = (int(row.device_id), int(row.port_id))
                if endpoint not in address_index[(context, address)]:
                    address_index[(context, address)].append(endpoint)
                try:
                    interface = ipaddress.ip_interface(f"{address}/{int(row.prefixlen)}")
                except (TypeError, ValueError):
                    continue
                if interface.network.prefixlen in (
                    (30, 31) if interface.version == 4 else (126, 127)
                ):
                    networks[(context, str(interface.network))].add(endpoint)

        links: list[Link] = []
        direct_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
        for (context, network), members in sorted(networks.items()):
            if len(members) != 2 or len({device_id for device_id, _ in members}) != 2:
                continue
            left, right = sorted(members)
            direct_pairs.add(_endpoint_pair(left, right))
            links.append(
                Link(
                    source_id=f"ip-p2p:{context}:{network}",
                    a=LinkEndpoint(device=str(left[0]), interface=str(left[1])),
                    b=LinkEndpoint(device=str(right[0]), interface=str(right[1])),
                    protocol="ip-p2p",
                    confidence=0.9,
                    attributes={
                        "relationship_kind": "direct",
                        "evidence": ["ip-p2p"],
                        "network": network,
                    },
                )
            )
        return address_index, links, direct_pairs

    def _load_routing_links(
        self,
        connection: Any,
        devices: list[Device],
        selected_ids: set[int],
        port_by_id: dict[int, Any],
        address_index: dict[tuple[str, str], list[tuple[int, int | None]]],
        direct_pairs: set[tuple[tuple[int, int], tuple[int, int]]],
    ) -> tuple[list[Link], list[Device], list[str]]:
        links: list[Link] = []
        warnings: list[str] = []
        external: dict[str, Device] = {}
        device_by_id = {int(device.source_id): device for device in devices}
        port_by_ifindex = {
            (int(port.device_id), int(port.ifIndex)): int(port.port_id)
            for port in port_by_id.values()
            if port.ifIndex is not None
        }

        def candidates(
            context: str, address: str | None, *, exclude: int | None = None
        ) -> list[tuple[int, int | None]]:
            if not address:
                return []
            return sorted(
                {
                    endpoint
                    for endpoint in address_index.get((context, address), [])
                    if endpoint[0] != exclude
                },
                key=lambda endpoint: (
                    endpoint[0],
                    endpoint[1] is None,
                    endpoint[1] or 0,
                ),
            )

        def unique_endpoint(
            endpoints: list[tuple[int, int | None]],
        ) -> tuple[int, int | None] | None:
            device_ids = {device_id for device_id, _ in endpoints}
            if len(device_ids) != 1:
                return None
            ports = {port_id for _, port_id in endpoints if port_id is not None}
            return next(iter(device_ids)), next(iter(ports)) if len(ports) == 1 else None

        if inspect(connection).has_table("bgpPeers"):
            statement = text(
                """
                SELECT bgpPeer_id AS id, device_id, bgpPeerIdentifier,
                       bgpPeerRemoteAs,
                       bgpPeerState, bgpPeerIface, bgpLocalAddr,
                       bgpPeerRemoteAddr, bgpPeerDescr, context_name
                FROM bgpPeers
                WHERE device_id IN :device_ids
                """
            ).bindparams(bindparam("device_ids", expanding=True))
            for row in connection.execute(statement, {"device_ids": sorted(selected_ids)}).all():
                if str(row.bgpPeerState or "").lower() != "established":
                    continue
                local_device = int(row.device_id)
                context = str(row.context_name or "")
                local_address = _decode_ip(row.bgpLocalAddr)
                remote_address = next(
                    (
                        address
                        for address in (
                            _decode_ip(row.bgpPeerRemoteAddr),
                            _decode_ip(row.bgpPeerIdentifier),
                        )
                        if address
                        and not ipaddress.ip_address(address).is_unspecified
                        and not ipaddress.ip_address(address).is_multicast
                    ),
                    None,
                )
                local = unique_endpoint(
                    [
                        endpoint
                        for endpoint in candidates(context, local_address)
                        if endpoint[0] == local_device
                    ]
                )
                local_port = local[1] if local else None
                if local_port is None and row.bgpPeerIface is not None:
                    local_port = port_by_ifindex.get((local_device, int(row.bgpPeerIface)))
                remote_candidates = candidates(context, remote_address, exclude=local_device)
                remote = unique_endpoint(remote_candidates)
                if remote:
                    direct = bool(
                        local_port is not None
                        and remote[1] is not None
                        and _endpoint_pair((local_device, local_port), (remote[0], remote[1]))
                        in direct_pairs
                    )
                    links.append(
                        Link(
                            source_id=f"bgp:{row.id}",
                            a=LinkEndpoint(
                                device=str(local_device),
                                interface=str(local_port) if direct else None,
                            ),
                            b=LinkEndpoint(
                                device=str(remote[0]),
                                interface=str(remote[1]) if direct else None,
                            ),
                            protocol="bgp",
                            confidence=0.9 if direct else 0.8,
                            attributes={
                                "relationship_kind": "direct" if direct else "logical",
                                "evidence": ["bgp"],
                            },
                        )
                    )
                    continue
                if remote_candidates:
                    warnings.append(
                        f"ambiguous BGP peer {remote_address or 'unknown'} on device {local_device}"
                    )
                    continue
                if not self.include_external_bgp_peers or not remote_address:
                    continue
                source_id = f"external:bgp:{remote_address}"
                description = str(row.bgpPeerDescr or "").strip()
                label = description or f"AS{row.bgpPeerRemoteAs} {remote_address}"
                external.setdefault(
                    source_id,
                    Device(
                        source_id=source_id,
                        hostname=label,
                        management_ip=remote_address,
                        layer=device_by_id[local_device].layer,
                        attributes={"external": True, "protocol": "bgp"},
                    ),
                )
                links.append(
                    Link(
                        source_id=f"bgp:{row.id}",
                        a=LinkEndpoint(device=str(local_device)),
                        b=LinkEndpoint(device=source_id),
                        protocol="bgp",
                        confidence=0.8,
                        attributes={
                            "relationship_kind": "logical",
                            "evidence": ["bgp"],
                        },
                    )
                )

        router_devices: dict[tuple[str, str], set[int]] = defaultdict(set)
        if inspect(connection).has_table("ospf_instances"):
            statement = text(
                """
                SELECT device_id, ospfRouterId, context_name AS context
                FROM ospf_instances
                WHERE device_id IN :device_ids
                """
            ).bindparams(bindparam("device_ids", expanding=True))
            for row in connection.execute(statement, {"device_ids": sorted(selected_ids)}).all():
                router_id = _decode_ip(row.ospfRouterId)
                if router_id:
                    router_devices[(str(row.context or ""), router_id)].add(int(row.device_id))

        if inspect(connection).has_table("ospf_nbrs"):
            statement = text(
                """
                SELECT id, device_id, port_id, ospfNbrIpAddr, ospfNbrRtrId,
                       ospfNbrState, context_name AS context
                FROM ospf_nbrs
                WHERE device_id IN :device_ids
                """
            ).bindparams(bindparam("device_ids", expanding=True))
            for row in connection.execute(statement, {"device_ids": sorted(selected_ids)}).all():
                if "full" not in str(row.ospfNbrState or "").lower():
                    continue
                local_device = int(row.device_id)
                local_port = int(row.port_id) if row.port_id in port_by_id else None
                context = str(row.context or "")
                neighbor_address = _decode_ip(row.ospfNbrIpAddr)
                remote_candidates = candidates(context, neighbor_address, exclude=local_device)
                remote = unique_endpoint(remote_candidates)
                if not remote:
                    router_id = _decode_ip(row.ospfNbrRtrId)
                    router_matches = router_devices.get((context, router_id or ""), set())
                    if len(router_matches - {local_device}) == 1:
                        remote = (next(iter(router_matches - {local_device})), None)
                if not remote:
                    warnings.append(
                        f"unresolved OSPF neighbor {neighbor_address or 'unknown'} "
                        f"on device {local_device}"
                    )
                    continue
                direct = bool(
                    local_port is not None
                    and remote[1] is not None
                    and _endpoint_pair((local_device, local_port), (remote[0], remote[1]))
                    in direct_pairs
                )
                links.append(
                    Link(
                        source_id=f"ospf:{row.id}",
                        a=LinkEndpoint(
                            device=str(local_device),
                            interface=str(local_port) if direct else None,
                        ),
                        b=LinkEndpoint(
                            device=str(remote[0]),
                            interface=str(remote[1]) if direct else None,
                        ),
                        protocol="ospf",
                        confidence=0.9 if direct else 0.8,
                        attributes={
                            "relationship_kind": "direct" if direct else "logical",
                            "evidence": ["ospf"],
                        },
                    )
                )

        return links, list(external.values()), warnings

    @staticmethod
    def _corroborate_with_port_text(
        links: list[Link], port_by_id: dict[int, Any], devices: dict[int, Device]
    ) -> None:
        def mentions(port: Any, remote: Device | None) -> bool:
            if not port or not remote:
                return False
            names = {
                value
                for value in (
                    remote.hostname,
                    remote.fqdn,
                    (remote.hostname or "").split(".")[0],
                )
                if value
            }
            description = " ".join(str(value or "") for value in (port.ifAlias, port.ifDescr))
            return any(
                re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])",
                    description,
                    re.IGNORECASE,
                )
                for name in names
            )

        for link in links:
            local_port = port_by_id.get(int(link.a.interface or 0))
            remote_port = port_by_id.get(int(link.b.interface or 0))
            local_device = devices.get(int(link.a.device)) if link.a.device.isdigit() else None
            remote_device = devices.get(int(link.b.device)) if link.b.device.isdigit() else None
            if not (mentions(local_port, remote_device) or mentions(remote_port, local_device)):
                continue
            evidence = list(link.attributes.get("evidence", []))
            if "port-text" not in evidence:
                evidence.append("port-text")
                link.attributes["evidence"] = evidence
                link.confidence = min(1.0, link.confidence + 0.05)
