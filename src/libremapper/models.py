from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class SourceRef(BaseModel):
    provider: str
    source_id: str


class Interface(BaseModel):
    source_id: str
    device_source_id: str
    name: str | None = None
    if_index: int | None = None
    mac: str | None = None
    librenms_port_id: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Device(BaseModel):
    source_id: str
    hostname: str | None = None
    fqdn: str | None = None
    management_ip: str | None = None
    librenms_device_id: int | None = None
    layer: int = 0
    short_hostname_parts: int | None = Field(default=None, ge=1, le=127)
    attributes: dict[str, Any] = Field(default_factory=dict)
    interfaces: list[Interface] = Field(default_factory=list)


class LinkEndpoint(BaseModel):
    device: str
    interface: str | None = None
    interface_definition: Interface | None = None


class Link(BaseModel):
    source_id: str
    a: LinkEndpoint
    b: LinkEndpoint
    protocol: str | None = None
    relationship_kind: Literal["direct", "logical"] | None = None
    confidence: float = 1.0
    attributes: dict[str, Any] = Field(default_factory=dict)


class SuppressLink(BaseModel):
    a: LinkEndpoint
    b: LinkEndpoint
    protocol: str | None = None


class Topology(BaseModel):
    provider: str
    devices: list[Device] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def fingerprint(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class ProviderInspection(BaseModel):
    provider: str
    provider_type: str
    revision: str | None = None
    matched_files: list[str] = Field(default_factory=list)
    matched_rules: dict[str, str] = Field(default_factory=dict)
    skipped_files: list[dict[str, str]] = Field(default_factory=list)
    resolution_failures: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked: bool = False
    parser_reports: list[dict[str, Any]] = Field(default_factory=list)
    topology: Topology


class FederatedInterface(BaseModel):
    canonical_id: str
    name: str | None = None
    if_index: int | None = None
    mac: str | None = None
    librenms_port_id: int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)


class FederatedDevice(BaseModel):
    canonical_id: str
    hostname: str | None = None
    fqdn: str | None = None
    management_ip: str | None = None
    librenms_device_id: int | None = None
    layer: int = 0
    short_hostname_parts: int | None = Field(default=None, ge=1, le=127)
    attributes: dict[str, Any] = Field(default_factory=dict)
    interfaces: dict[str, FederatedInterface] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return self.hostname or self.fqdn or self.management_ip or self.canonical_id


class FederatedLink(BaseModel):
    canonical_id: str
    a_device: str
    b_device: str
    a_interface: str | None = None
    b_interface: str | None = None
    librenms_port_id: int | None = None
    protocol: str | None = None
    confidence: float = 1.0
    attributes: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)


class LinkFallback(BaseModel):
    source: SourceRef
    a_device: str
    b_device: str
    status: Literal["active", "corroborated", "parallel-covered"]
    canonical_ids: list[str]
    display_name: str | None = None


class FederatedTopology(BaseModel):
    devices: dict[str, FederatedDevice] = Field(default_factory=dict)
    links: dict[str, FederatedLink] = Field(default_factory=dict)
    link_fallbacks: list[LinkFallback] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    def fingerprint(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class Position(BaseModel):
    x: int
    y: int


class MapNodeSpec(BaseModel):
    canonical_id: str
    device_id: int | None
    label: str
    x_pos: int
    y_pos: int
    style: str = "box"
    icon: str | None = None
    image: str = ""
    node_image_id: int | None = None
    size: int = 25
    border_width: int = 1
    text_face: str = "arial"
    text_size: int = 14
    text_colour: str = "#343434"
    colour_bg: str | None = "#D2E5FF"
    colour_bdr: str | None = "#2B7CE9"

    def db_values(self) -> dict[str, Any]:
        return self.model_dump(exclude={"canonical_id"})


class MapEdgeSpec(BaseModel):
    canonical_id: str
    from_node: str
    to_node: str
    port_id: int | None = None
    reverse: bool = False
    style: str = "dynamic"
    showpct: bool = True
    showbps: bool = False
    label: str = ""
    fixed_width: float | None = None
    text_face: str = "arial"
    text_size: int = 12
    text_colour: str = "#343434"
    text_align: str = "horizontal"
    mid_x: int = 0
    mid_y: int = 0

    def db_values(self) -> dict[str, Any]:
        return self.model_dump(exclude={"canonical_id", "from_node", "to_node"})


class Operation(BaseModel):
    action: Literal["create", "adopt", "update", "delete", "noop"]
    kind: Literal["map", "node", "edge"]
    canonical_id: str
    display_name: str | None = None
    layer: int | None = None
    librenms_id: int | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    generated_values: dict[str, Any] = Field(default_factory=dict)
    preserved_fields: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    protocol: str | None = None
    a_interface: str | None = None
    b_interface: str | None = None
    graph: dict[str, Any] = Field(default_factory=dict)
    reconciliation_reason: str | None = None
    previous_canonical_id: str | None = None


class Plan(BaseModel):
    version: int = 1
    map_key: str
    map_name: str
    config_path: str
    config_hash: str
    source_hash: str
    database_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    layout_summary: dict[str, Any] = Field(default_factory=dict)
    relayout: bool = False
    adopt: bool = False
    operations: list[Operation] = Field(default_factory=list)
    link_fallbacks: list[LinkFallback] = Field(default_factory=list)
    link_choices: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @property
    def changes(self) -> int:
        return sum(operation.action != "noop" for operation in self.operations)


class LinkSyncOperation(BaseModel):
    action: Literal["create", "update", "delete", "noop"]
    source_id: str
    link_id: int | None = None
    natural_key: str
    values: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class LinkSyncPlan(BaseModel):
    version: int = 1
    provider: str
    source_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    operations: list[LinkSyncOperation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @property
    def changes(self) -> int:
        return sum(operation.action != "noop" for operation in self.operations)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)
