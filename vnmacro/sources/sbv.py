"""State Bank of Vietnam (sbv.gov.vn) — the monetary block.

This is the only source for Vietnam's monetary aggregates and bank credit:
IMF's MFS flows are empty for VNM, and NSO dropped the banking section from
the monthly report years ago.

Two things make this source awkward, and both are handled rather than hidden:

* SBV sits behind an F5 WAF that answers "Request Rejected" (a 246-byte 200,
  not an error status) when it dislikes the traffic pattern. Requests here are
  paced slowly, sent with a browser-like User-Agent, and a rejection is
  reported as a skip — never parsed as if it were data.
* Each page shows **only the latest month**; there is no archive. History
  therefore accumulates by running the pipeline monthly. Everything is stored
  with its own reference month parsed from the table caption, so re-running
  is idempotent and a missed month simply stays missing rather than being
  back-filled with the wrong period.

Tables share one shape::

    DƯ NỢ TÍN DỤNG ĐỐI VỚI NỀN KINH TẾ VÀ TỐC ĐỘ TĂNG TRƯỞNG
    (Tháng 10 Năm 2025)
    STT | Chỉ tiêu | Số dư (Tỷ đồng) | Tốc độ tăng (Giảm) so với cuối năm ... (%)
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from bs4 import BeautifulSoup

from .. import http
from ..config import PATHS
from ..util import norm_ws, slugify, strip_accents, to_float

log = logging.getLogger(__name__)

BASE = "https://sbv.gov.vn/vi/"
REJECTED = "Request Rejected"

#: SBV throttles aggressively; this is deliberately slower than the global gap.
PAGE_DELAY = 6.0

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

#: Column wording -> what the number is.
LEVEL_HINT = re.compile(r"so du|du no|gia tri", re.I)
GROWTH_HINT = re.compile(r"toc do tang|tang \(giam\)|tang truong", re.I)


class Rejected(RuntimeError):
    """SBV's WAF refused the request."""


def fetch_page(slug: str) -> str:
    url = slug if slug.startswith("http") else BASE + slug
    r = http.get(url, headers={"User-Agent": BROWSER_UA})
    html = r.text
    if REJECTED in html or len(html) < 1000:
        raise Rejected(f"SBV refused {url} (WAF); try again later")
    time.sleep(PAGE_DELAY)
    return html


def _period_from(text: str) -> dt.date | None:
    """'(Tháng 10 Năm 2025)' / 'THÁNG 06 NĂM 2026' -> 2025-10-01 / 2026-06-01."""
    m = re.search(r"thang\s*(\d{1,2})\s*nam\s*(\d{4})", strip_accents(text))
    if not m:
        return None
    month, year = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        return None
    return dt.date(year, month, 1)


def _assign(nums: list[float], level_first: bool) -> tuple[float | None, float | None]:
    """Split a row's numbers into (level, growth) when columns don't line up.

    Magnitude decides it when the two are unmistakable: SBV levels are balances
    in tỷ đồng (millions of them for system-wide credit) while growth is a
    percentage. Only when that test is inconclusive does header order apply —
    older tables on the same page order their columns differently, so trusting
    position alone silently swaps the two.
    """
    if not nums:
        return None, None
    if len(nums) == 1:
        return (nums[0], None) if level_first else (None, nums[0])
    big = [n for n in nums if abs(n) >= 1000]
    small = [n for n in nums if abs(n) < 1000]
    if len(big) == 1 and len(small) >= 1:
        return big[0], small[0]
    return (nums[0], nums[1]) if level_first else (nums[1], nums[0])


def _period_near(table, page_text: str) -> dt.date | None:
    """Find the reference month for a table.

    SBV nests the data table inside wrapper tables and puts the caption
    ("(Tháng 10 Năm 2025)") in the wrapper, so the month is usually *outside*
    the table holding the numbers. Walk up a few ancestors before falling back
    to the page as a whole.
    """
    node = table
    for _ in range(5):
        node = node.parent
        if node is None:
            break
        p = _period_from(node.get_text(" "))
        if p:
            return p
    return _period_from(page_text)


def parse_tables(html: str) -> list[dict]:
    """Pull (label, level, growth) rows out of every SBV statistics table."""
    soup = BeautifulSoup(html, "lxml")
    # Strip navigation before the page-level fallback looks for a month.
    for tag in soup.find_all(["nav", "header", "footer", "script", "style"]):
        tag.decompose()
    page_text = soup.get_text(" ")

    out: list[dict] = []
    seen: set[tuple] = set()
    for table in soup.find_all("table"):
        rows = [[norm_ws(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
                for tr in table.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue

        caption = " ".join(" ".join(r) for r in rows[:3])
        period = _period_from(caption) or _period_near(table, page_text)

        # Locate the header row: the one naming a level and/or a growth column.
        head_i, level_c, growth_c = None, None, None
        for i, r in enumerate(rows[:6]):
            folded = [strip_accents(c) for c in r]
            lv = next((j for j, c in enumerate(folded) if LEVEL_HINT.search(c)), None)
            gr = next((j for j, c in enumerate(folded) if GROWTH_HINT.search(c)), None)
            if lv is not None or gr is not None:
                head_i, level_c, growth_c = i, lv, gr
                break
        if head_i is None:
            continue
        header = rows[head_i]
        label_c = 1 if len(header) > 2 and strip_accents(header[0]) in {"stt", ""} else 0
        level_first = (level_c is None or growth_c is None or level_c < growth_c)

        for r in rows[head_i + 1:]:
            # Cell counts drift: the numbered sector rows carry an STT column
            # but "TỔNG CỘNG" does not, and older tables on the same page are
            # laid out differently again. So only trust the header's column
            # indices when the widths actually agree, and otherwise read the
            # row positionally: first non-numeric cell is the label, the
            # numbers that follow are the values in header order.
            if len(r) == len(header):
                label = r[label_c].strip(" -–")
                level = to_float(r[level_c]) if level_c is not None and level_c < len(r) else None
                growth = to_float(r[growth_c]) if growth_c is not None and growth_c < len(r) else None
            else:
                label, nums = "", []
                for cell in r:
                    val = to_float(cell)
                    if val is None and not label and cell.strip(" -–"):
                        label = cell.strip(" -–")
                    elif val is not None and label:
                        nums.append(val)
                level, growth = _assign(nums, level_first)

            if not label or to_float(label) is not None:
                continue
            if level is None and growth is None:
                continue
            # Nested wrapper tables re-expose the same rows; keep them once.
            key = (label, level, growth, period)
            if key in seen:
                continue
            seen.add(key)
            out.append({"label": label, "level": level, "growth": growth,
                        "period": period, "caption": norm_ws(caption)[:200]})
    return out


def to_observations(records: list[dict], *, page_id: str, url: str) -> list[dict]:
    today = dt.date.today()
    obs = []
    for r in records:
        if not r["period"]:
            log.warning("%s: no reference month in caption %r — skipped",
                        page_id, r["caption"][:80])
            continue
        item = slugify(r["label"], 40).upper()
        for suffix, value, unit in (
            ("LEVEL", r["level"], "billion VND"),
            ("GROWTH_YTD", r["growth"], "% vs end of previous year"),
        ):
            if value is None:
                continue
            obs.append({
                "series_id": f"SBV.{page_id.upper()}.{suffix}.{item}",
                "dataset": "sbv_monetary",
                "source": "SBV",
                "freq": "M",
                "date": r["period"],
                "ref_period": f"{r['period'].year}-M{r['period'].month:02d}",
                "value": value,
                "unit": unit,
                "scale": 0,
                "status": "published",
                # SBV pages carry no publication date, only a reference month.
                "vintage": today,
                "partner": None,
                "breakdown": r["label"],
                "label_vi": r["label"],
                "measure": suffix,
                "dims": {"page": page_id, "url": url, "caption": r["caption"]},
                "raw_file": f"sbv:{page_id}",
            })
    return obs


def collect(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """Fetch and parse each configured page. Returns (observations, skipped)."""
    obs: list[dict] = []
    skipped: list[str] = []
    archive = PATHS.raw / "sbv"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()

    for page in pages:
        if not page.get("enabled", True):
            continue
        pid, slug = page["id"], page["slug"]
        try:
            html = fetch_page(slug)
        except Rejected as exc:
            log.warning("%s", exc)
            skipped.append(pid)
            continue
        except Exception as exc:
            log.error("SBV %s failed: %s", pid, exc)
            skipped.append(pid)
            continue
        # Keep the page as fetched — SBV overwrites it next month.
        (archive / f"{stamp}__{pid}.html").write_text(html, encoding="utf-8")
        recs = parse_tables(html)
        page_obs = to_observations(recs, page_id=pid, url=slug)
        log.info("SBV %s: %d rows -> %d observations", pid, len(recs), len(page_obs))
        if not page_obs:
            skipped.append(pid)
        obs.extend(page_obs)
    return obs, skipped
