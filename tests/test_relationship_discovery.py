from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine

from libremapper.config import AppConfig, IdentityConfig, MapConfig, SelectorConfig, SourceConfig
from libremapper.federation import FederationEngine
from libremapper.models import (
    Device,
    FederatedDevice,
    FederatedLink,
    FederatedTopology,
    Interface,
    Link,
    LinkEndpoint,
    Position,
    Topology,
)
from libremapper.providers.librenms import LibreNMSProvider
from libremapper.service import LibremapperService
from libremapper.state import StateStore


def _relationship_engine() -> Engine:
    engine = create_engine("sqlite://")
    statements = [
        "CREATE TABLE locations (id INTEGER PRIMARY KEY, location TEXT)",
        """
        CREATE TABLE devices (
            device_id INTEGER PRIMARY KEY, hostname TEXT, sysName TEXT, ip BLOB,
            os TEXT, hardware TEXT, type TEXT, location_id INTEGER
        )
        """,
        "CREATE TABLE device_groups (id INTEGER PRIMARY KEY, name TEXT)",
        "CREATE TABLE device_group_device (device_id INTEGER, device_group_id INTEGER)",
        """
        CREATE TABLE ports (
            port_id INTEGER PRIMARY KEY, device_id INTEGER, ifName TEXT, ifDescr TEXT,
            ifAlias TEXT, ifIndex INTEGER, ifPhysAddress TEXT, ifType TEXT,
            ifSpeed INTEGER, deleted INTEGER
        )
        """,
        """
        CREATE TABLE links (
            id INTEGER PRIMARY KEY, local_device_id INTEGER, local_port_id INTEGER,
            remote_device_id INTEGER, remote_port_id INTEGER, remote_port TEXT,
            protocol TEXT, active INTEGER
        )
        """,
        """
        CREATE TABLE ipv4_addresses (
            ipv4_address_id INTEGER PRIMARY KEY, ipv4_address BLOB,
            ipv4_prefixlen INTEGER, ipv4_network_id INTEGER,
            port_id INTEGER, context_name TEXT
        )
        """,
        """
        CREATE TABLE ipv4_mac (
            id INTEGER PRIMARY KEY, port_id INTEGER, device_id INTEGER,
            mac_address TEXT, ipv4_address BLOB, context_name TEXT
        )
        """,
        """
        CREATE TABLE bgpPeers (
            bgpPeer_id INTEGER PRIMARY KEY, device_id INTEGER, bgpPeerIdentifier BLOB,
            bgpPeerRemoteAs INTEGER, bgpPeerState TEXT, bgpPeerIface INTEGER,
            bgpLocalAddr BLOB, bgpPeerRemoteAddr BLOB, bgpPeerDescr TEXT,
            context_name TEXT
        )
        """,
        """
        CREATE TABLE ospf_instances (
            id INTEGER PRIMARY KEY, device_id INTEGER, ospf_instance_id INTEGER,
            ospfRouterId TEXT, context_name TEXT
        )
        """,
        """
        CREATE TABLE ospf_nbrs (
            id INTEGER PRIMARY KEY, device_id INTEGER, port_id INTEGER,
            ospf_nbr_id INTEGER, ospfNbrIpAddr TEXT, ospfNbrRtrId TEXT,
            ospfNbrState TEXT, context_name TEXT
        )
        """,
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO devices VALUES "
            "(1,'a.example.test','a',?,NULL,'','network',NULL),"
            "(2,'b.example.test','b',?,NULL,'','network',NULL),"
            "(3,'c.example.test','c',?,NULL,'','network',NULL)",
            tuple(
                ipaddress.ip_address(address).packed
                for address in ("192.0.2.1", "192.0.2.2", "192.0.2.3")
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO ports VALUES "
            "(11,1,'eth0','eth0','to b.example.test',1,'02:00:00:00:00:11',"
            "'ethernetCsmacd',1000,0),"
            "(12,1,'lo0','lo0','',2,'02:00:00:00:00:12','softwareLoopback',0,0),"
            "(21,2,'eth0','eth0','to a.example.test',1,'020000000021','ethernetCsmacd',1000,0),"
            "(31,3,'eth0','eth0','',1,'020000000031','ethernetCsmacd',1000,0)"
        )
        addresses = [
            (1, "10.0.0.0", 31, 11),
            (2, "10.0.0.1", 31, 21),
            (3, "10.0.1.1", 32, 12),
            (4, "10.0.2.1", 32, 31),
        ]
        connection.exec_driver_sql(
            "INSERT INTO ipv4_addresses VALUES (?,?,?,?,?,NULL)",
            [
                (row_id, ipaddress.ip_address(address).packed, prefix, row_id, port_id)
                for row_id, address, prefix, port_id in addresses
            ],
        )
        connection.exec_driver_sql(
            "INSERT INTO ipv4_mac VALUES (1,11,1,'02-00-00-00-00-31',NULL,NULL)"
        )
        bgp = [
            (1, 1, "10.0.0.0", 65002, "established", 1, "10.0.0.0", "10.0.0.1", ""),
            (2, 1, "10.0.2.1", 65003, "established", 2, "10.0.1.1", "10.0.2.1", ""),
            (3, 2, "203.0.113.9", 64500, "established", 1, "10.0.0.1", "203.0.113.9", "Transit A"),
        ]
        connection.exec_driver_sql(
            "INSERT INTO bgpPeers VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            [
                (
                    row_id,
                    device_id,
                    ipaddress.ip_address(identifier).packed,
                    remote_as,
                    state,
                    iface,
                    ipaddress.ip_address(local).packed,
                    ipaddress.ip_address(remote).packed,
                    description,
                )
                for (
                    row_id,
                    device_id,
                    identifier,
                    remote_as,
                    state,
                    iface,
                    local,
                    remote,
                    description,
                ) in bgp
            ],
        )
        connection.exec_driver_sql("INSERT INTO ospf_instances VALUES (1,3,0,'10.255.0.3',NULL)")
        connection.exec_driver_sql(
            "INSERT INTO ospf_nbrs VALUES "
            "(1,2,21,1,'10.0.2.1','10.255.0.3','full',NULL),"
            "(2,1,11,2,'198.51.100.2','198.51.100.2','full',NULL)"
        )
    return engine


@pytest.mark.parametrize(
    ("layout_type", "infer_layers"), [("auto", False), ("auto", True), ("top", True)]
)
def test_scope_layers_apply_after_manual_merge_and_return_when_inference_is_disabled(
    tmp_path: Path, layout_type: str, infer_layers: bool
) -> None:
    engine = _relationship_engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO device_groups VALUES (1, 'Core')")
        connection.exec_driver_sql("INSERT INTO device_group_device VALUES (1, 1)")
    manual_path = tmp_path / "manual.yaml"
    manual_path.write_text(
        "devices:\n  - {source_id: a, hostname: a.example.test, librenms_device_id: 1, layer: 7}\n"
    )
    original_file = manual_path.read_bytes()
    config = AppConfig(
        state_path=tmp_path / "state.sqlite3",
        providers={
            "native": {"type": "librenms", "settings": {"include_mac_adjacency": False}},
            "manual": {"type": "file", "settings": {"path": str(manual_path)}},
        },
        manual_provider="manual",
        maps=[
            MapConfig(
                name="Scope layers",
                sources=[
                    SourceConfig(
                        provider="native",
                        selectors=[
                            SelectorConfig(type="group", value="Core", layer=0),
                            SelectorConfig(type="hostname", value=".*", layer=10),
                        ],
                    )
                ],
                layout={"type": "top"},
            )
        ],
    )
    mapper = LibremapperService(config, engine, StateStore(config.state_path))
    map_config = config.maps[0]

    def layers() -> dict[int | None, int]:
        desired = mapper.build_desired(map_config)
        return {node.device_id: desired.node_layers[key] for key, node in desired.nodes.items()}

    assert layers() == {1: 0, 2: 10, 3: 10}
    map_config.sources[0].selectors[0] = SelectorConfig(type="group", value="Core")
    assert layers() == {1: 7, 2: 10, 3: 10}
    map_config.sources[0].selectors[0] = SelectorConfig(type="group", value="Core", layer=0)
    map_config.layer_overrides = [SelectorConfig(type="device_id", value=1, layer=-3)]
    assert layers() == {1: -3, 2: 10, 3: 10}
    map_config.layout = map_config.layout.model_copy(
        update={"type": layout_type, "infer_layers": infer_layers}
    )
    saved = map_config.model_dump()
    assert layers() == {1: 0, 2: 0, 3: 0}
    assert map_config.model_dump() == saved
    map_config.layout = map_config.layout.model_copy(update={"type": "top", "infer_layers": False})
    assert layers() == {1: -3, 2: 10, 3: 10}
    assert manual_path.read_bytes() == original_file


def test_librenms_discovers_relationship_evidence() -> None:
    provider = LibreNMSProvider(
        "native",
        _relationship_engine(),
        {
            "include_ip_adjacency": True,
            "include_mac_adjacency": True,
            "include_routing_adjacency": True,
            "include_external_bgp_peers": True,
        },
    )
    topology = provider.load(
        SourceConfig(
            provider="native",
            selectors=[{"type": "hostname", "value": r"\.example\.test$"}],
        )
    )

    assert {device.source_id for device in topology.devices} == {
        "1",
        "2",
        "3",
        "external:bgp:203.0.113.9",
    }
    kinds = {(link.protocol, link.attributes["relationship_kind"]) for link in topology.links}
    assert {
        ("ip-p2p", "direct"),
        ("bgp", "direct"),
        ("bgp", "logical"),
        ("ospf", "logical"),
        ("mac", "logical"),
    } <= kinds
    assert any("port-text" in link.attributes["evidence"] for link in topology.links)
    assert any("unresolved OSPF neighbor" in warning for warning in topology.warnings)


def test_federation_prefers_direct_edges_and_unions_evidence() -> None:
    devices = [
        Device(
            source_id=device,
            hostname=f"{device}.example.test",
            interfaces=[
                Interface(source_id=f"{device}{index}", device_source_id=device, name=f"eth{index}")
                for index in (1, 2)
            ],
        )
        for device in ("a", "b", "c", "d")
    ]
    topology = Topology(
        provider="native",
        devices=devices,
        links=[
            Link(
                source_id="p1",
                a=LinkEndpoint(device="a", interface="a1"),
                b=LinkEndpoint(device="b", interface="b1"),
                protocol="ip-p2p",
                attributes={"relationship_kind": "direct", "evidence": ["ip-p2p"]},
            ),
            Link(
                source_id="p2",
                a=LinkEndpoint(device="a", interface="a2"),
                b=LinkEndpoint(device="b", interface="b2"),
                protocol="lldp",
                attributes={"relationship_kind": "direct", "evidence": ["native-links"]},
            ),
            Link(
                source_id="r1",
                a=LinkEndpoint(device="a"),
                b=LinkEndpoint(device="b"),
                protocol="bgp",
                confidence=0.8,
                attributes={"relationship_kind": "logical", "evidence": ["bgp"]},
            ),
            Link(
                source_id="r2",
                a=LinkEndpoint(device="c"),
                b=LinkEndpoint(device="d"),
                protocol="bgp",
                confidence=0.8,
                attributes={"relationship_kind": "logical", "evidence": ["bgp"]},
            ),
            Link(
                source_id="r3",
                a=LinkEndpoint(device="c"),
                b=LinkEndpoint(device="d"),
                protocol="ospf",
                confidence=0.9,
                attributes={"relationship_kind": "logical", "evidence": ["ospf"]},
            ),
        ],
    )

    merged = FederationEngine(IdentityConfig()).merge([topology])

    def endpoint_names(link: FederatedLink) -> set[str | None]:
        return {
            merged.devices[link.a_device].hostname,
            merged.devices[link.b_device].hostname,
        }

    ab = [
        link
        for link in merged.links.values()
        if endpoint_names(link) == {"a.example.test", "b.example.test"}
    ]
    cd = [
        link
        for link in merged.links.values()
        if endpoint_names(link) == {"c.example.test", "d.example.test"}
    ]
    assert len(ab) == 2
    assert all(link.attributes["relationship_kind"] == "direct" for link in ab)
    assert all("bgp" in link.attributes["evidence"] for link in ab)
    assert len(cd) == 1
    assert cd[0].attributes["relationship_kind"] == "logical"
    assert cd[0].attributes["evidence"] == ["bgp", "ospf"]
    assert cd[0].confidence == 0.9


def test_logical_edges_use_non_traffic_style() -> None:
    topology = FederatedTopology(
        devices={
            "a": FederatedDevice(canonical_id="a"),
            "b": FederatedDevice(canonical_id="b"),
        },
        links={
            "direct": FederatedLink(
                canonical_id="direct",
                a_device="a",
                b_device="b",
                protocol="lldp",
                attributes={"relationship_kind": "direct"},
            ),
            "logical": FederatedLink(
                canonical_id="logical",
                a_device="a",
                b_device="b",
                protocol="bgp",
                attributes={"relationship_kind": "logical"},
            ),
        },
    )
    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
        edge_defaults={"style": "cubicBezier", "showbps": True, "showpct": False},
    )

    edges = LibremapperService._desired_edges(
        config, topology, {"a": Position(x=0, y=0), "b": Position(x=100, y=100)}
    )

    assert edges["direct"].style == "cubicBezier"
    assert edges["direct"].showbps is True
    assert edges["logical"].style == "dynamic"
    assert edges["logical"].showbps is False
    assert edges["logical"].label == "BGP"
    assert edges["logical"].text_colour == "#243A54"


def test_short_hostname_labels_keep_non_dns_fallbacks() -> None:
    topology = FederatedTopology(
        devices={
            "dns": FederatedDevice(canonical_id="dns", hostname="edge01.site01.example.test"),
            "plain": FederatedDevice(canonical_id="plain", hostname="label03"),
            "ip": FederatedDevice(canonical_id="ip", management_ip="192.0.2.1"),
        }
    )
    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
        node_label="short_hostname",
    )

    nodes = LibremapperService._desired_nodes(
        config,
        topology,
        {
            "dns": Position(x=0, y=0),
            "plain": Position(x=100, y=100),
            "ip": Position(x=200, y=200),
        },
    )

    assert nodes["dns"].label == "edge01.site01"
    assert nodes["plain"].label == "label03"
    assert nodes["ip"].label == "192.0.2.1"


def test_sys_name_labels_fall_back_to_short_hostnames() -> None:
    topology = FederatedTopology(
        devices={
            "named": FederatedDevice(
                canonical_id="named",
                hostname="inventory-name.site01.example.test",
                attributes={"sys_name": "router-configured-name"},
            ),
            "fallback": FederatedDevice(
                canonical_id="fallback", hostname="edge01.site01.example.test"
            ),
        }
    )
    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
        node_label="sys_name",
    )

    nodes = LibremapperService._desired_nodes(
        config,
        topology,
        {"named": Position(x=0, y=0), "fallback": Position(x=100, y=100)},
    )

    assert nodes["named"].label == "router-configured-name"
    assert nodes["fallback"].label == "edge01.site01"


@pytest.mark.parametrize(
    "parts,expected",
    [
        (1, "router1"),
        (2, "router1.site1"),
        (3, "router1.site1.edge"),
        (127, "router1.site1.edge.example.test"),
    ],
)
@pytest.mark.parametrize("mode", ["short_hostname", "sys_name"])
def test_short_hostnames_keep_configured_components_from_the_left(
    parts: int, expected: str, mode: str
) -> None:
    names = {
        "dns": "router1.site1.edge.example.test.",
        "plain": "router",
        "ipv4": "192.0.2.1",
        "ipv6": "2001:db8::1",
    }
    topology = FederatedTopology(
        devices={
            key: FederatedDevice(canonical_id=key, hostname=name) for key, name in names.items()
        }
    )
    config = MapConfig.model_validate(
        {
            "name": "Labels",
            "sources": [],
            "node_label": mode,
            "short_hostname_parts": parts,
        }
    )
    nodes = LibremapperService._desired_nodes(
        config, topology, {key: Position(x=0, y=0) for key in names}
    )
    assert {key: node.label for key, node in nodes.items()} == names | {"dns": expected}


def test_file_map_images_override_node_default_with_fallback() -> None:
    topology = FederatedTopology(
        devices={
            "override": FederatedDevice(
                canonical_id="override",
                attributes={"map_image": "firewall.svg"},
            ),
            "fallback": FederatedDevice(canonical_id="fallback"),
        }
    )
    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
        node_defaults={"style": "image", "image": "router.svg"},
    )

    nodes = LibremapperService._desired_nodes(
        config,
        topology,
        {
            "override": Position(x=0, y=0),
            "fallback": Position(x=100, y=100),
        },
    )

    assert nodes["override"].image == "firewall.svg"
    assert nodes["fallback"].image == "router.svg"


def test_parallel_edge_lanes_match_librenms_and_ignore_endpoint_direction() -> None:
    def topology(reverse: bool) -> FederatedTopology:
        return FederatedTopology(
            devices={
                "a": FederatedDevice(canonical_id="a"),
                "b": FederatedDevice(canonical_id="b"),
            },
            links={
                canonical_id: FederatedLink(
                    canonical_id=canonical_id,
                    a_device="b" if reverse else "a",
                    b_device="a" if reverse else "b",
                )
                for canonical_id in ("first", "second", "third")
            },
        )

    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
    )
    positions = {"a": Position(x=0, y=0), "b": Position(x=100, y=100)}
    forward = LibremapperService._desired_edges(config, topology(False), positions)
    reversed_edges = LibremapperService._desired_edges(config, topology(True), positions)

    assert [(forward[key].mid_x, forward[key].mid_y) for key in forward] == [
        (50, 50),
        (62, 38),
        (38, 62),
    ]
    assert {key: (edge.mid_x, edge.mid_y) for key, edge in forward.items()} == {
        key: (edge.mid_x, edge.mid_y) for key, edge in reversed_edges.items()
    }


def test_single_horizontal_edge_avoids_a_collinear_midpoint() -> None:
    topology = FederatedTopology(
        devices={
            "a": FederatedDevice(canonical_id="a", layer=0),
            "b": FederatedDevice(canonical_id="b", layer=0),
        },
        links={"ab": FederatedLink(canonical_id="ab", a_device="a", b_device="b")},
    )
    config = MapConfig(
        name="Generated",
        sources=[SourceConfig(provider="native")],
    )
    positions = {"a": Position(x=0, y=0), "b": Position(x=100, y=0)}

    same_layer = LibremapperService._desired_edges(config, topology, positions)
    topology.devices["b"].layer = 1
    different_layers = LibremapperService._desired_edges(config, topology, positions)

    assert (same_layer["ab"].mid_x, same_layer["ab"].mid_y) == (50, -24)
    assert (different_layers["ab"].mid_x, different_layers["ab"].mid_y) == (50, 1)
