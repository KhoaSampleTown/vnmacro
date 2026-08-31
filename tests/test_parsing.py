"""Regression tests for the two parsers that fail *silently* when wrong.

Run:  python -m tests.test_parsing      (no pytest dependency needed)

Both cases here have already bitten once:

  * Vietnamese number format — "1.225.073" tỷ đồng of credit reads as 1.2 if
    the dot is treated as a decimal point. Nothing raises; the series is just
    wrong by six orders of magnitude.
  * CPI column classification — the Vietnamese for "compared with" is "so với"
    with ớ (U+1EDB). A pattern written with ơ never matches, the split never
    happens, and the *subject* month gets read as the *reference* month, so
    every column looks like a comparison against itself.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vnmacro.sources.nso_cpi import classify           # noqa: E402
from vnmacro.util import parse_period, to_float        # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def test_numbers() -> None:
    cases = {
        # Vietnamese: dot groups thousands, comma is the decimal mark
        "20.414.616": 20414616.0,
        "1.225.073": 1225073.0,
        "546.391": 546391.0,
        "2.787": 2787.0,
        "4,99": 4.99,
        "72,5": 72.5,
        "-3,59": -3.59,
        "15,20": 15.2,
        # both separators present: the last one is the decimal
        "1.834,6": 1834.6,
        "1,234.56": 1234.56,
        # unambiguous English grouping
        "1,234,567": 1234567.0,
        # non-numbers
        "": None, "-": None, "...": None, "n/a": None, "16.CPI": None,
    }
    for text, want in cases.items():
        check(f"to_float({text!r})", to_float(text), want)


def test_periods() -> None:
    # Reference period comes from the title, never the WordPress date.
    p = parse_period("Báo cáo tình hình kinh tế - xã hội tháng Bảy và 7 tháng năm 2026")
    check("July 2026 freq", p["freq"], "M")
    check("July 2026 date", p["date"], dt.date(2026, 7, 1))

    # A quarterly issue carries the LAST month of the quarter's monthly tables.
    q = parse_period("Báo cáo tình hình kinh tế - xã hội Quý II và sáu tháng đầu năm 2026")
    check("Q2 2026 freq", q["freq"], "Q")
    check("Q2 2026 month_date", q["month_date"], dt.date(2026, 6, 1))

    # Numeric month form used by the older migrated posts.
    o = parse_period("tinh-hinh-kinh-te-xa-hoi-thang-01-nam-2005")
    check("Jan 2005 date", o["date"], dt.date(2005, 1, 1))

    # "7 tháng" is a cumulative span, not a month reference. "năm" means both
    # "five" and "year", so this is the case that must not become May.
    check("cumulative not month", parse_period("7 tháng năm 2026")["freq"], "A")

    # ...but a real "tháng Năm" title still has to resolve to May.
    titles = {
        "Báo cáo tình hình kinh tế - xã hội tháng Năm và 5 tháng đầu năm 2026": "2026-M05",
        "Báo cáo tình hình kinh tế - xã hội tháng Bảy và 7 tháng năm 2026": "2026-M07",
        "Báo cáo tình hình kinh tế - xã hội tháng Mười Hai và 12 tháng năm 2025": "2025-M12",
        "Báo cáo tình hình kinh tế - xã hội tháng Tư và 4 tháng đầu năm 2026": "2026-M04",
        "Báo cáo tình hình kinh tế - xã hội Quý IV và năm 2025": "2025-Q4",
    }
    for title, want in titles.items():
        got = parse_period(title)
        check(f"title {title[38:60]!r}", got["ref_period"] if got else None, want)


def test_cpi_classification() -> None:
    apr = dt.date(2026, 4, 1)
    cases = [
        ("Tháng 4 năm 2026 so với Tháng 3 năm 2026", apr, "MOM"),
        ("Tháng 4 năm 2026 so với Tháng 4 năm 2025", apr, "YOY"),
        ("Tháng 4 năm 2026 so với Tháng 12 năm 2025", apr, "YTD"),
        ("Tháng 4 năm 2026 so với Kỳ gốc 2024", apr, "BASE"),
        ("Bình quân 4 tháng năm 2026 so với cùng kỳ năm trước", apr, "AVG_YOY"),
        # Standalone CPI workbook folds the sheet title into the column label,
        # so the LAST "so với" is the one that matters.
        ("CHỈ SỐ GIÁ TIÊU DÙNG CẢ NƯỚC Tháng 5 năm 2026 CHỈ SỐ GIÁ THÁNG 5 "
         "NĂM 2026 SO VỚI Tháng 4 năm 2026", dt.date(2026, 5, 1), "MOM"),
    ]
    for measure, period, want in cases:
        got = classify(measure, period)
        check(f"classify({measure[:40]!r})", got[0] if got else None, want)


def main() -> int:
    for fn in (test_numbers, test_periods, test_cpi_classification):
        fn()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("all parsing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
