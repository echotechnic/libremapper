from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

from libremapper.config import AppConfig, SourceConfig
from libremapper.providers.oxidized import SAOS10_NEIGHBORS
from libremapper.providers.regex_artifacts import RegexArtifactsProvider
from libremapper.service import LibremapperService
from libremapper.state import StateStore


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
                ip BLOB,
                location_id INTEGER
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
        connection.exec_driver_sql("CREATE TABLE locations (id INTEGER PRIMARY KEY, location TEXT)")
        connection.exec_driver_sql("CREATE TABLE device_groups (id INTEGER PRIMARY KEY, name TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE device_group_device (device_id INTEGER, device_group_id INTEGER)"
        )
        connection.exec_driver_sql(
            """
            INSERT INTO devices (device_id, hostname, sysName, ip, location_id) VALUES
                (1, 'local.example.net', 'local', X'C0000201', NULL),
                (2, 'known.example.net', 'known', X'C0000202', 1)
            """
        )
        connection.exec_driver_sql("INSERT INTO locations VALUES (1, 'Branch 1')")
        connection.exec_driver_sql("INSERT INTO device_groups VALUES (1, 'Core')")
        connection.exec_driver_sql("INSERT INTO device_group_device VALUES (1, 1)")
        connection.exec_driver_sql(
            """
            INSERT INTO ports (
                port_id, device_id, ifName, ifDescr, ifAlias, ifIndex,
                ifPhysAddress, ifType, ifSpeed, deleted
            ) VALUES
                (11, 1, 'Ethernet1', 'Ethernet1', 'uplink', 1,
                 '001122334455', 'ethernetCsmacd', 1000000000, 0),
                (21, 2, 'Ethernet9', 'Ethernet9', NULL, 9,
                 '00aabbccddee', 'ethernetCsmacd', 1000000000, 0)
            """
        )
    return engine


def source() -> SourceConfig:
    return SourceConfig(provider="artifacts")


def settings(root: Path) -> dict[str, object]:
    return {
        "root": str(root),
        "file_glob": "**/*.txt",
        "path_regex": r"^(?P<site>[^/]+)/(?P<device_name>[^/]+)/.*\.txt$",
        "host_inventory": {
            "hosts": [
                {
                    "glob": "hq/wrong-device/*.txt",
                    "variables": {"device_name": "local.example.net"},
                }
            ]
        },
        "zero_output_guard": True,
        "rules": [
            {
                "name": "neighbors",
                "flags": ["MULTILINE"],
                "regex": (
                    r"^LOCAL (?P<local_interface>\S+) "
                    r"CONTENT_DEVICE (?P<device_name>\S+) "
                    r"REMOTE (?P<remote_hostname>\S+) "
                    r"PORT (?P<remote_interface>\S+)$"
                ),
            }
        ],
    }


def test_regex_artifacts_precedence_repeated_matches_and_resolution(
    tmp_path: Path, identity_engine: Engine
) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "hq" / "wrong-device" / "lldp.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "\n".join(
            [
                "LOCAL Ethernet1 CONTENT_DEVICE content-wrong "
                "REMOTE known.example.net PORT Ethernet9",
                "LOCAL Ethernet1 CONTENT_DEVICE content-wrong "
                "REMOTE external.example.net PORT xe-0/0/0",
                "LOCAL MissingPort CONTENT_DEVICE content-wrong "
                "REMOTE malformed.example.net PORT Ethernet1",
            ]
        )
    )

    inspection = RegexArtifactsProvider("artifacts", identity_engine, settings(root)).inspect()

    assert inspection.blocked is False
    assert inspection.matched_rules == {"hq/wrong-device/lldp.txt": "neighbors"}
    assert len(inspection.topology.links) == 3
    devices = {device.hostname: device for device in inspection.topology.devices}
    assert devices["local.example.net"].librenms_device_id == 1
    assert devices["known.example.net"].librenms_device_id == 2
    assert devices["external.example.net"].librenms_device_id is None
    assert devices["external.example.net"].attributes["external"] is True
    assert any("MissingPort" in failure["reason"] for failure in inspection.resolution_failures)

    selected = RegexArtifactsProvider("artifacts", identity_engine, settings(root)).load(source())
    assert len(selected.links) == 3


def test_saos_interface_warnings_separate_device_and_port_without_changing_captures(
    tmp_path: Path, identity_engine: Engine
) -> None:
    with identity_engine.begin() as connection:
        connection.execute(text("UPDATE devices SET sysName = 'BCR1-ABQ1' WHERE device_id = 2"))
    artifact = tmp_path / "topology" / "local.example.net--lldp-neighbors"
    artifact.parent.mkdir()
    artifact.write_text(
        "| interface | 2/3 |\n"
        "| chassis-id | 00aabbccddee |\n"
        "| port-id | 2/4 |\n"
        "| system-name | BCR1-ABQ1 |\n"
    )
    inspection = RegexArtifactsProvider(
        "oxidized",
        identity_engine,
        {
            "root": str(tmp_path),
            "file_glob": "topology/*--lldp-neighbors",
            "path_regex": r"^topology/(?P<device_name>.+)--lldp-neighbors$",
            "rules": [
                {
                    "name": "saos10-lldp-neighbors",
                    "flags": ["MULTILINE", "DOTALL"],
                    "regex": SAOS10_NEIGHBORS,
                }
            ],
        },
    ).inspect()

    assert any(
        "remote interface BCR1-ABQ1:2/4 did not resolve uniquely" in warning
        for warning in inspection.warnings
    )
    assert any(
        "local interface local.example.net:2/3 did not resolve uniquely" in warning
        for warning in inspection.warnings
    )
    link = inspection.topology.links[0]
    assert link.attributes["remote_hostname"] == "BCR1-ABQ1"
    assert link.attributes["remote_interface"] == "2/4"
    devices = {device.source_id: device for device in inspection.topology.devices}
    remote = devices[link.b.device]
    assert remote.librenms_device_id == 2
    assert (
        next(port for port in remote.interfaces if port.source_id == link.b.interface).name == "2/4"
    )


def test_regex_artifacts_zero_output_guard_blocks_planning(
    tmp_path: Path, identity_engine: Engine
) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "hq" / "wrong-device" / "empty.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("not an LLDP block\n")
    provider = RegexArtifactsProvider("artifacts", identity_engine, settings(root))

    inspection = provider.inspect()

    assert inspection.blocked is True
    assert inspection.skipped_files[0]["reason"] == ("no parsing rule produced a valid neighbor")
    with pytest.raises(RuntimeError, match="zero_output_guard"):
        provider.load(source())


def test_path_variables_override_content_captures(tmp_path: Path, identity_engine: Engine) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "hq" / "local.example.net" / "lldp.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "LOCAL Ethernet1 CONTENT_DEVICE content-wrong REMOTE known.example.net PORT Ethernet9\n"
    )
    provider_settings = settings(root)
    provider_settings["host_inventory"] = None

    inspection = RegexArtifactsProvider("artifacts", identity_engine, provider_settings).inspect()

    assert inspection.blocked is False
    assert len(inspection.topology.links) == 1
    assert not any(
        "local device" in failure["reason"] for failure in inspection.resolution_failures
    )


def test_group_and_location_selectors_match_resolved_artifact_devices(
    tmp_path: Path, identity_engine: Engine
) -> None:
    root = tmp_path / "artifacts"
    artifact = root / "hq" / "local.example.net" / "lldp.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "LOCAL Ethernet1 CONTENT_DEVICE local.example.net REMOTE known.example.net PORT Ethernet9\n"
    )
    provider_settings = settings(root)
    provider_settings["host_inventory"] = None
    selected = RegexArtifactsProvider("artifacts", identity_engine, provider_settings).load(
        SourceConfig.model_validate(
            {
                "provider": "artifacts",
                "selectors": [
                    {"type": "group", "value": "Core", "layer": 0},
                    {"type": "location", "value": "^Branch", "layer": 10},
                ],
            }
        )
    )

    assert {device.hostname: device.layer for device in selected.devices} == {
        "local.example.net": 0,
        "known.example.net": 10,
    }
    assert len(selected.links) == 1


def test_duplicate_ciena_port_uses_only_unique_positive_speed(identity_engine: Engine) -> None:
    from libremapper.providers.regex_artifacts import LibreNMSIdentityResolver

    with identity_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE ports SET ifName = '1', ifDescr = '1' WHERE port_id = 11"
        )
        connection.exec_driver_sql(
            "INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifType, ifSpeed) "
            "VALUES (12, 1, '1', 'virtual', 'ethernetCsmacd', 0)"
        )
    resolver = LibreNMSIdentityResolver(identity_engine)
    physical = resolver.resolve_port(1, "1")
    assert physical is not None and physical.port_id == 11
    with identity_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE ports SET ifSpeed = 1000000000 WHERE port_id = 12")
    assert LibreNMSIdentityResolver(identity_engine).resolve_port(1, "1") is None
    with identity_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE ports SET ifSpeed = NULL WHERE device_id = 1")
    assert LibreNMSIdentityResolver(identity_engine).resolve_port(1, "1") is None


@pytest.mark.parametrize("ethernet_type", ["ethernetCsmacd", "ETHERNETCSMACD"])
@pytest.mark.parametrize("ethernet_speed", [0, None])
@pytest.mark.parametrize("virtual_speed", [None, 1000000000])
def test_duplicate_ciena_port_prefers_ethernet_with_zero_or_unknown_speed(
    identity_engine: Engine,
    ethernet_type: str,
    ethernet_speed: int | None,
    virtual_speed: int | None,
) -> None:
    from libremapper.providers.regex_artifacts import LibreNMSIdentityResolver

    with identity_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ports SET port_id = 13458, ifName = '2/4', ifDescr = '2/4', "
                "ifAlias = '2/4', ifIndex = 199, ifType = 'propVirtual', ifSpeed = :speed "
                "WHERE port_id = 21"
            ),
            {"speed": virtual_speed},
        )
        connection.execute(
            text(
                "INSERT INTO ports (port_id, device_id, ifName, ifDescr, ifAlias, ifIndex, "
                "ifType, ifSpeed, ifPhysAddress) VALUES "
                "(12761, 2, '2/4', '2/4', '2/4', 191, :type, :speed, '00aabbccddee')"
            ),
            {"type": ethernet_type, "speed": ethernet_speed},
        )

    resolved = LibreNMSIdentityResolver(identity_engine).resolve_port(2, "2/4")

    assert resolved is not None and resolved.port_id == 12761


@pytest.mark.parametrize("ethernet_speed", [0, None, 1000000000])
def test_duplicate_ethernet_ambiguity_never_selects_a_virtual_port(
    identity_engine: Engine, ethernet_speed: int | None
) -> None:
    from libremapper.providers.regex_artifacts import LibreNMSIdentityResolver

    with identity_engine.begin() as connection:
        connection.execute(
            text("UPDATE ports SET ifName = '2/4', ifSpeed = :speed WHERE port_id = 11"),
            {"speed": ethernet_speed},
        )
        connection.execute(
            text(
                "INSERT INTO ports (port_id, device_id, ifName, ifType, ifSpeed) VALUES "
                "(12, 1, '2/4', 'ethernetCsmacd', :speed), "
                "(13, 1, '2/4', 'propVirtual', 10000000000)"
            ),
            {"speed": ethernet_speed},
        )

    assert LibreNMSIdentityResolver(identity_engine).resolve_port(1, "2/4") is None


def test_duplicate_non_ethernet_ports_keep_unique_positive_speed_resolution(
    identity_engine: Engine,
) -> None:
    from libremapper.providers.regex_artifacts import LibreNMSIdentityResolver

    with identity_engine.begin() as connection:
        connection.exec_driver_sql("UPDATE ports SET ifType = 'other' WHERE port_id = 11")
        connection.exec_driver_sql(
            "INSERT INTO ports (port_id, device_id, ifName, ifType, ifSpeed) VALUES "
            "(12, 1, 'Ethernet1', 'propVirtual', 0)"
        )

    resolved = LibreNMSIdentityResolver(identity_engine).resolve_port(1, "Ethernet1")

    assert resolved is not None and resolved.port_id == 11


def test_optional_map_inspection_context_scopes_legacy_regex_providers(
    tmp_path: Path,
    identity_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "hq" / "wrong-device" / "lldp.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "LOCAL Ethernet1 CONTENT_DEVICE ignored REMOTE known.example.net PORT Ethernet9"
    )
    config = AppConfig.model_validate(
        {
            "providers": {"artifacts": {"type": "regex_artifacts", "settings": settings(tmp_path)}},
            "maps": [
                {
                    "name": "Scoped",
                    "sources": [
                        {"provider": "artifacts", "selectors": [{"type": "device_id", "value": 1}]}
                    ],
                },
                {"name": "Absent", "sources": []},
            ],
        }
    )
    service = LibremapperService(config, identity_engine, StateStore(tmp_path / "state.sqlite3"))
    assert len(service.inspect_provider("artifacts").topology.links) == 1
    scoped = service.inspect_provider("artifacts", "scoped")
    assert not scoped.blocked and scoped.provider_type == "regex_artifacts"
    assert len(scoped.topology.devices) == 1 and not scoped.topology.links
    monkeypatch.setattr(
        RegexArtifactsProvider,
        "inspect",
        lambda *args, **kwargs: pytest.fail("out-of-map inspection must not read artifacts"),
    )
    assert not service.inspect_provider("artifacts", "absent").topology.devices
