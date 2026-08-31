"""Cục Hải quan (Vietnam Customs) periodic trade releases.

Two things to know before using this module.

1. The catalog behind "Số liệu định kỳ" is served by
   ``/bridge?url=/customs/api/GetTKHQInfo``, and every call from outside a
   browser session comes back ``{"message": "Invalid Captcha"}``. This
   pipeline does **not** attempt to defeat that. ``refresh_catalog`` tries the
   plain call and, when it is refused, tells you to export the listing from
   the browser instead (``catalog_from_json``). Automated bilateral history
   comes from IMF IMTS, which is an open API.

2. The releases themselves are PDFs on ``files.customs.gov.vn``, which is
   open — once you have the URLs, downloading and parsing is unattended.
   The tables pack several logical rows into one PDF cell as newline-separated
   lines, so the parser splits and re-zips them.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from .. import http
from ..config import PATHS, sources
from ..util import slugify, to_float

log = logging.getLogger(__name__)

CAPTCHA_MSG = "Invalid Captcha"

CAPTCHA_HELP = """\
Vietnam Customs refused the catalog call with "Invalid Captcha".

The listing is only served to a browser session that has cleared the site's
CAPTCHA, and this pipeline will not work around that. To refresh the catalog:

  1. open https://www.customs.gov.vn/index.jsp?pageId=5002
  2. open DevTools -> Network, then reload
  3. copy the JSON response of the `GetTKHQInfo` request
  4. save it to  <data>/raw/customs/catalog_YYYY-MM-DD.json
  5. re-run:  python -m vnmacro.cli customs --catalog <that file>

Bilateral trade history is collected automatically from IMF IMTS, so this
step is only needed for the official Vietnamese figures at commodity detail.
"""

# 2026-t7-5x(vn-sb).pdf  ->  kind letters: X = xuất khẩu, N = nhập khẩu
KIND = {"x": "exports", "n": "imports"}


def refresh_catalog(*, page_size: int | None = None) -> list[dict]:
    """Attempt the open catalog call. Raises with instructions if gated."""
    cfg = sources().get("customs", {})
    url = cfg.get("catalog_url")
    take = page_size or cfg.get("page_size", 20)
    payload = {
        "skip": 0, "take": take, "ky": "", "textSearch": "", "the_loai": "0",
        "thoigianCongBo": "", "typeName": "GetListSoLieu", "language": "TIENG_VIET",
    }
    r = http.SESSION.post(
        url, data=payload, timeout=60,
        headers={"X-Requested-With": "XMLHttpRequest",
                 "Referer": cfg.get("referer", ""),
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    r.raise_for_status()
    body = r.json()
    if body.get("message") == CAPTCHA_MSG or body.get("arr") is None:
        raise PermissionError(CAPTCHA_HELP)
    return body["arr"]


def catalog_from_json(path: Path) -> list[dict]:
    """Load a catalog response saved from the browser."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("arr") if isinstance(data, dict) else data
    if not rows:
        raise ValueError(f"no `arr` array in {path}")
    return rows


def normalise(rows: list[dict]) -> list[dict]:
    """Flatten the CMS records into something worth keeping."""
    out = []
    for r in rows:
        url = r.get("FILE_SO_BO") or ""
        if not url.lower().startswith("http"):
            # FILE_CHINH_THUC points at an internal 10.x address — unreachable.
            url = ""
        period = r.get("NGAY_CONG_BO") or ""            # "07-2026"
        m = re.fullmatch(r"(\d{2})-(\d{4})", period.strip())
        out.append({
            "id": r.get("MA"),
            "title": r.get("TIEU_DE", "").strip(),
            "month": int(m.group(1)) if m else None,
            "year": int(m.group(2)) if m else None,
            "published_prelim": r.get("NGAY_SO_BO"),
            "published_final": r.get("NGAY_CHINH_THUC"),
            "status": r.get("TEN_THE_LOAI"),
            "url": url,
        })
    return out


def download(entries: list[dict]) -> list[dict]:
    """Fetch every PDF that has a reachable URL."""
    got = []
    for e in entries:
        if not e.get("url"):
            continue
        name = f"{e['year']}-{e['month']:02d}__{slugify(e['title'], 70)}.pdf" \
            if e.get("year") else f"{slugify(e['title'], 70)}.pdf"
        dest = PATHS.raw / "customs" / name
        try:
            e.update(http.download(e["url"], dest))
            got.append(e)
        except Exception as exc:
            log.error("customs download failed %s: %s", e["url"], exc)
    return got


# ------------------------------------------------------------ PDF parsing ---

_NUM = re.compile(r"^-?[\d.]+(?:,\d+)?$")


def _split_cell(cell: str | None) -> list[str]:
    return [s.strip() for s in (cell or "").split("\n")]


def parse_pdf(path: Path, *, max_pages: int | None = None) -> list[dict]:
    """Best-effort extraction of the country x commodity tables.

    Each PDF cell holds several logical rows as newline-separated lines that
    line up across columns, so the cells are split and zipped back together.
    Rows whose numeric columns do not line up are skipped rather than guessed.
    """
    import pdfplumber

    records: list[dict] = []
    with pdfplumber.open(str(path)) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        for pageno, page in enumerate(pages, 1):
            for table in page.extract_tables():
                if len(table) < 3:
                    continue
                for raw_row in table[2:]:
                    cols = [_split_cell(c) for c in raw_row]
                    if len(cols) < 4:
                        continue
                    labels, units = cols[0], cols[1]
                    numerics = cols[2:]
                    n = len(labels)
                    if n == 0 or any(len(c) not in (n, 1, 0) for c in numerics):
                        continue
                    country = labels[0] if labels else ""
                    for i in range(n):
                        label = labels[i].strip()
                        if not label:
                            continue
                        vals = []
                        for c in numerics:
                            v = c[i] if len(c) == n else (c[0] if len(c) == 1 else "")
                            vals.append(to_float(v) if _NUM.match(v.strip() or "x") else None)
                        if not any(v is not None for v in vals):
                            continue
                        records.append({
                            "page": pageno,
                            "country": country,
                            "item": label if i else "TOTAL",
                            "unit": units[i].strip() if i < len(units) else None,
                            "qty_month": vals[0] if len(vals) > 0 else None,
                            "value_month": vals[1] if len(vals) > 1 else None,
                            "qty_ytd": vals[2] if len(vals) > 2 else None,
                            "value_ytd": vals[3] if len(vals) > 3 else None,
                        })
    return records


def to_observations(records: list[dict], *, entry: dict, raw_file: str) -> list[dict]:
    year, month = entry.get("year"), entry.get("month")
    if not (year and month):
        return []
    date = dt.date(year, month, 1)
    kind = "exports" if re.search(r"xu[ấa]t kh[ẩa]u", entry.get("title", ""), re.I) else "imports"
    vintage = None
    if entry.get("published_prelim"):
        try:
            d, m, y = entry["published_prelim"].split("/")
            vintage = dt.date(int(y), int(m), int(d))
        except Exception:
            pass

    out = []
    for r in records:
        for field, scope, unit in (
            ("value_month", "month", "USD"), ("value_ytd", "ytd", "USD"),
            ("qty_month", "month", r.get("unit")), ("qty_ytd", "ytd", r.get("unit")),
        ):
            if r.get(field) is None:
                continue
            out.append({
                "series_id": f"CUSTOMS.{kind.upper()}.{slugify(r['country'], 24).upper()}"
                             f".{slugify(r['item'], 30).upper()}.{scope.upper()}"
                             f".{'VALUE' if field.startswith('value') else 'QTY'}",
                "dataset": "customs_trade",
                "source": "CUSTOMS",
                "freq": "M",
                "date": date,
                "ref_period": f"{year}-M{month:02d}",
                "value": r[field],
                "unit": unit,
                "scale": 0,
                "status": "so_bo",
                "vintage": vintage,
                "partner": r["country"],
                "breakdown": r["item"],
                "label_vi": r["item"],
                "measure": f"{kind} {scope}",
                "dims": {"page": r["page"], "flow": kind},
                "raw_file": raw_file,
            })
    return out
