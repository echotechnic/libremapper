from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, inspect, text


def build_inventory(engine: Engine) -> dict[str, Any]:
    schema = inspect(engine)
    tables = set(schema.get_table_names())
    counts: dict[str, int] = {}
    devices: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    locations: list[dict[str, Any]] = []
    device_columns = (
        {column["name"] for column in schema.get_columns("devices")}
        if "devices" in tables
        else set()
    )
    with engine.connect() as connection:
        for table in ("devices", "ports", "links"):
            if table in tables:
                counts[table] = int(
                    connection.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
                )
        if {"device_groups", "device_group_device"} <= tables:
            groups = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT dg.id, dg.name, COUNT(dgd.device_id) AS device_count
                        FROM device_groups dg
                        LEFT JOIN device_group_device dgd
                          ON dgd.device_group_id = dg.id
                        GROUP BY dg.id, dg.name
                        ORDER BY dg.name, dg.id
                        """
                    )
                ).mappings()
            ]
        if "locations" in tables and "location_id" in device_columns:
            locations = [
                dict(row)
                for row in connection.execute(
                    text(
                        """
                        SELECT l.id, l.location, COUNT(d.device_id) AS device_count
                        FROM locations l
                        LEFT JOIN devices d ON d.location_id = l.id
                        GROUP BY l.id, l.location
                        ORDER BY l.location, l.id
                        """
                    )
                ).mappings()
            ]
        if "device_id" in device_columns:
            columns = [
                f"d.`{column}` AS `{name}`" if column in device_columns else f"NULL AS `{name}`"
                for column, name in (
                    ("device_id", "device_id"),
                    ("hostname", "hostname"),
                    ("sysName", "sys_name"),
                    ("os", "os"),
                    ("hardware", "hardware"),
                    ("type", "type"),
                )
            ]
            join = ""
            if "locations" in tables and "location_id" in device_columns:
                columns.append("l.location")
                join = "LEFT JOIN locations l ON l.id = d.location_id"
            else:
                columns.append("d.location" if "location" in device_columns else "NULL AS location")
            devices = [
                {**dict(row), "groups": [], "discovered_link_count": 0}
                for row in connection.execute(
                    text(f"SELECT {', '.join(columns)} FROM devices d {join} ORDER BY d.device_id")
                ).mappings()
            ]
            devices_by_id = {device["device_id"]: device for device in devices}
            if {"device_groups", "device_group_device"} <= tables:
                for row in connection.execute(
                    text(
                        "SELECT dgd.device_id, dg.id, dg.name FROM device_group_device dgd "
                        "JOIN device_groups dg ON dg.id = dgd.device_group_id "
                        "ORDER BY dg.name, dg.id"
                    )
                ).mappings():
                    if row["device_id"] in devices_by_id:
                        devices_by_id[row["device_id"]]["groups"].extend(
                            [str(row["id"]), str(row["name"])]
                        )
            if "links" in tables:
                link_columns = {column["name"] for column in schema.get_columns("links")}
                if {"local_device_id", "remote_device_id"} <= link_columns:
                    active = "WHERE active = 1" if "active" in link_columns else ""
                    for row in connection.execute(
                        text(f"SELECT local_device_id, remote_device_id FROM links {active}")
                    ).mappings():
                        for device_id in {row["local_device_id"], row["remote_device_id"]}:
                            if device_id in devices_by_id:
                                devices_by_id[device_id]["discovered_link_count"] += 1
    return {
        "counts": counts,
        "devices": devices,
        "groups": groups,
        "locations": locations,
        "schema": {
            "custom_maps": "custom_maps" in tables,
            "custom_map_nodes": "custom_map_nodes" in tables,
            "custom_map_edges": "custom_map_edges" in tables,
        },
    }
