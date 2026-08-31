"""Build a CPI series that survives the basket revisions.

The problem: NSO re-weights the CPI basket and moves the base period about
every five years (2009 → 2014 → 2019 → 2024). Published index *levels* restart
at 100 on each new base, so concatenating them invents a discontinuity, and
the weights themselves change so the two regimes are not directly comparable.

The fix used here is the standard one: month-on-month ratios are always
computed within a single basket, so they are comparable across the seam even
when levels are not. Chaining them gives one continuous index:

    I(t) = I(t-1) * MoM(t) / 100,    I(anchor) = 100

Two diagnostics come out of it for free:

  * ``rebasing_break``  — flags the months where the published base year
    changes, so you can see exactly where a naive splice would have jumped;
  * ``yoy_residual``    — chained YoY minus published YoY. It should be ~0.
    A non-trivial residual at a seam is the re-weighting effect, and it is
    worth looking at before feeding the series to an estimator.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import polars as pl

from .. import store

log = logging.getLogger(__name__)

ANCHOR_VALUE = 100.0

#: A rebasing resets the published level from ~110-130 back to 100, so its
#: signature is a double-digit gap between the level's implied month-on-month
#: move and the published one. Smaller gaps are revisions or a level carried
#: over from a different vintage — real, worth seeing, but not a rebasing.
BREAK_TOLERANCE_PP = 5.0

#: Anything above this but below the break threshold is reported separately.
NOISE_TOLERANCE_PP = 0.5


def _latest_vintage(df: pl.DataFrame) -> pl.DataFrame:
    """One row per (series_id, date): the most recently published figure."""
    return (df.sort("vintage", descending=True, nulls_last=True)
              .unique(subset=["series_id", "date"], keep="first")
              .sort("date"))


def _pick(df: pl.DataFrame, suffix: str) -> pl.DataFrame:
    return df.filter(pl.col("series_id").str.starts_with(f"NSO.CPI.{suffix}."))


def build(anchor: dt.date | None = None) -> pl.DataFrame:
    """Chain-link every CPI item and return the continuous index."""
    raw = store.read("nso_cpi", "M")
    if raw.is_empty():
        log.warning("no nso_cpi observations found — run `fetch`/`parse` first")
        return pl.DataFrame()

    raw = _latest_vintage(raw)
    mom = _pick(raw, "MOM").select(
        pl.col("series_id").str.replace(r"^NSO\.CPI\.MOM\.", "").alias("item"),
        "date", "breakdown",
        pl.col("value").alias("mom_index"),
    )
    if mom.is_empty():
        log.warning("no month-on-month CPI columns parsed; cannot chain")
        return pl.DataFrame()

    # Published base year, to mark where the basket was replaced.
    base = _pick(raw, "BASE").select(
        pl.col("series_id").str.replace(r"^NSO\.CPI\.BASE\.", "").alias("item"),
        "date",
        pl.col("value").alias("published_base_index"),
        pl.col("dims").str.json_path_match("$.base_year")
          .cast(pl.Int32, strict=False).alias("base_year"),
    )
    yoy = _pick(raw, "YOY").select(
        pl.col("series_id").str.replace(r"^NSO\.CPI\.YOY\.", "").alias("item"),
        "date", pl.col("value").alias("published_yoy_index"),
    )

    df = (mom.join(base, on=["item", "date"], how="left")
             .join(yoy, on=["item", "date"], how="left")
             .sort(["item", "date"]))

    if anchor is not None:
        df = df.filter(pl.col("date") >= anchor)

    # A missing month is the one thing that silently corrupts a chained index:
    # the product simply skips it and every later level is wrong by that
    # month's inflation. Flag it rather than pretending the series is dense.
    df = df.with_columns(
        ((pl.col("date").dt.year() * 12 + pl.col("date").dt.month())
         - (pl.col("date").shift(1).over("item").dt.year() * 12
            + pl.col("date").shift(1).over("item").dt.month()))
        .alias("month_step")
    ).with_columns(
        (pl.col("month_step") > 1).fill_null(False).alias("gap_before")
    )
    n_gaps = int(df.select(pl.col("gap_before").sum()).item() or 0)
    if n_gaps:
        missing = (df.filter(pl.col("gap_before"))
                     .select("item", "date", "month_step").head(10))
        log.warning("chained CPI spans %d gap(s) — levels after a gap are "
                    "understated by the missing months' inflation:\n%s",
                    n_gaps, missing)

    # I(t) = 100 * prod(MoM/100) — cumulative product in log space keeps it
    # numerically tame over 20 years of monthly links.
    df = df.with_columns(
        (pl.col("mom_index") / 100.0).log().cum_sum().over("item").exp()
        .mul(ANCHOR_VALUE).alias("chained_index")
    )

    # Detecting the rebasing.
    #
    # The header only names the base year in recent issues ("Kỳ gốc 2024");
    # older ones just say "Kỳ gốc", so watching that label alone finds nothing.
    # The reliable signal is in the numbers: if there were no rebasing, the
    # published level would move exactly by the published month-on-month
    # ratio. When the basket is replaced the level restarts and those two stop
    # agreeing, which is precisely the discontinuity a naive splice inherits.
    df = df.with_columns(
        (pl.col("published_base_index")
         / pl.col("published_base_index").shift(1).over("item") * 100.0)
        .alias("implied_mom_from_level")
    ).with_columns(
        (pl.col("implied_mom_from_level") - pl.col("mom_index")).abs()
        .alias("level_mom_mismatch")
    )

    df = df.with_columns([
        (pl.col("chained_index") /
         pl.col("chained_index").shift(12).over("item") * 100.0).alias("chained_yoy_index"),
        (
            # the label changed...
            (pl.col("base_year") != pl.col("base_year").shift(1).over("item")).fill_null(False)
            # ...or the published level jumped inconsistently with its own MoM
            | ((pl.col("level_mom_mismatch") > BREAK_TOLERANCE_PP)
               & pl.col("month_step").eq(1))
        ).fill_null(False).alias("rebasing_break"),
    ])
    df = df.with_columns(
        (pl.col("chained_yoy_index") - pl.col("published_yoy_index")).alias("yoy_residual")
    )
    # The first observation of each item is the anchor, not a real break.
    df = df.with_columns(
        pl.when(pl.col("date") == pl.col("date").min().over("item"))
          .then(False).otherwise(pl.col("rebasing_break")).alias("rebasing_break")
    )
    return df


def to_observations(df: pl.DataFrame) -> list[dict]:
    if df.is_empty():
        return []
    now = dt.datetime.now()
    out = []
    for row in df.iter_rows(named=True):
        dims = json.dumps({
            "base_year": row["base_year"],
            "rebasing_break": bool(row["rebasing_break"]),
            "gap_before": bool(row.get("gap_before")),
            "yoy_residual": row["yoy_residual"],
            "method": "chain-linked from published month-on-month ratios",
        }, ensure_ascii=False, default=str)
        for suffix, value, unit in (
            ("CHAINED", row["chained_index"], f"index (first month = {ANCHOR_VALUE:g})"),
            ("CHAINED_YOY", row["chained_yoy_index"], "index (same month last year = 100)"),
        ):
            if value is None:
                continue
            out.append({
                "series_id": f"NSO.CPI.{suffix}.{row['item']}",
                "dataset": "cpi_chained", "source": "NSO", "freq": "M",
                "date": row["date"],
                "ref_period": f"{row['date'].year}-M{row['date'].month:02d}",
                "value": value, "unit": unit, "scale": 0,
                "status": "derived", "vintage": now.date(),
                "partner": None, "breakdown": row.get("breakdown"),
                "label_vi": row.get("breakdown"), "measure": suffix,
                "dims": dims, "raw_file": "derived:cpi_chain",
                "ingested_at": now,
            })
    return out


def report(df: pl.DataFrame) -> str:
    """Human-readable summary of where the basket changed and what it cost."""
    if df.is_empty():
        return "no CPI data"
    head = df.filter(pl.col("item") == "HEADLINE")
    if head.is_empty():
        head = df
    breaks = head.filter(pl.col("rebasing_break"))
    lines = [
        f"CPI chained: {head.height} months, "
        f"{head['date'].min()} -> {head['date'].max()}",
    ]
    if breaks.is_empty():
        lines.append("no base-period change detected in the collected range")
    else:
        lines.append(f"{breaks.height} base-period change(s) — where a naive "
                     f"splice of published levels would jump:")
        for r in breaks.iter_rows(named=True):
            lvl = r["published_base_index"]
            lines.append(
                f"  {r['date']}  base year -> {r['base_year']}"
                f"   published level {lvl:.2f}" if lvl is not None else
                f"  {r['date']}  base year -> {r['base_year']}")
            if r["level_mom_mismatch"] is not None:
                lines.append(
                    f"              published level implies {r['implied_mom_from_level']:.2f} "
                    f"m/m but NSO published {r['mom_index']:.2f} "
                    f"({r['level_mom_mismatch']:.2f} pp apart)")
    wobble = head.filter(
        (pl.col("level_mom_mismatch") > NOISE_TOLERANCE_PP)
        & (pl.col("level_mom_mismatch") <= BREAK_TOLERANCE_PP))
    if not wobble.is_empty():
        lines.append(f"{wobble.height} smaller level/MoM inconsistenc(ies) "
                     f"(>{NOISE_TOLERANCE_PP} pp, not a rebasing) at "
                     + ", ".join(str(d) for d in wobble["date"].to_list()[:6]))

    gaps = head.filter(pl.col("gap_before")) if "gap_before" in head.columns else None
    if gaps is not None and not gaps.is_empty():
        lines.append(f"WARNING: {gaps.height} missing month(s) in the chain "
                     f"({', '.join(str(d) for d in gaps['date'].to_list()[:6])}) "
                     f"— backfill those releases before using the levels")
    resid = head.select(pl.col("yoy_residual").abs().max()).item()
    if resid is not None:
        lines.append(f"max |chained YoY - published YoY| = {resid:.3f} pp")
    return "\n".join(lines)
