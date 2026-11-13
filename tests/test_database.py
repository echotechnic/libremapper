from __future__ import annotations

import pytest

from libremapper.config import LibreNMSConfig
from libremapper.database import create_librenms_engine


def test_official_db_environment_names_take_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_NAME", "official")
    monkeypatch.setenv("DB_DATABASE", "legacy")
    monkeypatch.setenv("DB_USER", "official_user")
    monkeypatch.setenv("DB_USERNAME", "legacy_user")
    monkeypatch.setenv("DB_PASSWORD", "secret")

    engine = create_librenms_engine(LibreNMSConfig())

    assert engine.url.database == "official"
    assert engine.url.username == "official_user"


def test_legacy_db_environment_aliases_remain_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.setenv("DB_DATABASE", "legacy")
    monkeypatch.setenv("DB_USERNAME", "legacy_user")
    monkeypatch.setenv("DB_PASSWORD", "secret")

    engine = create_librenms_engine(LibreNMSConfig())

    assert engine.url.database == "legacy"
    assert engine.url.username == "legacy_user"
