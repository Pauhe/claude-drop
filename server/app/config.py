"""Settings. Defaults are the values the design fixed; the environment
exists so the container can be tuned without a rebuild."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MB = 1024 * 1024
GB = 1024 * MB


@dataclass(frozen=True)
class Settings:
    root: Path
    max_file_bytes: int = 25 * MB
    max_files_per_request: int = 20
    quota_bytes: int = 8 * GB
    free_reserve_bytes: int = 4 * GB
    retention_days: int = 30
    retention_max_drops: int = 500
    sweep_interval_seconds: int = 900
    max_concurrent_decodes: int = 2


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    src = os.environ if env is None else env

    def number(name: str, default: int) -> int:
        raw = src.get(name)
        return default if raw in (None, "") else int(raw)

    return Settings(
        root=Path(src.get("DROP_ROOT", "/data")),
        max_file_bytes=number("DROP_MAX_FILE_BYTES", 25 * MB),
        max_files_per_request=number("DROP_MAX_FILES", 20),
        quota_bytes=number("DROP_QUOTA_BYTES", 8 * GB),
        free_reserve_bytes=number("DROP_FREE_RESERVE_BYTES", 4 * GB),
        retention_days=number("DROP_RETENTION_DAYS", 30),
        retention_max_drops=number("DROP_RETENTION_MAX_DROPS", 500),
        sweep_interval_seconds=number("DROP_SWEEP_INTERVAL_SECONDS", 900),
        max_concurrent_decodes=number("DROP_MAX_CONCURRENT_DECODES", 2),
    )
