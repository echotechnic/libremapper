from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine

from libremapper.models import ProviderInspection
from libremapper.parser_definitions import (
    DEFINITION_ADAPTER,
    NniParserDefinition,
    ParserDefinition,
    RegexParserDefinition,
    definition_json,
)


def _worker(
    connection: Connection,
    provider: str,
    parser_id: str,
    raw_definition: dict[str, Any],
    root: str,
    database_url: str,
    preview: bool,
) -> None:
    # A spawned worker owns its DB pool and can be killed during pathological regex matching.
    engine = create_engine(database_url)
    try:
        from libremapper.providers.ciena_nni import inspect_ciena_nni
        from libremapper.providers.regex_artifacts import (
            LibreNMSIdentityResolver,
            RegexArtifactsProvider,
        )

        definition = DEFINITION_ADAPTER.validate_python(raw_definition)
        resolver = LibreNMSIdentityResolver(engine)
        captures: list[dict[str, Any]] | None = [] if preview else None
        if isinstance(definition, NniParserDefinition):
            report = inspect_ciena_nni(
                provider,
                engine,
                Path(root),
                resolver,
                definition=definition,
                parser_id=parser_id,
                captures=captures,
            )
        else:
            assert isinstance(definition, RegexParserDefinition)
            rules = [
                {
                    "name": rule.id
                    if parser_id == "saos10-lldp-neighbors"
                    else f"{parser_id}:{rule.id}",
                    "regex": rule.regex,
                    "flags": rule.flags,
                }
                for rule in definition.rules
            ]
            report = RegexArtifactsProvider(
                provider,
                engine,
                {
                    "root": root,
                    "file_globs": definition.file_globs,
                    "path_regex": definition.path_regex,
                    "zero_output_guard": definition.zero_output_guard,
                    "protocol": definition.protocol,
                    "rules": rules,
                },
            ).inspect(resolver, preview_records=captures)
        connection.send({"report": report.model_dump(mode="json"), "captures": captures or []})
    except RuntimeError as error:
        connection.send({"error": str(error)})
    except Exception:
        # Exceptions from parsing may include raw configuration lines or credentials.
        connection.send(
            {"error": "Parser execution failed; check the definition and artifact format."}
        )
    finally:
        engine.dispose()
        connection.close()


def execute_parser(
    provider: str,
    parser_id: str,
    definition: ParserDefinition,
    root: Path,
    engine: Engine,
    *,
    preview: bool = False,
    timeout: float | None = None,
) -> tuple[ProviderInspection, list[dict[str, Any]]]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(
            sender,
            provider,
            parser_id,
            definition_json(definition),
            str(root),
            engine.url.render_as_string(hide_password=False),
            preview,
        ),
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout if timeout is not None else (10 if preview else 30)):
            raise RuntimeError(f"Parser {parser_id} timed out; no changes were published.")
        try:
            result = receiver.recv()
        except EOFError as error:
            raise RuntimeError(f"Parser {parser_id} worker stopped unexpectedly.") from error
        if "error" in result:
            raise RuntimeError(f"Parser {parser_id}: {result['error']}")
        return ProviderInspection.model_validate(result["report"]), result["captures"]
    finally:
        receiver.close()
        process.join(timeout=0.2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
