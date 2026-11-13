from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path
from typing import Annotated

import typer

from libremapper.config import AppConfig, load_config
from libremapper.database import create_librenms_engine
from libremapper.inventory import build_inventory
from libremapper.librenms import LibreNMSRepository
from libremapper.models import LinkSyncPlan, Plan
from libremapper.parser_definitions import describe_catalog
from libremapper.parser_preview import parser_preview
from libremapper.runtime import drop_privileges
from libremapper.service import LibremapperService
from libremapper.state import StateStore

app = typer.Typer(
    name="libremapper",
    help="Federate topology sources into LibreNMS Custom Maps.",
    no_args_is_help=True,
)
state_app = typer.Typer(help="Inspect reconciliation ownership state.")
provider_app = typer.Typer(help="Inspect configured topology providers.")
links_app = typer.Typer(help="Plan and synchronize experimental native links.")
app.add_typer(state_app, name="state")
app.add_typer(provider_app, name="provider")
app.add_typer(links_app, name="links")

DEFAULT_CONFIG = os.getenv("LIBREMAPPER_CONFIG", "/config/libremapper.yaml")


def _service(config_path: str) -> tuple[AppConfig, LibremapperService]:
    config = load_config(config_path)
    engine = create_librenms_engine(config.librenms)
    state = StateStore(config.state_path)
    return config, LibremapperService(config, engine, state)


@contextmanager
def _locked_service(config_path: str) -> Iterator[tuple[AppConfig, LibremapperService]]:
    config = load_config(config_path)
    if os.getenv("LIBREMAPPER_LOCK_HELD") == "1":
        engine = create_librenms_engine(config.librenms)
        yield config, LibremapperService(config, engine, StateStore(config.state_path))
        return
    runtime = Path(os.getenv("LIBREMAPPER_DATA_ROOT", str(config.state_path.parent)))
    lock_path = Path(
        os.getenv(
            "LIBREMAPPER_LOCK_PATH",
            str(runtime / "action.lock"),
        )
    )
    # ponytail: one global lock; use per-map locks only if reconciliation throughput matters.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            flock(lock.fileno(), LOCK_EX | LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another Libremapper action is already running.") from error
        try:
            engine = create_librenms_engine(config.librenms)
            yield config, LibremapperService(config, engine, StateStore(config.state_path))
        finally:
            flock(lock.fileno(), LOCK_UN)


def _print_plan(plan: Plan) -> None:
    typer.echo(
        f"Map {plan.map_key!r}: {plan.changes} change(s), "
        f"{len(plan.warnings)} warning(s), {len(plan.conflicts)} conflict(s)"
    )
    for operation in plan.operations:
        if operation.action == "noop":
            continue
        target = (
            f" LibreNMS ID {operation.librenms_id}" if operation.librenms_id is not None else ""
        )
        preserved = (
            f"; preserved {', '.join(operation.preserved_fields)}"
            if operation.preserved_fields
            else ""
        )
        typer.echo(
            f"  {operation.action.upper():6} {operation.kind:4} "
            f"{operation.canonical_id}{target}{preserved}"
        )
    for warning in plan.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    for conflict in plan.conflicts:
        typer.echo(f"CONFLICT: {conflict}", err=True)


def _print_link_plan(plan: LinkSyncPlan) -> None:
    typer.echo(
        f"Provider {plan.provider!r}: {plan.changes} change(s), "
        f"{len(plan.warnings)} warning(s), {len(plan.conflicts)} conflict(s)"
    )
    for operation in plan.operations:
        if operation.action != "noop":
            target = f" LibreNMS ID {operation.link_id}" if operation.link_id else ""
            typer.echo(f"  {operation.action.upper():6} {operation.source_id}{target}")
    for warning in plan.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    for conflict in plan.conflicts:
        typer.echo(f"CONFLICT: {conflict}", err=True)


@app.command()
def validate(
    config_path: Annotated[
        str,
        typer.Option("--config", "-c", help="Path to libremapper YAML or JSON config."),
    ] = DEFAULT_CONFIG,
    map_key: Annotated[str | None, typer.Option("--map")] = None,
    structural: Annotated[bool, typer.Option("--structural-only")] = False,
) -> None:
    """Validate configuration, provider files, connectivity, and LibreNMS schema."""
    try:
        with _locked_service(config_path) as (_, service):
            errors = service.validate(map_key, structural=structural)
    except Exception as error:
        typer.echo(f"Validation failed: {error}", err=True)
        raise typer.Exit(1) from error
    if errors:
        for validation_error in errors:
            typer.echo(f"ERROR: {validation_error}", err=True)
        raise typer.Exit(1)
    typer.echo("Configuration and LibreNMS schema are valid.")


@app.command()
def inventory(
    config_path: Annotated[
        str,
        typer.Option("--config", "-c", help="Path to libremapper YAML or JSON config."),
    ] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
) -> None:
    """Report read-only LibreNMS device, link, group, and location inventory."""
    try:
        config = load_config(config_path)
        report = build_inventory(create_librenms_engine(config.librenms))
    except Exception as error:
        typer.echo(f"Unable to read inventory: {error}", err=True)
        raise typer.Exit(1) from error
    if as_json:
        typer.echo(json.dumps(report, indent=2, sort_keys=True, default=str))
        return
    counts = report["counts"]
    typer.echo(
        f"Devices: {counts.get('devices', 0)}; ports: {counts.get('ports', 0)}; "
        f"links: {counts.get('links', 0)}"
    )
    typer.echo("Groups:")
    for group in report["groups"]:
        typer.echo(f"  {group['id']}: {group['name']} ({group['device_count']} devices)")
    typer.echo("Locations:")
    for location in report["locations"]:
        typer.echo(
            f"  {location['id']}: {location['location']} ({location['device_count']} devices)"
        )


@app.command("schema-check")
def schema_check(
    config_path: Annotated[
        str,
        typer.Option("--config", "-c", help="Path to libremapper YAML or JSON config."),
    ] = DEFAULT_CONFIG,
) -> None:
    """Validate only the LibreNMS Custom Map database schema."""
    try:
        config = load_config(config_path)
        errors = LibreNMSRepository(create_librenms_engine(config.librenms)).validate_schema()
    except Exception as error:
        typer.echo(f"Schema check failed: {error}", err=True)
        raise typer.Exit(1) from error
    if errors:
        for validation_error in errors:
            typer.echo(f"ERROR: {validation_error}", err=True)
        raise typer.Exit(1)
    typer.echo("LibreNMS Custom Map schema is healthy.")


@app.command()
def plan(
    map_key: Annotated[str, typer.Argument(help="Configured map key.")],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
    relayout: Annotated[
        bool, typer.Option("--relayout", help="Replace existing generated coordinates.")
    ] = False,
    adopt: Annotated[
        bool, typer.Option("--adopt", help="Adopt a matching existing LibreNMS map.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the plan as JSON.")
    ] = None,
) -> None:
    """Build and display a deterministic reconciliation plan."""
    try:
        with _locked_service(config_path) as (_, service):
            generated = service.create_plan(map_key, relayout=relayout, adopt=adopt)
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(generated.model_dump_json(indent=2))
    except Exception as error:
        typer.echo(f"Unable to create plan: {error}", err=True)
        raise typer.Exit(1) from error
    _print_plan(generated)
    if output:
        typer.echo(f"Wrote plan to {output}.")


@app.command()
def apply(
    target: Annotated[str, typer.Argument(help="Configured map key or saved plan JSON.")],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Build and display a plan without writing.")
    ] = False,
    relayout: Annotated[
        bool, typer.Option("--relayout", help="Replace existing generated coordinates.")
    ] = False,
    adopt: Annotated[
        bool, typer.Option("--adopt", help="Adopt a matching existing LibreNMS map.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Apply without interactive confirmation.")
    ] = False,
) -> None:
    """Apply a fresh map plan or a previously saved plan."""
    try:
        with _locked_service(config_path) as (_, service):
            target_path = Path(target)
            if target_path.is_file():
                generated = Plan.model_validate_json(target_path.read_text())
            else:
                generated = service.create_plan(target, relayout=relayout, adopt=adopt)
            _print_plan(generated)
            if dry_run:
                return
            if (
                generated.changes
                and not yes
                and not typer.confirm("Apply these changes to LibreNMS?")
            ):
                raise typer.Abort()
            service.apply(generated)
    except typer.Abort:
        raise
    except Exception as error:
        typer.echo(f"Apply failed: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Plan applied successfully.")


@app.command()
def reconcile(
    map_key: Annotated[str | None, typer.Argument(help="Optional scheduled map key.")] = None,
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
) -> None:
    """Reconcile initialized maps whose configuration enables scheduling."""
    failed = False
    try:
        with _locked_service(config_path) as (config, service):
            errors = service.validate(structural=True)
            if errors:
                raise RuntimeError("; ".join(errors))
            configured = {str(item.key): item for item in config.maps}
            if map_key is not None and map_key not in configured:
                raise RuntimeError(f"Unknown map: {map_key}")
            selected = [
                item
                for item in config.maps
                if item.scheduled and service.state.map_record(str(item.key)) is not None
            ]
            if map_key is not None:
                selected = [item for item in selected if item.key == map_key]
                if not selected:
                    raise RuntimeError(
                        f"Map {map_key} must be scheduled and successfully applied once first."
                    )
            for map_config in selected:
                key = str(map_config.key)
                try:
                    if service.has_draft(key):
                        typer.echo(
                            f"{key}: unpublished draft; review and publish before scheduling."
                        )
                        continue
                    generated = service.create_plan(key)
                    if generated.conflicts:
                        raise RuntimeError("Plan has conflicts.")
                    if generated.warnings:
                        raise RuntimeError("Plan has warnings; review and apply it manually.")
                    if generated.changes:
                        service.apply(generated)
                    typer.echo(f"{key} reconciled ({generated.changes} change(s))")
                except Exception as error:
                    failed = True
                    typer.echo(f"{key}: {error}", err=True)
    except Exception as error:
        typer.echo(f"Reconciliation failed: {error}", err=True)
        raise typer.Exit(1) from error
    if failed:
        raise typer.Exit(1)


@state_app.command("show")
def state_show(
    map_key: Annotated[str, typer.Argument(help="Configured map key.")],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
) -> None:
    """Show the owned map and object mappings from SQLite."""
    try:
        config = load_config(config_path)
        data = StateStore(config.state_path).show(map_key)
    except Exception as error:
        typer.echo(f"Unable to read state: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@provider_app.command("inspect")
def provider_inspect(
    name: Annotated[str, typer.Argument(help="Configured provider name.")],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")] = False,
    map_key: Annotated[str | None, typer.Option("--map")] = None,
) -> None:
    """Inspect normalized artifact topology and parser diagnostics."""
    try:
        _, service = _service(config_path)
        report = service.inspect_provider(name, map_key)
    except Exception as error:
        typer.echo(f"Unable to inspect provider: {error}", err=True)
        raise typer.Exit(1) from error
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(
        f"Provider {name!r}: {len(report.matched_files)} file(s), "
        f"{len(report.topology.devices)} device(s), "
        f"{len(report.topology.links)} link(s), blocked={report.blocked}"
    )
    for path, rule in report.matched_rules.items():
        typer.echo(f"  {path}: {rule}")
    for warning in report.warnings:
        typer.echo(f"WARNING: {warning}", err=True)


@provider_app.command("catalog")
def provider_catalog(
    config_path: Annotated[str, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
) -> None:
    """Read global parser definitions and map usage without fetching Git."""
    try:
        result = describe_catalog(load_config(config_path))
    except Exception:
        typer.echo("Unable to load parser catalog; check configuration structure.", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))


@provider_app.command("preview")
def provider_preview(
    config_path: Annotated[str, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
    list_files: Annotated[bool, typer.Option("--list-files")] = False,
) -> None:
    """Test an unsaved parser received on stdin; never change configuration."""
    try:
        config = load_config(config_path)
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw.encode()) > 1024 * 1024:
            raise ValueError("preview input exceeds 1 MiB")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("preview input must be an object")
        engine = create_librenms_engine(config.librenms)
        try:
            result = parser_preview(config, engine, payload, list_files=list_files)
        finally:
            engine.dispose()
    except Exception as error:
        # Pydantic errors include input values, which can contain pasted configurations.
        message = (
            str(error)
            if isinstance(error, RuntimeError)
            else "Invalid preview input or parser settings."
        )
        typer.echo(message, err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result))


@provider_app.command("test-source")
def provider_test_source(
    name: str,
    config_path: Annotated[str, typer.Option("--config", "-c")] = DEFAULT_CONFIG,
) -> None:
    """Verify Git access independently of map activation and parser output."""
    from libremapper.providers.oxidized import OxidizedGitProvider

    config = load_config(config_path)
    settings = config.providers[name]
    if settings.type != "oxidized":
        raise typer.BadParameter("source must use Oxidized")
    from libremapper.database import create_librenms_engine

    engine = create_librenms_engine(config.librenms)
    try:
        provider = OxidizedGitProvider(name, engine, settings.settings, config.oxidized_parsers)
        result = provider.test_connection()
    except Exception as error:
        typer.echo(f"Unable to test source: {error}", err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(json.dumps(result))


@links_app.command("plan")
def links_plan(
    provider: Annotated[str, typer.Argument(help="regex_artifacts provider name.")],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the plan as JSON.")
    ] = None,
) -> None:
    """Plan experimental LibreNMS links materialization."""
    try:
        with _locked_service(config_path) as (_, service):
            generated = service.create_link_plan(provider)
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(generated.model_dump_json(indent=2))
    except Exception as error:
        typer.echo(f"Unable to create link plan: {error}", err=True)
        raise typer.Exit(1) from error
    _print_link_plan(generated)
    if output:
        typer.echo(f"Wrote link plan to {output}.")
    if generated.blocked:
        raise typer.Exit(1)


@links_app.command("sync")
def links_sync(
    target: Annotated[
        str,
        typer.Argument(help="regex_artifacts provider name or saved link plan JSON."),
    ],
    config_path: Annotated[
        str, typer.Option("--config", "-c", help="Configuration file.")
    ] = DEFAULT_CONFIG,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Synchronize without confirmation.")
    ] = False,
) -> None:
    """Synchronize owned lm-artifact rows through the LibreNMS PHP adapter."""
    try:
        with _locked_service(config_path) as (_, service):
            target_path = Path(target)
            if target_path.is_file():
                generated = LinkSyncPlan.model_validate_json(target_path.read_text())
            else:
                generated = service.create_link_plan(target)
            _print_link_plan(generated)
            if generated.blocked:
                raise typer.Exit(1)
            if generated.changes == 0:
                return
            if not yes and not typer.confirm("Synchronize these LibreNMS links?"):
                raise typer.Abort()
            service.sync_links(generated)
    except (typer.Abort, typer.Exit):
        raise
    except Exception as error:
        typer.echo(f"Link synchronization failed: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo("Native links synchronized successfully.")


def main() -> None:
    drop_privileges()
    app()
