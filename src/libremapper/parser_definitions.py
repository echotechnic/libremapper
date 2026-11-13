from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

PARSER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
MAX_DEFINITION_BYTES = 64 * 1024
MAX_SAMPLE_BYTES = 256 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024
REGEX_FLAGS = {"ASCII", "DOTALL", "IGNORECASE", "MULTILINE", "VERBOSE"}
SAOS10_NEIGHBORS = (
    r"^\|\s*interface\s*\|\s*(?P<local_interface>[^|\r\n]+).*?"
    r"^\|\s*chassis-id\s*\|\s*(?P<remote_chassis_id>[^|\r\n]+)"
    r"(?:(?:(?!^\|\s*(?:interface|port-id)\s*\|).)*?"
    r"^\|\s*port-desc\s*\|\s*(?P<remote_port_description>[^|\r\n]+))?"
    r"(?:(?!^\|\s*(?:interface|port-id)\s*\|).)*?"
    r"^\|\s*port-id\s*\|\s*(?P<remote_interface>[^|\r\n]+).*?"
    r"^\|\s*system-name\s*\|\s*(?P<remote_hostname>[^|\r\n]+)"
)


def safe_artifact_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
    ):
        raise ValueError("artifact paths must stay inside the Git snapshot")
    return value


class ParserRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=PARSER_ID.pattern, min_length=1, max_length=64)
    label: str = Field(default="", max_length=255)
    regex: str = Field(min_length=1, max_length=MAX_DEFINITION_BYTES)
    flags: list[str] = Field(default_factory=lambda: ["MULTILINE"], max_length=5)

    @model_validator(mode="after")
    def validate_rule(self) -> ParserRule:
        if PARSER_ID.fullmatch(self.id) is None:
            raise ValueError("rule ID must be a stable lowercase name")
        if set(self.flags) - REGEX_FLAGS or len(set(self.flags)) != len(self.flags):
            raise ValueError("unsupported or duplicate regex flags")
        try:
            compiled = re.compile(self.regex, sum(int(getattr(re, flag)) for flag in self.flags))
        except re.error as error:
            raise ValueError(f"invalid extraction regex: {error}") from error
        if "remote_hostname" not in compiled.groupindex:
            raise ValueError("extraction rules require a named remote_hostname capture")
        return self


class ParserBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=255)
    file_globs: list[str] = Field(min_length=1, max_length=32)
    path_regex: str = Field(min_length=1, max_length=MAX_DEFINITION_BYTES)
    zero_output_guard: bool = True

    @model_validator(mode="after")
    def validate_paths(self) -> ParserBase:
        for pattern in self.file_globs:
            safe_artifact_path(pattern)
            if len(pattern) > 1024:
                raise ValueError("artifact pattern is too long")
        try:
            re.compile(self.path_regex)
        except re.error as error:
            raise ValueError(f"invalid filename regex: {error}") from error
        if len(self.model_dump_json().encode()) > MAX_DEFINITION_BYTES:
            raise ValueError("parser definition exceeds 64 KiB")
        return self


class RegexParserDefinition(ParserBase):
    type: Literal["regex"] = "regex"
    protocol: str = Field(default="lldp", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    rules: list[ParserRule] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_rules(self) -> RegexParserDefinition:
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("rule IDs must be unique within a parser")
        return self


class NniParserDefinition(ParserBase):
    type: Literal["saos10_nni"] = "saos10_nni"
    zero_output_guard: bool = False
    peer_hostname_regex: str = r"^car\d+(?:\.|$)"
    interface_types: list[str] = Field(default_factory=lambda: ["point-to-point"], min_length=1)
    ipv4_prefix_lengths: list[int] = Field(default_factory=lambda: [30, 31])
    ipv6_prefix_lengths: list[int] = Field(default_factory=lambda: [126, 127])

    @model_validator(mode="after")
    def validate_matching(self) -> NniParserDefinition:
        try:
            re.compile(self.peer_hostname_regex, re.IGNORECASE)
        except re.error as error:
            raise ValueError(f"invalid peer hostname regex: {error}") from error
        if "device_name" not in re.compile(self.path_regex).groupindex:
            raise ValueError("NNI filename regex requires a named device_name capture")
        for prefixes, maximum in ((self.ipv4_prefix_lengths, 32), (self.ipv6_prefix_lengths, 128)):
            if any(value < 0 or value > maximum for value in prefixes):
                raise ValueError("invalid IP prefix length")
            if len(set(prefixes)) != len(prefixes):
                raise ValueError("duplicate IP prefix length")
        if not self.ipv4_prefix_lengths and not self.ipv6_prefix_lengths:
            raise ValueError("enable at least one IP address family")
        return self


ParserDefinition = Annotated[
    RegexParserDefinition | NniParserDefinition, Field(discriminator="type")
]
DEFINITION_ADAPTER = TypeAdapter[ParserDefinition](ParserDefinition)
BUILTIN_PARSERS: dict[str, ParserDefinition] = {
    "saos10-lldp-neighbors": RegexParserDefinition(
        label="Ciena SAOS 10 LLDP neighbors",
        file_globs=["topology/*--lldp-neighbors"],
        path_regex=r"^topology/(?P<device_name>.+)--lldp-neighbors$",
        rules=[
            ParserRule(
                id="saos10-lldp-neighbors", regex=SAOS10_NEIGHBORS, flags=["MULTILINE", "DOTALL"]
            )
        ],
    ),
    "saos10-nni-config": NniParserDefinition(
        label="Ciena SAOS 10 NNI configuration",
        file_globs=["*", "configs/*"],
        path_regex=r"^(?:configs/)?(?P<device_name>[^/]+)$",
    ),
}


def parser_catalog(overrides: dict[str, ParserDefinition]) -> dict[str, ParserDefinition]:
    return {**BUILTIN_PARSERS, **overrides}


def validate_parser_id(value: str) -> str:
    if PARSER_ID.fullmatch(value) is None:
        raise ValueError("parser ID must be a stable lowercase name")
    return value


def definition_json(definition: ParserDefinition) -> dict[str, Any]:
    return definition.model_dump(mode="json")


def preview_definition(payload: dict[str, Any]) -> tuple[str, ParserDefinition, str]:
    parser_id = validate_parser_id(str(payload.get("parser_id", "")))
    raw = payload.get("definition")
    if (
        len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_DEFINITION_BYTES
    ):
        raise ValueError("parser definition exceeds 64 KiB")
    definition = DEFINITION_ADAPTER.validate_python(raw)
    filename = safe_artifact_path(str(payload.get("filename", "")))
    if any(char in filename for char in "*?[") or PurePosixPath(filename).name == ".complete":
        raise ValueError("choose a relative artifact filename, not a pattern")
    sample = payload.get("sample")
    if sample is not None and (
        not isinstance(sample, str) or len(sample.encode()) > MAX_SAMPLE_BYTES
    ):
        raise ValueError("pasted sample exceeds 256 KiB")
    return parser_id, definition, filename


def describe_catalog(config: Any) -> dict[str, Any]:
    catalog = parser_catalog(config.oxidized_parsers)
    selections: dict[str, dict[str, list[str]]] = {}
    for map_config in config.maps:
        sources: dict[str, list[str]] = {}
        for source in map_config.sources:
            if config.providers[source.provider].type != "oxidized":
                continue
            selected = sources.setdefault(source.provider, [])
            for parser_id in config.effective_parsers(source):
                if parser_id not in selected:
                    selected.append(parser_id)
        selections[str(map_config.key)] = sources
    items = []
    for parser_id, definition in catalog.items():
        items.append(
            {
                "id": parser_id,
                "definition": definition_json(definition),
                "builtin": parser_id in BUILTIN_PARSERS,
                "overridden": parser_id in config.oxidized_parsers and parser_id in BUILTIN_PARSERS,
                "default_definition": definition_json(BUILTIN_PARSERS[parser_id])
                if parser_id in BUILTIN_PARSERS
                else None,
                "maps": [
                    key
                    for key, sources in selections.items()
                    if any(parser_id in values for values in sources.values())
                ],
                "legacy_defaults": [
                    name
                    for name, provider in config.providers.items()
                    if provider.type == "oxidized"
                    and parser_id in provider.settings.get("parsers", ["saos10-lldp-neighbors"])
                ],
            }
        )
    return {"items": items, "selections": selections}
