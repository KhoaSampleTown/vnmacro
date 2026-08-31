"""Paths and runtime settings.

Data root defaults to ``<project>/data`` but can be moved off OneDrive with
``VNMACRO_DATA_DIR`` — recommended once the raw archive grows past a few
hundred MB, since OneDrive will otherwise re-sync every downloaded PDF.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent

# YAML cấu hình nằm TRONG gói (`vnmacro/config/`) để `pip install` mang theo được.
# Trước đây nó ở gốc dự án, chạy từ bản clone thì được nhưng cài bằng pip là mất —
# `narrative_patterns.yaml` không có trong wheel nên mọi lần bóc lời văn đều hỏng.
# Vẫn giữ đường dẫn cũ làm dự phòng cho bản clone đang dùng dở, và cho ai muốn
# ghi đè bằng bộ pattern riêng đặt ở gốc dự án.
CONFIG_DIR = PKG_DIR / "config"
_LEGACY_CONFIG_DIR = PROJECT_DIR / "config"


def _data_root() -> Path:
    env = os.environ.get("VNMACRO_DATA_DIR")
    return Path(env).expanduser().resolve() if env else PROJECT_DIR / "data"


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def curated(self) -> Path:
        return self.root / "curated"

    @property
    def panel(self) -> Path:
        return self.root / "panel"

    @property
    def state(self) -> Path:
        return self.root / "_state"

    def ensure(self) -> "Paths":
        for p in (self.raw, self.staging, self.curated, self.panel, self.state):
            p.mkdir(parents=True, exist_ok=True)
        return self


PATHS = Paths(_data_root())

USER_AGENT = os.environ.get(
    "VNMACRO_USER_AGENT",
    "vn-macro-pipeline/0.1 (research data collection; contact: local user)",
)
REQUEST_TIMEOUT = float(os.environ.get("VNMACRO_TIMEOUT", "90"))
REQUEST_DELAY = float(os.environ.get("VNMACRO_DELAY", "0.7"))  # politeness gap


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        path = _LEGACY_CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def sources() -> dict:
    return load_yaml("sources.yaml")


def series_map() -> dict:
    return load_yaml("series_map.yaml")
