from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine


@pytest.fixture()
def custom_map_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'librenms.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE custom_maps (
                custom_map_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                width VARCHAR(10) NOT NULL,
                height VARCHAR(10) NOT NULL,
                node_align INTEGER NOT NULL DEFAULT 0,
                reverse_arrows INTEGER NOT NULL DEFAULT 0,
                edge_separation INTEGER NOT NULL DEFAULT 10,
                legend_x INTEGER NOT NULL DEFAULT -1,
                legend_y INTEGER NOT NULL DEFAULT -1,
                legend_steps INTEGER NOT NULL DEFAULT 7,
                legend_font_size INTEGER NOT NULL DEFAULT 14,
                legend_hide_invalid INTEGER NOT NULL DEFAULT 0,
                legend_hide_overspeed INTEGER NOT NULL DEFAULT 0,
                legend_colours TEXT,
                options TEXT,
                newnodeconfig TEXT NOT NULL,
                newedgeconfig TEXT NOT NULL,
                menu_group VARCHAR(100),
                background_type VARCHAR(16) NOT NULL DEFAULT 'none',
                background_data TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE custom_map_nodes (
                custom_map_node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                custom_map_id INTEGER NOT NULL,
                device_id INTEGER,
                linked_custom_map_id INTEGER,
                label VARCHAR(50) NOT NULL,
                style VARCHAR(50) NOT NULL,
                icon VARCHAR(8),
                image VARCHAR(255) NOT NULL DEFAULT '',
                node_image_id INTEGER,
                size INTEGER NOT NULL,
                border_width INTEGER NOT NULL,
                text_face VARCHAR(50) NOT NULL,
                text_size INTEGER NOT NULL,
                text_colour VARCHAR(10) NOT NULL,
                colour_bg VARCHAR(10),
                colour_bdr VARCHAR(10),
                x_pos INTEGER NOT NULL,
                y_pos INTEGER NOT NULL,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE custom_map_edges (
                custom_map_edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                custom_map_id INTEGER NOT NULL,
                custom_map_node1_id INTEGER NOT NULL,
                custom_map_node2_id INTEGER NOT NULL,
                port_id INTEGER,
                reverse INTEGER NOT NULL,
                style VARCHAR(50) NOT NULL,
                showpct INTEGER NOT NULL,
                showbps INTEGER NOT NULL DEFAULT 0,
                label VARCHAR(255) NOT NULL DEFAULT '',
                fixed_width NUMERIC(3, 1),
                text_face VARCHAR(50) NOT NULL,
                text_size INTEGER NOT NULL,
                text_colour VARCHAR(10) NOT NULL,
                text_align VARCHAR(16) NOT NULL DEFAULT 'horizontal',
                mid_x INTEGER NOT NULL,
                mid_y INTEGER NOT NULL,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
    return engine
