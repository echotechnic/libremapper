from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS managed_maps (
                    map_key TEXT PRIMARY KEY,
                    map_name TEXT NOT NULL,
                    librenms_map_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_objects (
                    map_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('node', 'edge')),
                    canonical_id TEXT NOT NULL,
                    librenms_id INTEGER NOT NULL,
                    generated_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    source_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (map_key, kind, canonical_id),
                    FOREIGN KEY (map_key) REFERENCES managed_maps(map_key)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    map_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS managed_links (
                    provider TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    librenms_link_id INTEGER NOT NULL,
                    natural_key TEXT NOT NULL,
                    generated_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, source_id)
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(managed_objects)")}
            if "metadata_json" not in columns:
                connection.execute(
                    "ALTER TABLE managed_objects ADD COLUMN "
                    "metadata_json TEXT NOT NULL DEFAULT '{}'"
                )

    def map_record(self, map_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM managed_maps WHERE map_key = ?", (map_key,)
            ).fetchone()
            return dict(row) if row else None

    def objects(self, map_key: str, kind: str | None = None) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            if kind:
                rows = connection.execute(
                    "SELECT * FROM managed_objects WHERE map_key = ? AND kind = ?",
                    (map_key, kind),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM managed_objects WHERE map_key = ?", (map_key,)
                ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["generated"] = json.loads(item.pop("generated_json"))
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result[f"{item['kind']}:{item['canonical_id']}"] = item
        return result

    def show(self, map_key: str) -> dict[str, Any]:
        return {
            "map": self.map_record(map_key),
            "objects": list(self.objects(map_key).values()),
        }

    def links(self, provider: str) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM managed_links WHERE provider = ?", (provider,)
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["generated"] = json.loads(item.pop("generated_json"))
            result[str(item["source_id"])] = item
        return result

    def start_run(self, map_key: str, plan_hash: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (map_key, status, plan_hash, created_at)
                VALUES (?, 'pending', ?, ?)
                """,
                (map_key, plan_hash, _now()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a run ID")
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, details: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, details = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (status, details, _now(), run_id),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def save_map(
        connection: sqlite3.Connection, map_key: str, map_name: str, map_id: int
    ) -> None:
        now = _now()
        connection.execute(
            """
            INSERT INTO managed_maps (
                map_key, map_name, librenms_map_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(map_key) DO UPDATE SET
                map_name = excluded.map_name,
                librenms_map_id = excluded.librenms_map_id,
                updated_at = excluded.updated_at
            """,
            (map_key, map_name, map_id, now, now),
        )

    @staticmethod
    def save_object(
        connection: sqlite3.Connection,
        map_key: str,
        kind: str,
        canonical_id: str,
        librenms_id: int,
        generated: dict[str, Any],
        source_hash: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO managed_objects (
                map_key, kind, canonical_id, librenms_id, generated_json,
                source_hash, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(map_key, kind, canonical_id) DO UPDATE SET
                librenms_id = excluded.librenms_id,
                generated_json = excluded.generated_json,
                source_hash = excluded.source_hash,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                map_key,
                kind,
                canonical_id,
                librenms_id,
                json.dumps(generated, sort_keys=True, default=str),
                source_hash,
                _now(),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    @staticmethod
    def delete_object(
        connection: sqlite3.Connection, map_key: str, kind: str, canonical_id: str
    ) -> None:
        connection.execute(
            """
            DELETE FROM managed_objects
            WHERE map_key = ? AND kind = ? AND canonical_id = ?
            """,
            (map_key, kind, canonical_id),
        )

    @staticmethod
    def save_link(
        connection: sqlite3.Connection,
        provider: str,
        source_id: str,
        link_id: int,
        natural_key: str,
        generated: dict[str, Any],
        source_hash: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO managed_links (
                provider, source_id, librenms_link_id, natural_key,
                generated_json, source_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, source_id) DO UPDATE SET
                librenms_link_id = excluded.librenms_link_id,
                natural_key = excluded.natural_key,
                generated_json = excluded.generated_json,
                source_hash = excluded.source_hash,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                source_id,
                link_id,
                natural_key,
                json.dumps(generated, sort_keys=True, default=str),
                source_hash,
                _now(),
            ),
        )

    @staticmethod
    def delete_link(
        connection: sqlite3.Connection, provider: str, source_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM managed_links WHERE provider = ? AND source_id = ?",
            (provider, source_id),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()
