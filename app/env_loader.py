from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path, *, override: bool = False) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, raw_value = line.split("=", 1)
        name = name.strip()
        value = _strip_quotes(raw_value.strip())
        if not name:
            continue

        loaded[name] = value
        if override or name not in os.environ:
            os.environ[name] = value

    return loaded


def get_first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _strip_quotes(value: str) -> str:
    if len(value) < 2:
        return value
    if value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
