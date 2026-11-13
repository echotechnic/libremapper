from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from sqlalchemy import Engine

from libremapper.config import AppConfig
from libremapper.models import ProviderInspection
from libremapper.parser_definitions import MAX_PREVIEW_BYTES, ParserDefinition, preview_definition
from libremapper.parser_execution import execute_parser
from libremapper.providers.oxidized import OxidizedGitProvider

DISPLAY_LIMIT = 100
FIELD_LIMIT = 1024


def _bounded_output(payload: dict[str, Any], *, truncated: bool = False) -> dict[str, Any]:
    def bounded(value: Any) -> Any:
        nonlocal truncated
        if isinstance(value, str):
            if len(value) > FIELD_LIMIT:
                truncated = True
                return value[: FIELD_LIMIT - 1] + "…"
            return value
        if isinstance(value, dict):
            if len(value) > DISPLAY_LIMIT:
                truncated = True
            return {
                bounded(key): bounded(item) for key, item in list(value.items())[:DISPLAY_LIMIT]
            }
        if isinstance(value, list):
            return [bounded(item) for item in value]
        return value

    result: dict[str, Any] = bounded(payload)
    result["truncated"] = truncated
    if truncated:
        result["truncation_notice"] = "Preview display limits were applied; totals are unchanged."
    while len(json.dumps(result).encode()) > MAX_PREVIEW_BYTES:
        # Reduce displayed rows in batches so large captures cannot stall serialization.
        arrays = [
            (key, value) for key, value in result.items() if isinstance(value, list) and value
        ]
        multiple_rows = [entry for entry in arrays if len(entry[1]) > 1]
        key, rows = max(multiple_rows or arrays, key=lambda entry: len(json.dumps(entry[1])))
        result[key] = rows[: len(rows) // 2]
        result["truncated"] = True
        result["truncation_notice"] = "Preview output was truncated to 1 MiB; totals are unchanged."
    return result


def _result(
    parser_id: str,
    report: ProviderInspection,
    captures: list[dict[str, Any]],
    revision: str | None,
) -> dict[str, Any]:
    devices = {device.source_id: device for device in report.topology.devices}
    links = []
    for link in report.topology.links[:DISPLAY_LIMIT]:
        endpoints = []
        for endpoint in (link.a, link.b):
            device = devices[endpoint.device]
            interface = next(
                (item for item in device.interfaces if item.source_id == endpoint.interface), None
            )
            endpoints.append(
                {
                    "hostname": device.hostname or device.source_id,
                    "device_id": device.librenms_device_id,
                    "interface": interface.name if interface else None,
                    "port_id": interface.librenms_port_id if interface else None,
                }
            )
        links.append({"a": endpoints[0], "b": endpoints[1], "protocol": link.protocol})
    return _bounded_output(
        {
            "parser_id": parser_id,
            "revision": revision,
            "blocked": report.blocked,
            "artifact_count": len(report.matched_files),
            "connection_count": len(report.topology.links),
            "device_count": len(report.topology.devices),
            "captures": captures[:DISPLAY_LIMIT],
            "links": links,
            "warnings": report.warnings[:DISPLAY_LIMIT],
            "warning_count": len(report.warnings),
            "resolution_failures": report.resolution_failures[:DISPLAY_LIMIT],
            "resolution_failure_count": len(report.resolution_failures),
            "skipped_files": report.skipped_files[:DISPLAY_LIMIT],
            "skipped_file_count": len(report.skipped_files),
            "display_limit": DISPLAY_LIMIT,
        },
        truncated=any(
            len(rows) > DISPLAY_LIMIT
            for rows in (
                captures,
                report.topology.links,
                report.warnings,
                report.resolution_failures,
                report.skipped_files,
            )
        ),
    )


def _run_preview(
    provider_name: str,
    parser_id: str,
    definition: ParserDefinition,
    root: Path,
    engine: Engine,
    revision: str | None,
) -> dict[str, Any]:
    try:
        report, captures = execute_parser(
            provider_name,
            parser_id,
            definition,
            root,
            engine,
            preview=True,
        )
        return _result(parser_id, report, captures, revision)
    except Exception as error:
        timeout_message = f"Parser {parser_id} timed out; no changes were published."
        if isinstance(error, RuntimeError) and str(error) == timeout_message:
            raise RuntimeError(timeout_message) from None
        # Parser exceptions may contain the pasted configuration or credentials.
        raise RuntimeError(
            "Parser preview failed; check the definition and artifact format."
        ) from None


def parser_preview(
    config: AppConfig,
    engine: Engine,
    payload: dict[str, Any],
    *,
    list_files: bool = False,
) -> dict[str, Any]:
    parser_id, definition, filename = preview_definition(payload)
    provider_name = payload.get("provider", "oxidized")
    if (
        not isinstance(provider_name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", provider_name) is None
    ):
        raise ValueError("invalid provider name")
    sample = payload.get("sample")
    if sample is not None and not list_files:
        with TemporaryDirectory(prefix="libremapper-preview-") as directory:
            root = Path(directory)
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(sample)
            path.chmod(0o600)
            return _run_preview(provider_name, parser_id, definition, root, engine, None)

    settings = config.providers.get(provider_name)
    if settings is None or settings.type != "oxidized":
        raise ValueError("configure an Oxidized source before testing repository artifacts")
    provider = OxidizedGitProvider(
        provider_name,
        engine,
        {**settings.settings, "parsers": [parser_id]},
        {**config.oxidized_parsers, parser_id: definition},
    )
    # Preview never marks/prunes successful snapshots or changes the active definition.
    with provider.repository.open() as root:
        files = sorted(
            {
                path.relative_to(root).as_posix()
                for pattern in definition.file_globs
                for path in root.glob(pattern)
                if path.is_file()
                and path.name != ".complete"
                and path.resolve().is_relative_to(root.resolve())
                and not any(
                    parent.is_symlink()
                    for parent in (path, *path.parents)
                    if parent != root and root in parent.parents
                )
            }
        )
        if list_files:
            query = payload.get("query", "")
            if not isinstance(query, str) or len(query) > 255:
                raise ValueError("invalid artifact search")
            matched = [path for path in files if query.lower() in path.lower()]
            displayed = [path for path in matched if len(path) <= FIELD_LIMIT][:500]
            return _bounded_output(
                {"files": displayed, "total": len(matched), "revision": root.name},
                truncated=len(displayed) < len(matched),
            )
        if filename not in files:
            raise ValueError("choose an artifact matching this parser's file patterns")
        with TemporaryDirectory(prefix="libremapper-preview-") as directory:
            target = Path(directory) / filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes((root / filename).read_bytes())
            target.chmod(0o600)
            return _run_preview(
                provider_name,
                parser_id,
                definition,
                Path(directory),
                engine,
                root.name,
            )
