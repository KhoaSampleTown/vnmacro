"""CPI extraction from the monthly workbook, with the rebasing problem solved.

NSO republishes the CPI basket and its base period roughly every five years
(2009 → 2014 → 2019 → 2024). At each switch the *level* series restarts at
100, so naively stitching published index levels produces a fake jump. The
month-on-month ratios, on the other hand, are always computed inside a single
consistent basket, so they chain across the break cleanly.

This module therefore classifies each published column by what it is compared
*against* — parsed from the header wording rather than the column position,
because the column order shifts between releases — and keeps them apart:

    CPI.MOM        this month / previous month           (chain-linkable)
    CPI.YOY        this month / same month last year
    CPI.YTD        this month / December last year
    CPI.AVG_YOY    average of the year so far / same period last year
    CPI.BASE       published level on the current base    (breaks at rebasing)

``transform.cpi_chain`` turns CPI.MOM into a single continuous index.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from ..util import norm_ws, slugify, strip_accents

log = logging.getLogger(__name__)

# Sheet names carrying CPI. The monthly report uses "16.CPI"; the standalone
# CPI release uses Vietnamese section names and adds region/province detail.
CPI_SHEET_HINT = re.compile(r"cpi|c[ảa] n[ưu][ớo]c|l[ạa]m ph[áa]t c[ơo] b[ảa]n", re.I)

# Row labels that are the headline, not a COICOP group.
HEADLINE = {"chi so gia tieu dung"}
CORE = {"lam phat co ban"}
GOLD = {"chi so gia vang", "chi so gia dola my", "chi so gia do la my"}


def _parse_ref(measure: str) -> tuple[str, int | None, int | None]:
    """Return (kind, ref_month, ref_year) for a CPI column header.

    Everything is matched on the accent-folded form. Vietnamese diacritics are
    a trap here: "với" carries ớ (U+1EDB), not ơ, so a pattern written against
    the accented text silently fails to split and the *subject* period gets
    read as the reference period.

    The split takes the LAST "so với", because the standalone CPI workbook
    folds the sheet title into the column label and that title contains one
    of its own ("CHỈ SỐ GIÁ THÁNG 5 NĂM 2026 SO VỚI ...").
    """
    folded = strip_accents(norm_ws(measure))

    if "binh quan" in folded:
        return "avg_ytd_yoy", None, None

    parts = re.split(r"so voi\s*:?", folded)
    ref = parts[-1] if len(parts) > 1 else folded

    m = re.search(r"ky goc\s*(\d{4})?", ref)
    if m:
        return "base", None, int(m.group(1)) if m.group(1) else None

    m = re.search(r"thang\s*(\d{1,2}).*?nam\s*(\d{4})", ref)
    if m:
        return "month", int(m.group(1)), int(m.group(2))

    if "thang truoc" in ref:
        return "prev_month", None, None
    if "cung ky" in ref:
        return "yoy", None, None
    return "unknown", None, None


def classify(measure: str, period: dt.date) -> tuple[str, str] | None:
    """Map a column header to (series suffix, human description)."""
    kind, rm, ry = _parse_ref(measure)
    if kind == "avg_ytd_yoy":
        return "AVG_YOY", "average year-to-date vs same period last year"
    if kind == "base":
        return "BASE", f"index, {ry}=100" if ry else "index, published base period=100"
    if kind == "prev_month":
        return "MOM", "vs previous month"
    if kind == "yoy":
        return "YOY", "vs same month last year"
    if kind == "month" and rm and ry:
        prev = (period.replace(day=1) - dt.timedelta(days=1))
        if (ry, rm) == (prev.year, prev.month):
            return "MOM", "vs previous month"
        if (ry, rm) == (period.year - 1, period.month):
            return "YOY", "vs same month last year"
        if (ry, rm) == (period.year - 1, 12):
            return "YTD", "vs December last year"
        return f"VS_{ry}M{rm:02d}", f"vs {ry}-{rm:02d}"
    return None


def group_kind(row_label: str) -> str:
    f = strip_accents(row_label)
    if f in HEADLINE:
        return "headline"
    if any(c in f for c in CORE):
        return "core"
    if any(g in f for g in GOLD):
        return "gold_usd"
    return "group"


def extract(raw_records: list[dict], *, release: dict, raw_file: str) -> list[dict]:
    """Turn parsed workbook rows from the CPI sheet into observations."""
    # Always the monthly reference: a quarterly issue publishes the *last*
    # month of the quarter, and dating it to the quarter's first month would
    # silently corrupt the month-on-month chain.
    period = release.get("month_date") or release["period_date"]
    if isinstance(period, str):
        period = dt.date.fromisoformat(period)
    vintage = release.get("wp_date", "")[:10]
    vintage = dt.date.fromisoformat(vintage) if vintage else None

    out: list[dict] = []
    for r in raw_records:
        if not CPI_SHEET_HINT.search(r["sheet"]):
            continue
        hit = classify(r["measure"], period)
        if not hit:
            log.debug("unclassified CPI column: %r", r["measure"])
            continue
        suffix, desc = hit
        kind = group_kind(r["row_label"])
        item = "HEADLINE" if kind == "headline" else slugify(r["row_label"], 40).upper()
        dims = {
            "comparison": desc,
            "item_kind": kind,
            "source_measure": r["measure"],
        }
        if r.get("base_year"):
            dims["base_year"] = r["base_year"]
        out.append({
            "series_id": f"NSO.CPI.{suffix}.{item}",
            "dataset": "nso_cpi",
            "source": "NSO",
            "freq": "M",
            "date": period,
            "ref_period": release.get("month_ref") or release["ref_period"],
            # published as "previous period = 100"; store as the ratio itself
            "value": r["value"],
            "unit": "index (compared period = 100)",
            "scale": 0,
            "status": "so_bo",
            "vintage": vintage,
            "partner": None,
            "breakdown": r["row_label"],
            "label_vi": r["row_label"],
            "measure": r["measure"],
            "dims": dims,
            "raw_file": raw_file,
        })
    return out
