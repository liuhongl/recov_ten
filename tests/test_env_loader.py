from __future__ import annotations

import os

from app.env_loader import get_first_env, load_env_file


def test_load_env_file_sets_missing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # comment
        EXISTING=from_file
        NEW_VALUE="quoted value"
        EMPTY=
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING", "from_env")

    loaded = load_env_file(env_file)

    assert loaded["EXISTING"] == "from_file"
    assert loaded["NEW_VALUE"] == "quoted value"
    assert os.environ["EXISTING"] == "from_env"
    assert os.environ["NEW_VALUE"] == "quoted value"


def test_load_env_file_strips_utf8_bom_from_first_key(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\ufeffPOSTGRES_DSN=postgresql://example\nSECOND=value\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("POSTGRES_DSN", raising=False)

    loaded = load_env_file(env_file)

    assert loaded["POSTGRES_DSN"] == "postgresql://example"
    assert os.environ["POSTGRES_DSN"] == "postgresql://example"
    assert "\ufeffPOSTGRES_DSN" not in loaded


def test_get_first_env_returns_first_available(monkeypatch):
    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.setenv("SECOND", "value")

    assert get_first_env(("FIRST", "SECOND")) == "value"
