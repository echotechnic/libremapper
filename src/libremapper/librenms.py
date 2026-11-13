from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, bindparam, inspect, text

from libremapper.database import row_dict
from libremapper.models import stable_hash

REQUIRED_COLUMNS = {
    "custom_maps": {
        "custom_map_id",
        "name",
        "width",
        "height",
        "node_align",
        "reverse_arrows",
        "edge_separation",
        "options",
        "newnodeconfig",
        "newedgeconfig",
        "background_type",
        "background_data",
    },
    "custom_map_nodes": {
        "custom_map_node_id",
        "custom_map_id",
        "device_id",
        "label",
        "style",
        "size",
        "x_pos",
        "y_pos",
    },
    "custom_map_edges": {
        "custom_map_edge_id",
        "custom_map_id",
        "custom_map_node1_id",
        "custom_map_node2_id",
        "port_id",
        "mid_x",
        "mid_y",
    },
}


class MapSnapshot(BaseModel):
    map: dict[str, Any] | None = None
    nodes: dict[int, dict[str, Any]] = Field(default_factory=dict)
    edges: dict[int, dict[str, Any]] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        return stable_hash(
            {
                "map": _normalized_row(self.map),
                "nodes": {
                    key: _normalized_row(value) for key, value in sorted(self.nodes.items())
                },
                "edges": {
                    key: _normalized_row(value) for key, value in sorted(self.edges.items())
                },
            }
        )


def _normalized_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        key: value
        for key, value in row.items()
        if key not in {"created_at", "updated_at"}
    }


class LibreNMSRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def validate_schema(self) -> list[str]:
        inspector = inspect(self.engine)
        errors: list[str] = []
        for table, required in REQUIRED_COLUMNS.items():
            if not inspector.has_table(table):
                errors.append(f"missing LibreNMS table: {table}")
                continue
            actual = {column["name"] for column in inspector.get_columns(table)}
            missing = sorted(required - actual)
            if missing:
                errors.append(f"{table} is missing columns: {', '.join(missing)}")
        return errors

    def traffic_ports(self, port_ids: set[int]) -> dict[int, dict[str, Any]]:
        if not port_ids or not inspect(self.engine).has_table("ports"):
            return {}
        with self.engine.connect() as connection:
            return {
                int(row["port_id"]): dict(row)
                for row in connection.execute(
                    text("SELECT * FROM ports WHERE port_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ), {"ids": sorted(port_ids)}
                ).mappings()
            }

    def snapshot(self, map_name: str, map_id: int | None = None) -> MapSnapshot:
        with self.engine.connect() as connection:
            return self.snapshot_in(connection, map_name, map_id)

    def snapshot_in(
        self, connection: Connection, map_name: str, map_id: int | None = None
    ) -> MapSnapshot:
        if map_id is not None:
            map_row = connection.execute(
                text("SELECT * FROM custom_maps WHERE custom_map_id = :id"),
                {"id": map_id},
            ).first()
        else:
            rows = connection.execute(
                text("SELECT * FROM custom_maps WHERE name = :name ORDER BY custom_map_id"),
                {"name": map_name},
            ).all()
            if len(rows) > 1:
                raise RuntimeError(f"multiple LibreNMS Custom Maps are named {map_name!r}")
            map_row = rows[0] if rows else None
        if map_row is None:
            return MapSnapshot()
        map_data = row_dict(map_row)
        actual_id = int(map_data["custom_map_id"])
        node_rows = connection.execute(
            text("SELECT * FROM custom_map_nodes WHERE custom_map_id = :id"),
            {"id": actual_id},
        ).all()
        edge_rows = connection.execute(
            text("SELECT * FROM custom_map_edges WHERE custom_map_id = :id"),
            {"id": actual_id},
        ).all()
        return MapSnapshot(
            map=map_data,
            nodes={
                int(row.custom_map_node_id): row_dict(row) for row in node_rows
            },
            edges={
                int(row.custom_map_edge_id): row_dict(row) for row in edge_rows
            },
        )

    @staticmethod
    def create_map(connection: Connection, values: dict[str, Any]) -> int:
        now = datetime.now(UTC)
        payload = {
            "name": values["name"],
            "menu_group": values.get("menu_group"),
            "width": values["width"],
            "height": values["height"],
            "node_align": values["node_align"],
            "reverse_arrows": int(values["reverse_arrows"]),
            "edge_separation": values["edge_separation"],
            "legend_x": -1,
            "legend_y": -1,
            "legend_steps": 7,
            "legend_font_size": 14,
            "legend_hide_invalid": 0,
            "legend_hide_overspeed": 0,
            "legend_colours": None,
            "options": json.dumps(values["options"]),
            "newnodeconfig": json.dumps(values["newnodeconfig"]),
            "newedgeconfig": json.dumps(values["newedgeconfig"]),
            "background_type": "none",
            "background_data": None,
            "created_at": now,
            "updated_at": now,
        }
        result = connection.execute(
            text(
                """
                INSERT INTO custom_maps (
                    name, menu_group, width, height, node_align, reverse_arrows,
                    edge_separation, legend_x, legend_y, legend_steps,
                    legend_font_size, legend_hide_invalid, legend_hide_overspeed,
                    legend_colours, options, newnodeconfig, newedgeconfig,
                    background_type, background_data, created_at, updated_at
                ) VALUES (
                    :name, :menu_group, :width, :height, :node_align, :reverse_arrows,
                    :edge_separation, :legend_x, :legend_y, :legend_steps,
                    :legend_font_size, :legend_hide_invalid, :legend_hide_overspeed,
                    :legend_colours, :options, :newnodeconfig, :newedgeconfig,
                    :background_type, :background_data, :created_at, :updated_at
                )
                """
            ),
            payload,
        )
        if result.lastrowid is None:
            raise RuntimeError("LibreNMS did not return the new custom map ID")
        return int(result.lastrowid)

    @staticmethod
    def update_map(connection: Connection, map_id: int, values: dict[str, Any]) -> None:
        payload = dict(values)
        for field in ("options", "newnodeconfig", "newedgeconfig"):
            if field in payload and not isinstance(payload[field], str):
                payload[field] = json.dumps(payload[field])
        allowed = {
            "name",
            "menu_group",
            "width",
            "height",
            "node_align",
            "reverse_arrows",
            "edge_separation",
            "options",
            "newnodeconfig",
            "newedgeconfig",
        }
        LibreNMSRepository._update_row(
            connection, "custom_maps", "custom_map_id", map_id, payload, allowed
        )

    @staticmethod
    def create_node(connection: Connection, map_id: int, values: dict[str, Any]) -> int:
        payload = dict(values)
        payload.update(
            {
                "custom_map_id": map_id,
                "linked_custom_map_id": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        result = connection.execute(
            text(
                """
                INSERT INTO custom_map_nodes (
                    custom_map_id, device_id, linked_custom_map_id, label, style,
                    icon, image, node_image_id, size, border_width, text_face,
                    text_size, text_colour, colour_bg, colour_bdr, x_pos, y_pos,
                    created_at, updated_at
                ) VALUES (
                    :custom_map_id, :device_id, :linked_custom_map_id, :label, :style,
                    :icon, :image, :node_image_id, :size, :border_width, :text_face,
                    :text_size, :text_colour, :colour_bg, :colour_bdr, :x_pos, :y_pos,
                    :created_at, :updated_at
                )
                """
            ),
            payload,
        )
        if result.lastrowid is None:
            raise RuntimeError("LibreNMS did not return the new custom map node ID")
        return int(result.lastrowid)

    @staticmethod
    def update_node(
        connection: Connection, node_id: int, values: dict[str, Any]
    ) -> None:
        allowed = {
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
        LibreNMSRepository._update_row(
            connection,
            "custom_map_nodes",
            "custom_map_node_id",
            node_id,
            values,
            allowed,
        )

    @staticmethod
    def create_edge(connection: Connection, map_id: int, values: dict[str, Any]) -> int:
        payload = dict(values)
        payload.update(
            {
                "custom_map_id": map_id,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        result = connection.execute(
            text(
                """
                INSERT INTO custom_map_edges (
                    custom_map_id, custom_map_node1_id, custom_map_node2_id, port_id,
                    reverse, style, showpct, showbps, label, fixed_width, text_face,
                    text_size, text_colour, text_align, mid_x, mid_y, created_at, updated_at
                ) VALUES (
                    :custom_map_id, :custom_map_node1_id, :custom_map_node2_id, :port_id,
                    :reverse, :style, :showpct, :showbps, :label, :fixed_width, :text_face,
                    :text_size, :text_colour, :text_align, :mid_x, :mid_y, :created_at,
                    :updated_at
                )
                """
            ),
            payload,
        )
        if result.lastrowid is None:
            raise RuntimeError("LibreNMS did not return the new custom map edge ID")
        return int(result.lastrowid)

    @staticmethod
    def update_edge(
        connection: Connection, edge_id: int, values: dict[str, Any]
    ) -> None:
        allowed = {
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
        LibreNMSRepository._update_row(
            connection,
            "custom_map_edges",
            "custom_map_edge_id",
            edge_id,
            values,
            allowed,
        )

    @staticmethod
    def delete_row(
        connection: Connection, table: str, id_column: str, row_id: int
    ) -> None:
        allowed = {
            ("custom_map_nodes", "custom_map_node_id"),
            ("custom_map_edges", "custom_map_edge_id"),
        }
        if (table, id_column) not in allowed:
            raise ValueError("unsupported delete target")
        connection.execute(
            text(f"DELETE FROM {table} WHERE {id_column} = :id"),  # noqa: S608
            {"id": row_id},
        )

    @staticmethod
    def _update_row(
        connection: Connection,
        table: str,
        id_column: str,
        row_id: int,
        values: dict[str, Any],
        allowed: Iterable[str],
    ) -> None:
        payload = {key: value for key, value in values.items() if key in allowed}
        if not payload:
            return
        payload["updated_at"] = datetime.now(UTC)
        payload["row_id"] = row_id
        assignments = ", ".join(f"{key} = :{key}" for key in payload if key != "row_id")
        connection.execute(
            text(
                f"UPDATE {table} SET {assignments} "  # noqa: S608
                f"WHERE {id_column} = :row_id"
            ),
            payload,
        )
