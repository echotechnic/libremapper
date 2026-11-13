import pytest

from libremapper.config import IdentityConfig
from libremapper.federation import FederationEngine
from libremapper.models import (
    Device,
    Interface,
    Link,
    LinkEndpoint,
    SuppressLink,
    Topology,
)


def test_provider_priority_merges_aliases_and_prefers_librenms_port() -> None:
    librenms = Topology(
        provider="librenms",
        devices=[
            Device(
                source_id="10",
                hostname="edge-a.example.net",
                management_ip="192.0.2.10",
                librenms_device_id=10,
                interfaces=[
                    Interface(
                        source_id="100",
                        device_source_id="10",
                        name="Ethernet1",
                        if_index=1,
                        librenms_port_id=100,
                    )
                ],
            ),
            Device(source_id="11", hostname="core-a.example.net", librenms_device_id=11),
        ],
        links=[
            Link(
                source_id="1",
                protocol="lldp",
                a=LinkEndpoint(device="10", interface="100"),
                b=LinkEndpoint(device="11"),
            )
        ],
    )
    external = Topology(
        provider="external",
        devices=[
            Device(
                source_id="edge",
                hostname="edge-a",
                management_ip="192.0.2.10",
                attributes={"owner": "WAN"},
            ),
            Device(source_id="core", hostname="core-a.example.net"),
        ],
        links=[
            Link(
                source_id="carrier",
                protocol="external",
                a=LinkEndpoint(device="edge"),
                b=LinkEndpoint(device="core"),
            )
        ],
    )
    identity = IdentityConfig(
        aliases={"hostname:edge-a": "management_ip:192.0.2.10"}
    )

    merged = FederationEngine(identity).merge([librenms, external])

    assert len(merged.devices) == 2
    edge = next(
        device
        for device in merged.devices.values()
        if device.management_ip == "192.0.2.10"
    )
    assert edge.hostname == "edge-a.example.net"
    assert edge.attributes["owner"] == "WAN"
    assert len(merged.links) == 1
    link = next(iter(merged.links.values()))
    assert link.librenms_port_id == 100
    assert {source.provider for source in link.sources} == {"librenms", "external"}


def test_alias_bridge_merges_devices_even_when_target_is_seen_later() -> None:
    external = Topology(
        provider="external",
        devices=[Device(source_id="edge", hostname="edge-a")],
    )
    librenms = Topology(
        provider="librenms",
        devices=[
            Device(
                source_id="10",
                hostname="edge-a.example.net",
                management_ip="192.0.2.10",
                librenms_device_id=10,
            )
        ],
    )
    identity = IdentityConfig(
        aliases={"hostname:edge-a": "management_ip:192.0.2.10"}
    )

    merged = FederationEngine(identity).merge([external, librenms])

    assert len(merged.devices) == 1
    device = next(iter(merged.devices.values()))
    assert device.librenms_device_id == 10
    assert {source.provider for source in device.sources} == {"external", "librenms"}


def test_librenms_device_id_bridges_guided_manual_devices() -> None:
    native = Topology(
        provider="native",
        devices=[Device(source_id="15", hostname="router.example.net", librenms_device_id=15)],
    )
    manual = Topology(
        provider="manual",
        devices=[Device(source_id="guided-librenms-15", librenms_device_id=15)],
    )

    merged = FederationEngine(IdentityConfig()).merge([native, manual])

    assert len(merged.devices) == 1
    assert {source.provider for source in next(iter(merged.devices.values())).sources} == {
        "native",
        "manual",
    }


def test_merge_preserves_legitimate_layer_zero() -> None:
    first = Topology(
        provider="first",
        devices=[Device(source_id="a", hostname="a.example.net", layer=0)],
    )
    second = Topology(
        provider="second",
        devices=[Device(source_id="a", hostname="a.example.net", layer=10)],
    )

    merged = FederationEngine(IdentityConfig()).merge([first, second])

    assert next(iter(merged.devices.values())).layer == 0


def test_same_provider_interfaces_with_shared_mac_remain_distinct() -> None:
    topology = Topology(
        provider="librenms",
        devices=[
            Device(
                source_id="15",
                interfaces=[
                    Interface(
                        source_id="569",
                        device_source_id="15",
                        name="Bundle-Ether2",
                        if_index=120,
                        mac="aa:bb:cc:dd:ee:ff",
                        librenms_port_id=569,
                    ),
                    Interface(
                        source_id="570",
                        device_source_id="15",
                        name="Bundle-Ether2.1",
                        if_index=121,
                        mac="aa:bb:cc:dd:ee:ff",
                        librenms_port_id=570,
                    ),
                ],
            )
        ],
    )

    merged = FederationEngine(IdentityConfig()).merge([topology])
    device = next(iter(merged.devices.values()))

    assert len(device.interfaces) == 2
    assert not merged.conflicts


def test_mac_is_only_a_fallback_for_cross_provider_interface_identity() -> None:
    def merge(interface: Interface):
        return FederationEngine(IdentityConfig()).merge(
            [
                Topology(
                    provider="librenms",
                    devices=[
                        Device(
                            source_id="15",
                            hostname="router.example.net",
                            interfaces=[
                                Interface(
                                    source_id="569",
                                    device_source_id="15",
                                    name="Bundle-Ether2",
                                    if_index=120,
                                    mac="aa:bb:cc:dd:ee:ff",
                                )
                            ],
                        )
                    ],
                ),
                Topology(
                    provider="external",
                    devices=[
                        Device(
                            source_id="router",
                            hostname="router.example.net",
                            interfaces=[interface],
                        )
                    ],
                ),
            ]
        )

    conflicting = merge(
        Interface(
            source_id="subinterface",
            device_source_id="router",
            name="Bundle-Ether2.1",
            if_index=121,
            mac="aa:bb:cc:dd:ee:ff",
        )
    )
    fallback = merge(
        Interface(
            source_id="unknown",
            device_source_id="router",
            mac="aa:bb:cc:dd:ee:ff",
        )
    )

    assert len(next(iter(conflicting.devices.values())).interfaces) == 2
    assert len(next(iter(fallback.devices.values())).interfaces) == 1
    assert not conflicting.conflicts
    assert not fallback.conflicts


def _device(
    source_id: str,
    hostname: str,
    *interfaces: tuple[str, str, int | None],
    device_id: int | None = None,
    layer: int = 0,
) -> Device:
    return Device(
        source_id=source_id,
        hostname=hostname,
        librenms_device_id=device_id,
        layer=layer,
        interfaces=[
            Interface(
                source_id=source,
                device_source_id=source_id,
                name=name,
                librenms_port_id=port,
            )
            for source, name, port in interfaces
        ],
    )


@pytest.mark.parametrize("manual_first", [False, True])
def test_custom_manual_labels_never_merge_with_real_interfaces_or_counters(
    manual_first: bool,
) -> None:
    native = Topology(
        provider="native",
        devices=[
            _device("a", "a.example.net", ("11", "eth0", 11), device_id=1),
            _device("b", "b.example.net", ("21", "eth0", 21), device_id=2),
        ],
        links=[
            Link(
                source_id="discovered",
                protocol="lldp",
                a=LinkEndpoint(device="a", interface="11"),
                b=LinkEndpoint(device="b", interface="21"),
            )
        ],
    )
    manual = Topology(
        provider="manual",
        devices=[
            _device("a", "a.example.net", ("custom-a", "eth0", None), device_id=1),
            _device("b", "b.example.net", ("custom-b", "eth0", None), device_id=2),
        ],
        links=[
            Link(
                source_id="custom-labelled",
                protocol="manual",
                relationship_kind="logical",
                a=LinkEndpoint(device="a", interface="custom-a"),
                b=LinkEndpoint(device="b", interface="custom-b"),
            )
        ],
    )
    for device in manual.devices:
        device.interfaces[0].attributes["manual_label_only"] = True

    topologies = [manual, native] if manual_first else [native, manual]
    merged = FederationEngine(IdentityConfig()).merge(topologies)

    assert len(merged.links) == 2
    links = {link.protocol: link for link in merged.links.values()}
    assert links["manual"].librenms_port_id is None
    assert links["lldp"].librenms_port_id in {11, 21}
    assert not merged.conflicts
    for device in merged.devices.values():
        assert len(device.interfaces) == 2
        labelled = next(
            interface for interface in device.interfaces.values()
            if interface.attributes.get("manual_label_only")
        )
        assert labelled.librenms_port_id is None
        assert {ref.provider for ref in labelled.sources} == {"manual"}


@pytest.mark.parametrize("manual_first", [False, True])
def test_custom_labels_ignore_accidentally_populated_port_ids(manual_first: bool) -> None:
    native = Topology(
        provider="native",
        devices=[
            _device("a", "a.example.net", ("11", "eth0", 11), device_id=1),
            _device("b", "b.example.net", ("21", "eth0", 21), device_id=2),
        ],
    )
    manual = Topology(
        provider="manual",
        devices=[
            _device("a", "a.example.net", ("custom-a", "eth0", 11), device_id=1),
            _device("b", "b.example.net", ("custom-b", "eth0", 21), device_id=2),
        ],
        links=[
            Link(
                source_id="custom-labelled",
                protocol="manual",
                relationship_kind="logical",
                a=LinkEndpoint(device="a", interface="custom-a"),
                b=LinkEndpoint(device="b", interface="custom-b"),
            )
        ],
    )
    for device in manual.devices:
        device.interfaces[0].attributes["manual_label_only"] = True

    topologies = [manual, native] if manual_first else [native, manual]
    merged = FederationEngine(IdentityConfig()).merge(topologies)

    assert len(merged.links) == 1
    link = next(iter(merged.links.values()))
    assert link.librenms_port_id is None
    for device_id, interface_id in (
        (link.a_device, link.a_interface), (link.b_device, link.b_interface)
    ):
        assert interface_id is not None
        device = merged.devices[device_id]
        assert len(device.interfaces) == 2
        assert device.interfaces[interface_id].attributes["manual_label_only"]
        assert {ref.provider for ref in device.interfaces[interface_id].sources} == {"manual"}
    assert not merged.conflicts


@pytest.mark.parametrize("manual_first", [False, True])
def test_exact_manual_port_id_matches_the_correct_duplicate_name(
    manual_first: bool,
) -> None:
    native = Topology(
        provider="native",
        devices=[
            _device("a", "a.example.net", ("11", "eth0", 11), ("12", "eth0", 12), device_id=1),
            _device("b", "b.example.net", device_id=2),
        ],
        links=[
            Link(
                source_id="discovered",
                protocol="lldp",
                a=LinkEndpoint(device="a", interface="12"),
                b=LinkEndpoint(device="b"),
            )
        ],
    )
    manual = Topology(
        provider="manual",
        devices=[
            _device("a", "a.example.net", ("chosen-port", "eth0", 12), device_id=1),
            _device("b", "b.example.net", device_id=2),
        ],
        links=[
            Link(
                source_id="exact-port",
                protocol="manual",
                relationship_kind="direct",
                a=LinkEndpoint(device="a", interface="chosen-port"),
                b=LinkEndpoint(device="b"),
            )
        ],
    )

    topologies = [manual, native] if manual_first else [native, manual]
    merged = FederationEngine(IdentityConfig()).merge(topologies)

    assert len(merged.links) == 1
    link = next(iter(merged.links.values()))
    assert link.librenms_port_id == 12
    assert {ref.provider for ref in link.sources} == {"native", "manual"}
    device = next(device for device in merged.devices.values() if device.librenms_device_id == 1)
    assert len(device.interfaces) == 2
    chosen = next(
        interface for interface in device.interfaces.values() if interface.librenms_port_id == 12
    )
    assert {ref.provider for ref in chosen.sources} == {"native", "manual"}
    other = next(
        interface for interface in device.interfaces.values() if interface.librenms_port_id == 11
    )
    assert {ref.provider for ref in other.sources} == {"native"}
    assert not merged.conflicts


def test_manual_links_are_relevant_one_hop_layered_and_logical_by_default() -> None:
    native = Topology(
        provider="native",
        devices=[
            _device(
                "1",
                "a.example.net",
                ("11", "eth0", 11),
                ("12", "eth1", 12),
                device_id=1,
                layer=20,
            )
        ],
    )
    manual = Topology(
        provider="manual",
        devices=[
            _device(
                "a",
                "a.example.net",
                ("uplink0", "eth0", None),
                ("uplink1", "eth1", None),
            ),
            _device("logical", "logical.example.net", ("wan", "wan0", None)),
            _device("direct", "direct.example.net", ("wan", "wan0", None)),
            _device("unused-a", "unused-a.example.net"),
            _device("unused-b", "unused-b.example.net"),
        ],
        links=[
            Link(
                source_id="logical",
                a=LinkEndpoint(device="a", interface="uplink0"),
                b=LinkEndpoint(device="logical", interface="wan"),
            ),
            Link(
                source_id="direct",
                a=LinkEndpoint(device="a", interface="uplink1"),
                b=LinkEndpoint(device="direct", interface="wan"),
                relationship_kind="direct",
            ),
            Link(
                source_id="disconnected",
                a=LinkEndpoint(device="unused-a"),
                b=LinkEndpoint(device="unused-b"),
            ),
        ],
    )

    merged = FederationEngine(IdentityConfig()).merge_with_manual(
        [native], manual, [], {"native"}
    )

    assert {device.hostname for device in merged.devices.values()} == {
        "a.example.net",
        "logical.example.net",
        "direct.example.net",
    }
    external = {
        device.hostname: device.layer
        for device in merged.devices.values()
        if device.librenms_device_id is None
    }
    assert external == {"logical.example.net": 21, "direct.example.net": 21}
    links = {link.attributes["relationship_kind"]: link for link in merged.links.values()}
    assert links["logical"].librenms_port_id == 11
    assert links["direct"].librenms_port_id == 12


def test_manual_suppressions_are_direction_independent_and_additions_win() -> None:
    native_devices = [
        _device(
            "a",
            "a.example.net",
            ("a1", "eth1", 11),
            ("a2", "eth2", 12),
            ("a3", "eth3", 13),
            device_id=1,
        ),
        _device(
            "b",
            "b.example.net",
            ("b1", "eth1", 21),
            ("b2", "eth2", 22),
            ("b3", "eth3", 23),
            device_id=2,
        ),
    ]
    native = Topology(
        provider="native",
        devices=native_devices,
        links=[
            Link(
                source_id="lldp",
                a=LinkEndpoint(device="a", interface="a1"),
                b=LinkEndpoint(device="b", interface="b1"),
                protocol="lldp",
            ),
            Link(
                source_id="ospf",
                a=LinkEndpoint(device="a", interface="a2"),
                b=LinkEndpoint(device="b", interface="b2"),
                protocol="ospf",
            ),
        ],
    )
    explicit = Topology(
        provider="explicit",
        devices=[device.model_copy(deep=True) for device in native_devices],
        links=[
            Link(
                source_id="file",
                a=LinkEndpoint(device="a", interface="a3"),
                b=LinkEndpoint(device="b", interface="b3"),
            )
        ],
    )
    manual = Topology(
        provider="manual",
        devices=[device.model_copy(deep=True) for device in native_devices],
        links=[
            Link(
                source_id="replacement",
                a=LinkEndpoint(device="a", interface="a1"),
                b=LinkEndpoint(device="b", interface="b1"),
                relationship_kind="direct",
            )
        ],
    )
    suppressions = [
        SuppressLink(
            a=LinkEndpoint(device="b", interface="b2"),
            b=LinkEndpoint(device="a", interface="a2"),
            protocol="OSPF",
        ),
        SuppressLink(a=LinkEndpoint(device="a"), b=LinkEndpoint(device="b")),
        SuppressLink(
            a=LinkEndpoint(device="a"),
            b=LinkEndpoint(device="b"),
            protocol="cdp",
        ),
    ]

    merged = FederationEngine(IdentityConfig()).merge_with_manual(
        [native, explicit], manual, suppressions, {"native", "artifacts"}
    )

    assert len(merged.links) == 2
    assert {source.provider for link in merged.links.values() for source in link.sources} >= {
        "manual",
        "explicit",
    }
    assert any("matched no automatic relationship" in warning for warning in merged.warnings)


def test_logical_fallback_coverage_does_not_duplicate_parallel_provenance() -> None:
    devices = [
        _device("a", "a.example.net", ("a1", "eth1", 11), ("a2", "eth2", 12), device_id=1),
        _device("b", "b.example.net", ("b1", "eth1", 21), ("b2", "eth2", 22), device_id=2),
    ]
    manual = Topology(
        provider="manual",
        devices=devices,
        links=[
            Link(
                source_id="guided:old:ab",
                protocol="manual",
                relationship_kind="logical",
                a=LinkEndpoint(device="a"),
                b=LinkEndpoint(device="b"),
            )
        ],
    )
    discovered = Topology(provider="oxidized", devices=devices)
    federation = FederationEngine(IdentityConfig())
    for count, status in [(0, "active"), (1, "corroborated"), (2, "parallel-covered")]:
        discovered.links = [
            Link(
                source_id=f"lldp-{index}",
                protocol="lldp",
                a=LinkEndpoint(device="a", interface=f"a{index}"),
                b=LinkEndpoint(device="b", interface=f"b{index}"),
            )
            for index in range(1, count + 1)
        ]
        merged = federation.merge_with_manual([discovered], manual, [], {"oxidized"})
        assert len(merged.links) == max(1, count)
        assert len(merged.link_fallbacks) == 1
        fallback = merged.link_fallbacks[0]
        assert fallback.status == status
        assert fallback.source.source_id == "guided:old:ab"
        assert set(fallback.canonical_ids) == set(merged.links)
        for link in merged.links.values():
            assert ("manual" in {source.provider for source in link.sources}) == (count < 2)
        assert len(manual.links) == 1


def test_parallel_advanced_manual_coverage_survives_automatic_suppressions() -> None:
    devices = [
        _device("a", "a.example.net", ("a1", "eth1", 11), ("a2", "eth2", 12), device_id=1),
        _device("b", "b.example.net", ("b1", "eth1", 21), ("b2", "eth2", 22), device_id=2),
    ]
    discovered = Topology(
        provider="native",
        devices=devices,
        links=[
            Link(
                source_id=f"lldp-{index}",
                protocol="lldp",
                a=LinkEndpoint(device="a", interface=f"a{index}"),
                b=LinkEndpoint(device="b", interface=f"b{index}"),
            )
            for index in (1, 2)
        ],
    )
    advanced = Topology(
        provider="core-reference",
        devices=devices,
        links=[
            Link(
                source_id="reference-ab",
                relationship_kind="logical",
                a=LinkEndpoint(device="a"),
                b=LinkEndpoint(device="b"),
            )
        ],
    )
    merged = FederationEngine(IdentityConfig()).merge_with_manual(
        [discovered, advanced],
        Topology(provider="manual", devices=devices),
        [SuppressLink(a=LinkEndpoint(device="a"), b=LinkEndpoint(device="b"), protocol="lldp")],
        {"native"},
        fallback_providers={"core-reference"},
    )
    assert len(merged.links) == 2
    assert merged.link_fallbacks[0].status == "parallel-covered"
    assert merged.link_fallbacks[0].source.provider == "core-reference"
    assert all(
        [source.provider for source in link.sources] == ["native"] for link in merged.links.values()
    )
