from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from libremapper.config import SelectorConfig, SourceConfig
from libremapper.models import Device, FederatedDevice, Interface, Link, SuppressLink, Topology
from libremapper.providers.base import TopologyProvider


class InventoryOverride(BaseModel):
    layer: int | None = None
    short_hostname_parts: int | None = Field(default=None, ge=1, le=127)
    attributes: dict[str, Any] = Field(default_factory=dict)
    interfaces: list[Interface] = Field(default_factory=list)


class FileTopology(BaseModel):
    version: Literal[1] = 1
    devices: list[Device] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    suppress_links: list[SuppressLink] = Field(default_factory=list)
    inventory_overrides: dict[str, InventoryOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> FileTopology:
        devices = {device.source_id: device for device in self.devices}
        if len(devices) != len(self.devices):
            raise ValueError("topology device source IDs must be unique")
        if len({link.source_id for link in self.links}) != len(self.links):
            raise ValueError("topology link source IDs must be unique")
        for source_id, override in self.inventory_overrides.items():
            reference = re.fullmatch(r"librenms:([1-9][0-9]*)", source_id)
            if not reference or source_id in devices:
                raise ValueError(f"invalid inventory override: {source_id}")
            if any(interface.device_source_id != source_id for interface in override.interfaces):
                raise ValueError(f"invalid inventory interface owner: {source_id}")
            attributes = {**override.attributes, "_libremapper_inventory_reference": True}
            if override.layer is not None:
                attributes["_libremapper_explicit_layer"] = True
            device = Device(
                source_id=source_id, librenms_device_id=int(reference.group(1)),
                layer=override.layer if override.layer is not None else 0,
                short_hostname_parts=override.short_hostname_parts,
                attributes=attributes, interfaces=override.interfaces,
            )
            devices[source_id] = device
            self.devices.append(device)
        for device in self.devices:
            if len({item.source_id for item in device.interfaces}) != len(device.interfaces):
                raise ValueError(f"device {device.source_id}: interface source IDs must be unique")
        relationships: list[Link | SuppressLink] = [*self.links, *self.suppress_links]
        for link in relationships:
            for endpoint in (link.a, link.b):
                endpoint_device = devices.get(endpoint.device)
                if endpoint_device is None:
                    reference = re.fullmatch(r"librenms:([1-9][0-9]*)", endpoint.device)
                    if not reference:
                        raise ValueError(f"unknown topology device: {endpoint.device}")
                    endpoint_device = Device(
                        source_id=endpoint.device,
                        librenms_device_id=int(reference.group(1)),
                        attributes={"_libremapper_inventory_reference": True},
                    )
                    devices[endpoint.device] = endpoint_device
                    self.devices.append(endpoint_device)
                if endpoint.interface_definition:
                    definition = endpoint.interface_definition
                    if (
                        not endpoint_device.attributes.get("_libremapper_inventory_reference")
                        or definition.source_id != endpoint.interface
                        or definition.device_source_id != endpoint.device
                    ):
                        raise ValueError("invalid inventory interface definition")
                    existing = next(
                        (item for item in endpoint_device.interfaces
                         if item.source_id == definition.source_id), None
                    )
                    if existing and existing != definition:
                        raise ValueError("conflicting inventory interface definitions")
                    if not existing:
                        endpoint_device.interfaces.append(definition)
                if endpoint.interface and not any(
                    item.source_id == endpoint.interface for item in endpoint_device.interfaces
                ):
                    raise ValueError(f"unknown topology interface: {endpoint.interface}")
        return self


def selector_matches(device: Device | FederatedDevice, selector: SelectorConfig) -> bool:
    value = str(selector.value)
    source_id = device.source_id if isinstance(device, Device) else device.canonical_id
    if selector.type == "hostname":
        return bool(re.search(value, device.hostname or ""))
    if selector.type == "location":
        return bool(re.search(value, str(device.attributes.get("location", ""))))
    if selector.type == "group":
        return value in {str(item) for item in device.attributes.get("groups", [])}
    if selector.type == "device_id":
        return value == str(device.librenms_device_id or source_id)
    if selector.type == "canonical":
        candidates = {source_id, device.hostname, device.fqdn, device.management_ip}
        if isinstance(device, FederatedDevice):
            candidates.update(f"{source.provider}:{source.source_id}" for source in device.sources)
        return value in candidates
    if selector.type == "attribute" and selector.attribute:
        return bool(re.search(value, str(device.attributes.get(selector.attribute, ""))))
    return False


def select_topology(topology: Topology, source: SourceConfig) -> Topology:
    selected: dict[str, Device]
    if not source.selectors:
        selected = {device.source_id: device.model_copy(deep=True) for device in topology.devices}
    else:
        selected = {}
        for selector in source.selectors:
            for original in topology.devices:
                if selector_matches(original, selector):
                    if original.source_id not in selected:
                        device = original.model_copy(deep=True)
                        device.layer = selector.layer
                        selected[original.source_id] = device

    protocols = {item.lower() for item in source.protocols}
    links = [
        link.model_copy(deep=True)
        for link in topology.links
        if link.a.device in selected
        and link.b.device in selected
        and (not protocols or (link.protocol or "").lower() in protocols)
    ]
    return Topology(
        provider=topology.provider,
        devices=list(selected.values()),
        links=links,
        warnings=list(topology.warnings),
    )


class FileProvider(TopologyProvider):
    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        super().__init__(name)
        self.path = Path(str(settings["path"]))

    def read(self) -> FileTopology:
        raw = self.path.read_text()
        if self.path.suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
        parsed = FileTopology.model_validate(data)
        raw_devices = data.get("devices", []) if isinstance(data, dict) else []
        for device, raw_device in zip(parsed.devices, raw_devices, strict=False):
            if isinstance(raw_device, dict) and "layer" in raw_device:
                device.attributes["_libremapper_explicit_layer"] = True
        return parsed

    def load(self, source: SourceConfig) -> Topology:
        parsed = self.read()
        if any(
            device.attributes.get("_libremapper_inventory_reference") for device in parsed.devices
        ):
            raise ValueError("LibreNMS references are only supported in the manual provider")
        topology = Topology(provider=self.name, devices=parsed.devices, links=parsed.links)
        return select_topology(topology, source)
