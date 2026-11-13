from __future__ import annotations

import fnmatch
import ipaddress
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine, inspect, text

from libremapper.config import SourceConfig
from libremapper.federation import normalize_identity
from libremapper.models import (
    Device,
    Interface,
    Link,
    LinkEndpoint,
    ProviderInspection,
    Topology,
    stable_hash,
)
from libremapper.providers.base import TopologyProvider
from libremapper.providers.file import select_topology


class RegexRule(BaseModel):
    name: str
    regex: str
    flags: list[str] = Field(default_factory=lambda: ["MULTILINE"])

    @model_validator(mode="after")
    def validate_capture(self) -> RegexRule:
        compiled = re.compile(self.regex, _regex_flags(self.flags))
        if "remote_hostname" not in compiled.groupindex:
            raise ValueError(f"rule {self.name!r} must define a named remote_hostname capture")
        return self

    def compile(self) -> re.Pattern[str]:
        return re.compile(self.regex, _regex_flags(self.flags))


class RegexArtifactSettings(BaseModel):
    root: Path
    file_glob: str = "**/*"
    file_globs: list[str] | None = None
    path_regex: str
    host_inventory: Any | None = None
    rules: list[RegexRule]
    zero_output_guard: bool = True
    protocol: str = "lldp"

    @model_validator(mode="after")
    def validate_settings(self) -> RegexArtifactSettings:
        re.compile(self.path_regex)
        if not self.rules:
            raise ValueError("at least one parsing rule is required")
        return self


@dataclass(frozen=True)
class ResolvedDevice:
    device_id: int
    hostname: str
    sys_name: str | None
    management_ip: str | None
    location: str | None
    groups: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedPort:
    port_id: int
    name: str
    if_index: int | None
    mac: str | None
    attributes: dict[str, Any]


class LibreNMSIdentityResolver:
    def __init__(self, engine: Engine) -> None:
        self.devices: dict[int, ResolvedDevice] = {}
        self.device_tokens: dict[str, set[int]] = defaultdict(set)
        self.ports: dict[int, dict[int, ResolvedPort]] = defaultdict(dict)
        self.port_tokens: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        self._load(engine)

    def _load(self, engine: Engine) -> None:
        schema = inspect(engine)
        if not schema.has_table("devices") or not schema.has_table("ports"):
            raise RuntimeError("regex_artifacts requires LibreNMS devices and ports tables")
        port_columns = {column["name"] for column in schema.get_columns("ports")}
        deleted_filter = "WHERE deleted = 0" if "deleted" in port_columns else ""
        has_locations = schema.has_table("locations") and "location_id" in {
            column["name"] for column in schema.get_columns("devices")
        }
        has_groups = schema.has_table("device_groups") and schema.has_table("device_group_device")
        with engine.connect() as connection:
            groups: dict[int, list[str]] = defaultdict(list)
            if has_groups:
                for group_row in connection.execute(
                    text(
                        """
                        SELECT dgd.device_id, dg.id AS group_id, dg.name AS group_name
                        FROM device_group_device dgd
                        JOIN device_groups dg ON dg.id = dgd.device_group_id
                        """
                    )
                ):
                    groups[int(group_row.device_id)].extend(
                        [str(group_row.group_id), str(group_row.group_name)]
                    )
            device_rows = connection.execute(
                text(
                    "SELECT d.device_id, d.hostname, d.sysName, d.ip, "
                    + ("l.location" if has_locations else "NULL AS location")
                    + " FROM devices d "
                    + ("LEFT JOIN locations l ON l.id = d.location_id" if has_locations else "")
                )
            ).mappings()
            for row in device_rows:
                device_id = int(row["device_id"])
                hostname = str(row["hostname"])
                device = ResolvedDevice(
                    device_id=device_id,
                    hostname=hostname,
                    sys_name=str(row["sysName"]) if row["sysName"] else None,
                    management_ip=_decode_ip(row["ip"]),
                    location=str(row["location"]) if row["location"] else None,
                    groups=tuple(groups[device_id]),
                )
                self.devices[device_id] = device
                for value in (device.hostname, device.sys_name, device.management_ip):
                    self._index_device(device_id, value)

            port_rows = connection.execute(
                text(
                    """
                    SELECT port_id, device_id, ifName, ifDescr, ifAlias, ifIndex,
                           ifPhysAddress, ifType, ifSpeed
                    FROM ports
                    """
                    + deleted_filter
                )
            ).mappings()
            for row in port_rows:
                device_id = int(row["device_id"])
                port_id = int(row["port_id"])
                name = str(row["ifName"] or row["ifDescr"] or row["ifAlias"] or port_id)
                port = ResolvedPort(
                    port_id=port_id,
                    name=name,
                    if_index=int(row["ifIndex"]) if row["ifIndex"] is not None else None,
                    mac=str(row["ifPhysAddress"]) if row["ifPhysAddress"] else None,
                    attributes={
                        "if_descr": row["ifDescr"],
                        "if_alias": row["ifAlias"],
                        "if_type": row["ifType"],
                        "if_speed": row["ifSpeed"],
                    },
                )
                self.ports[device_id][port_id] = port
                for value in (
                    row["ifName"],
                    row["ifDescr"],
                    row["ifAlias"],
                    row["ifIndex"],
                ):
                    if value is not None and str(value).strip():
                        self.port_tokens[device_id][normalize_identity(value)].add(port_id)

    def _index_device(self, device_id: int, value: str | None) -> None:
        if not value:
            return
        token = normalize_identity(value)
        self.device_tokens[token].add(device_id)
        if "." in token:
            self.device_tokens[token.split(".", 1)[0]].add(device_id)

    def resolve_device(
        self, name: str | None, management_ip: str | None = None
    ) -> ResolvedDevice | None:
        candidates: set[int] = set()
        for value in (name, management_ip):
            if value:
                candidates.update(self.device_tokens.get(normalize_identity(value), set()))
        if len(candidates) != 1:
            return None
        return self.devices[next(iter(candidates))]

    def resolve_port(self, device_id: int, value: str | None) -> ResolvedPort | None:
        if not value:
            return None
        candidates = self.port_tokens[device_id].get(normalize_identity(value), set())
        if len(candidates) > 1:
            ethernet = {
                port_id
                for port_id in candidates
                if str(self.ports[device_id][port_id].attributes.get("if_type") or "").lower()
                == "ethernetcsmacd"
            }
            if ethernet:
                candidates = ethernet
        if len(candidates) > 1:
            candidates = {
                port_id
                for port_id in candidates
                if (self.ports[device_id][port_id].attributes.get("if_speed") or 0) > 0
            }
        if len(candidates) != 1:
            return None
        return self.ports[device_id][next(iter(candidates))]


class RegexArtifactsProvider(TopologyProvider):
    def __init__(self, name: str, engine: Engine, settings: dict[str, Any]) -> None:
        super().__init__(name)
        self.settings = RegexArtifactSettings.model_validate(settings)
        self.engine = engine

    def load(self, source: SourceConfig) -> Topology:
        inspection = self.inspect()
        if inspection.blocked:
            raise RuntimeError(
                f"provider {self.name}: zero_output_guard blocked planning because "
                f"{len(inspection.matched_files)} matching artifact(s) produced no valid links"
            )
        return select_topology(inspection.topology, source)

    def inspect(
        self,
        resolver: LibreNMSIdentityResolver | None = None,
        *,
        preview_records: list[dict[str, Any]] | None = None,
    ) -> ProviderInspection:
        resolver = resolver or LibreNMSIdentityResolver(self.engine)
        root = self.settings.root
        if not root.is_dir():
            raise RuntimeError(f"artifact root does not exist or is not a directory: {root}")

        path_pattern = re.compile(self.settings.path_regex)
        rules = [(rule, rule.compile()) for rule in self.settings.rules]
        host_inventory = _load_host_inventory(self.settings.host_inventory)
        devices: dict[str, Device] = {}
        links: list[Link] = []
        matched_files: list[str] = []
        matched_rules: dict[str, str] = {}
        skipped_files: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        warnings: list[str] = []

        files = sorted(
            {
                path
                for pattern in (self.settings.file_globs or [self.settings.file_glob])
                for path in root.glob(pattern)
                if path.is_file() and not path.is_symlink()
            }
        )
        for path in files:
            relative = path.relative_to(root).as_posix()
            matched_files.append(relative)
            path_match = path_pattern.search(relative)
            if not path_match:
                reason = "path_regex did not match"
                skipped_files.append({"path": relative, "reason": reason})
                warnings.append(f"{relative}: {reason}")
                continue
            path_variables = _clean_captures(path_match.groupdict())
            host_variables = _host_variables(relative, host_inventory)
            try:
                content = path.read_text(errors="replace")
            except OSError as error:
                reason = f"unable to read artifact: {error}"
                skipped_files.append({"path": relative, "reason": reason})
                warnings.append(f"{relative}: {reason}")
                continue

            selected_rule: str | None = None
            for rule, compiled in rules:
                rule_links: list[Link] = []
                for block_number, match in enumerate(compiled.finditer(content), start=1):
                    captures = _clean_captures(match.groupdict())
                    variables = {**captures, **path_variables, **host_variables}
                    if preview_records is not None and len(preview_records) < 100:
                        preview_records.append(
                            {
                                "path": relative,
                                "rule": rule.name,
                                "block": block_number,
                                "captures": {
                                    key: str(value)[:1024]
                                    for key, value in list(variables.items())[:32]
                                },
                            }
                        )
                    remote_hostname = captures.get("remote_hostname")
                    local_name = _first(variables, "device_name", "local_hostname", "hostname")
                    if not remote_hostname:
                        _failure(
                            failures,
                            warnings,
                            relative,
                            rule.name,
                            block_number,
                            "remote_hostname capture is empty",
                        )
                        continue
                    if not local_name:
                        _failure(
                            failures,
                            warnings,
                            relative,
                            rule.name,
                            block_number,
                            "local device variable is missing",
                        )
                        continue

                    local = resolver.resolve_device(
                        local_name, variables.get("local_management_ip")
                    )
                    if local is None:
                        _failure(
                            failures,
                            warnings,
                            relative,
                            rule.name,
                            block_number,
                            f"local device {local_name!r} did not resolve uniquely",
                        )
                        continue
                    local_interface_name = captures.get("local_interface")
                    local_port = resolver.resolve_port(local.device_id, local_interface_name)
                    if local_interface_name and local_port is None:
                        _failure(
                            failures,
                            warnings,
                            relative,
                            rule.name,
                            block_number,
                            f"local interface {local_name}:{local_interface_name} "
                            "did not resolve uniquely; retaining it as an unbound interface",
                        )

                    remote = resolver.resolve_device(
                        remote_hostname, captures.get("remote_management_ip")
                    )
                    local_source_id = _resolved_device_source(local)
                    _add_resolved_device(devices, local, local_port)
                    local_interface_id = (
                        _resolved_port_source(local_port)
                        if local_port
                        else (
                            _add_named_interface(devices[local_source_id], local_interface_name)
                            if local_interface_name
                            else None
                        )
                    )

                    remote_interface_id: str | None
                    remote_member_interface = None
                    if remote:
                        remote_source_id = _resolved_device_source(remote)
                        remote_port = resolver.resolve_port(
                            remote.device_id, captures.get("remote_interface")
                        )
                        description = captures.get("remote_port_description", "")
                        member = captures.get("remote_interface", "")
                        if remote_port and member and description.endswith("/" + member):
                            aggregate = resolver.resolve_port(
                                remote.device_id, description[: -len(member) - 1]
                            )
                            if aggregate and str(
                                aggregate.attributes.get("if_type") or ""
                            ).lower() in {"ieee8023adlag", "bridge"}:
                                _add_resolved_device(devices, remote, remote_port)
                                remote_member_interface = _resolved_port_source(remote_port)
                                remote_port = aggregate
                        _add_resolved_device(devices, remote, remote_port)
                        if captures.get("remote_interface") and remote_port is None:
                            _failure(
                                failures,
                                warnings,
                                relative,
                                rule.name,
                                block_number,
                                f"remote interface {remote_hostname}:"
                                f"{captures['remote_interface']} did not resolve uniquely; "
                                "retaining it as an unbound interface",
                            )
                            remote_interface_id = _add_named_interface(
                                devices[remote_source_id],
                                captures["remote_interface"],
                            )
                        else:
                            remote_interface_id = (
                                _resolved_port_source(remote_port) if remote_port else None
                            )
                    else:
                        remote_source_id = f"external:{normalize_identity(remote_hostname)}"
                        remote_device = devices.setdefault(
                            remote_source_id,
                            Device(
                                source_id=remote_source_id,
                                hostname=remote_hostname,
                                fqdn=remote_hostname if "." in remote_hostname else None,
                                management_ip=captures.get("remote_management_ip"),
                                attributes={
                                    "external": True,
                                    "site": variables.get("site"),
                                    "platform": captures.get("remote_platform"),
                                    "description": captures.get("remote_description"),
                                },
                            ),
                        )
                        remote_interface_id = (
                            _add_named_interface(remote_device, captures["remote_interface"])
                            if captures.get("remote_interface")
                            else None
                        )

                    source_id = stable_hash(
                        {
                            "path": relative,
                            "rule": rule.name,
                            "block": block_number,
                            "local": local_source_id,
                            "local_interface": local_interface_name,
                            "remote": remote_source_id,
                            "remote_interface": captures.get("remote_interface"),
                        }
                    )[:24]
                    rule_links.append(
                        Link(
                            source_id=source_id,
                            a=LinkEndpoint(
                                device=local_source_id,
                                interface=local_interface_id,
                            ),
                            b=LinkEndpoint(
                                device=remote_source_id,
                                interface=remote_interface_id,
                            ),
                            protocol=self.settings.protocol,
                            attributes={
                                "artifact_path": relative,
                                "rule": rule.name,
                                "block": block_number,
                                "site": variables.get("site"),
                                "local_endpoint": "a",
                                "local_hostname": local.hostname,
                                "local_management_ip": local.management_ip,
                                "local_interface": local_interface_name,
                                "remote_hostname": remote_hostname,
                                "remote_interface": captures.get("remote_interface"),
                                **(
                                    {"remote_member_interface": remote_member_interface}
                                    if remote_member_interface
                                    else {}
                                ),
                                "remote_management_ip": captures.get("remote_management_ip"),
                                "remote_chassis_id": captures.get("remote_chassis_id"),
                                "remote_platform": captures.get("remote_platform"),
                                "remote_description": captures.get("remote_description"),
                                "relationship_kind": "direct",
                                "evidence": [f"{self.settings.protocol}-artifact"],
                            },
                        )
                    )
                if rule_links:
                    links.extend(rule_links)
                    selected_rule = rule.name
                    matched_rules[relative] = rule.name
                    break

            if selected_rule is None:
                reason = "no parsing rule produced a valid neighbor"
                skipped_files.append({"path": relative, "reason": reason})
                warnings.append(f"{relative}: {reason}")

        topology = Topology(
            provider=self.name,
            devices=list(devices.values()),
            links=links,
            warnings=warnings,
        )
        return ProviderInspection(
            provider=self.name,
            provider_type="regex_artifacts",
            matched_files=matched_files,
            matched_rules=matched_rules,
            skipped_files=skipped_files,
            resolution_failures=failures,
            warnings=warnings,
            blocked=self.settings.zero_output_guard and not links,
            topology=topology,
        )


def _regex_flags(names: list[str]) -> int:
    result = 0
    allowed = {
        "ASCII": re.ASCII,
        "DOTALL": re.DOTALL,
        "IGNORECASE": re.IGNORECASE,
        "MULTILINE": re.MULTILINE,
        "VERBOSE": re.VERBOSE,
    }
    for name in names:
        normalized = name.upper()
        if normalized not in allowed:
            raise ValueError(f"unsupported regex flag: {name}")
        result |= allowed[normalized]
    return result


def _decode_ip(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return str(ipaddress.ip_address(bytes(value)))
        except ValueError:
            return None
    return str(value)


def _clean_captures(values: dict[str, str | None]) -> dict[str, str]:
    return {
        key: value.strip() for key, value in values.items() if value is not None and value.strip()
    }


def _first(values: dict[str, str], *keys: str) -> str | None:
    return next((values[key] for key in keys if values.get(key)), None)


def _resolved_device_source(device: ResolvedDevice) -> str:
    return f"device:{device.device_id}"


def _resolved_port_source(port: ResolvedPort) -> str:
    return f"port:{port.port_id}"


def _add_resolved_device(
    devices: dict[str, Device],
    resolved: ResolvedDevice,
    port: ResolvedPort | None,
) -> None:
    source_id = _resolved_device_source(resolved)
    device = devices.setdefault(
        source_id,
        Device(
            source_id=source_id,
            hostname=resolved.hostname,
            fqdn=resolved.hostname if "." in resolved.hostname else None,
            management_ip=resolved.management_ip,
            librenms_device_id=resolved.device_id,
            attributes={
                "sys_name": resolved.sys_name,
                "location": resolved.location,
                "groups": list(resolved.groups),
            },
        ),
    )
    if port and all(item.librenms_port_id != port.port_id for item in device.interfaces):
        device.interfaces.append(
            Interface(
                source_id=_resolved_port_source(port),
                device_source_id=source_id,
                name=port.name,
                if_index=port.if_index,
                mac=port.mac,
                librenms_port_id=port.port_id,
                attributes=port.attributes,
            )
        )


def _add_named_interface(device: Device, name: str) -> str:
    source_id = f"name:{normalize_identity(name)}"
    if all(interface.source_id != source_id for interface in device.interfaces):
        device.interfaces.append(
            Interface(
                source_id=source_id,
                device_source_id=device.source_id,
                name=name,
            )
        )
    return source_id


def _failure(
    failures: list[dict[str, str]],
    warnings: list[str],
    path: str,
    rule: str,
    block: int,
    reason: str,
) -> None:
    location = f"{path} rule {rule} block {block}"
    failures.append({"path": path, "rule": rule, "block": str(block), "reason": reason})
    warnings.append(f"{location}: {reason}")


def _load_host_inventory(value: Any | None) -> Any:
    if not isinstance(value, str):
        return value
    path = Path(value)
    raw = path.read_text()
    if path.suffix.lower() == ".json":
        return json.loads(raw)
    return yaml.safe_load(raw)


def _host_variables(relative: str, inventory: Any | None) -> dict[str, str]:
    if not inventory:
        return {}
    entries = inventory.get("hosts", inventory) if isinstance(inventory, dict) else inventory
    if isinstance(entries, dict):
        value = entries.get(relative, {})
        return {str(key): str(item) for key, item in value.items()} if value else {}
    if not isinstance(entries, list):
        raise ValueError("host_inventory must be a mapping or list")
    variables: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("path") or entry.get("glob") or "")
        if pattern and not fnmatch.fnmatch(relative, pattern):
            continue
        raw_variables = entry.get("variables")
        if not isinstance(raw_variables, dict):
            raw_variables = {
                key: value
                for key, value in entry.items()
                if key not in {"path", "glob", "variables"}
            }
        variables.update(
            {str(key): str(value) for key, value in raw_variables.items() if value is not None}
        )
    return variables
