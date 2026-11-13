from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from typing import Any

from sqlalchemy import Engine, inspect, text

from libremapper.models import (
    Link,
    LinkSyncOperation,
    LinkSyncPlan,
    Topology,
)
from libremapper.state import StateStore

PROTOCOL = "lm-artifact"


class NativeLinkSynchronizer:
    def __init__(
        self,
        engine: Engine,
        state: StateStore,
        provider: str,
        adapter_command: list[str],
    ) -> None:
        self.engine = engine
        self.state = state
        self.provider = provider
        self.adapter_command = adapter_command

    def plan(self, topology: Topology) -> LinkSyncPlan:
        if not inspect(self.engine).has_table("links"):
            raise RuntimeError("LibreNMS links table is missing")
        desired, warnings = _materializable_links(topology)
        source_hash = topology.fingerprint()
        managed = self.state.links(self.provider)
        rows = self._rows()
        rows_by_id = {int(row["id"]): row for row in rows}
        rows_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rows_by_key[_natural_key(row)].append(row)

        plan = LinkSyncPlan(
            provider=self.provider,
            source_hash=source_hash,
            warnings=warnings,
        )
        for source_id, values in desired.items():
            natural_key = _natural_key(values)
            record = managed.get(source_id)
            recorded_link_id = (
                int(record["librenms_link_id"]) if record is not None else None
            )
            collisions = [
                row
                for row in rows_by_key.get(natural_key, [])
                if int(row["id"]) != recorded_link_id
            ]
            if collisions:
                ids = sorted(int(row["id"]) for row in collisions)
                plan.conflicts.append(
                    f"{source_id}: natural key collides with unowned link IDs {ids}"
                )
                continue
            if record:
                link_id = int(record["librenms_link_id"])
                current_row = rows_by_id.get(link_id)
                if current_row and current_row.get("protocol") != PROTOCOL:
                    plan.conflicts.append(
                        f"{source_id}: owned link ID {link_id} no longer uses protocol "
                        f"{PROTOCOL}; refusing to modify it"
                    )
                    continue
                if current_row is None:
                    plan.operations.append(
                        LinkSyncOperation(
                            action="create",
                            source_id=source_id,
                            natural_key=natural_key,
                            values=values,
                            reason="owned row is missing",
                        )
                    )
                elif _comparable_row(current_row) != _comparable_values(values):
                    plan.operations.append(
                        LinkSyncOperation(
                            action="update",
                            source_id=source_id,
                            link_id=link_id,
                            natural_key=natural_key,
                            values=values,
                            reason="owned row differs from artifact",
                        )
                    )
                else:
                    plan.operations.append(
                        LinkSyncOperation(
                            action="noop",
                            source_id=source_id,
                            link_id=link_id,
                            natural_key=natural_key,
                            values=values,
                        )
                    )
                continue

            plan.operations.append(
                LinkSyncOperation(
                    action="create",
                    source_id=source_id,
                    natural_key=natural_key,
                    values=values,
                )
            )

        for source_id, record in managed.items():
            if source_id in desired:
                continue
            link_id = int(record["librenms_link_id"])
            current_row = rows_by_id.get(link_id)
            if current_row and current_row.get("protocol") != PROTOCOL:
                plan.conflicts.append(
                    f"{source_id}: stale owned link ID {link_id} no longer uses protocol "
                    f"{PROTOCOL}; refusing to delete it"
                )
                continue
            plan.operations.append(
                LinkSyncOperation(
                    action="delete",
                    source_id=source_id,
                    link_id=link_id if current_row else None,
                    natural_key=str(record["natural_key"]),
                    reason="artifact link is stale",
                )
            )

        for natural_key, matching_rows in rows_by_key.items():
            if len(matching_rows) > 1 and natural_key in {
                operation.natural_key for operation in plan.operations
            }:
                plan.conflicts.append(
                    f"duplicate LibreNMS natural key {natural_key}: "
                    f"{sorted(int(row['id']) for row in matching_rows)}"
                )
        plan.operations.sort(
            key=lambda item: (
                {"delete": 0, "update": 1, "create": 2, "noop": 3}[item.action],
                item.source_id,
            )
        )
        return plan

    def sync(self, plan: LinkSyncPlan) -> None:
        if plan.provider != self.provider:
            raise RuntimeError("link plan provider does not match synchronizer")
        if plan.blocked:
            raise RuntimeError("link synchronization is blocked by conflicts")
        changes = [operation for operation in plan.operations if operation.action != "noop"]
        results: dict[str, int] = {}
        if changes:
            if not self.adapter_command:
                raise RuntimeError("link adapter command is not configured")
            completed = subprocess.run(
                self.adapter_command,
                input=json.dumps(
                    {
                        "provider": self.provider,
                        "protocol": PROTOCOL,
                        "operations": [operation.model_dump(mode="json") for operation in changes],
                    }
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise RuntimeError(f"LibreNMS link adapter failed: {detail}")
            try:
                response = json.loads(completed.stdout)
                results = {
                    str(item["source_id"]): int(item["link_id"])
                    for item in response.get("results", [])
                    if item.get("link_id") is not None
                }
            except (ValueError, KeyError, TypeError) as error:
                raise RuntimeError("LibreNMS link adapter returned invalid JSON") from error

        with self.state.transaction() as connection:
            for operation in plan.operations:
                if operation.action == "delete":
                    StateStore.delete_link(connection, self.provider, operation.source_id)
                    continue
                link_id = results.get(operation.source_id, operation.link_id)
                if link_id is None:
                    raise RuntimeError(
                        f"link adapter did not return an ID for {operation.source_id}"
                    )
                StateStore.save_link(
                    connection,
                    self.provider,
                    operation.source_id,
                    link_id,
                    operation.natural_key,
                    operation.values,
                    plan.source_hash,
                )

    def _rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT id, local_port_id, local_device_id, remote_port_id,
                               active, protocol, remote_hostname, remote_device_id,
                               remote_port, remote_platform, remote_version
                        FROM links
                        """
                    )
                ).mappings()
            ]


def _materializable_links(
    topology: Topology,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    devices = {device.source_id: device for device in topology.devices}
    interfaces = {
        (device.source_id, interface.source_id): interface
        for device in topology.devices
        for interface in device.interfaces
    }
    desired: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for link in topology.links:
        local_endpoint, remote_endpoint = _ordered_endpoints(link)
        local_device = devices.get(local_endpoint.device)
        remote_device = devices.get(remote_endpoint.device)
        local_interface = (
            interfaces.get((local_endpoint.device, local_endpoint.interface))
            if local_endpoint.interface
            else None
        )
        remote_interface = (
            interfaces.get((remote_endpoint.device, remote_endpoint.interface))
            if remote_endpoint.interface
            else None
        )
        if not local_device or local_device.librenms_device_id is None:
            warnings.append(f"{link.source_id}: unresolved local device; not materialized")
            continue
        if not local_interface or local_interface.librenms_port_id is None:
            warnings.append(
                f"{link.source_id}: unresolved or missing local interface; not materialized"
            )
            continue
        remote_hostname = str(
            link.attributes.get("remote_hostname")
            or (remote_device.hostname if remote_device else "")
            or remote_endpoint.device
        )
        desired[link.source_id] = {
            "protocol": PROTOCOL,
            "local_device_id": local_device.librenms_device_id,
            "local_port_id": local_interface.librenms_port_id,
            "local_hostname": local_device.hostname,
            "local_management_ip": local_device.management_ip,
            "local_interface": local_interface.name,
            "remote_device_id": (
                remote_device.librenms_device_id
                if remote_device and remote_device.librenms_device_id is not None
                else 0
            ),
            "remote_port_id": (
                remote_interface.librenms_port_id
                if remote_interface and remote_interface.librenms_port_id is not None
                else None
            ),
            "remote_hostname": remote_hostname,
            "remote_management_ip": link.attributes.get("remote_management_ip"),
            "remote_port": (
                remote_interface.name
                if remote_interface and remote_interface.name
                else str(link.attributes.get("remote_interface") or "")
            ),
            "remote_platform": link.attributes.get("remote_platform"),
            "remote_version": str(link.attributes.get("remote_description") or ""),
        }
    return desired, warnings


def _ordered_endpoints(link: Link) -> tuple[Any, Any]:
    if link.attributes.get("local_endpoint") == "b":
        return link.b, link.a
    return link.a, link.b


def _natural_key(values: dict[str, Any]) -> str:
    return json.dumps(
        [
            values.get("remote_hostname"),
            values.get("local_port_id"),
            values.get("protocol"),
            values.get("remote_port"),
        ],
        separators=(",", ":"),
    )


def _comparable_values(values: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "local_port_id",
        "local_device_id",
        "remote_port_id",
        "protocol",
        "remote_hostname",
        "remote_device_id",
        "remote_port",
        "remote_platform",
        "remote_version",
    }
    result = {field: values.get(field) for field in fields}
    result["active"] = 1
    return result


def _comparable_row(row: dict[str, Any]) -> dict[str, Any]:
    result = _comparable_values(row)
    result["active"] = int(bool(row.get("active")))
    return result
