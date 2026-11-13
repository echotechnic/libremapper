from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from libremapper.config import AppConfig, MapConfig, SelectorConfig
from libremapper.federation import FederationEngine
from libremapper.inventory import build_inventory
from libremapper.layouts import LayoutResult, generate_layout
from libremapper.librenms import LibreNMSRepository, MapSnapshot
from libremapper.link_sync import NativeLinkSynchronizer
from libremapper.models import (
    Device,
    FederatedDevice,
    FederatedLink,
    FederatedTopology,
    LinkFallback,
    LinkSyncPlan,
    MapEdgeSpec,
    MapNodeSpec,
    Operation,
    Plan,
    Position,
    ProviderInspection,
    SourceRef,
    Topology,
    stable_hash,
)
from libremapper.providers import (
    FileProvider,
    LibreNMSProvider,
    OxidizedGitProvider,
    RegexArtifactsProvider,
    TopologyProvider,
)
from libremapper.providers.file import select_topology, selector_matches
from libremapper.state import StateStore

NODE_AUTHORITATIVE_FIELDS = {"device_id"}
NODE_LAYOUT_FIELDS = {"x_pos", "y_pos"}
EDGE_AUTHORITATIVE_FIELDS = {
    "custom_map_node1_id",
    "custom_map_node2_id",
    "port_id",
}
EDGE_LAYOUT_FIELDS = {"mid_x", "mid_y"}


@dataclass
class DesiredMap:
    map_values: dict[str, Any]
    nodes: dict[str, MapNodeSpec]
    edges: dict[str, MapEdgeSpec]
    node_display_names: dict[str, str]
    node_layers: dict[str, int]
    node_sources: dict[str, list[SourceRef]]
    edge_display_names: dict[str, str]
    edge_sources: dict[str, list[SourceRef]]
    edge_protocols: dict[str, str | None]
    edge_interfaces: dict[str, tuple[str | None, str | None]]
    edge_graphs: dict[str, dict[str, Any]]
    link_fallbacks: list[LinkFallback]
    link_choices: list[dict[str, Any]]
    layout_summary: dict[str, Any]
    source_hash: str
    warnings: list[str]
    conflicts: list[str]


class LibremapperService:
    def __init__(
        self,
        config: AppConfig,
        engine: Engine,
        state: StateStore,
    ) -> None:
        self.config = config
        self.engine = engine
        self.repository = LibreNMSRepository(engine)
        self.state = state

    def validate(self, map_key: str | None = None, *, structural: bool = False) -> list[str]:
        errors = self.repository.validate_schema()
        for name, provider_config in self.config.providers.items():
            if provider_config.type == "file":
                path = Path(str(provider_config.settings.get("path", "")))
                if not path.is_file():
                    errors.append(f"provider {name}: topology file does not exist: {path}")
            elif provider_config.type == "oxidized":
                from libremapper.providers.oxidized import OxidizedGitSettings
                try:
                    OxidizedGitSettings.model_validate(provider_config.settings)
                except ValueError:
                    errors.append(f"provider {name}: invalid Oxidized connection settings")
        if not errors and not structural:
            selected = [self.config.map_by_key(map_key)] if map_key else self.config.maps
            for map_config in selected:
                try:
                    self.build_desired(map_config)
                except Exception as error:
                    errors.append(f"map {map_config.key}: {error}")
        return errors

    def inspect_provider(self, name: str, map_key: str | None = None) -> ProviderInspection:
        provider = self._provider(name)
        if not isinstance(provider, (OxidizedGitProvider, RegexArtifactsProvider)):
            raise RuntimeError(f"provider {name!r} does not support diagnostic inspection")
        if map_key is not None:
            sources = [
                source for source in self.config.map_by_key(map_key).sources
                if source.provider == name
            ]
            result = ProviderInspection(
                provider=name, provider_type=self.config.providers[name].type,
                topology=Topology(provider=name),
            )
            devices = {}
            links = {}
            for index, source in enumerate(sources):
                if isinstance(provider, OxidizedGitProvider):
                    report = provider.inspect(source)
                else:
                    report = provider.inspect()
                    report.topology = select_topology(report.topology, source)
                result.blocked |= report.blocked
                result.revision = report.revision or result.revision
                for field in ("matched_files", "skipped_files", "resolution_failures", "warnings"):
                    rows = getattr(result, field)
                    for row in getattr(report, field):
                        if row not in rows:
                            rows.append(row)
                result.matched_rules.update(report.matched_rules)
                result.parser_reports.extend(
                    {**row, **({"source_index": index + 1} if len(sources) > 1 else {})}
                    for row in report.parser_reports
                )
                for device in report.topology.devices:
                    if device.source_id not in devices:
                        devices[device.source_id] = device
                    else:
                        current = devices[device.source_id]
                        known = {interface.source_id for interface in current.interfaces}
                        current.interfaces.extend(
                            interface for interface in device.interfaces
                            if interface.source_id not in known
                        )
                links.update({link.source_id: link for link in report.topology.links})
            result.topology.devices = list(devices.values())
            result.topology.links = list(links.values())
            result.topology.warnings = list(result.warnings)
            return result
        return provider.inspect()

    def create_link_plan(self, name: str) -> LinkSyncPlan:
        if not self.config.experimental.native_link_sync:
            raise RuntimeError(
                "experimental native link synchronization is disabled in configuration"
            )
        provider = self._provider(name)
        if not isinstance(provider, (OxidizedGitProvider, RegexArtifactsProvider)):
            raise RuntimeError("native link synchronization requires regex_artifacts")
        inspection = provider.inspect()
        if inspection.blocked:
            raise RuntimeError(f"provider {name}: zero_output_guard blocked link planning")
        return NativeLinkSynchronizer(
            self.engine,
            self.state,
            name,
            self.config.experimental.link_adapter_command,
        ).plan(inspection.topology)

    def sync_links(self, plan: LinkSyncPlan) -> None:
        if not self.config.experimental.native_link_sync:
            raise RuntimeError(
                "experimental native link synchronization is disabled in configuration"
            )
        NativeLinkSynchronizer(
            self.engine,
            self.state,
            plan.provider,
            self.config.experimental.link_adapter_command,
        ).sync(plan)

    def build_desired(self, map_config: MapConfig) -> DesiredMap:
        topologies = []
        for source in map_config.sources:
            provider = self._provider(source.provider)
            topologies.append(provider.load(source))
        federation = FederationEngine(map_config.identity)
        fallback_providers = {
            name for name, provider in self.config.providers.items() if provider.type == "file"
        }
        if self.config.manual_provider:
            manual_provider = self._provider(self.config.manual_provider)
            assert isinstance(manual_provider, FileProvider)
            manual = manual_provider.read()
            manual_ids = {device.source_id for device in manual.devices}
            for source_id in map_config.manual_device_inclusions:
                match = re.fullmatch(r"librenms:([1-9][0-9]*)", source_id)
                if match and source_id not in manual_ids:
                    manual.devices.append(Device(
                        source_id=source_id, librenms_device_id=int(match.group(1)),
                        attributes={"_libremapper_inventory_reference": True},
                    ))
            references = [
                device for device in manual.devices
                if device.attributes.pop("_libremapper_inventory_reference", False)
            ]
            if references:
                inventory = {
                    item["device_id"]: item for item in build_inventory(self.engine)["devices"]
                }
                for reference_device in references:
                    record = inventory.get(reference_device.librenms_device_id)
                    if not record:
                        raise ValueError(
                            f"LibreNMS device {reference_device.librenms_device_id} is missing"
                        )
                    reference_device.hostname = record["hostname"]
                    reference_device.attributes.update({
                        key: record[key]
                        for key in ("sys_name", "location", "groups", "os", "hardware", "type")
                    })
            topology = federation.merge_with_manual(
                topologies,
                Topology(
                    provider=self.config.manual_provider,
                    devices=manual.devices,
                    links=[
                        link
                        for link in manual.links
                        if link.source_id not in map_config.manual_link_exclusions
                    ],
                ),
                manual.suppress_links,
                {
                    name
                    for name, provider in self.config.providers.items()
                    if provider.type in {"librenms", "oxidized", "regex_artifacts"}
                },
                fallback_providers=fallback_providers,
                manual_device_inclusions=set(map_config.manual_device_inclusions),
            )
        else:
            topology = federation.merge(topologies, fallback_providers=fallback_providers)
        excluded = set(map_config.device_exclusions)
        topology.devices = {
            key: device for key, device in topology.devices.items()
            if device.librenms_device_id not in excluded
            and not any(
                source.provider == self.config.manual_provider
                and source.source_id in map_config.manual_device_exclusions
                for source in device.sources
            )
        }
        topology.links = {
            key: link for key, link in topology.links.items()
            if link.a_device in topology.devices and link.b_device in topology.devices
        }
        display_label_mode = "sys_name" if map_config.node_label == "sys_name" else "short_hostname"
        link_choices: list[dict[str, Any]] = []
        for link in topology.links.values():
            discovered = any(
                self.config.providers[source.provider].type
                in {"librenms", "oxidized", "regex_artifacts"}
                for source in link.sources
            )
            one_sided = discovered and bool(link.a_interface) != bool(link.b_interface)
            included = link.canonical_id not in map_config.link_exclusions and (
                not one_sided or link.canonical_id in map_config.link_inclusions
            )
            link_choices.append({
                "canonical_id": link.canonical_id,
                "display_name": _edge_display_name(topology, link, map_config, display_label_mode),
                "a_interface": (
                    topology.devices[link.a_device].interfaces[link.a_interface].name
                    if link.a_interface else None
                ),
                "b_interface": (
                    topology.devices[link.b_device].interfaces[link.b_interface].name
                    if link.b_interface else None
                ),
                "sources": [source.model_dump() for source in link.sources],
                "one_sided": one_sided,
                "included": included,
            })
        included_links = {choice["canonical_id"] for choice in link_choices if choice["included"]}
        topology.links = {
            key: link for key, link in topology.links.items() if key in included_links
        }
        topology.link_fallbacks = [
            fallback for fallback in topology.link_fallbacks
            if fallback.a_device in topology.devices and fallback.b_device in topology.devices
            and any(key in included_links for key in fallback.canonical_ids)
        ]
        edge_graphs = self._bind_graphs(
            map_config, topology, {choice["canonical_id"] for choice in link_choices}
        )
        if not (map_config.layout.infer_layers or map_config.layout.type == "auto"):
            for device in topology.devices.values():
                if "_libremapper_manual_layer" in device.attributes:
                    device.layer = int(device.attributes["_libremapper_manual_layer"])
            _apply_layer_overrides(
                topology,
                [
                    selector
                    for source in map_config.sources
                    if self.config.providers[source.provider].type == "librenms"
                    for selector in source.selectors
                ],
                explicit_layers_only=True,
            )
            _apply_layer_overrides(topology, map_config.layer_overrides)
        layout = generate_layout(topology, map_config.layout)
        nodes = self._desired_nodes(map_config, topology, layout.positions)
        edges = self._desired_edges(map_config, topology, layout.positions)
        for fallback in topology.link_fallbacks:
            first = _device_label(
                topology.devices[fallback.a_device], map_config, display_label_mode
            )
            second = _device_label(
                topology.devices[fallback.b_device], map_config, display_label_mode
            )
            fallback.display_name = f"{first} ↔ {second}"
        source_hash = stable_hash(
            {
                "topology": topology.model_dump(mode="json"),
                "nodes": {key: value.model_dump(mode="json") for key, value in nodes.items()},
                "edges": {key: value.model_dump(mode="json") for key, value in edges.items()},
                "link_choices": link_choices,
            }
        )
        return DesiredMap(
            map_values=self._map_values(map_config, layout),
            nodes=nodes,
            edges=edges,
            node_display_names={
                canonical_id: _device_label(device, map_config, display_label_mode)
                for canonical_id, device in topology.devices.items()
            },
            node_layers={
                canonical_id: device.layer for canonical_id, device in topology.devices.items()
            },
            node_sources={
                canonical_id: device.sources for canonical_id, device in topology.devices.items()
            },
            edge_display_names={
                canonical_id: _edge_display_name(topology, link, map_config, display_label_mode)
                for canonical_id, link in topology.links.items()
            },
            edge_sources={
                canonical_id: link.sources for canonical_id, link in topology.links.items()
            },
            edge_protocols={
                canonical_id: link.protocol for canonical_id, link in topology.links.items()
            },
            edge_interfaces={
                canonical_id: (
                    topology.devices[link.a_device].interfaces[link.a_interface].name
                    if link.a_interface
                    else None,
                    topology.devices[link.b_device].interfaces[link.b_interface].name
                    if link.b_interface
                    else None,
                )
                for canonical_id, link in topology.links.items()
            },
            edge_graphs=edge_graphs,
            link_fallbacks=topology.link_fallbacks,
            link_choices=link_choices,
            layout_summary={
                "requested": layout.requested_layout,
                "resolved": layout.resolved_layout,
                "width": layout.width,
                "height": layout.height,
                "hierarchy_inferred": layout.hierarchy_inferred,
            },
            source_hash=source_hash,
            warnings=topology.warnings,
            conflicts=topology.conflicts,
        )

    def create_plan(self, map_key: str, *, relayout: bool = False, adopt: bool = False) -> Plan:
        map_config = self.config.map_by_key(map_key)
        desired = self.build_desired(map_config)
        managed_map = self.state.map_record(map_key)
        managed_id = int(managed_map["librenms_map_id"]) if managed_map else None
        snapshot = self.repository.snapshot(map_config.name, managed_id)

        if managed_map and snapshot.map is None:
            raise RuntimeError(
                f"managed map {map_key!r} points to missing LibreNMS map {managed_id}"
            )
        if not managed_map and snapshot.map is not None and not adopt:
            raise RuntimeError(
                f"LibreNMS map {map_config.name!r} already exists; use --adopt to manage it"
            )

        plan = Plan(
            map_key=map_key,
            map_name=map_config.name,
            config_path=str(self.config.config_path or ""),
            config_hash=self.config.plan_fingerprint(map_key),
            source_hash=desired.source_hash,
            database_hash=snapshot.fingerprint(),
            relayout=relayout,
            adopt=adopt,
            warnings=list(desired.warnings),
            conflicts=list(desired.conflicts),
            link_fallbacks=desired.link_fallbacks,
            link_choices=desired.link_choices,
            layout_summary=desired.layout_summary,
        )

        if snapshot.map is None:
            plan.operations.append(
                Operation(
                    action="create",
                    kind="map",
                    canonical_id=map_key,
                    display_name=map_config.name,
                    values=desired.map_values,
                    generated_values=desired.map_values,
                )
            )
        else:
            map_id = int(snapshot.map["custom_map_id"])
            changes = _map_changed_values(snapshot.map, desired.map_values)
            plan.operations.append(
                Operation(
                    action="adopt" if not managed_map else ("update" if changes else "noop"),
                    kind="map",
                    canonical_id=map_key,
                    display_name=map_config.name,
                    librenms_id=map_id,
                    values=changes,
                    generated_values=desired.map_values,
                )
            )

        managed_objects = self.state.objects(map_key)
        adopted_nodes, adopted_edges = self._adoption_matches(
            snapshot, desired, enabled=adopt and not managed_map
        )
        node_bindings: dict[str, int] = {}
        desired_device_counts = Counter(
            node.device_id for node in desired.nodes.values() if node.device_id is not None
        )
        node_rebindings: dict[str, dict[str, Any]] = {}
        for canonical_id, node_spec in desired.nodes.items():
            device_id = node_spec.device_id
            if (device_id is None or desired_device_counts[device_id] != 1
                    or f"node:{canonical_id}" in managed_objects
                    or not any(source.provider == self.config.manual_provider
                               and source.source_id == f"librenms:{device_id}"
                               for source in desired.node_sources[canonical_id])):
                continue
            candidates = [
                record for record in managed_objects.values()
                if record["kind"] == "node" and record["canonical_id"] not in desired.nodes
                and record["generated"].get("device_id") == device_id
                and snapshot.nodes.get(int(record["librenms_id"]), {}).get("device_id") == device_id
            ]
            if len(candidates) == 1:
                node_rebindings[canonical_id] = candidates[0]
        rebound_nodes = {record["canonical_id"] for record in node_rebindings.values()}

        for canonical_id, node_spec in desired.nodes.items():
            record = managed_objects.get(f"node:{canonical_id}")
            previous_canonical_id = None
            if record is None and canonical_id in node_rebindings:
                record = node_rebindings[canonical_id]
                previous_canonical_id = str(record["canonical_id"])
            existing_id = int(record["librenms_id"]) if record else adopted_nodes.get(canonical_id)
            current = snapshot.nodes.get(existing_id) if existing_id else None
            if existing_id and current is None:
                plan.warnings.append(
                    f"managed node {canonical_id} points to missing row {existing_id}; recreating"
                )
                existing_id = None
            if existing_id:
                node_bindings[canonical_id] = existing_id
                previous = record["generated"] if record else _node_generated(current or {})
                desired_values = node_spec.db_values()
                if record is None:
                    effective, preserved = _adopted_effective_values(
                        current or {},
                        desired_values,
                        authoritative=NODE_AUTHORITATIVE_FIELDS,
                        forced=NODE_LAYOUT_FIELDS if relayout else set(),
                    )
                else:
                    effective, preserved = _effective_values(
                        current or {},
                        previous,
                        desired_values,
                        authoritative=NODE_AUTHORITATIVE_FIELDS,
                        forced=NODE_LAYOUT_FIELDS if relayout else set(),
                    )
                changes = _changed_values(current or {}, effective)
                plan.operations.append(
                    Operation(
                        action="adopt" if record is None else (
                            "update" if changes or previous_canonical_id else "noop"
                        ),
                        kind="node",
                        canonical_id=canonical_id,
                        display_name=desired.node_display_names[canonical_id],
                        layer=desired.node_layers[canonical_id],
                        librenms_id=existing_id,
                        values=changes,
                        generated_values=desired_values,
                        preserved_fields=preserved,
                        sources=desired.node_sources[canonical_id],
                        previous_canonical_id=previous_canonical_id,
                        reconciliation_reason=(
                            "inventory reference migrated" if previous_canonical_id else None
                        ),
                    )
                )
            else:
                plan.operations.append(
                    Operation(
                        action="create",
                        kind="node",
                        canonical_id=canonical_id,
                        display_name=desired.node_display_names[canonical_id],
                        layer=desired.node_layers[canonical_id],
                        values=node_spec.db_values(),
                        generated_values=node_spec.db_values(),
                        sources=desired.node_sources[canonical_id],
                    )
                )

        active_fallbacks = {
            canonical_id
            for fallback in desired.link_fallbacks
            if fallback.status == "active"
            for canonical_id in fallback.canonical_ids
        }
        desired_pairs: dict[tuple[str, ...], list[str]] = {}
        for canonical_id, edge_spec in desired.edges.items():
            pair = tuple(sorted((edge_spec.from_node, edge_spec.to_node)))
            desired_pairs.setdefault(pair, []).append(canonical_id)
        reverse_nodes = {node_id: canonical_id for canonical_id, node_id in node_bindings.items()}
        stale_pairs: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for record in managed_objects.values():
            if record["kind"] != "edge" or record["canonical_id"] in desired.edges:
                continue
            current = snapshot.edges.get(int(record["librenms_id"]), {})
            endpoints = [
                reverse_nodes.get(current.get(field, 0))
                for field in ("custom_map_node1_id", "custom_map_node2_id")
            ]
            if all(endpoint is not None for endpoint in endpoints):
                pair = tuple(sorted(str(endpoint) for endpoint in endpoints))
                stale_pairs.setdefault(pair, []).append(record)
        rebindings: dict[str, dict[str, Any]] = {}
        for pair, canonical_ids in desired_pairs.items():
            candidates = stale_pairs.get(pair, [])
            if len(canonical_ids) == len(candidates) == 1:
                canonical_id, record = canonical_ids[0], candidates[0]
                old_sources = {
                    (source.get("provider"), source.get("source_id"))
                    for source in record.get("metadata", {}).get("sources", [])
                }
                same_manual_source = any(
                    source.provider == self.config.manual_provider
                    and (source.provider, source.source_id) in old_sources
                    for source in desired.edge_sources[canonical_id]
                ) and any(node in node_rebindings for node in pair)
                if f"edge:{canonical_id}" not in managed_objects and (
                    _is_manual_fallback(record) != (canonical_id in active_fallbacks)
                    or same_manual_source
                ):
                    rebindings[canonical_id] = record
        rebound_ids = {record["canonical_id"] for record in rebindings.values()}

        for canonical_id, edge_spec in desired.edges.items():
            record = managed_objects.get(f"edge:{canonical_id}")
            previous_canonical_id = None
            if record is None and canonical_id in rebindings:
                record = rebindings[canonical_id]
                previous_canonical_id = str(record["canonical_id"])
            metadata = record.get("metadata", {}) if record else {}
            metadata_changed = bool(record) and (
                metadata.get("sources")
                != [source.model_dump() for source in desired.edge_sources[canonical_id]]
                or metadata.get("protocol") != desired.edge_protocols[canonical_id]
                or metadata.get("a_interface") != desired.edge_interfaces[canonical_id][0]
                or metadata.get("b_interface") != desired.edge_interfaces[canonical_id][1]
                or metadata.get("fallback") != (canonical_id in active_fallbacks)
            )
            reason = "ownership provenance updated" if metadata_changed else None
            if record and _is_manual_fallback(record) != (canonical_id in active_fallbacks):
                reason = (
                    "discovery unavailable; manual fallback restored"
                    if canonical_id in active_fallbacks
                    else "manual fallback upgraded to "
                    + _discovery_label(
                        desired.edge_sources[canonical_id],
                        desired.edge_protocols[canonical_id],
                        self.config.manual_provider,
                    )
                )
            existing_id = int(record["librenms_id"]) if record else adopted_edges.get(canonical_id)
            current = snapshot.edges.get(existing_id) if existing_id else None
            if existing_id and current is None:
                plan.warnings.append(
                    f"managed edge {canonical_id} points to missing row {existing_id}; recreating"
                )
                existing_id = None
            endpoint_values = {
                "from_node": edge_spec.from_node,
                "to_node": edge_spec.to_node,
            }
            if existing_id:
                previous = record["generated"] if record else _edge_generated(current or {})
                desired_values = edge_spec.db_values()
                comparison_values = dict(desired_values)
                comparison_values["custom_map_node1_id"] = node_bindings.get(edge_spec.from_node)
                comparison_values["custom_map_node2_id"] = node_bindings.get(edge_spec.to_node)
                generated_values = dict(desired_values)
                generated_values.update(endpoint_values)
                if record is None:
                    effective, preserved = _adopted_effective_values(
                        current or {},
                        comparison_values,
                        authoritative=EDGE_AUTHORITATIVE_FIELDS,
                        forced=EDGE_LAYOUT_FIELDS if relayout else set(),
                    )
                else:
                    effective, preserved = _effective_values(
                        current or {},
                        previous,
                        comparison_values,
                        authoritative=EDGE_AUTHORITATIVE_FIELDS,
                        forced=EDGE_LAYOUT_FIELDS if relayout else set(),
                    )
                changes = _changed_values(current or {}, effective)
                changes.pop("custom_map_node1_id", None)
                changes.pop("custom_map_node2_id", None)
                if current and (
                    current.get("custom_map_node1_id")
                    != comparison_values.get("custom_map_node1_id")
                    or current.get("custom_map_node2_id")
                    != comparison_values.get("custom_map_node2_id")
                ):
                    changes.update(endpoint_values)
                plan.operations.append(
                    Operation(
                        action="adopt"
                        if record is None
                        else (
                            "update"
                            if changes or previous_canonical_id or metadata_changed
                            else "noop"
                        ),
                        kind="edge",
                        canonical_id=canonical_id,
                        display_name=desired.edge_display_names[canonical_id],
                        librenms_id=existing_id,
                        values=changes,
                        generated_values=generated_values,
                        preserved_fields=preserved,
                        sources=desired.edge_sources[canonical_id],
                        protocol=desired.edge_protocols[canonical_id],
                        a_interface=desired.edge_interfaces[canonical_id][0],
                        b_interface=desired.edge_interfaces[canonical_id][1],
                        graph=desired.edge_graphs[canonical_id],
                        reconciliation_reason=reason,
                        previous_canonical_id=previous_canonical_id,
                    )
                )
            else:
                values = edge_spec.db_values()
                values.update(endpoint_values)
                plan.operations.append(
                    Operation(
                        action="create",
                        kind="edge",
                        canonical_id=canonical_id,
                        display_name=desired.edge_display_names[canonical_id],
                        values=values,
                        generated_values=values,
                        sources=desired.edge_sources[canonical_id],
                        protocol=desired.edge_protocols[canonical_id],
                        a_interface=desired.edge_interfaces[canonical_id][0],
                        b_interface=desired.edge_interfaces[canonical_id][1],
                        graph=desired.edge_graphs[canonical_id],
                    )
                )

        desired_nodes = set(desired.nodes) | rebound_nodes
        desired_edges = set(desired.edges) | rebound_ids
        stale_managed_edges = {
            int(record["librenms_id"])
            for record in managed_objects.values()
            if record["kind"] == "edge" and str(record["canonical_id"]) not in desired_edges
        }
        for record in managed_objects.values():
            canonical_id = str(record["canonical_id"])
            kind = str(record["kind"])
            if kind == "node" and canonical_id not in desired_nodes:
                node_id = int(record["librenms_id"])
                attached_edges = {
                    edge_id
                    for edge_id, edge in snapshot.edges.items()
                    if edge.get("custom_map_node1_id") == node_id
                    or edge.get("custom_map_node2_id") == node_id
                }
                unmanaged_attached = attached_edges - stale_managed_edges
                if unmanaged_attached:
                    plan.warnings.append(
                        f"retained stale managed node {canonical_id}; attached "
                        f"unmanaged or retained edge IDs: {sorted(unmanaged_attached)}"
                    )
                    continue
                plan.operations.append(
                    Operation(
                        action="delete",
                        kind="node",
                        canonical_id=canonical_id,
                        display_name=_stale_node_display_name(record, snapshot.nodes.get(node_id)),
                        librenms_id=node_id,
                    )
                )
            if kind == "edge" and canonical_id not in desired_edges:
                metadata = record.get("metadata", {})
                replacements = next(
                    (
                        desired_pairs.get(pair, [])
                        for pair, candidates in stale_pairs.items()
                        if record in candidates
                    ),
                    [],
                )
                reason = "relationship no longer selected or observed"
                if replacements and _is_manual_fallback(record):
                    reason = (
                        "duplicate fallback removed; discovered edge retained"
                        if any(f"edge:{key}" in managed_objects for key in replacements)
                        else "manual fallback replaced by discovered edges; remove/create required"
                    )
                if not metadata:
                    reason += "; legacy ownership record has no provenance"
                plan.operations.append(
                    Operation(
                        action="delete",
                        kind="edge",
                        canonical_id=canonical_id,
                        display_name=_stale_edge_display_name(record, snapshot, managed_objects),
                        librenms_id=int(record["librenms_id"]),
                        sources=[
                            SourceRef.model_validate(source)
                            for source in metadata.get("sources", [])
                        ],
                        protocol=metadata.get("protocol"),
                        a_interface=metadata.get("a_interface"),
                        b_interface=metadata.get("b_interface"),
                        reconciliation_reason=reason,
                    )
                )

        plan.operations.sort(key=_operation_sort_key)
        return plan

    def has_draft(self, map_key: str) -> bool:
        return self._draft_path(map_key).is_file()

    def _draft_path(self, map_key: str) -> Path:
        filename = f"{hashlib.sha256(map_key.encode()).hexdigest()}.pending"
        runtime = Path(os.getenv("LIBREMAPPER_DATA_ROOT", str(self.state.path.parent)))
        return runtime / "drafts" / filename

    def apply(self, plan: Plan) -> None:
        if plan.conflicts:
            raise RuntimeError("Plan has conflicts; resolve them before publishing.")
        self._validate_plan_freshness(plan)
        run_id = self.state.start_run(plan.map_key, stable_hash(plan.model_dump(mode="json")))
        try:
            with self.state.transaction() as state_connection:
                with self.engine.begin() as db_connection:
                    map_id = self._apply_map(db_connection, plan)
                    StateStore.save_map(state_connection, plan.map_key, plan.map_name, map_id)
                    self._delete_edges(db_connection, state_connection, plan)
                    node_ids = self._apply_nodes(db_connection, state_connection, plan, map_id)
                    self._apply_edges(db_connection, state_connection, plan, map_id, node_ids)
            self.state.finish_run(run_id, "applied")
            self._draft_path(plan.map_key).unlink(missing_ok=True)
        except Exception as error:
            self.state.finish_run(run_id, "failed", str(error))
            raise

    def _validate_plan_freshness(self, plan: Plan) -> None:
        if plan.config_hash != self.config.plan_fingerprint(plan.map_key):
            raise RuntimeError("configuration changed after this plan was generated")
        desired = self.build_desired(self.config.map_by_key(plan.map_key))
        if desired.source_hash != plan.source_hash:
            raise RuntimeError("source topology changed after this plan was generated")
        managed = self.state.map_record(plan.map_key)
        map_id = int(managed["librenms_map_id"]) if managed else None
        snapshot = self.repository.snapshot(plan.map_name, map_id)
        if snapshot.fingerprint() != plan.database_hash:
            raise RuntimeError("LibreNMS Custom Map changed after this plan was generated")

    def _apply_map(self, connection: Any, plan: Plan) -> int:
        operation = next(item for item in plan.operations if item.kind == "map")
        if operation.action == "create":
            return self.repository.create_map(connection, operation.values)
        if operation.librenms_id is None:
            raise RuntimeError("map operation is missing a LibreNMS ID")
        if operation.values:
            self.repository.update_map(connection, operation.librenms_id, operation.values)
        return operation.librenms_id

    def _apply_nodes(
        self,
        db_connection: Any,
        state_connection: Any,
        plan: Plan,
        map_id: int,
    ) -> dict[str, int]:
        node_ids: dict[str, int] = {}
        for operation in (item for item in plan.operations if item.kind == "node"):
            if operation.action == "delete":
                if operation.librenms_id is not None:
                    self.repository.delete_row(
                        db_connection,
                        "custom_map_nodes",
                        "custom_map_node_id",
                        operation.librenms_id,
                    )
                StateStore.delete_object(
                    state_connection, plan.map_key, "node", operation.canonical_id
                )
                continue
            if operation.action == "create":
                node_id = self.repository.create_node(db_connection, map_id, operation.values)
            else:
                if operation.librenms_id is None:
                    raise RuntimeError("node operation is missing a LibreNMS ID")
                node_id = operation.librenms_id
                if operation.values:
                    self.repository.update_node(db_connection, node_id, operation.values)
            node_ids[operation.canonical_id] = node_id
            StateStore.save_object(
                state_connection,
                plan.map_key,
                "node",
                operation.canonical_id,
                node_id,
                operation.generated_values,
                plan.source_hash,
            )
            if operation.previous_canonical_id:
                StateStore.delete_object(
                    state_connection, plan.map_key, "node", operation.previous_canonical_id
                )
        return node_ids

    def _apply_edges(
        self,
        db_connection: Any,
        state_connection: Any,
        plan: Plan,
        map_id: int,
        node_ids: dict[str, int],
    ) -> None:
        operations = [item for item in plan.operations if item.kind == "edge"]
        for operation in operations:
            if operation.action == "delete":
                continue
            values = dict(operation.values)
            generated_values = dict(operation.generated_values)
            from_node = values.pop("from_node", None)
            to_node = values.pop("to_node", None)
            generated_from_node = generated_values.pop("from_node", None)
            generated_to_node = generated_values.pop("to_node", None)
            if from_node is not None:
                values["custom_map_node1_id"] = node_ids[from_node]
            if to_node is not None:
                values["custom_map_node2_id"] = node_ids[to_node]
            if generated_from_node is not None:
                generated_values["custom_map_node1_id"] = node_ids[generated_from_node]
            if generated_to_node is not None:
                generated_values["custom_map_node2_id"] = node_ids[generated_to_node]
            if operation.action == "create":
                edge_id = self.repository.create_edge(db_connection, map_id, values)
            else:
                if operation.librenms_id is None:
                    raise RuntimeError("edge operation is missing a LibreNMS ID")
                edge_id = operation.librenms_id
                if values:
                    self.repository.update_edge(db_connection, edge_id, values)
            StateStore.save_object(
                state_connection,
                plan.map_key,
                "edge",
                operation.canonical_id,
                edge_id,
                generated_values,
                plan.source_hash,
                {
                    "sources": [source.model_dump() for source in operation.sources],
                    "protocol": operation.protocol,
                    "a_interface": operation.a_interface,
                    "b_interface": operation.b_interface,
                    "fallback": any(
                        fallback.status == "active"
                        and operation.canonical_id in fallback.canonical_ids
                        for fallback in plan.link_fallbacks
                    ),
                },
            )
            if operation.previous_canonical_id:
                StateStore.delete_object(
                    state_connection, plan.map_key, "edge", operation.previous_canonical_id
                )

    def _delete_edges(self, db_connection: Any, state_connection: Any, plan: Plan) -> None:
        for operation in (
            item for item in plan.operations if item.kind == "edge" and item.action == "delete"
        ):
            if operation.librenms_id is not None:
                self.repository.delete_row(
                    db_connection,
                    "custom_map_edges",
                    "custom_map_edge_id",
                    operation.librenms_id,
                )
            StateStore.delete_object(state_connection, plan.map_key, "edge", operation.canonical_id)

    def _provider(self, name: str) -> TopologyProvider:
        config = self.config.providers[name]
        if config.type == "file":
            return FileProvider(name, config.settings)
        if config.type == "regex_artifacts":
            return RegexArtifactsProvider(name, self.engine, config.settings)
        if config.type == "oxidized":
            return OxidizedGitProvider(
                name, self.engine, config.settings, self.config.oxidized_parsers
            )
        return LibreNMSProvider(name, self.engine, config.settings)

    @staticmethod
    def _map_values(map_config: MapConfig, layout: LayoutResult) -> dict[str, Any]:
        node = map_config.node_defaults
        edge = map_config.edge_defaults
        return {
            "name": map_config.name,
            "menu_group": map_config.menu_group,
            "width": str(layout.width) if map_config.layout.auto_grow else map_config.width,
            "height": str(layout.height) if map_config.layout.auto_grow else map_config.height,
            "node_align": map_config.node_align,
            "reverse_arrows": map_config.reverse_arrows,
            "edge_separation": map_config.edge_separation,
            "options": {
                "interaction": {
                    "dragNodes": False,
                    "dragView": False,
                    "zoomView": False,
                },
                "manipulation": {"enabled": False},
                "physics": {"enabled": False},
                "nodes": {"font": {"strokeWidth": 3, "strokeColor": "#FFFFFF"}},
                "edges": {"font": {"strokeWidth": 3, "strokeColor": "#FFFFFF"}},
            },
            "newnodeconfig": {
                "borderWidth": node.border_width,
                "color": {
                    "border": node.colour_bdr,
                    "background": node.colour_bg,
                },
                "font": {
                    "color": node.text_colour,
                    "size": node.text_size,
                    "face": node.text_face,
                },
                "icon": {},
                "label": True,
                "shape": node.style,
                "size": node.size,
            },
            "newedgeconfig": {
                "arrows": {"to": {"enabled": True}},
                "smooth": {"type": edge.style},
                "font": {
                    "color": edge.text_colour,
                    "size": edge.text_size,
                    "face": edge.text_face,
                    "align": edge.text_align,
                },
                "label": edge.showpct,
            },
        }

    @staticmethod
    def _desired_nodes(
        map_config: MapConfig,
        topology: FederatedTopology,
        positions: dict[str, Position],
    ) -> dict[str, MapNodeSpec]:
        defaults = map_config.node_defaults
        return {
            canonical_id: MapNodeSpec(
                canonical_id=canonical_id,
                device_id=device.librenms_device_id,
                label=_device_label(device, map_config)[:50],
                x_pos=positions[canonical_id].x,
                y_pos=positions[canonical_id].y,
                style=defaults.style,
                icon=defaults.icon,
                image=str(device.attributes.get("map_image") or defaults.image),
                node_image_id=defaults.node_image_id,
                size=defaults.size,
                border_width=defaults.border_width,
                text_face=defaults.text_face,
                text_size=defaults.text_size,
                text_colour=defaults.text_colour,
                colour_bg=defaults.colour_bg,
                colour_bdr=defaults.colour_bdr,
            )
            for canonical_id, device in topology.devices.items()
        }

    def _bind_graphs(
        self, map_config: MapConfig, topology: FederatedTopology, known_links: set[str]
    ) -> dict[str, dict[str, Any]]:
        overrides = {item.canonical_id: item.port_id for item in map_config.link_graph_overrides}
        ports = self.repository.traffic_ports(
            {interface.librenms_port_id for device in topology.devices.values()
             for interface in device.interfaces.values() if interface.librenms_port_id}
            | {port_id for port_id in overrides.values() if port_id}
        )
        for canonical_id in overrides.keys() - known_links:
            topology.conflicts.append(
                f"Traffic interface override targets missing connection {canonical_id}; remove it."
            )
        graphs: dict[str, dict[str, Any]] = {}

        def valid(port_id: int | None, owners: set[int | None]) -> bool:
            port = ports.get(port_id or 0)
            return bool(port and port["device_id"] in owners and not port.get("deleted"))

        for canonical_id, link in topology.links.items():
            devices = [topology.devices[link.a_device], topology.devices[link.b_device]]
            owners = {device.librenms_device_id for device in devices} - {None}

            automatic_ports: list[int] = []
            for device, interface_id in zip(
                devices, (link.a_interface, link.b_interface), strict=True
            ):
                interface = device.interfaces.get(interface_id or "")
                if (not interface or not interface.librenms_port_id
                        or interface.attributes.get("manual_label_only")):
                    continue
                if valid(interface.librenms_port_id, {device.librenms_device_id}):
                    automatic_ports.append(interface.librenms_port_id)
                else:
                    topology.conflicts.append(
                        f"Connection {canonical_id}: endpoint interface "
                        f"{interface.librenms_port_id} is deleted, missing, or belongs to "
                        "another device; correct its source."
                    )
            selected = overrides.get(canonical_id)
            if selected is not None:
                if not valid(selected, owners):
                    topology.conflicts.append(
                        f"Traffic interface {selected} for {canonical_id} is deleted, missing, "
                        "or belongs to another device; choose a replacement or Automatic."
                    )
                    selected = None
            else:
                selected = next(iter(automatic_ports), None)
            link.librenms_port_id = selected
            port = ports.get(selected or 0, {})
            owner = next((device for device in devices
                          if port and device.librenms_device_id == port.get("device_id")), None)
            graphs[canonical_id] = {
                "mode": "overridden" if overrides.get(canonical_id) is not None else "automatic",
                "port_id": selected,
                "requested_port_id": overrides.get(canonical_id),
                "device_id": port.get("device_id"),
                "device": owner.hostname if owner else None,
                "interface": port.get("ifName") or port.get("ifDescr"),
                "endpoint_device_ids": sorted(owners),
            }
        return graphs

    @staticmethod
    def _desired_edges(
        map_config: MapConfig,
        topology: FederatedTopology,
        positions: dict[str, Position],
    ) -> dict[str, MapEdgeSpec]:
        result: dict[str, MapEdgeSpec] = {}
        midpoints = _edge_midpoints(topology, positions, map_config.edge_separation)
        for canonical_id, link in topology.links.items():
            logical = link.attributes.get("relationship_kind") == "logical"
            defaults = map_config.logical_edge_defaults if logical else map_config.edge_defaults
            result[canonical_id] = MapEdgeSpec(
                canonical_id=canonical_id,
                from_node=link.a_device,
                to_node=link.b_device,
                port_id=link.librenms_port_id,
                style=defaults.style,
                showpct=defaults.showpct,
                showbps=defaults.showbps,
                label=(link.protocol or "logical").upper() if logical else "",
                fixed_width=defaults.fixed_width,
                text_face=defaults.text_face,
                text_size=defaults.text_size,
                text_colour=defaults.text_colour,
                text_align=defaults.text_align,
                mid_x=midpoints[canonical_id].x,
                mid_y=midpoints[canonical_id].y,
            )
        return result

    @staticmethod
    def _adoption_matches(
        snapshot: MapSnapshot, desired: DesiredMap, *, enabled: bool
    ) -> tuple[dict[str, int], dict[str, int]]:
        if not enabled:
            return {}, {}
        nodes: dict[str, int] = {}
        used_node_ids: set[int] = set()
        for canonical_id, node_spec in desired.nodes.items():
            candidates = [
                node_id
                for node_id, row in snapshot.nodes.items()
                if node_id not in used_node_ids
                and (
                    (
                        node_spec.device_id is not None
                        and row.get("device_id") == node_spec.device_id
                    )
                    or row.get("label") == node_spec.label
                )
            ]
            if len(candidates) == 1:
                nodes[canonical_id] = candidates[0]
                used_node_ids.add(candidates[0])

        reverse_nodes = {node_id: canonical_id for canonical_id, node_id in nodes.items()}
        edges: dict[str, int] = {}
        used_edge_ids: set[int] = set()
        for canonical_id, edge_spec in desired.edges.items():
            expected = {edge_spec.from_node, edge_spec.to_node}
            candidates = []
            for edge_id, row in snapshot.edges.items():
                if edge_id in used_edge_ids:
                    continue
                actual = {
                    reverse_nodes.get(int(row["custom_map_node1_id"])),
                    reverse_nodes.get(int(row["custom_map_node2_id"])),
                }
                if actual == expected:
                    candidates.append(edge_id)
            if len(candidates) == 1:
                edges[canonical_id] = candidates[0]
                used_edge_ids.add(candidates[0])
        return nodes, edges


def _is_manual_fallback(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata", {})
    return bool(
        metadata.get("fallback", str(record["generated"].get("label", "")).upper() == "MANUAL")
    )


def _discovery_label(sources: list[SourceRef], protocol: str | None, manual: str | None) -> str:
    providers = sorted({source.provider for source in sources if source.provider != manual})
    return f"{', '.join(providers)} {(protocol or '').upper()}".strip()


def _node_label(label: str, mode: str, hostname_parts: int = 2) -> str:
    if mode == "short_hostname":
        try:
            ip_address(label)
            return label
        except ValueError:
            pass
        parts = label.rstrip(".").split(".")
        return ".".join(parts[:hostname_parts])
    return label


def _device_label(device: FederatedDevice, config: MapConfig, mode: str | None = None) -> str:
    mode = mode or config.node_label
    hostname_parts = next(
        (rule.parts for rule in config.short_hostname_overrides
         if rule.device_id == device.librenms_device_id),
        device.short_hostname_parts or config.short_hostname_parts,
    )
    if mode == "sys_name":
        sys_name = device.attributes.get("sys_name")
        if isinstance(sys_name, str) and sys_name.strip():
            return sys_name.strip()
        return _node_label(device.label, "short_hostname", hostname_parts)
    return _node_label(device.label, mode, hostname_parts)


def _edge_display_name(
    topology: FederatedTopology, link: FederatedLink, config: MapConfig, label_mode: str
) -> str:
    def endpoint(device_id: str, interface_id: str | None) -> str:
        device = topology.devices[device_id]
        name = _device_label(device, config, label_mode)
        if not interface_id:
            return name
        interface = device.interfaces.get(interface_id)
        return f"{name} [{interface.name if interface and interface.name else interface_id}]"

    name = (
        f"{endpoint(link.a_device, link.a_interface)}"
        f" ↔ {endpoint(link.b_device, link.b_interface)}"
    )
    return f"{name} — {link.protocol.upper()}" if link.protocol else name


def _stale_node_display_name(
    record: dict[str, Any], current: dict[str, Any] | None
) -> str:
    for label in ((current or {}).get("label"), record["generated"].get("label")):
        if label:
            return _node_label(str(label), "short_hostname")
    return str(record["canonical_id"])


def _stale_edge_display_name(
    record: dict[str, Any],
    snapshot: MapSnapshot,
    managed_objects: dict[str, dict[str, Any]],
) -> str:
    current = snapshot.edges.get(int(record["librenms_id"]))
    if current:
        saved_nodes = {
            int(item["librenms_id"]): item
            for item in managed_objects.values()
            if item["kind"] == "node"
        }

        def node_name(field: str) -> str | None:
            node_id = current.get(field)
            if node_id is None:
                return None
            node = snapshot.nodes.get(int(node_id), {})
            saved = saved_nodes.get(int(node_id), {}).get("generated", {})
            label = node.get("label") or saved.get("label")
            return _node_label(str(label), "short_hostname") if label else None

        first = node_name("custom_map_node1_id")
        second = node_name("custom_map_node2_id")
        if first and second:
            name = f"{first} ↔ {second}"
            label = current.get("label") or record["generated"].get("label")
            return f"{name} — {str(label).upper()}" if label else name
    label = (current or {}).get("label") or record["generated"].get("label")
    return str(label) if label else str(record["canonical_id"])


def _apply_layer_overrides(
    topology: FederatedTopology,
    selectors: list[SelectorConfig],
    *,
    explicit_layers_only: bool = False,
) -> None:
    for device in topology.devices.values():
        for selector in selectors:
            if selector_matches(device, selector):
                if not explicit_layers_only or "layer" in selector.model_fields_set:
                    device.layer = selector.layer
                break


def _edge_midpoints(
    topology: FederatedTopology,
    positions: dict[str, Position],
    separation: int,
) -> dict[str, Position]:
    pairs: dict[tuple[str, str], list[str]] = {}
    for canonical_id, link in topology.links.items():
        pair = (
            min(link.a_device, link.b_device),
            max(link.a_device, link.b_device),
        )
        pairs.setdefault(pair, []).append(canonical_id)

    result: dict[str, Position] = {}
    for pair, canonical_ids in pairs.items():
        first, second = positions[pair[0]], positions[pair[1]]
        center_x = (first.x + second.x) // 2
        center_y = (first.y + second.y) // 2
        if first.x < second.x:
            left, right = first, second
        else:
            left, right = second, first
        x_diff = right.y - left.y
        y_diff = left.x - right.x
        total = abs(x_diff) + abs(y_diff)
        offset_x = math.floor(separation * x_diff / total + 0.5) if total else 0
        offset_y = math.floor(separation * y_diff / total + 0.5) if total else 0
        singleton_lane = (
            len(canonical_ids) == 1
            and topology.devices[pair[0]].layer == topology.devices[pair[1]].layer
        )
        for index, canonical_id in enumerate(sorted(canonical_ids)):
            lane = (
                1
                if singleton_lane
                else 0
                if index == 0
                else (index + 1) // 2 * (1 if index % 2 else -1)
            )
            mid_y = center_y + lane * offset_y
            # Vis renders a label at the canvas origin for a collinear horizontal curve.
            if first.y == second.y and mid_y == center_y:
                mid_y += 1
            result[canonical_id] = Position(
                x=center_x + lane * offset_x,
                y=mid_y,
            )
    return result


def _effective_values(
    current: dict[str, Any],
    previous: dict[str, Any],
    desired: dict[str, Any],
    *,
    authoritative: set[str],
    forced: set[str],
) -> tuple[dict[str, Any], list[str]]:
    effective: dict[str, Any] = {}
    preserved: list[str] = []
    for field, desired_value in desired.items():
        if field in authoritative or field in forced:
            effective[field] = desired_value
        elif field in previous and current.get(field) != previous.get(field):
            effective[field] = current.get(field)
            preserved.append(field)
        else:
            effective[field] = desired_value
    return effective, preserved


def _adopted_effective_values(
    current: dict[str, Any],
    desired: dict[str, Any],
    *,
    authoritative: set[str],
    forced: set[str],
) -> tuple[dict[str, Any], list[str]]:
    effective: dict[str, Any] = {}
    preserved: list[str] = []
    for field, desired_value in desired.items():
        if field in authoritative or field in forced:
            effective[field] = desired_value
        else:
            effective[field] = current.get(field)
            if current.get(field) != desired_value:
                preserved.append(field)
    return effective, preserved


def _changed_values(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in desired.items() if current.get(field) != value}


def _map_changed_values(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(current)
    for field in ("options", "newnodeconfig", "newedgeconfig"):
        value = normalized.get(field)
        if isinstance(value, str):
            try:
                normalized[field] = json.loads(value)
            except ValueError:
                pass
    return _changed_values(normalized, desired)


def _node_generated(row: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "device_id",
        "label",
        "style",
        "icon",
        "image",
        "node_image_id",
        "size",
        "border_width",
        "text_face",
        "text_size",
        "text_colour",
        "colour_bg",
        "colour_bdr",
        "x_pos",
        "y_pos",
    }
    return {field: row.get(field) for field in fields}


def _edge_generated(row: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "custom_map_node1_id",
        "custom_map_node2_id",
        "port_id",
        "reverse",
        "style",
        "showpct",
        "showbps",
        "label",
        "fixed_width",
        "text_face",
        "text_size",
        "text_colour",
        "text_align",
        "mid_x",
        "mid_y",
    }
    return {field: row.get(field) for field in fields}


def _operation_sort_key(operation: Operation) -> tuple[int, int, str]:
    kind_order = {"map": 0, "edge": 1 if operation.action == "delete" else 3, "node": 2}
    action_order = {"delete": 0, "adopt": 1, "update": 2, "noop": 3, "create": 4}
    return (
        kind_order[operation.kind],
        action_order[operation.action],
        operation.canonical_id,
    )
