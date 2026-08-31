"""Shared helpers: Vietnamese period parsing, number cleaning, slugs."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

# ---------------------------------------------------------------- periods ---

# Vietnamese month words as they appear in NSO titles/slugs.
_MONTH_WORDS = {
    "mot": 1, "gieng": 1,
    "hai": 2,
    "ba": 3,
    "tu": 4, "bon": 4,
    "nam": 5,
    "sau": 6,
    "bay": 7,
    "tam": 8,
    "chin": 9,
    "muoi": 10,
    "muoi mot": 11, "muoimot": 11,
    "muoi hai": 12, "muoihai": 12,
}

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}


def strip_accents(s: str) -> str:
    """'tháng Bảy' -> 'thang bay' (lowercase, no diacritics)."""
    if s is None:
        return ""
    s = s.replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def _clean_title(s: str) -> str:
    s = strip_accents(s)
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9 /]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_period(title: str) -> dict | None:
    """Extract the *reference* period from an NSO post title or slug.

    The WordPress publish date is unreliable (a 2005 report can carry a 2019
    date from the site migration), so the period always comes from the text.

    Returns {"freq": "M"|"Q"|"A", "year": int, "period": int, "date": date,
             "ref_period": "2026-M07"} or None.
    """
    t = _clean_title(title or "")
    if not t:
        return None

    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", t)]
    if not years:
        return None
    year = max(years)  # "thang bay va 7 thang nam 2026" -> 2026

    # -- quarter: "quy ii va sau thang dau nam 2026" / "quy iv va nam 2025"
    m = re.search(r"\bquy\s+(i{1,3}|iv)\b", t)
    if m:
        q = _ROMAN[m.group(1)]
        return _mk("Q", year, q)

    # -- month, numeric: "thang 01 nam 2005", "thang 7 va 7 thang nam 2026"
    m = re.search(r"\bthang\s+(\d{1,2})\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        # Guard against "7 thang nam 2026" (= cumulative 7 months, not July):
        # a real month reference has the number *after* "thang".
        return _mk("M", year, int(m.group(1)))

    # -- month, spelled out: "thang muoi mot", "thang bay"
    #
    # "năm" means both "five" and "year", so "tháng Năm" is May but
    # "7 tháng năm 2026" is "7 months of 2026". Only read it as May when it is
    # not preceded by a count and not immediately followed by a year.
    m = re.search(
        r"(?<!\d\s)\bthang\s+(muoi hai|muoi mot|muoi|mot|hai|ba|tu|bon|"
        r"nam(?!\s+\d{4})|sau|bay|tam|chin|gieng)\b", t)
    if m:
        return _mk("M", year, _MONTH_WORDS[m.group(1)])

    # -- annual: "nam 2025" with no month/quarter
    if re.search(r"\bnam\s+(19|20)\d{2}\b", t):
        return _mk("A", year, 1)

    return None


def _mk(freq: str, year: int, period: int) -> dict:
    """Build the period record.

    ``month`` is the *monthly* observation the release's tables describe. A
    quarterly report ("Quý II và sáu tháng đầu năm 2026") still carries June's
    monthly CPI and trade tables, not April's, so its monthly data must be
    dated to the last month of the quarter — otherwise the CPI chain links
    the wrong months together.
    """
    if freq == "M":
        d = dt.date(year, period, 1)
        ref = f"{year}-M{period:02d}"
        month = period
    elif freq == "Q":
        d = dt.date(year, 3 * period - 2, 1)
        ref = f"{year}-Q{period}"
        month = 3 * period
    else:
        d = dt.date(year, 1, 1)
        ref = str(year)
        month = 12
    return {
        "freq": freq, "year": year, "period": period, "date": d, "ref_period": ref,
        "month_date": dt.date(year, month, 1),
        "month_ref": f"{year}-M{month:02d}",
    }


def month_end(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return nxt - dt.timedelta(days=1)


def sdmx_period_to_date(p: str) -> dt.date | None:
    """'2026-M07' / '2026-Q3' / '2026' -> first day of the period."""
    p = (p or "").strip()
    m = re.fullmatch(r"(\d{4})-M(\d{2})", p)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1)
    m = re.fullmatch(r"(\d{4})-Q(\d)", p)
    if m:
        return dt.date(int(m.group(1)), 3 * int(m.group(2)) - 2, 1)
    m = re.fullmatch(r"(\d{4})", p)
    if m:
        return dt.date(int(m.group(1)), 1, 1)
    return None


def sdmx_freq(p: str) -> str:
    if "-M" in p:
        return "M"
    if "-Q" in p:
        return "Q"
    return "A"


# ----------------------------------------------------------------- numbers ---

#: Digits grouped in threes by dots — "20.414.616" is twenty million, not 20.4.
_VN_GROUPED = re.compile(r"-?\d{1,3}(?:\.\d{3})+")
_EN_GROUPED = re.compile(r"-?\d{1,3}(?:,\d{3})+")


def to_float(v) -> float | None:
    """Parse a cell value into a float, Vietnamese conventions first.

    Every text source in this pipeline (NSO prose, SBV pages, customs PDFs)
    writes numbers the Vietnamese way: **dot groups thousands, comma marks the
    decimal**. Getting this backwards is silent and expensive — "1.225.073"
    billion VND of credit reads as 1.2 if the dot is taken as a decimal point.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f  # drop NaN
    s = str(v).strip()
    if not s or s in {"-", "--", "...", "..", "n/a", "N/A", "x", "X"}:
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace("%", "")

    if "," in s and "." in s:
        # Both present: whichever comes last is the decimal separator.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")      # 1.234,56 -> 1234.56
        else:
            s = s.replace(",", "")                        # 1,234.56 -> 1234.56
    elif "." in s:
        # Dots only: groups of exactly three are thousands separators.
        s = s.replace(".", "") if _VN_GROUPED.fullmatch(s) else s
    elif "," in s:
        # Commas only: decimal comma, unless it is clearly grouped thousands
        # ("1,234,567" — two or more groups leaves no other reading).
        s = s.replace(",", "") if _EN_GROUPED.fullmatch(s) and s.count(",") > 1 \
            else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip() if s is not None else ""


def slugify(s: str, maxlen: int = 80) -> str:
    s = strip_accents(s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen] or "item"
