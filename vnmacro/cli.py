"""Command line entry point.

    python -m vnmacro.cli all                 # everything, incremental
    python -m vnmacro.cli nso --from-year 2015
    python -m vnmacro.cli imf
    python -m vnmacro.cli cpi
    python -m vnmacro.cli panel
    python -m vnmacro.cli status
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

import polars as pl

from . import store
from .config import PATHS, sources
from .sources import customs, imf, nso_catalog, nso_cpi, nso_docx, nso_xlsx, sbv
from .transform import cpi_chain, panel

log = logging.getLogger("vnmacro")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


# ---------------------------------------------------------------------- NSO --

def _parse_release(meta: dict, paths: list[str], post_id) -> int:
    """Run every parser over one release's files. Returns rows written."""
    n = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            log.debug("missing from archive: %s", p.name)
            continue
        key = f"{meta['ref_period']}-{post_id}"
        try:
            if p.suffix.lower() in (".xlsx", ".xls"):
                raw = nso_xlsx.parse_workbook(p)
                if not raw:
                    continue
                obs = nso_xlsx.to_observations(raw, release=meta, raw_file=p.name)
                store.write(store.frame(obs), "nso_monthly_tables", key)
                cpi_obs = nso_cpi.extract(raw, release=meta, raw_file=p.name)
                store.write(store.frame(cpi_obs), "nso_cpi", key)
                n += len(obs) + len(cpi_obs)
            elif p.suffix.lower() in (".docx", ".doc"):
                paras = nso_docx.read_text(p)
                obs = nso_docx.extract(paras, release=meta, raw_file=p.name)
                store.write(store.frame(obs), "nso_narrative", key)
                n += len(obs)
        except Exception as exc:
            log.error("parse failed for %s: %s", p.name, exc)
    return n


def cmd_reparse(args) -> None:
    """Re-run the parsers over the local archive — no network at all.

    Parsers change more often than the source data does; this replays a fix
    across every release already downloaded, in seconds instead of an hour.
    """
    done = store.load_state("nso_done")
    if not done:
        log.error("nothing in the archive yet — run `nso` first")
        return
    total = done_n = 0
    for marker, rec in sorted(done.items()):
        if not isinstance(rec, dict):
            continue
        ref = rec.get("ref_period")
        if not ref:
            continue
        meta = {k: rec.get(k) for k in
                ("freq", "ref_period", "period_date", "month_ref", "month_date", "wp_date")}
        if not meta.get("period_date"):
            # State written before --reparse existed: rebuild what can be
            # rebuilt from the reference period, and leave the publication
            # date null rather than guessing a vintage.
            meta.update(_meta_from_ref(ref))
        files = rec.get("files") or _archive_files(marker, ref)
        if not files:
            continue
        if args.from_year and int(ref[:4]) < args.from_year:
            continue
        n = _parse_release(meta, files, marker.split(":")[-1])
        total += n
        done_n += 1
        log.info("%s %s: %d observations", ref, marker, n)
    log.info("reparsed %d releases -> %d observations", done_n, total)


def _meta_from_ref(ref: str) -> dict:
    """'2026-M07' / '2026-Q2' -> the period fields, without the title."""
    year = int(ref[:4])
    if "-M" in ref:
        month = int(ref.split("-M")[1])
        freq, period_date = "M", dt.date(year, month, 1)
    elif "-Q" in ref:
        q = int(ref.split("-Q")[1])
        freq, period_date, month = "Q", dt.date(year, 3 * q - 2, 1), 3 * q
    else:
        freq, period_date, month = "A", dt.date(year, 1, 1), 12
    if freq == "M":
        month = int(ref.split("-M")[1])
    return {"freq": freq, "ref_period": ref, "period_date": period_date.isoformat(),
            "month_ref": f"{year}-M{month:02d}",
            "month_date": dt.date(year, month, 1).isoformat(), "wp_date": ""}


def _archive_files(marker: str, ref: str) -> list[str]:
    """Locate a release's archived files by tag and reference-period prefix."""
    tag = marker.split(":")[0]
    d = PATHS.raw / "nso" / tag
    if not d.exists():
        return []
    return [str(p) for p in sorted(d.glob(f"{ref}__*"))
            if p.suffix.lower() in (".xlsx", ".xls", ".docx", ".doc")]


def cmd_nso(args) -> None:
    if getattr(args, "reparse", False):
        return cmd_reparse(args)

    cfg = sources().get("nso", {})
    from_year = args.from_year or cfg.get("backfill_from", 2015)
    tags = args.tags or [t for t, v in cfg.get("tags", {}).items() if v.get("enabled")]

    done = store.load_state("nso_done")
    for tag in tags:
        log.info("cataloguing NSO tag %s", tag)
        releases = nso_catalog.list_posts(tag)
        releases = [r for r in releases
                    if r.ref_period and r.period_date
                    and int(r.period_date[:4]) >= from_year]
        if args.limit:
            releases = releases[: args.limit]
        log.info("%s: %d releases from %s onwards", tag, len(releases), from_year)

        for rel in releases:
            marker = f"{tag}:{rel.post_id}"
            if marker in done and not args.force:
                continue
            log.info("-> %s (%s)", rel.ref_period, rel.title[:70])
            nso_catalog.fetch(rel)
            meta = rel.to_dict()
            files = [a["path"] for a in rel.attachments
                     if a.get("path") and not a.get("error")]
            n = _parse_release(meta, files, rel.post_id)
            log.info("   %d observations", n)
            # Record enough to replay the parsers offline (`nso --reparse`).
            done[marker] = {
                "ref_period": rel.ref_period, "freq": rel.freq,
                "period_date": rel.period_date, "month_ref": rel.month_ref,
                "month_date": rel.month_date, "wp_date": rel.wp_date,
                "title": rel.title, "files": files,
                "at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            store.save_state("nso_done", done)


# ---------------------------------------------------------------------- IMF --

def cmd_imf(args) -> None:
    cfg = sources().get("imf", {})
    start = args.start or cfg.get("default_start", "2000")
    for flow in cfg.get("flows", []):
        if not flow.get("enabled"):
            log.info("skipping %s (disabled: %s)", flow["id"],
                     (flow.get("note") or "").split(".")[0].strip())
            continue
        if args.flows and flow["id"] not in args.flows:
            continue
        log.info("IMF %s ...", flow["id"])
        try:
            rows = imf.fetch(flow["agency"], flow["id"],
                             filters=flow.get("filters") or {}, start=start)
        except Exception as exc:
            log.error("IMF %s failed: %s", flow["id"], exc)
            continue
        obs = imf.to_observations(rows, dataset=flow["dataset"], flow=flow["id"])
        store.write(store.frame(obs), flow["dataset"], dt.date.today().isoformat())
        log.info("IMF %s: %d observations", flow["id"], len(obs))


# ---------------------------------------------------------------------- SBV --

def cmd_sbv(args) -> None:
    cfg = sources().get("sbv", {})
    if not cfg.get("enabled", True):
        log.info("SBV disabled in sources.yaml")
        return
    pages = cfg.get("pages", [])
    if args.pages:
        pages = [p for p in pages if p["id"] in args.pages]
    obs, skipped = sbv.collect(pages)
    if obs:
        # Key on (month, page), not month alone: a WAF-skipped page is often
        # re-run on its own later, and a month-only key would make that second
        # run overwrite the pages the first run had already stored.
        by_key: dict[tuple[str, str], list[dict]] = {}
        for o in obs:
            page = o["series_id"].split(".")[1].lower()
            by_key.setdefault((o["ref_period"], page), []).append(o)
        for (ref, page), rows in by_key.items():
            store.write(store.frame(rows), "sbv_monetary", f"{ref}-{page}")
    log.info("SBV: %d observations, %d page(s) skipped%s",
             len(obs), len(skipped),
             f" ({', '.join(skipped)})" if skipped else "")
    if skipped:
        log.info("SBV shows only the current month and has no archive — "
                 "re-run later to catch the skipped pages before it rolls over")


# ------------------------------------------------------------------ customs --

def cmd_customs(args) -> None:
    cfg = sources().get("customs", {})
    if not cfg.get("enabled", True):
        log.info("customs disabled in sources.yaml")
        return
    try:
        rows = (customs.catalog_from_json(Path(args.catalog)) if args.catalog
                else customs.refresh_catalog())
    except PermissionError as exc:
        log.warning("%s", exc)
        return
    entries = customs.normalise(rows)
    log.info("customs catalog: %d entries", len(entries))
    if not cfg.get("download_pdfs", True):
        return
    got = customs.download(entries)
    log.info("downloaded %d PDFs", len(got))

    if not (args.parse or cfg.get("parse_pdfs")):
        return
    for e in got:
        p = Path(e["path"])
        try:
            recs = customs.parse_pdf(p, max_pages=args.max_pages)
            obs = customs.to_observations(recs, entry=e, raw_file=p.name)
            store.write(store.frame(obs), "customs_trade", p.stem)
            log.info("%s: %d observations", p.name, len(obs))
        except Exception as exc:
            log.error("customs parse failed for %s: %s", p.name, exc)


# ------------------------------------------------------------------ derived --

def cmd_cpi(args) -> None:
    df = cpi_chain.build()
    if df.is_empty():
        return
    obs = cpi_chain.to_observations(df)
    store.write(store.frame(obs), "cpi_chained", "latest")
    print()
    print(cpi_chain.report(df))


def cmd_panel(args) -> None:
    shares = panel.trade_shares()
    if not shares.is_empty():
        latest = shares["date"].max()
        top = (shares.filter((pl.col("date") == latest) & (pl.col("flow") == "exports"))
                     .sort("share_12m", descending=True, nulls_last=True).head(8))
        print(f"\nTop export partners (12m share, {latest}):")
        for r in top.iter_rows(named=True):
            s = r["share_12m"]
            if s is not None:
                print(f"  {r['partner']}  {s * 100:5.2f}%")
    wide = panel.monthly_panel()
    if not wide.is_empty():
        print(f"\nmonthly panel: {wide.height} months x {len(wide.columns)} columns")
        print(f"  {wide['date'].min()} -> {wide['date'].max()}")


def cmd_status(args) -> None:
    print(f"data root: {PATHS.root}")
    base = PATHS.curated
    if not base.exists():
        print("  (empty — run `all` first)")
        return
    rows = []
    for d in sorted(base.glob("dataset=*")):
        name = d.name.split("=", 1)[1]
        for f in sorted(d.glob("freq=*")):
            freq = f.name.split("=", 1)[1]
            files = list(f.glob("*.parquet"))
            if not files:
                continue
            df = pl.concat([pl.read_parquet(x) for x in files], how="diagonal_relaxed")
            rows.append((name, freq, len(files), df.height,
                         df["series_id"].n_unique(), str(df["date"].min()),
                         str(df["date"].max())))
    if not rows:
        print("  (empty)")
        return
    print(f"\n{'dataset':<26}{'f':<3}{'files':>6}{'rows':>10}{'series':>8}  range")
    for r in rows:
        print(f"{r[0]:<26}{r[1]:<3}{r[2]:>6}{r[3]:>10}{r[4]:>8}  {r[5]} .. {r[6]}")
    raw = sum(f.stat().st_size for f in PATHS.raw.rglob("*") if f.is_file())
    cur = sum(f.stat().st_size for f in base.rglob("*.parquet"))
    print(f"\nraw archive {raw / 1e6:,.1f} MB   parquet {cur / 1e6:,.1f} MB")


def cmd_all(args) -> None:
    cmd_nso(args)
    cmd_imf(args)
    cmd_sbv(args)
    cmd_customs(args)
    cmd_cpi(args)
    cmd_panel(args)
    cmd_status(args)


def cmd_disclaimer(args) -> None:
    """In tuyên bố miễn trừ. Đặt ở CLI để người chạy dòng lệnh cũng đọc được,
    không chỉ người đọc README trên GitHub."""
    from . import DISCLAIMER
    print(DISCLAIMER)


def _utf8_console() -> None:
    """Console Windows mặc định dung cp1252/cp437, không mã hoá nổi tiếng Việt.
    Thiếu đoạn này thì ngay `--help` đã ném UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _utf8_console()
    p = argparse.ArgumentParser(prog="vnmacro", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    n = sub.add_parser("nso", help="NSO monthly reports (tables + narrative)")
    n.add_argument("--from-year", type=int)
    n.add_argument("--limit", type=int, help="only the N most recent releases")
    n.add_argument("--tags", nargs="*")
    n.add_argument("--force", action="store_true", help="re-fetch already-done releases")
    n.add_argument("--reparse", action="store_true",
                   help="re-run the parsers over the local archive, no network")
    n.set_defaults(func=cmd_nso)

    i = sub.add_parser("imf", help="IMF SDMX flows")
    i.add_argument("--start", help="e.g. 2000 or 2015-01")
    i.add_argument("--flows", nargs="*", help="subset of flow ids")
    i.set_defaults(func=cmd_imf)

    b = sub.add_parser("sbv", help="State Bank of Vietnam monetary statistics")
    b.add_argument("--pages", nargs="*", help="subset of page ids")
    b.set_defaults(func=cmd_sbv)

    c = sub.add_parser("customs", help="Vietnam Customs releases")
    c.add_argument("--catalog", help="path to a catalog JSON saved from the browser")
    c.add_argument("--parse", action="store_true", help="parse the PDF tables")
    c.add_argument("--max-pages", type=int, default=None)
    c.set_defaults(func=cmd_customs)

    x = sub.add_parser("cpi", help="chain-link CPI across basket revisions")
    x.set_defaults(func=cmd_cpi)

    pl_ = sub.add_parser("panel", help="trade shares + wide monthly panel")
    pl_.set_defaults(func=cmd_panel)

    s = sub.add_parser("status", help="what is in the store")
    s.set_defaults(func=cmd_status)

    a = sub.add_parser("all", help="run everything")
    d_ = sub.add_parser("disclaimer", help="tuyên bố miễn trừ")
    d_.set_defaults(func=cmd_disclaimer)
    a.add_argument("--from-year", type=int)
    a.add_argument("--limit", type=int)
    a.add_argument("--tags", nargs="*")
    a.add_argument("--force", action="store_true")
    a.add_argument("--start")
    a.add_argument("--flows", nargs="*")
    a.add_argument("--catalog")
    a.add_argument("--parse", action="store_true")
    a.add_argument("--max-pages", type=int, default=None)
    a.add_argument("--pages", nargs="*")
    a.set_defaults(func=cmd_all)

    args = p.parse_args(argv)
    if getattr(args, "func", None) is cmd_disclaimer:
        cmd_disclaimer(args)          # không cần log, không tạo thư mục dữ liệu
        return 0
    _setup_logging(args.verbose)
    PATHS.ensure()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
