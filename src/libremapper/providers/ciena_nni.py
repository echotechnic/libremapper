from __future__ import annotations

import ipaddress
import re
import shlex
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine, inspect, text

from libremapper.federation import normalize_identity
from libremapper.models import Device, Link, LinkEndpoint, ProviderInspection, Topology, stable_hash
from libremapper.parser_definitions import BUILTIN_PARSERS, NniParserDefinition
from libremapper.providers.regex_artifacts import (
    LibreNMSIdentityResolver,
    _add_named_interface,
    _add_resolved_device,
    _decode_ip,
    _resolved_device_source,
    _resolved_port_source,
)

PARSER_NAME = "saos10-nni-config"
DEFAULT_DEFINITION = cast(NniParserDefinition, BUILTIN_PARSERS[PARSER_NAME])


def _underlay_ports(content: str) -> dict[str, str]:
    bindings: dict[str, set[str]] = defaultdict(set)
    ports: dict[str, set[str]] = defaultdict(set)
    for line in content.splitlines():
        if line.startswith("oc-if:interfaces interface ") and " underlay-binding " in line:
            tokens = shlex.split(line)
            if "fd" in tokens and tokens.index("fd") + 1 < len(tokens):
                bindings[tokens[2]].add(tokens[tokens.index("fd") + 1])
        elif line.startswith("fps fp ") and " fd-name " in line and " logical-port " in line:
            tokens = shlex.split(line)
            if tokens.index("fd-name") + 1 < len(tokens) and tokens.index("logical-port") + 1 < len(
                tokens
            ):
                ports[tokens[tokens.index("fd-name") + 1]].add(
                    tokens[tokens.index("logical-port") + 1]
                )
    return {
        name: next(iter(ports[next(iter(domains))]))
        for name, domains in bindings.items()
        if len(domains) == 1 and len(ports[next(iter(domains))]) == 1
    }


def _interfaces(
    content: str,
    definition: NniParserDefinition | None = None,
) -> list[tuple[str, str, ipaddress.IPv4Interface | ipaddress.IPv6Interface]]:
    definition = definition or DEFAULT_DEFINITION
    definitions: dict[str, dict[str, str]] = defaultdict(dict)
    addresses: list[tuple[str, str]] = []
    for line in content.splitlines():
        if not line.startswith("oc-if:interfaces interface "):
            continue
        tokens = shlex.split(line)
        if len(tokens) < 5:
            raise ValueError("incomplete Ciena interface statement")
        name, tail = tokens[2], tokens[3:]
        if tail[0] == "config":
            values = dict(zip(tail[1::2], tail[2::2], strict=False))
            definitions[name].update(
                (key, values[key]) for key in ("type", "vrfName") if key in values
            )
        elif tail[0] in {"ipv4", "ipv6"} and "prefix-length" in tail and "ip" in tail:
            addresses.append(
                (name, tail[tail.index("ip") + 1] + "/" + tail[tail.index("prefix-length") + 1])
            )
    result = []
    for name, address in addresses:
        if definitions[name].get("type") not in definition.interface_types:
            continue
        interface = ipaddress.ip_interface(address)
        prefixes = (
            definition.ipv4_prefix_lengths
            if interface.version == 4
            else definition.ipv6_prefix_lengths
        )
        if interface.network.prefixlen in prefixes:
            result.append((name, definitions[name].get("vrfName", ""), interface))
    return result


def inspect_ciena_nni(
    provider: str,
    engine: Engine,
    root: Path,
    resolver: LibreNMSIdentityResolver,
    definition: NniParserDefinition | None = None,
    parser_id: str = PARSER_NAME,
    *,
    captures: list[dict[str, Any]] | None = None,
) -> ProviderInspection:
    definition = definition or DEFAULT_DEFINITION
    peer_name = re.compile(definition.peer_hostname_regex, re.IGNORECASE)
    filename_regex = re.compile(definition.path_regex)
    addresses: dict[tuple[str, str], set[tuple[int, int, str]]] = defaultdict(set)
    car_ports = {
        port_id: device_id
        for device_id, ports in resolver.ports.items()
        if any(
            peer_name.match(value or "")
            for value in (
                resolver.devices[device_id].hostname,
                resolver.devices[device_id].sys_name,
            )
        )
        for port_id in ports
    }
    schema = inspect(engine)
    if not any(schema.has_table(f"ipv{version}_addresses") for version in (4, 6)):
        raise RuntimeError("NNI parsing requires LibreNMS interface address inventory")
    with engine.connect() as connection:
        for version in (4, 6):
            table = f"ipv{version}_addresses"
            if not schema.has_table(table):
                continue
            compressed = "ipv6_compressed" if version == 6 else "NULL"
            for row in connection.execute(
                text(
                    f"SELECT port_id, ipv{version}_address AS address, "
                    f"{compressed} AS compressed, ipv{version}_prefixlen AS prefixlen, "
                    f"context_name FROM {table}"
                )
            ).mappings():
                port_id = int(row["port_id"])
                if port_id not in car_ports:
                    continue
                address = _decode_ip(row["compressed"]) or _decode_ip(row["address"])
                if not address:
                    continue
                try:
                    interface = ipaddress.ip_interface(f"{address}/{int(row['prefixlen'])}")
                except (ValueError, TypeError):
                    continue
                addresses[(str(row["context_name"] or ""), str(interface.network))].add(
                    (car_ports[port_id], port_id, str(interface.ip))
                )

    devices: dict[str, Device] = {}
    links: dict[str, Link] = {}
    matched_files: list[str] = []
    matched_rules: dict[str, str] = {}
    skipped_files: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    warnings: list[str] = []
    paths = sorted({path for pattern in definition.file_globs for path in root.glob(pattern)})
    for path in paths:
        if (
            not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
            or any(
                parent.is_symlink()
                for parent in (path, *path.parents)
                if parent != root and root in parent.parents
            )
        ):
            continue
        relative = path.relative_to(root).as_posix()
        filename_match = filename_regex.match(relative)
        if filename_match is None:
            continue
        content = path.read_text(errors="replace")
        if "oc-if:interfaces interface " not in content:
            continue
        matched_files.append(relative)
        local_hostname = (filename_match.group("device_name") or "").strip()
        local = resolver.resolve_device(local_hostname)
        if local is None:
            reason = "configuration device did not resolve uniquely in LibreNMS"
            skipped_files.append({"path": relative, "reason": reason})
            if captures is None:
                continue
        try:
            interfaces = _interfaces(content, definition)
            underlay_ports = _underlay_ports(content)
        except (ValueError, IndexError) as error:
            raise RuntimeError(f"{relative}: invalid Ciena interface configuration") from error
        for interface_name, context, interface in interfaces:
            extracted: dict[str, Any] = {
                "local_hostname": local_hostname,
                "local_interface": interface_name,
                "context": context,
                "address": str(interface),
                "network": str(interface.network),
            }
            if captures is not None and len(captures) < 100:
                captures.append(
                    {
                        "path": relative,
                        "rule": parser_id,
                        "block": len(captures) + 1,
                        "captures": extracted,
                    }
                )
            if local is None:
                extracted["reason"] = reason
                continue
            extracted["local_device_id"] = local.device_id
            candidates = {
                (device_id, port_id)
                for device_id, port_id, address in addresses.get(
                    (context, str(interface.network)), set()
                )
                if address != str(interface.ip) and device_id != local.device_id
            }
            if not candidates:
                extracted["reason"] = "no peer interface in the same network and context"
                continue
            if len(candidates) != 1:
                peer_label = "CAR peer" if parser_id == PARSER_NAME else "peer"
                reason = f"interface {interface_name}: {peer_label} address is ambiguous"
                extracted["reason"] = reason
                failures.append({"path": relative, "rule": parser_id, "reason": reason})
                warnings.append(f"{relative}: {reason}")
                continue
            remote_id, remote_port_id = next(iter(candidates))
            remote = resolver.devices[remote_id]
            remote_port = resolver.ports[remote_id][remote_port_id]
            extracted.update(
                {
                    "remote_hostname": remote.hostname,
                    "remote_interface": remote_port.name,
                    "remote_device_id": remote_id,
                    "remote_port_id": remote_port_id,
                }
            )
            local_port = resolver.resolve_port(local.device_id, interface_name)
            extracted["local_port_id"] = local_port.port_id if local_port else None
            extracted["local_interface_unbound"] = local_port is None
            if local_port is None and resolver.port_tokens[local.device_id].get(
                normalize_identity(interface_name)
            ):
                reason = f"interface {interface_name}: inventory interface is ambiguous"
                extracted["reason"] = reason
                failures.append({"path": relative, "rule": parser_id, "reason": reason})
                warnings.append(f"{relative}: {reason}")
                continue
            local_source = _resolved_device_source(local)
            remote_source = _resolved_device_source(remote)
            _add_resolved_device(devices, local, local_port)
            _add_resolved_device(devices, remote, remote_port)
            underlay = resolver.resolve_port(local.device_id, underlay_ports.get(interface_name))
            aggregate = (
                resolver.resolve_port(remote.device_id, remote_port.name.rsplit(".", 1)[0])
                if "." in remote_port.name
                else None
            )
            if (
                not underlay
                or not aggregate
                or str(aggregate.attributes.get("if_type") or "").lower()
                not in {"ieee8023adlag", "bridge"}
            ):
                underlay = aggregate = None
            if underlay and aggregate:
                _add_resolved_device(devices, local, underlay)
                _add_resolved_device(devices, remote, aggregate)
            local_interface = (
                _resolved_port_source(local_port)
                if local_port
                else _add_named_interface(devices[local_source], interface_name)
            )
            source_id = stable_hash(
                {
                    "parser": parser_id,
                    "local": local_source,
                    "interface": interface_name,
                    "remote": remote_source,
                    "remote_port": remote_port_id,
                }
            )[:24]
            links.setdefault(
                source_id,
                Link(
                    source_id=source_id,
                    a=LinkEndpoint(device=local_source, interface=local_interface),
                    b=LinkEndpoint(
                        device=remote_source, interface=_resolved_port_source(remote_port)
                    ),
                    protocol="ip-p2p",
                    confidence=0.9,
                    relationship_kind="direct",
                    attributes={
                        "artifact_path": relative,
                        "rule": parser_id,
                        "local_hostname": local.hostname,
                        "local_interface": interface_name,
                        "local_interface_unbound": local_port is None,
                        **(
                            {
                                "local_underlay_interface": _resolved_port_source(underlay),
                                "remote_aggregate_interface": _resolved_port_source(aggregate),
                            }
                            if underlay and aggregate
                            else {}
                        ),
                        "remote_hostname": remote.hostname,
                        "remote_interface": remote_port.name,
                        "network": str(interface.network),
                        "context": context,
                        "relationship_kind": "direct",
                        "evidence": ["ciena-config", "ip-p2p"],
                    },
                ),
            )
            matched_rules[relative] = parser_id
    return ProviderInspection(
        provider=provider,
        provider_type="oxidized",
        matched_files=matched_files,
        matched_rules=matched_rules,
        skipped_files=skipped_files,
        resolution_failures=failures,
        warnings=warnings,
        blocked=definition.zero_output_guard and not links,
        topology=Topology(
            provider=provider,
            devices=list(devices.values()),
            links=list(links.values()),
            warnings=warnings,
        ),
    )
