from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_installer_updates_both_components_from_one_image(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  *"ps -q libremapper") printf 'sidecar-id\n' ;;
  *"ps -q librenms") printf 'librenms-id\n' ;;
  "inspect sidecar-id "*) printf 'healthy\n' ;;
  "inspect librenms-id "*) printf 'healthy\n' ;;
  *"urllib.request"*) printf '{\"status\":\"ok\",\"version\":\"0.1.0\"}\n' ;;
  *"composer show echotechnic/libremapper --format=json"*) printf '{\"versions\":[\"v0.1.0\"]}\n' ;;
  *"route:list --path=plugin/libremapper --json"*) printf '{\"name\":\"libremapper.apply\"}\n' ;;
esac
"""
    )
    docker.chmod(0o755)
    (tmp_path / "compose.yml").write_text("services: {}\n")
    (tmp_path / "compose.libremapper.yaml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("LIBREMAPPER_VERSION=0.0.1\n")
    log = tmp_path / "docker.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
    }

    result = subprocess.run(
        ["bash", str(root / "scripts/install-or-update"), "0.1.0"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    commands = log.read_text().splitlines()
    pull = next(i for i, line in enumerate(commands) if "pull libremapper" in line)
    health = next(i for i, line in enumerate(commands) if "urllib.request" in line)
    plugin = next(i for i, line in enumerate(commands) if "plugin:add" in line)
    assert pull < health < plugin
    assert any("/usr/share/libremapper-plugin" in line for line in commands)
    assert any("/opt/librenms/libremapper-package" in line for line in commands)
    assert any("plugin:enable libremapper" in line for line in commands)
    assert (tmp_path / ".env").read_text() == "LIBREMAPPER_VERSION=0.1.0\n"
    assert "Libremapper 0.1.0 is installed" in result.stdout
