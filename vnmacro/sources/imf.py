"""IMF SDMX 2.1 client (data.imf.org / api.imf.org).

Covers the parts of a small-open-economy model that NSO does not publish
monthly, or publishes only as prose:

    IMTS      trade in goods by partner country  (the former DOTS) — the
              bilateral panel behind import/export shares
    ITG       trade in goods, totals
    BOP       balance of payments, incl. direct investment in/out
    MFS_MA    monetary aggregates (M1/M2/base money)
    MFS_IR    policy, deposit and lending rates
    ER / EER  exchange rates, NEER/REER
    QNEA/ANEA national accounts (GDP and its expenditure split)
    QGFS      quarterly government finance statistics
    CPI       IMF-harmonised CPI, useful as an independent check on NSO
    PCPS      primary commodity prices (import-price / terms-of-trade shocks)

Dimension order differs per dataflow and IMF changes it between vintages, so
keys are assembled from the dataflow's own DSD rather than hardcoded.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from .. import http
from ..config import PATHS
from ..util import sdmx_freq, sdmx_period_to_date

log = logging.getLogger(__name__)

BASE = "https://api.imf.org/external/sdmx/2.1"
NS_STR = "{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure}"

_DSD_CACHE: dict[str, list[str]] = {}


def _cache_path(agency: str, flow: str, version: str) -> Path:
    return PATHS.state / "imf_dsd" / f"{agency}_{flow}_{version}.json"


def dimensions(agency: str, flow: str, version: str = "latest") -> list[str]:
    """Ordered dimension ids of a dataflow, TIME_PERIOD excluded."""
    ck = f"{agency}/{flow}/{version}"
    if ck in _DSD_CACHE:
        return _DSD_CACHE[ck]
    cp = _cache_path(agency, flow, version)
    if cp.exists():
        dims = json.loads(cp.read_text(encoding="utf-8"))
        _DSD_CACHE[ck] = dims
        return dims

    dims: list[str] = []
    # The dataflow carries a reference to its DSD; ask for both in one call.
    urls = [
        f"{BASE}/dataflow/{agency}/{flow}/{version}?references=all",
        f"{BASE}/datastructure/{agency}/DSD_{flow}",
    ]
    for url in urls:
        try:
            r = http.get(url)
        except Exception as exc:
            log.debug("DSD lookup %s failed: %s", url, exc)
            continue
        if not r.content or r.status_code == 204:
            continue
        root = ET.fromstring(r.content)
        found: list[tuple[int, str]] = []
        for dl in root.iter(NS_STR + "DimensionList"):
            for d in dl:
                if d.tag.endswith("}Dimension") and d.get("id"):
                    found.append((int(d.get("position") or 0), d.get("id")))
        if found:
            found.sort()
            dims = [name for _, name in found]
            break
    if not dims:
        raise RuntimeError(f"could not resolve dimensions for {agency}:{flow}")

    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(dims), encoding="utf-8")
    _DSD_CACHE[ck] = dims
    return dims


def build_key(agency: str, flow: str, version: str, filters: dict[str, str]) -> str:
    """'{COUNTRY: VNM, FREQUENCY: M}' -> 'VNM...M' for this flow's dim order."""
    dims = dimensions(agency, flow, version)
    unknown = set(filters) - set(dims)
    if unknown:
        log.warning("%s: ignoring unknown dimensions %s (available: %s)",
                    flow, sorted(unknown), dims)
    return ".".join(str(filters.get(d, "")) for d in dims)


def fetch(agency: str, flow: str, *, version: str = "latest",
          filters: dict | None = None, start: str | None = None,
          end: str | None = None) -> list[dict]:
    """Return one dict per observation, with all series dimensions attached."""
    key = build_key(agency, flow, version, filters or {})
    url = f"{BASE}/data/{agency},{flow},{version}/{key}"
    params = {}
    if start:
        params["startPeriod"] = start
    if end:
        params["endPeriod"] = end
    r = http.get(url, params=params)
    if not r.content:
        return []

    root = ET.fromstring(r.content)
    out: list[dict] = []
    for series in root.iter():
        if not series.tag.endswith("}Series") and series.tag != "Series":
            continue
        dims = dict(series.attrib)
        for obs in series:
            oa = obs.attrib
            period = oa.get("TIME_PERIOD")
            value = oa.get("OBS_VALUE")
            if period is None or value in (None, "", "NaN"):
                continue
            try:
                val = float(value)
            except ValueError:
                continue
            out.append({"dims": dims, "period": period, "value": val,
                        "obs_attrs": {k: v for k, v in oa.items()
                                      if k not in ("TIME_PERIOD", "OBS_VALUE")}})
    return out


#: Các chiều XÁC ĐỊNH PHÉP ĐO, phải nằm trong series_id thì id mới là duy nhất.
#: Thứ tự cố định để id ổn định giữa các lần chạy. Chiều nào không có thì bỏ qua,
#: nên các flow không dùng chúng (ví dụ IMTS) giữ nguyên id như cũ.
MEASURE_DIMS = ("PRICE_TYPE", "TYPE_OF_TRANSFORMATION", "DATA_TRANSFORMATION",
                "BOP_ACCOUNTING_ENTRY", "COICOP_1999", "SECTOR")


def to_observations(rows: list[dict], *, dataset: str, flow: str,
                    vintage: dt.date | None = None) -> list[dict]:
    """Map raw SDMX observations onto the store schema."""
    vintage = vintage or dt.date.today()
    obs = []
    for r in rows:
        d = r["dims"]
        period = r["period"]
        date = sdmx_period_to_date(period)
        if date is None:
            continue
        indicator = (d.get("INDICATOR") or d.get("INDEX_TYPE")
                     or d.get("COICOP_1999") or "VALUE")
        partner = d.get("COUNTERPART_COUNTRY")
        # Keep every dimension bar the ones already promoted to columns.
        extra = {k: v for k, v in d.items()
                 if k not in {"COUNTRY", "INDICATOR", "COUNTERPART_COUNTRY",
                              "FREQUENCY", "SCALE"}}
        extra["flow"] = flow
        # Chỉ (flow, indicator, partner) là KHÔNG đủ để định danh một chuỗi.
        # Nhiều flow trả về cùng một indicator ở nhiều dạng đo khác nhau — giá
        # hiện hành và giá so sánh (ANEA PRICE_TYPE), ghi có và ghi nợ (BOP
        # ACCOUNTING_ENTRY), mức và chỉ số (TYPE_OF_TRANSFORMATION). Bỏ các
        # chiều đó ra khỏi id thì hai quan sát khác bản chất trùng khoá
        # (series_id, date), và bên đọc sẽ nhặt bừa một cái.
        #
        # Đo trên kho ngày 31/8/2026: 100% cặp khoá của imf_commodity_prices và
        # imf_cpi bị trùng, imf_exchange_rates 99,4%, imf_national_accounts
        # 88,8%, imf_bop 44,0%. Giá trị vẫn đúng và dims vẫn giữ đủ — chỉ id là
        # hỏng, nên vá ở đây là đủ, không mất dữ liệu nào.
        qual = ".".join(d[k] for k in MEASURE_DIMS if d.get(k))
        sid = (f"IMF.{flow}.{indicator}"
               + (f".{partner}" if partner else "")
               + (f".{qual}" if qual else ""))
        obs.append({
            "series_id": sid,
            "dataset": dataset,
            "source": "IMF",
            "freq": d.get("FREQUENCY") or sdmx_freq(period),
            "date": date,
            "ref_period": period,
            "value": r["value"],
            "unit": d.get("UNIT") or d.get("UNIT_MEASURE"),
            # SDMX SCALE is a *display* hint; OBS_VALUE is stored unscaled.
            "scale": int(d["SCALE"]) if str(d.get("SCALE", "")).lstrip("-").isdigit() else 0,
            "status": r["obs_attrs"].get("STATUS"),
            "vintage": vintage,
            "partner": partner,
            "breakdown": indicator,
            "label_vi": None,
            "measure": indicator,
            "dims": extra,
            "raw_file": f"imf:{flow}",
        })
    return obs
