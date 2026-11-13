from __future__ import annotations

import fnmatch
import io
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Engine

from libremapper.config import SourceConfig
from libremapper.models import Device, ProviderInspection, Topology, stable_hash
from libremapper.parser_definitions import (
    BUILTIN_PARSERS,
    ParserDefinition,
    parser_catalog,
)
from libremapper.parser_definitions import (
    SAOS10_NEIGHBORS as SAOS10_NEIGHBORS,
)
from libremapper.parser_execution import execute_parser
from libremapper.providers.base import TopologyProvider
from libremapper.providers.file import select_topology

MAX_TOPOLOGY_BYTES = 50 * 1024 * 1024
SAFE_REF = re.compile(r"(?:HEAD|[A-Za-z0-9][A-Za-z0-9._/-]*)")
SCP_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]+")
PARSERS = BUILTIN_PARSERS


class OxidizedGitSettings(BaseModel):
    repository: str
    ref: str = "HEAD"
    private_key: Path | None = None
    known_hosts: Path | None = None
    accept_new_host_key: bool = False
    fetch_timeout: int = Field(default=30, ge=1, le=300)
    parsers: list[str] = Field(default_factory=lambda: ["saos10-lldp-neighbors"])

    @model_validator(mode="after")
    def validate_settings(self) -> OxidizedGitSettings:
        if not self.parsers or len(set(self.parsers)) != len(self.parsers):
            raise ValueError("select at least one parser without duplicates")
        self.repository = self.repository.strip()
        self.ref = self.ref.strip()
        if not self.repository or self.repository.startswith("-"):
            raise ValueError("repository must be a non-empty Git path or URL")
        if SAFE_REF.fullmatch(self.ref) is None or ".." in self.ref or self.ref.endswith("/"):
            raise ValueError("ref must be a safe branch, tag, commit, or HEAD")

        local = self.repository.startswith("/")
        ssh = self.repository.startswith("ssh://") or SCP_REPOSITORY.fullmatch(self.repository)
        if self.repository.startswith("ssh://") and urlsplit(self.repository).password:
            raise ValueError("repository credentials must not be stored in configuration")
        if not local and not ssh:
            parsed = urlsplit(self.repository)
            if parsed.scheme not in {"https", "file"}:
                raise ValueError("repository must use an absolute path, SSH, HTTPS, or file URL")
            if parsed.username or parsed.password:
                raise ValueError("repository credentials must not be stored in configuration")
        if local and (self.private_key or self.known_hosts):
            raise ValueError("local repositories do not use SSH credentials")
        if ssh and (self.private_key is None or self.known_hosts is None):
            raise ValueError("SSH repositories require private_key and known_hosts")
        if not local and not ssh and (self.private_key or self.known_hosts):
            raise ValueError("only SSH repositories use private_key and known_hosts")
        if (self.private_key is None) != (self.known_hosts is None):
            raise ValueError("private_key and known_hosts must be configured together")
        for path, label in (
            (self.private_key, "private_key"),
            (self.known_hosts, "known_hosts"),
        ):
            if path is not None and not path.is_absolute():
                raise ValueError(f"{label} must be an absolute path")
        return self


class GitTopologySnapshot:
    def __init__(
        self,
        provider: str,
        settings: OxidizedGitSettings,
        definitions: dict[str, ParserDefinition] | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.definitions = definitions or PARSERS
        cache_root = Path(os.getenv("LIBREMAPPER_GIT_CACHE", "/data/libremapper/git"))
        cache_key = stable_hash(
            {
                "provider": provider,
                "repository": settings.repository,
                "parsers": {
                    name: self.definitions[name].model_dump(mode="json")
                    for name in sorted(settings.parsers)
                },
            }
        )[:20]
        self.cache = cache_root / cache_key

    @contextmanager
    def open(self) -> Iterator[Path]:
        self.cache.mkdir(parents=True, exist_ok=True)
        with (self.cache / "fetch.lock").open("a+b") as lock:
            flock(lock.fileno(), LOCK_EX)
            try:
                repository = self._repository()
                commit = (
                    self._git(
                        repository,
                        "rev-parse",
                        "--verify",
                        f"{self.settings.ref}^{{commit}}",
                    )
                    .decode()
                    .strip()
                )
                if re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
                    raise RuntimeError("Git returned an invalid commit identifier")
                snapshot = self.cache / "snapshots" / commit
                if not (snapshot / ".complete").is_file():
                    self._extract(repository, commit, snapshot)
                yield snapshot
            finally:
                flock(lock.fileno(), LOCK_UN)

    def _repository(self) -> Path:
        repository = self.settings.repository
        if repository.startswith("/"):
            path = Path(repository)
            if not path.is_dir():
                raise RuntimeError(f"local Oxidized Git repository is unavailable: {path}")
            return path / ".git" if (path / ".git").is_dir() else path

        mirror = self.cache / "repository.git"
        if not mirror.is_dir():
            with TemporaryDirectory(prefix="clone-", dir=self.cache) as temporary:
                candidate = Path(temporary) / "repository.git"
                self._run(
                    "clone",
                    "--mirror",
                    "--",
                    repository,
                    str(candidate),
                )
                candidate.replace(mirror)
        else:
            self._git(mirror, "remote", "set-url", "origin", repository)
            self._git(mirror, "fetch", "--prune", "origin")
        return mirror

    def _extract(self, repository: Path, commit: str, snapshot: Path) -> None:
        snapshots = snapshot.parent
        snapshots.mkdir(parents=True, exist_ok=True)
        entries = self._git(repository, "ls-tree", "-z", commit).decode().split("\0")
        roots = {}
        for entry in entries:
            if entry:
                metadata, name = entry.split("\t", 1)
                roots[name] = metadata.split()[1]
        selected = [self.definitions[name] for name in self.settings.parsers]
        paths = sorted(
            {
                name
                for name, kind in roots.items()
                if any(
                    (
                        kind == "tree"
                        and (
                            pattern.split("/")[0] == "**"
                            or ("/" in pattern and fnmatch.fnmatchcase(name, pattern.split("/")[0]))
                        )
                    )
                    or (
                        kind == "blob"
                        and (
                            pattern.startswith("**/")
                            or ("/" not in pattern and fnmatch.fnmatchcase(name, pattern))
                        )
                    )
                    for parser in selected
                    for pattern in parser.file_globs
                )
            }
        )
        if not paths:
            raise RuntimeError("Oxidized Git repository has no files for the selected parsers")
        archive = self._git(repository, "archive", "--format=tar", commit, "--", *paths)
        with TemporaryDirectory(prefix="snapshot-", dir=snapshots) as temporary:
            target = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as contents:
                members = contents.getmembers()
                size = 0
                for member in members:
                    path = PurePosixPath(member.name)
                    if (
                        not path.parts
                        or path.parts[0] not in paths
                        or any(part in {"", ".", ".."} for part in path.parts)
                        or not (member.isfile() or member.isdir())
                    ):
                        raise RuntimeError("Oxidized Git archive contains an unsafe entry")
                    size += member.size
                    member.mode = 0o700 if member.isdir() else 0o600
                if size > MAX_TOPOLOGY_BYTES:
                    raise RuntimeError("Oxidized parser archive exceeds 50 MiB")
                contents.extractall(target, members=members, filter="data")
            (target / ".complete").write_text("complete\n")
            target.replace(snapshot)

    def mark_successful(self, snapshot: Path) -> None:
        for candidate in snapshot.parent.iterdir():
            if candidate != snapshot and candidate.is_dir():
                shutil.rmtree(candidate)

    def discard_failed(self, snapshot: Path) -> None:
        has_fallback = any(
            candidate != snapshot and candidate.is_dir() for candidate in snapshot.parent.iterdir()
        )
        if has_fallback:
            shutil.rmtree(snapshot)

    def _git(self, repository: Path, *arguments: str) -> bytes:
        return self._run(
            "-c",
            f"safe.directory={repository}",
            f"--git-dir={repository}",
            *arguments,
        )

    def _run(self, *arguments: str) -> bytes:
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        if self.settings.private_key is not None:
            key = self.settings.private_key
            known_hosts = self.settings.known_hosts
            assert known_hosts is not None
            if not key.is_file():
                raise RuntimeError(f"Oxidized Git private key is unavailable: {key}")
            if not known_hosts.is_file():
                raise RuntimeError(f"Oxidized Git known_hosts is unavailable: {known_hosts}")
            if key.stat().st_mode & 0o077:
                raise RuntimeError("Oxidized Git private key must be accessible only by its owner")
            environment["GIT_SSH_COMMAND"] = " ".join(
                [
                    "ssh",
                    "-F",
                    "/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "StrictHostKeyChecking="
                    + ("accept-new" if self.settings.accept_new_host_key else "yes"),
                    "-o",
                    f"UserKnownHostsFile={shlex.quote(str(known_hosts))}",
                    "-i",
                    shlex.quote(str(key)),
                ]
            )
        try:
            result = subprocess.run(
                ["git", *arguments],
                capture_output=True,
                timeout=self.settings.fetch_timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Oxidized Git operation timed out") from error
        except OSError as error:
            raise RuntimeError(f"unable to run Git: {error}") from error
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip().splitlines()
            detail = message[-1] if message else "unknown error"
            raise RuntimeError(f"Oxidized Git operation failed: {detail}")
        return result.stdout


class OxidizedGitProvider(TopologyProvider):
    def __init__(
        self,
        name: str,
        engine: Engine,
        settings: dict[str, Any],
        definitions: dict[str, ParserDefinition] | None = None,
    ) -> None:
        super().__init__(name)
        self.engine = engine
        self.settings = OxidizedGitSettings.model_validate(settings)
        self.definitions = parser_catalog(definitions or {})
        if set(self.settings.parsers) - self.definitions.keys():
            raise ValueError("unknown Oxidized parser")
        self.repository = GitTopologySnapshot(name, self.settings, self.definitions)

    def selected(self, source: SourceConfig | None) -> list[str]:
        return self.settings.parsers if source is None or source.parsers is None else source.parsers

    def snapshot(self, selected: list[str]) -> GitTopologySnapshot:
        settings = self.settings.model_copy(update={"parsers": selected})
        return GitTopologySnapshot(self.name, settings, self.definitions)

    def test_connection(self) -> dict[str, str]:
        self.repository.cache.mkdir(parents=True, exist_ok=True)
        with (self.repository.cache / "fetch.lock").open("a+b") as lock:
            flock(lock.fileno(), LOCK_EX)
            try:
                repository = self.repository._repository()
                revision = (
                    self.repository._git(
                        repository, "rev-parse", "--verify", f"{self.settings.ref}^{{commit}}"
                    )
                    .decode()
                    .strip()
                )
            finally:
                flock(lock.fileno(), LOCK_UN)
        if re.fullmatch(r"[0-9a-f]{40,64}", revision) is None:
            raise RuntimeError("Git returned an invalid commit identifier")
        return {"revision": revision}

    def load(self, source: SourceConfig) -> Topology:
        inspection = self.inspect(source)
        if inspection.blocked:
            raise RuntimeError(
                f"provider {self.name}: zero_output_guard blocked planning because "
                f"{len(inspection.matched_files)} matching artifact(s) produced no valid links"
            )
        return inspection.topology

    def inspect(self, source: SourceConfig | None = None) -> ProviderInspection:
        selected = self.selected(source)
        inspection = ProviderInspection(
            provider=self.name,
            provider_type="oxidized",
            topology=Topology(provider=self.name),
        )
        if not selected:
            return inspection
        if set(selected) - self.definitions.keys():
            raise ValueError("unknown Oxidized parser")
        repository = self.snapshot(selected)
        with repository.open() as root:
            try:
                devices: dict[str, Device] = {}
                for parser in selected:
                    definition = self.definitions[parser]
                    report, _ = execute_parser(self.name, parser, definition, root, self.engine)
                    scoped = (
                        select_topology(report.topology, source)
                        if source is not None
                        else report.topology
                    )
                    inspection.parser_reports.append(
                        {
                            "name": parser,
                            "label": definition.label,
                            "matched_files": len(report.matched_files),
                            "links": len(scoped.links),
                            "blocked": report.blocked,
                            "unbound_interfaces": sum(
                                interface.librenms_port_id is None
                                for device in scoped.devices
                                for interface in device.interfaces
                            ),
                        }
                    )
                    inspection.blocked |= report.blocked
                    inspection.matched_files.extend(report.matched_files)
                    inspection.matched_rules.update(report.matched_rules)
                    inspection.skipped_files.extend(report.skipped_files)
                    inspection.resolution_failures.extend(report.resolution_failures)
                    inspection.warnings.extend(report.warnings)
                    inspection.topology.links.extend(report.topology.links)
                    inspection.topology.warnings.extend(report.topology.warnings)
                    for device in report.topology.devices:
                        if device.source_id not in devices:
                            devices[device.source_id] = device
                        else:
                            current = devices[device.source_id]
                            known = {interface.source_id for interface in current.interfaces}
                            current.interfaces.extend(
                                interface
                                for interface in device.interfaces
                                if interface.source_id not in known
                            )
                inspection.topology.devices = list(devices.values())
            except Exception:
                repository.discard_failed(root)
                raise
            if inspection.blocked:
                repository.discard_failed(root)
            else:
                repository.mark_successful(root)
            inspection.revision = root.name
        if source is not None:
            inspection.topology = select_topology(inspection.topology, source)
        return inspection
