from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine

from libremapper.link_sync import PROTOCOL, NativeLinkSynchronizer
from libremapper.models import Device, Interface, Link, LinkEndpoint, Topology
from libremapper.state import StateStore


def links_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'links.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_port_id INTEGER,
                local_device_id INTEGER NOT NULL,
                remote_port_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                protocol TEXT,
                remote_hostname TEXT NOT NULL,
                remote_device_id INTEGER NOT NULL,
                remote_port TEXT NOT NULL,
                remote_platform TEXT,
                remote_version TEXT NOT NULL
            )
            """
        )
    return engine


def topology() -> Topology:
    return Topology(
        provider="artifacts",
        devices=[
            Device(
                source_id="local",
                hostname="local.example.net",
                librenms_device_id=1,
                interfaces=[
                    Interface(
                        source_id="local-port",
                        device_source_id="local",
                        name="Ethernet1",
                        librenms_port_id=11,
                    )
                ],
            ),
            Device(
                source_id="remote",
                hostname="remote.example.net",
                librenms_device_id=2,
                interfaces=[
                    Interface(
                        source_id="remote-port",
                        device_source_id="remote",
                        name="Ethernet9",
                        librenms_port_id=21,
                    )
                ],
            ),
        ],
        links=[
            Link(
                source_id="neighbor-1",
                a=LinkEndpoint(device="local", interface="local-port"),
                b=LinkEndpoint(device="remote", interface="remote-port"),
                attributes={
                    "local_endpoint": "a",
                    "remote_hostname": "remote.example.net",
                    "remote_interface": "Ethernet9",
                },
            )
        ],
    )


def test_link_plan_is_idempotent_for_owned_rows(tmp_path: Path) -> None:
    engine = links_engine(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    synchronizer = NativeLinkSynchronizer(engine, state, "artifacts", ["/bin/false"])
    first = synchronizer.plan(topology())
    create = first.operations[0]
    assert create.action == "create"

    with engine.begin() as connection:
        link_id = int(
            connection.exec_driver_sql(
                """
                INSERT INTO links (
                    local_port_id, local_device_id, remote_port_id, active,
                    protocol, remote_hostname, remote_device_id, remote_port,
                    remote_platform, remote_version
                ) VALUES (11, 1, 21, 1, ?, 'remote.example.net', 2,
                          'Ethernet9', NULL, '')
                """,
                (PROTOCOL,),
            ).lastrowid
        )
    with state.transaction() as connection:
        StateStore.save_link(
            connection,
            "artifacts",
            "neighbor-1",
            link_id,
            create.natural_key,
            create.values,
            first.source_hash,
        )

    second = synchronizer.plan(topology())

    assert second.changes == 0
    assert second.operations[0].action == "noop"


def test_link_plan_never_adopts_or_deletes_unowned_rows(tmp_path: Path) -> None:
    engine = links_engine(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO links (
                local_port_id, local_device_id, remote_port_id, active,
                protocol, remote_hostname, remote_device_id, remote_port,
                remote_platform, remote_version
            ) VALUES (11, 1, 21, 1, 'lm-artifact', 'remote.example.net', 2,
                      'Ethernet9', NULL, '')
            """
        )

    plan = NativeLinkSynchronizer(engine, state, "artifacts", ["/bin/false"]).plan(topology())

    assert plan.blocked is True
    assert any("collides with unowned link IDs" in item for item in plan.conflicts)
    assert not any(operation.action == "delete" for operation in plan.operations)


def test_stale_owned_protocol_change_is_not_deleted(tmp_path: Path) -> None:
    engine = links_engine(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    with engine.begin() as connection:
        link_id = int(
            connection.exec_driver_sql(
                """
                INSERT INTO links (
                    local_port_id, local_device_id, remote_port_id, active,
                    protocol, remote_hostname, remote_device_id, remote_port,
                    remote_platform, remote_version
                ) VALUES (11, 1, 21, 1, 'lldp', 'remote.example.net', 2,
                          'Ethernet9', NULL, '')
                """
            ).lastrowid
        )
    with state.transaction() as connection:
        StateStore.save_link(
            connection,
            "artifacts",
            "stale",
            link_id,
            "stale-key",
            {},
            "old-hash",
        )

    plan = NativeLinkSynchronizer(engine, state, "artifacts", ["/bin/false"]).plan(
        Topology(provider="artifacts")
    )

    assert plan.blocked is True
    assert any("refusing to delete it" in item for item in plan.conflicts)
    assert not plan.operations


def test_stale_owned_artifact_row_is_planned_for_deletion(tmp_path: Path) -> None:
    engine = links_engine(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3")
    with engine.begin() as connection:
        link_id = int(
            connection.exec_driver_sql(
                """
                INSERT INTO links (
                    local_port_id, local_device_id, remote_port_id, active,
                    protocol, remote_hostname, remote_device_id, remote_port,
                    remote_platform, remote_version
                ) VALUES (11, 1, 21, 1, 'lm-artifact', 'remote.example.net', 2,
                          'Ethernet9', NULL, '')
                """
            ).lastrowid
        )
    with state.transaction() as connection:
        StateStore.save_link(
            connection,
            "artifacts",
            "stale",
            link_id,
            "stale-key",
            {},
            "old-hash",
        )

    plan = NativeLinkSynchronizer(engine, state, "artifacts", ["/bin/false"]).plan(
        Topology(provider="artifacts")
    )

    assert plan.blocked is False
    assert plan.operations[0].action == "delete"
    assert plan.operations[0].link_id == link_id
