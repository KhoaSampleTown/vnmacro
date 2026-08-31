"""Parquet store.

Everything lands as tidy long observations with one row per
(series_id, dims, date, vintage). Vintage is kept because a DSGE estimated on
revised data and one estimated on the real-time release are different exercises
— NSO revises "sơ bộ" figures for months afterwards.

Layout::

    curated/dataset=<dataset>/freq=<M|Q|A>/part-<key>.parquet

which duckdb and polars both read as a partitioned table.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Iterable, Sequence

import polars as pl

from .config import PATHS

log = logging.getLogger(__name__)

SCHEMA: dict[str, pl.DataType] = {
    "series_id": pl.Utf8,      # canonical id, e.g. NSO.CPI.MOM.HEADLINE
    "dataset": pl.Utf8,        # nso_monthly_tables | nso_cpi | imf_imts | ...
    "source": pl.Utf8,         # NSO | IMF | CUSTOMS
    "freq": pl.Utf8,           # M | Q | A
    "date": pl.Date,           # first day of the reference period
    "ref_period": pl.Utf8,     # 2026-M07
    "value": pl.Float64,
    "unit": pl.Utf8,
    "scale": pl.Int32,         # power of ten already applied to `value` (0 = none)
    "status": pl.Utf8,         # so_bo | uoc_tinh | dieu_chinh | chinh_thuc | ...
    "vintage": pl.Date,        # publication date of the release this came from
    "partner": pl.Utf8,        # ISO3 counterpart, for bilateral series
    "breakdown": pl.Utf8,      # commodity / province / COICOP group / sector
    "label_vi": pl.Utf8,
    "measure": pl.Utf8,        # column header of the source table, verbatim
    "dims": pl.Utf8,           # JSON blob of anything else worth keeping
    "raw_file": pl.Utf8,
    "ingested_at": pl.Datetime("us"),
}

COLUMNS: Sequence[str] = tuple(SCHEMA)


def empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def frame(records: Iterable[dict]) -> pl.DataFrame:
    """Coerce loose dicts into the canonical schema."""
    rows = list(records)
    if not rows:
        return empty_frame()
    now = dt.datetime.now()
    norm = []
    for r in rows:
        d = {c: r.get(c) for c in COLUMNS}
        d["ingested_at"] = d["ingested_at"] or now
        d["scale"] = int(d["scale"]) if d["scale"] is not None else 0
        if isinstance(d["dims"], dict):
            d["dims"] = json.dumps(d["dims"], ensure_ascii=False, sort_keys=True)
        norm.append(d)
    return pl.DataFrame(norm, schema=SCHEMA, strict=False)


def write(df: pl.DataFrame, dataset: str, key: str) -> Path | None:
    """Write one release's observations. ``key`` makes the file idempotent."""
    if df.is_empty():
        log.warning("nothing to write for %s/%s", dataset, key)
        return None
    df = df.drop_nulls(subset=["value"])
    if df.is_empty():
        return None
    freqs = df["freq"].unique().to_list()
    out = None
    for f in freqs:
        part = df.filter(pl.col("freq") == f)
        d = PATHS.curated / f"dataset={dataset}" / f"freq={f}"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"part-{key}.parquet"
        part.write_parquet(out, compression="zstd", statistics=True)
        log.info("wrote %d rows -> %s", part.height, out)
    return out


def read(dataset: str | None = None, freq: str | None = None) -> pl.DataFrame:
    """Read curated observations back, optionally filtered by partition."""
    base = PATHS.curated
    pattern = f"dataset={dataset}" if dataset else "dataset=*"
    pattern += f"/freq={freq}" if freq else "/freq=*"
    files = sorted(base.glob(pattern + "/*.parquet"))
    if not files:
        return empty_frame()
    frames = []
    for f in files:
        part = pl.read_parquet(f)
        # partition values live in the path, not the file
        parts = {k: v for k, v in (s.split("=", 1) for s in f.parts if "=" in s)}
        for col, val in parts.items():
            if col in part.columns:
                part = part.with_columns(pl.lit(val).alias(col))
        frames.append(part)
    return pl.concat(frames, how="diagonal_relaxed")


def save_state(name: str, payload: dict) -> Path:
    PATHS.state.mkdir(parents=True, exist_ok=True)
    p = PATHS.state / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_state(name: str) -> dict:
    p = PATHS.state / f"{name}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))
