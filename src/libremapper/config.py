from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from libremapper.models import stable_hash
from libremapper.parser_definitions import ParserDefinition, parser_catalog, validate_parser_id


class LibreNMSConfig(BaseModel):
    url: str | None = None
    host_env: str = "DB_HOST"
    port_env: str = "DB_PORT"
    database_env: str = "DB_NAME"
    username_env: str = "DB_USER"
    password_env: str = "DB_PASSWORD"
    connect_timeout: int = 10


class ProviderConfig(BaseModel):
    type: Literal["librenms", "file", "oxidized", "regex_artifacts"]
    settings: dict[str, Any] = Field(default_factory=dict)


class SelectorConfig(BaseModel):
    type: Literal["hostname", "location", "group", "device_id", "canonical", "attribute"]
    value: str | int
    layer: int = 0
    attribute: str | None = None

    @model_validator(mode="after")
    def validate_regex(self) -> SelectorConfig:
        if self.type in {"hostname", "location", "attribute"}:
            try:
                re.compile(str(self.value))
            except re.error as error:
                raise ValueError(f"invalid selector regex: {error}") from error
        return self


class SourceConfig(BaseModel):
    provider: str
    selectors: list[SelectorConfig] = Field(default_factory=list)
    protocols: list[str] = Field(default_factory=list)
    interface_types: list[str] = Field(default_factory=list)
    parsers: list[str] | None = None


def _default_identity_keys() -> list[Literal["fqdn", "management_ip", "hostname"]]:
    return ["fqdn", "management_ip", "hostname"]


class IdentityConfig(BaseModel):
    keys: list[Literal["fqdn", "management_ip", "hostname"]] = Field(
        default_factory=_default_identity_keys
    )
    aliases: dict[str, str] = Field(default_factory=dict)


class LayoutConfig(BaseModel):
    type: Literal["auto", "force", "top", "left", "radial"] = "force"
    width: int = 1600
    height: int = 800
    margin: int = 60
    layer_spacing: int = 180
    node_spacing: int = 120
    radius: int = 180
    seed: int = 42
    iterations: int = 100
    infer_layers: bool = False
    auto_grow: bool = False
    center_layer: int | None = None
    separation: Literal["packed", "constrained"] = "packed"
    layer_margins: dict[int, tuple[int, int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_layer_margins(self) -> LayoutConfig:
        if self.layer_margins and self.type not in {"auto", "top", "left"}:
            raise ValueError("layer_margins is only supported for auto, top and left layouts")
        if any(value < 0 for margins in self.layer_margins.values() for value in margins):
            raise ValueError("layer_margins values must be non-negative")
        return self


class NodeDefaults(BaseModel):
    style: str = "box"
    icon: str | None = None
    image: str = ""
    node_image_id: int | None = None
    size: int = 25
    border_width: int = 1
    text_face: str = "arial"
    text_size: int = 14
    text_colour: str = "#243A54"
    colour_bg: str = "#D2E5FF"
    colour_bdr: str = "#2B7CE9"


class EdgeDefaults(BaseModel):
    style: str = "dynamic"
    showpct: bool = True
    showbps: bool = False
    fixed_width: float | None = None
    text_face: str = "arial"
    text_size: int = 12
    text_colour: str = "#243A54"
    text_align: str = "horizontal"


class ShortHostnameOverride(BaseModel):
    device_id: int = Field(gt=0)
    parts: int = Field(ge=1, le=127)


class LinkGraphOverride(BaseModel):
    canonical_id: str = Field(min_length=1)
    port_id: int | None = Field(default=None, gt=0)


class MapConfig(BaseModel):
    key: str | None = None
    name: str
    example: bool = Field(default=False, exclude=True)
    menu_group: str | None = None
    width: str = "1600px"
    height: str = "800px"
    node_align: int = 10
    edge_separation: int = 24
    scheduled: bool = Field(default=False, exclude=True)
    reverse_arrows: bool = False
    node_label: Literal["full", "short_hostname", "sys_name"] = "full"
    short_hostname_parts: int = Field(default=2, ge=1, le=127)
    short_hostname_overrides: list[ShortHostnameOverride] = Field(default_factory=list)
    sources: list[SourceConfig]
    device_exclusions: list[int] = Field(default_factory=list)
    manual_link_exclusions: list[str] = Field(default_factory=list)
    link_inclusions: list[str] = Field(default_factory=list)
    link_exclusions: list[str] = Field(default_factory=list)
    manual_device_inclusions: list[str] = Field(default_factory=list)
    manual_device_exclusions: list[str] = Field(default_factory=list)
    link_graph_overrides: list[LinkGraphOverride] = Field(default_factory=list)
    layer_overrides: list[SelectorConfig] = Field(default_factory=list)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    layout: LayoutConfig = Field(default_factory=LayoutConfig)
    node_defaults: NodeDefaults = Field(default_factory=NodeDefaults)
    edge_defaults: EdgeDefaults = Field(default_factory=EdgeDefaults)
    logical_edge_defaults: EdgeDefaults = Field(
        default_factory=lambda: EdgeDefaults(showpct=False, showbps=False)
    )

    @model_validator(mode="after")
    def populate_key(self) -> MapConfig:
        if set(self.link_inclusions).intersection(self.link_exclusions):
            raise ValueError("a link cannot be both included and excluded")
        if set(self.manual_device_inclusions).intersection(self.manual_device_exclusions):
            raise ValueError("a manual device cannot be both included and excluded")
        graph_ids = [override.canonical_id for override in self.link_graph_overrides]
        if len(graph_ids) != len(set(graph_ids)):
            raise ValueError("graph overrides must specify each connection only once")
        overridden_ids = [override.device_id for override in self.short_hostname_overrides]
        if len(overridden_ids) != len(set(overridden_ids)):
            raise ValueError("short hostname overrides must specify each device only once")
        included = {
            int(selector.value)
            for source in self.sources
            for selector in source.selectors
            if selector.type == "device_id" and str(selector.value).isdigit()
        }
        if included.intersection(self.device_exclusions):
            raise ValueError("a device cannot be both explicitly included and excluded")
        if any(device_id <= 0 for device_id in self.device_exclusions):
            raise ValueError("device exclusions must be positive device IDs")
        if not self.key:
            self.key = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        if not self.key:
            raise ValueError("map key cannot be empty")
        return self


class ExperimentalConfig(BaseModel):
    native_link_sync: bool = False
    link_adapter_command: list[str] = Field(default_factory=lambda: ["librenms-link-adapter"])


class AppConfig(BaseModel):
    version: Literal[1] = 1
    state_path: Path = Path(".libremapper-state.sqlite3")
    librenms: LibreNMSConfig = Field(default_factory=LibreNMSConfig)
    experimental: ExperimentalConfig = Field(default_factory=ExperimentalConfig)
    providers: dict[str, ProviderConfig]
    oxidized_parsers: dict[str, ParserDefinition] = Field(default_factory=dict)
    manual_provider: str | None = None
    map_directory: Path | None = None
    maps: list[MapConfig]
    config_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_references(self) -> AppConfig:
        catalog = parser_catalog(self.oxidized_parsers)
        for parser_id in self.oxidized_parsers:
            validate_parser_id(parser_id)
        for provider_config in self.providers.values():
            if provider_config.type == "oxidized":
                if provider_config.settings.get("parsers") == []:
                    raise ValueError(
                        "legacy provider parser defaults cannot be empty; "
                        "disable parsers on the map source"
                    )
                self._validate_parser_selection(
                    provider_config.settings.get("parsers", ["saos10-lldp-neighbors"]), catalog
                )
        if self.manual_provider:
            provider = self.providers.get(self.manual_provider)
            if provider is None:
                raise ValueError(
                    f"manual_provider references unknown provider {self.manual_provider}"
                )
            if provider.type != "file":
                raise ValueError("manual_provider must reference a file provider")
        keys: set[str] = set()
        for map_config in self.maps:
            assert map_config.key is not None
            if map_config.key in keys:
                raise ValueError(f"duplicate map key: {map_config.key}")
            keys.add(map_config.key)
            for source in map_config.sources:
                if source.provider == self.manual_provider:
                    raise ValueError(
                        f"manual provider {source.provider} cannot also be a map source"
                    )
                if source.provider not in self.providers:
                    raise ValueError(
                        f"map {map_config.key} references unknown provider {source.provider}"
                    )
                provider = self.providers[source.provider]
                if source.parsers is not None:
                    if provider.type != "oxidized":
                        raise ValueError("parser selections require an Oxidized source")
                    self._validate_parser_selection(source.parsers, catalog)
                if provider.type == "librenms" and not source.selectors:
                    raise ValueError(
                        f"map {map_config.key} source {source.provider} must configure "
                        "at least one selector; selecting the entire LibreNMS database "
                        "is not allowed"
                    )
        return self

    @staticmethod
    def _validate_parser_selection(selection: Any, catalog: dict[str, ParserDefinition]) -> None:
        if not isinstance(selection, list) or any(not isinstance(item, str) for item in selection):
            raise ValueError("parser selections must be a list of parser IDs")
        if len(set(selection)) != len(selection) or set(selection) - catalog.keys():
            raise ValueError("unknown or duplicate Oxidized parser")

    def effective_parsers(self, source: SourceConfig) -> list[str]:
        if source.parsers is not None:
            return source.parsers
        selected = self.providers[source.provider].settings.get(
            "parsers", ["saos10-lldp-neighbors"]
        )
        return list(selected)

    def map_by_key(self, key: str) -> MapConfig:
        for map_config in self.maps:
            if map_config.key == key:
                return map_config
        available = ", ".join(str(item.key) for item in self.maps)
        raise KeyError(f"unknown map {key!r}; available maps: {available}")

    def fingerprint(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"config_path"}))

    def plan_fingerprint(self, map_key: str) -> str:
        payload = self.model_dump(mode="json", exclude={"config_path"})
        catalog = parser_catalog(self.oxidized_parsers)
        selected = {
            parser_id
            for source in self.map_by_key(map_key).sources
            if self.providers[source.provider].type == "oxidized"
            for parser_id in self.effective_parsers(source)
        }
        payload["oxidized_parsers"] = {
            parser_id: catalog[parser_id].model_dump(mode="json") for parser_id in sorted(selected)
        }
        return stable_hash(payload)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw = config_path.read_text()
    if config_path.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("main configuration must be a mapping")
    map_directory = data.get("map_directory")
    if map_directory is not None:
        if not isinstance(map_directory, str):
            raise ValueError("map_directory must be a relative directory name")
        relative = Path(map_directory)
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
            raise ValueError("map_directory must be a relative directory name")
        directory = config_path.parent / relative
        if directory.is_symlink():
            raise ValueError(f"map_directory cannot be a symlink: {map_directory}")
        if not directory.is_dir():
            raise ValueError(f"map_directory must be a directory: {map_directory}")
        if not directory.resolve().is_relative_to(config_path.parent):
            raise ValueError(f"map_directory escapes the configuration root: {map_directory}")
        inline_maps = data.get("maps", [])
        if not isinstance(inline_maps, list):
            raise ValueError("maps must be a list")
        maps = list(inline_maps)
        for fragment in sorted(directory.iterdir(), key=lambda item: item.name):
            if fragment.name.startswith("."):
                raise ValueError(f"hidden map path is not allowed: {fragment.name}")
            if fragment.is_symlink():
                raise ValueError(f"map fragment cannot be a symlink: {fragment.name}")
            if fragment.is_dir():
                raise ValueError(f"nested map paths are not allowed: {fragment.name}")
            if fragment.suffix not in {".yaml", ".yml"}:
                continue
            if not fragment.is_file():
                raise ValueError(f"map fragment must be a regular file: {fragment.name}")
            fragment_data = yaml.safe_load(fragment.read_text())
            if not isinstance(fragment_data, dict):
                raise ValueError(f"map fragment must be a mapping: {fragment.name}")
            key = fragment_data.get("key")
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key) is None
                or fragment.stem != key
            ):
                raise ValueError(
                    f"map fragment filename must match its safe lowercase key: {fragment.name}"
                )
            maps.append(fragment_data)
        data = {**data, "maps": maps}
    config = AppConfig.model_validate(data)
    config.config_path = config_path
    state_override = os.getenv("LIBREMAPPER_STATE_PATH")
    if state_override:
        config.state_path = Path(state_override).expanduser().resolve()
    elif not config.state_path.is_absolute():
        config.state_path = (config_path.parent / config.state_path).resolve()
    for provider in config.providers.values():
        if provider.type == "file" and "path" in provider.settings:
            provider_path = Path(str(provider.settings["path"])).expanduser()
            if not provider_path.is_absolute():
                provider.settings["path"] = str((config_path.parent / provider_path).resolve())
        if provider.type == "regex_artifacts":
            if "root" in provider.settings:
                root = Path(str(provider.settings["root"])).expanduser()
                if not root.is_absolute():
                    provider.settings["root"] = str((config_path.parent / root).resolve())
            inventory = provider.settings.get("host_inventory")
            if isinstance(inventory, str):
                inventory_path = Path(inventory).expanduser()
                if not inventory_path.is_absolute():
                    provider.settings["host_inventory"] = str(
                        (config_path.parent / inventory_path).resolve()
                    )
    command = config.experimental.link_adapter_command
    if command and "/" in command[0]:
        executable = Path(command[0]).expanduser()
        if not executable.is_absolute():
            command[0] = str((config_path.parent / executable).resolve())
    return config
