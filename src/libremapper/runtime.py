from __future__ import annotations

import os
from pathlib import Path


def drop_privileges() -> None:
    """Initialize mounted directories, then leave root behind inside the container."""
    if os.getenv("LIBREMAPPER_DROP_PRIVILEGES") != "1" or os.geteuid() != 0:
        return

    uid = int(os.getenv("PUID", "1000"))
    gid = int(os.getenv("PGID", "1000"))
    if uid <= 0 or gid <= 0:
        raise RuntimeError("PUID and PGID must identify a non-root user")

    for value in (
        os.getenv("LIBREMAPPER_CONFIG_ROOT", "/config"),
        os.getenv("LIBREMAPPER_DATA_ROOT", "/data/libremapper"),
    ):
        path = Path(value)
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)

    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.umask(0o007)
