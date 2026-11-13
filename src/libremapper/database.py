from __future__ import annotations

import os
from typing import Any

from sqlalchemy import URL, Engine, create_engine

from libremapper.config import LibreNMSConfig


def _environment_value(primary: str, aliases: tuple[str, ...], default: str) -> str:
    for name in (primary, *aliases):
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def create_librenms_engine(config: LibreNMSConfig) -> Engine:
    if config.url:
        return create_engine(config.url, pool_pre_ping=True)

    host = _environment_value(config.host_env, (), "localhost")
    port = int(_environment_value(config.port_env, (), "3306"))
    database = _environment_value(
        config.database_env,
        tuple(name for name in ("DB_NAME", "DB_DATABASE") if name != config.database_env),
        "librenms",
    )
    username = _environment_value(
        config.username_env,
        tuple(name for name in ("DB_USER", "DB_USERNAME") if name != config.username_env),
        "librenms",
    )
    password = os.getenv(config.password_env, "")
    url = URL.create(
        "mysql+pymysql",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": config.connect_timeout},
    )


def row_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)
