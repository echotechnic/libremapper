from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine

from libremapper.config import IdentityConfig, SourceConfig
from libremapper.federation import FederationEngine
from libremapper.models import Device, Interface, Link, LinkEndpoint, Topology
from libremapper.providers.oxidized import (
    GitTopologySnapshot,
    OxidizedGitProvider,
    OxidizedGitSettings,
)

NNI_CONFIG = "\n".join(
    [
        'oc-if:interfaces interface "IF_ACCESS" config name "IF_ACCESS" type point-to-point',
        'oc-if:interfaces interface "IF_ACCESS" ipv4 addresses address "192.0.2.10" '
        'config ip "192.0.2.10" prefix-length 31',
    ]
)


@pytest.fixture()
def nni_engine(identity_engine: Engine) -> Engine:
    with identity_engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO devices (device_id, hostname, sysName, ip) VALUES
            (3, 'car1.school.example.net', 'car1.school', X'C0000203')
        """)
        connection.exec_driver_sql("""
            INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifAlias, ifIndex,
                               ifPhysAddress, ifType, ifSpeed, deleted)
            VALUES (31, 3, 'ae1.100', 'ae1.100', NULL, 31, NULL, 'l2vlan', 1000000000, 0)
        """)
        connection.exec_driver_sql("""
            CREATE TABLE ipv4_addresses (
                port_id INTEGER, ipv4_address TEXT, ipv4_prefixlen INTEGER, context_name TEXT
            )
        """)
        connection.exec_driver_sql("""
            INSERT INTO ipv4_addresses VALUES (31, '192.0.2.11', 31, '')
        """)
    return identity_engine


def add_configuration(
    worktree: Path,
    bare: Path,
    content: str = NNI_CONFIG,
    configuration_path: str = "local.example.net",
) -> None:
    target = worktree / configuration_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", configuration_path, cwd=worktree)
    git("commit", "-m", "save main configuration", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)


@pytest.mark.parametrize(
    ("description", "expected_port"),
    [
        ("ae1/sfp28-1", "port:33"),
        ("ae1/sfp28-2", "port:32"),
        ("unknown/sfp28-1", "port:32"),
        ("", "port:32"),
    ],
)
@pytest.mark.parametrize("aggregate_type", ["ieee8023adLag", "bridge"])
def test_lldp_uses_bond_from_port_description(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    description: str,
    expected_port: str,
    aggregate_type: str,
) -> None:
    worktree, bare = repository(tmp_path)
    with nni_engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifIndex,
                               ifType, ifSpeed, deleted)
            VALUES (32, 3, 'sfp28-1', 'sfp28-1', 32, 'ethernetCsmacd', 1000000000, 0),
                   (33, 3, 'ae1', 'ae1', 33, ?, 1000000000, 0)
        """, (aggregate_type,))
    (worktree / "topology/local.example.net--lldp-neighbors").write_text(
        "| interface | Ethernet1 |\n| chassis-id | 00aabbccddee |\n"
        + (f"| port-desc | {description} |\n" if description else "")
        + "| port-id | sfp28-1 |\n| system-name | car1.school |\n"
    )
    git("add", "topology", cwd=worktree)
    git("commit", "-m", "add bond neighbor", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    result = OxidizedGitProvider("oxidized", nni_engine, {"repository": str(bare)}).inspect()
    assert result.topology.links[0].b.interface == expected_port


@pytest.mark.parametrize(
    ("forwarding_points", "expected_links"),
    [
        (['fps fp "FP_ACCESS" fd-name "FD_ACCESS" logical-port "Ethernet1"'], 2),
        ([], 3),
        (
            [
                'fps fp "FP_ACCESS" fd-name "FD_ACCESS" logical-port "Ethernet1"',
                'fps fp "FP_OTHER" fd-name "FD_ACCESS" logical-port "Ethernet2"',
            ],
            3,
        ),
    ],
)
@pytest.mark.parametrize("aggregate_type", ["ieee8023adLag", "bridge"])
def test_bond_and_routed_observations_merge_only_with_underlay_evidence(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    forwarding_points: list[str],
    expected_links: int,
    aggregate_type: str,
) -> None:
    worktree, bare = repository(tmp_path)
    with nni_engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifIndex,
                               ifType, ifSpeed, deleted)
            VALUES (32, 3, 'sfp28-1', 'sfp28-1', 32, 'ethernetCsmacd', 1000000000, 0),
                   (33, 3, 'ae1', 'ae1', 33, ?, 1000000000, 0)
        """, (aggregate_type,))
    (worktree / "topology/local.example.net--lldp-neighbors").write_text(
        "| interface | Ethernet1 |\n| chassis-id | 00aabbccddee |\n"
        "| port-desc | ae1/sfp28-1 |\n| port-id | sfp28-1 |\n| system-name | car1.school |\n"
    )
    git("add", "topology", cwd=worktree)
    git("commit", "-m", "add bond neighbor", cwd=worktree)
    config = (
        NNI_CONFIG
        + "\n"
        + "\n".join(
            [
                'oc-if:interfaces interface "IF_ACCESS" config underlay-binding '
                'config fd "FD_ACCESS"',
                *forwarding_points,
            ]
        )
    )
    add_configuration(worktree, bare, config)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    result = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-lldp-neighbors", "saos10-nni-config"],
        },
    ).inspect()
    native = Topology(
        provider="native",
        devices=[
            Device(
                source_id="local",
                hostname="local.example.net",
                librenms_device_id=1,
                interfaces=[
                    Interface(
                        source_id="physical",
                        device_source_id="local",
                        name="Ethernet1",
                        librenms_port_id=11,
                    ),
                    Interface(source_id="routed", device_source_id="local", name="IF_ACCESS"),
                    Interface(source_id="other", device_source_id="local", name="Ethernet2"),
                ],
            ),
            Device(
                source_id="remote",
                hostname="car1.school.example.net",
                librenms_device_id=3,
                interfaces=[
                    Interface(
                        source_id="member",
                        device_source_id="remote",
                        name="sfp28-1",
                        librenms_port_id=32,
                    ),
                    Interface(
                        source_id="vlan",
                        device_source_id="remote",
                        name="ae1.100",
                        librenms_port_id=31,
                    ),
                ],
            ),
        ],
        links=[
            Link(
                source_id="reverse-lldp",
                a=LinkEndpoint(device="remote", interface="member"),
                b=LinkEndpoint(device="local", interface="physical"),
                protocol="lldp",
            ),
            Link(
                source_id="native-ip",
                a=LinkEndpoint(device="local", interface="routed"),
                b=LinkEndpoint(device="remote", interface="vlan"),
                protocol="ip-p2p",
            ),
            Link(
                source_id="parallel",
                a=LinkEndpoint(device="local", interface="other"),
                b=LinkEndpoint(device="remote", interface="member"),
                protocol="lldp",
            ),
        ],
    )
    merged = FederationEngine(IdentityConfig()).merge([result.topology, native])
    assert len(merged.links) == expected_links
    if expected_links != 2:
        return
    link = next(link for link in merged.links.values() if link.attributes.get("evidence"))
    names = {
        device.interfaces[interface].name
        for device_id, interface in [
            (link.a_device, link.a_interface),
            (link.b_device, link.b_interface),
        ]
        for device in [merged.devices[device_id]]
    }
    assert names == {"Ethernet1", "ae1"}
    assert {"ip-p2p", "ciena-config"} <= set(link.attributes["evidence"])


def test_multiple_parsers_preserve_lldp_and_add_missing_nni_interfaces(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, bare = repository(tmp_path)
    add_configuration(worktree, bare, NNI_CONFIG + "\ncredential secret-do-not-expose\n")
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    old = OxidizedGitProvider("oxidized", nni_engine, {"repository": str(bare)}).inspect()
    provider = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-lldp-neighbors", "saos10-nni-config"],
        },
    )
    result = provider.inspect()
    assert not result.blocked and not result.warnings
    assert len(result.topology.links) == 2
    assert old.topology.links[0] == result.topology.links[0]
    assert {r["name"] for r in result.parser_reports} == {
        "saos10-lldp-neighbors",
        "saos10-nni-config",
    }
    assert result.parser_reports[1]["unbound_interfaces"] == 1
    local = next(d for d in result.topology.devices if d.librenms_device_id == 1)
    assert {i.name for i in local.interfaces} == {"Ethernet1", "IF_ACCESS"}
    interface = next(i for i in local.interfaces if i.name == "IF_ACCESS")
    assert interface.librenms_port_id is None
    nni = result.topology.links[1]
    assert nni.protocol == "ip-p2p"
    assert nni.a.interface == interface.source_id and nni.b.interface == "port:31"
    assert nni.attributes["local_interface_unbound"] is True
    assert "secret-do-not-expose" not in result.model_dump_json()
    snapshot = provider.repository.cache / "snapshots" / str(result.revision)
    assert (snapshot / "local.example.net").stat().st_mode & 0o777 == 0o600
    assert snapshot.stat().st_mode & 0o777 == 0o700
    with nni_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM devices").scalar() == 3
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM ports").scalar() == 3


@pytest.mark.parametrize("directory", ["", "configs/"])
def test_nni_parser_supports_configuration_only_repository_and_binds_known_ports(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
) -> None:
    worktree, bare = repository(tmp_path)
    git("rm", "-r", "topology", cwd=worktree)
    add_configuration(worktree, bare, configuration_path=directory + "local.example.net")
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    provider = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-nni-config"],
        },
    )
    before = provider.inspect()
    with nni_engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifIndex, ifSpeed, deleted)
            VALUES (12, 1, 'IF_ACCESS', 'IF_ACCESS', 12, 1000000000, 0)
        """)
    after = provider.inspect()
    assert not after.blocked
    assert len(after.topology.links) == 1
    assert after.topology.links[0].source_id == before.topology.links[0].source_id
    assert after.topology.links[0].a.interface == "port:12"
    assert after.parser_reports[0]["unbound_interfaces"] == 0


@pytest.mark.parametrize("parsers", [[], ["saos10-nni-config"] * 2])
def test_legacy_parser_defaults_reject_empty_and_duplicate_parsers(parsers: list[str]) -> None:
    with pytest.raises(ValidationError, match="parser"):
        OxidizedGitSettings.model_validate({"repository": "/example.git", "parsers": parsers})


@pytest.mark.parametrize(
    "scenario",
    [
        "unknown",
        "deleted",
        "other_context",
        "wrong_prefix",
        "same_ip",
        "ambiguous",
        "ambiguous_local",
    ],
)
def test_nni_parser_requires_unique_existing_car_interfaces(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    worktree, bare = repository(tmp_path)
    add_configuration(worktree, bare)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    with nni_engine.begin() as connection:
        if scenario == "unknown":
            connection.exec_driver_sql(
                "UPDATE devices SET hostname='other.example.net', sysName='other' WHERE device_id=3"
            )
        elif scenario == "deleted":
            connection.exec_driver_sql("UPDATE ports SET deleted=1 WHERE port_id=31")
        elif scenario == "other_context":
            connection.exec_driver_sql("UPDATE ipv4_addresses SET context_name='other'")
        elif scenario == "wrong_prefix":
            connection.exec_driver_sql("UPDATE ipv4_addresses SET ipv4_prefixlen=24")
        elif scenario == "same_ip":
            connection.exec_driver_sql("UPDATE ipv4_addresses SET ipv4_address='192.0.2.10'")
        elif scenario == "ambiguous_local":
            connection.exec_driver_sql("""
                INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifIndex, ifSpeed, deleted)
                VALUES (12, 1, 'IF_ACCESS', 'IF_ACCESS', 12, 1000000000, 0),
                       (13, 1, 'IF_ACCESS', 'IF_ACCESS', 13, 1000000000, 0)
            """)
        else:
            connection.exec_driver_sql("""
                INSERT INTO devices (device_id, hostname, sysName)
                VALUES (4, 'car2.school.example.net', 'car2.school')
            """)
            connection.exec_driver_sql("""
                INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifIndex, ifSpeed, deleted)
                VALUES (41, 4, 'ae1.100', 'ae1.100', 41, 1000000000, 0)
            """)
            connection.exec_driver_sql("INSERT INTO ipv4_addresses VALUES (41,'192.0.2.11',31,'')")
    result = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-nni-config"],
        },
    ).inspect()
    assert not result.blocked
    assert not result.topology.links and not result.topology.devices
    assert bool(result.resolution_failures) == (scenario in {"ambiguous", "ambiguous_local"})


def test_nni_parser_deduplicates_dual_stack_and_rejects_broad_subnets(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, bare = repository(tmp_path)
    config = (
        NNI_CONFIG
        + "\n"
        + "\n".join(
            [
                'oc-if:interfaces interface "IF_ACCESS" ipv6 addresses address "2001:db8::10" '
                'config ip "2001:db8::10" prefix-length 127',
                'oc-if:interfaces interface "LAN" config name "LAN" type point-to-point',
                'oc-if:interfaces interface "LAN" ipv4 addresses address "192.0.2.1" '
                'config ip "192.0.2.1" prefix-length 24',
            ]
        )
    )
    add_configuration(worktree, bare, config)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    with nni_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE ipv6_addresses (
                port_id INTEGER, ipv6_address TEXT, ipv6_compressed BLOB,
                ipv6_prefixlen INTEGER, context_name TEXT
            )
        """)
        connection.exec_driver_sql(
            "INSERT INTO ipv6_addresses VALUES (31, '2001:db8::11', NULL, 127, '')"
        )
    result = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-nni-config"],
        },
    ).inspect()
    assert not result.blocked and not result.warnings
    assert len(result.topology.links) == 1
    assert result.topology.links[0].attributes["local_interface"] == "IF_ACCESS"


@pytest.mark.parametrize(
    ("local_address", "remote_address", "prefixlen"),
    [
        ("192.0.2.9", "192.0.2.10", 30),
        ("192.0.2.10", "192.0.2.11", 31),
        ("2001:db8::11", "2001:db8::10", 126),
        ("2001:db8::11", "2001:db8::10", 127),
    ],
)
def test_nni_parser_matches_point_to_point_networks_in_the_same_context(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    local_address: str,
    remote_address: str,
    prefixlen: int,
) -> None:
    version = 6 if ":" in local_address else 4
    worktree, bare = repository(tmp_path)
    add_configuration(
        worktree,
        bare,
        "\n".join(
            [
                'oc-if:interfaces interface "NNI" config name "NNI" description "type" '
                'type point-to-point vrfName "tenant"',
                'oc-if:interfaces interface "NNI" config description "vrfName" mtu 9168',
                f'oc-if:interfaces interface "NNI" ipv{version} addresses address '
                f'"{local_address}" config ip "{local_address}" prefix-length {prefixlen}',
            ]
        ),
    )
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    with nni_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM ipv4_addresses")
        connection.exec_driver_sql("""
            CREATE TABLE ipv6_addresses (
                port_id INTEGER, ipv6_address TEXT, ipv6_compressed BLOB,
                ipv6_prefixlen INTEGER, context_name TEXT
            )
        """)
        connection.exec_driver_sql(
            f"INSERT INTO ipv{version}_addresses "
            f"(port_id, ipv{version}_address, ipv{version}_prefixlen, context_name) "
            "VALUES (31, ?, ?, 'tenant')",
            (remote_address, prefixlen),
        )
    result = OxidizedGitProvider(
        "oxidized", nni_engine, {"repository": str(bare), "parsers": ["saos10-nni-config"]}
    ).inspect()
    assert not result.blocked and not result.warnings
    assert len(result.topology.links) == 1
    assert result.topology.links[0].attributes["context"] == "tenant"
    assert result.topology.links[0].b.interface == "port:31"


def test_one_broken_parser_blocks_combined_results(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, bare = repository(tmp_path)
    add_configuration(worktree, bare)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    provider = OxidizedGitProvider(
        "oxidized",
        nni_engine,
        {
            "repository": str(bare),
            "parsers": ["saos10-lldp-neighbors", "saos10-nni-config"],
        },
    )
    assert not provider.inspect().blocked
    successful = next((provider.repository.cache / "snapshots").iterdir())
    (worktree / "topology/local.example.net--lldp-neighbors").write_text("broken output")
    git("add", ".", cwd=worktree)
    git("commit", "-m", "break LLDP parser", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)
    result = provider.inspect()
    assert result.blocked and len(result.topology.links) == 1
    assert successful.is_dir()
    with pytest.raises(RuntimeError, match="zero_output_guard"):
        provider.load(SourceConfig(provider="oxidized"))


@pytest.mark.parametrize("failure", ["malformed_config", "no_address_inventory"])
def test_nni_parse_failures_preserve_the_last_successful_snapshot(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    worktree, bare = repository(tmp_path)
    add_configuration(worktree, bare)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    provider = OxidizedGitProvider(
        "oxidized", nni_engine, {"repository": str(bare), "parsers": ["saos10-nni-config"]}
    )
    successful = provider.inspect()
    if failure == "malformed_config":
        add_configuration(worktree, bare, 'oc-if:interfaces interface "secret-do-not-expose')
        message = "invalid Ciena interface configuration"
    else:
        with nni_engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE ipv4_addresses")
        message = "requires LibreNMS interface address inventory"
    with pytest.raises(RuntimeError, match=message) as error:
        provider.inspect()
    assert "secret-do-not-expose" not in str(error.value)
    snapshots = provider.repository.cache / "snapshots"
    assert [path.name for path in snapshots.iterdir()] == [successful.revision]


def test_main_config_snapshot_rejects_symlinks(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree, bare = repository(tmp_path)
    (worktree / "local.example.net").symlink_to("/etc/passwd")
    git("add", ".", cwd=worktree)
    git("commit", "-m", "unsafe main config", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    with pytest.raises(RuntimeError, match="unsafe entry"):
        OxidizedGitProvider(
            "oxidized",
            nni_engine,
            {
                "repository": str(bare),
                "parsers": ["saos10-nni-config"],
            },
        ).inspect()


def git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "worktree"
    bare = tmp_path / "oxidized.git"
    worktree.mkdir()
    git("init", "-b", "main", cwd=worktree)
    git("config", "user.name", "Test", cwd=worktree)
    git("config", "user.email", "test@example.net", cwd=worktree)
    topology = worktree / "topology"
    topology.mkdir()
    (topology / "local.example.net--lldp-neighbors").write_text(
        "\n".join(
            [
                "| interface | Ethernet1 |",
                "| chassis-id | 00aabbccddee |",
                "| port-id | Ethernet9 |",
                "| system-name | known |",
            ]
        )
    )
    git("add", "topology", cwd=worktree)
    git("commit", "-m", "add topology", cwd=worktree)
    git("clone", "--bare", str(worktree), str(bare))
    return worktree, bare


@pytest.fixture()
def identity_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE devices (
                device_id INTEGER PRIMARY KEY,
                hostname TEXT NOT NULL,
                sysName TEXT,
                ip BLOB
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE ports (
                port_id INTEGER PRIMARY KEY,
                device_id INTEGER NOT NULL,
                ifName TEXT,
                ifDescr TEXT,
                ifAlias TEXT,
                ifIndex INTEGER,
                ifPhysAddress TEXT,
                ifType TEXT,
                ifSpeed INTEGER,
                deleted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO devices (device_id, hostname, sysName, ip) VALUES
                (1, 'local.example.net', 'local', X'C0000201'),
                (2, 'known.example.net', 'known', X'C0000202')
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO ports (
                port_id, device_id, ifName, ifDescr, ifAlias, ifIndex,
                ifPhysAddress, ifType, ifSpeed, deleted
            ) VALUES
                (11, 1, 'Ethernet1', 'Ethernet1', NULL, 1,
                 '001122334455', 'ethernetCsmacd', 1000000000, 0),
                (21, 2, 'Ethernet9', 'Ethernet9', NULL, 9,
                 '00aabbccddee', 'ethernetCsmacd', 1000000000, 0)
            """
        )
    return engine


@pytest.mark.parametrize("repository_kind", ["bare", "worktree"])
def test_local_repository_supplies_ciena_neighbors(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    repository_kind: str,
) -> None:
    worktree, bare = repository(tmp_path)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    provider = OxidizedGitProvider(
        "oxidized",
        identity_engine,
        {
            "repository": str(bare if repository_kind == "bare" else worktree),
            "ref": "main",
        },
    )

    inspection = provider.inspect()
    topology = provider.load(SourceConfig(provider="oxidized"))

    assert inspection.provider_type == "oxidized"
    assert inspection.revision == git("rev-parse", "main", cwd=bare)
    assert inspection.blocked is False
    assert inspection.matched_files == ["topology/local.example.net--lldp-neighbors"]
    assert len(topology.links) == 1
    assert {device.hostname for device in topology.devices} == {
        "known.example.net",
        "local.example.net",
    }


def test_remote_fetch_failure_blocks_and_retains_cached_snapshot(
    tmp_path: Path, identity_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bare = repository(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(cache))
    provider = OxidizedGitProvider(
        "oxidized",
        identity_engine,
        {"repository": bare.as_uri(), "ref": "main"},
    )
    assert provider.inspect().blocked is False
    snapshots = list(cache.glob("*/snapshots/*/topology"))
    assert len(snapshots) == 1

    bare.rename(tmp_path / "offline.git")

    with pytest.raises(RuntimeError, match="Oxidized Git operation failed"):
        provider.inspect()
    assert snapshots[0].is_dir()


def test_parse_failure_blocks_and_retains_last_successful_snapshot(
    tmp_path: Path, identity_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree, bare = repository(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(cache))
    provider = OxidizedGitProvider(
        "oxidized",
        identity_engine,
        {"repository": bare.as_uri(), "ref": "main"},
    )
    assert provider.inspect().blocked is False
    successful = next(cache.glob("*/snapshots/*/topology"))

    (worktree / "topology" / "local.example.net--lldp-neighbors").write_text("not LLDP output\n")
    git("add", "topology", cwd=worktree)
    git("commit", "-m", "break topology", cwd=worktree)
    git("push", str(bare), "main", cwd=worktree)

    assert provider.inspect().blocked is True
    assert successful.is_dir()
    assert list(cache.glob("*/snapshots/*/topology")) == [successful]


def test_ssh_repository_requires_external_key_and_known_hosts() -> None:
    with pytest.raises(ValidationError, match="private_key and known_hosts"):
        OxidizedGitSettings.model_validate(
            {"repository": "git@gitea.example.net:network/oxidized.git"}
        )
    with pytest.raises(ValidationError, match="credentials must not be stored"):
        OxidizedGitSettings.model_validate(
            {"repository": "https://user:secret@gitea.example.net/network/oxidized.git"}
        )
    with pytest.raises(ValidationError, match="credentials must not be stored"):
        OxidizedGitSettings.model_validate(
            {
                "repository": "ssh://user:secret@gitea.example.net/network/oxidized.git",
                "private_key": "/run/secrets/key",
                "known_hosts": "/run/secrets/known_hosts",
            }
        )


def test_managed_ssh_key_accepts_and_pins_a_new_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "id_git_fetch"
    known_hosts = tmp_path / "known_hosts"
    key.write_text("private key")
    key.chmod(0o600)
    known_hosts.write_text("")
    captured: dict[str, object] = {}

    def run(command: list[str], **arguments: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(arguments)
        return subprocess.CompletedProcess(command, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", run)
    snapshot = GitTopologySnapshot(
        "oxidized",
        OxidizedGitSettings.model_validate(
            {
                "repository": "git@gitea.example.net:network/oxidized.git",
                "private_key": key,
                "known_hosts": known_hosts,
                "accept_new_host_key": True,
            }
        ),
    )

    assert snapshot._run("ls-remote") == b"ok"
    assert "StrictHostKeyChecking=accept-new" in str(captured["env"])


def test_map_report_counts_only_its_scoped_connections(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from libremapper.config import SelectorConfig

    _, bare = repository(tmp_path)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    provider = OxidizedGitProvider("oxidized", identity_engine, {"repository": str(bare)})
    scoped = provider.inspect(
        SourceConfig(provider="oxidized", selectors=[SelectorConfig(type="device_id", value=1)])
    )
    assert not scoped.blocked
    assert not scoped.topology.links
    assert scoped.parser_reports[0]["links"] == 0
    assert provider.inspect().parser_reports[0]["links"] == 1


def test_map_report_combines_multiple_scopes_for_the_same_provider(
    tmp_path: Path,
    nni_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from libremapper.config import AppConfig
    from libremapper.service import LibremapperService
    from libremapper.state import StateStore

    worktree, bare = repository(tmp_path)
    add_configuration(worktree, bare)
    monkeypatch.setenv("LIBREMAPPER_GIT_CACHE", str(tmp_path / "cache"))
    config = AppConfig.model_validate(
        {
            "providers": {"oxidized": {"type": "oxidized", "settings": {"repository": str(bare)}}},
            "maps": [
                {
                    "name": "Multi scope",
                    "sources": [
                        {"provider": "oxidized", "parsers": ["saos10-lldp-neighbors"]},
                        {"provider": "oxidized", "parsers": ["saos10-nni-config"]},
                        {"provider": "oxidized", "parsers": []},
                    ],
                }
            ],
        }
    )
    mapper = LibremapperService(config, nni_engine, StateStore(tmp_path / "state.sqlite3"))
    result = mapper.inspect_provider("oxidized", "multi-scope")
    assert not result.blocked and len(result.topology.links) == 2
    assert {row["name"] for row in result.parser_reports} == {
        "saos10-lldp-neighbors",
        "saos10-nni-config",
    }
    assert {row["source_index"] for row in result.parser_reports} == {1, 2}
    assert len(result.topology.devices) == 3
