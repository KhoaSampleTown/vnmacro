"""Model-ready outputs: partner trade shares and a wide monthly panel.

Two artefacts land in ``data/panel/``:

  ``trade_shares.parquet``   partner-country weights in exports and imports,
                             monthly plus a 12-month rolling version. These are
                             the openness/weight parameters a small-open-economy
                             DSGE needs, and the rolling version is the one to
                             calibrate with — single months are noisy.

  ``vn_macro_monthly.parquet`` one row per month, one column per headline
                             series, ready to hand to an estimation routine.
"""
from __future__ import annotations

import logging

import polars as pl

from .. import store
from ..config import PATHS

log = logging.getLogger(__name__)

EXPORTS = "XG_FOB_USD"
IMPORTS = "MG_CIF_USD"

#: IMTS counterpart codes that are country groups, not countries. Including
#: them in the denominator would double-count.
AGGREGATE_HINT = ("W", "_")
KNOWN_AGGREGATES = {
    "W00", "W00A", "W00B", "A10", "R1", "5Y", "XW", "ALL", "WLD", "WORLD",
}


def _is_country(code: str | None) -> bool:
    if not code:
        return False
    if code in KNOWN_AGGREGATES:
        return False
    if any(ch.isdigit() for ch in code):
        return False
    return len(code) == 3 and code.isalpha()


def trade_shares(min_months: int = 1) -> pl.DataFrame:
    """Partner shares of Vietnam's monthly goods exports and imports."""
    df = store.read("imf_trade_partner", "M")
    if df.is_empty():
        log.warning("no IMF IMTS data — run `python -m vnmacro.cli imf` first")
        return pl.DataFrame()

    df = (df.filter(pl.col("measure").is_in([EXPORTS, IMPORTS]))
            .filter(pl.col("partner").map_elements(_is_country, return_dtype=pl.Boolean))
            .sort("vintage", descending=True, nulls_last=True)
            .unique(subset=["date", "partner", "measure"], keep="first"))
    if df.is_empty():
        return pl.DataFrame()

    df = df.select(
        "date", "partner",
        pl.when(pl.col("measure") == EXPORTS).then(pl.lit("exports"))
          .otherwise(pl.lit("imports")).alias("flow"),
        pl.col("value").alias("value_usd"),
    )

    totals = df.group_by(["date", "flow"]).agg(
        pl.col("value_usd").sum().alias("total_usd"))
    out = (df.join(totals, on=["date", "flow"])
             .with_columns((pl.col("value_usd") / pl.col("total_usd")).alias("share"))
             .sort(["flow", "partner", "date"]))

    # A 12-month rolling share is what you actually calibrate against; single
    # months swing on shipment timing and Tet.
    out = out.with_columns([
        pl.col("value_usd").rolling_sum(window_size=12, min_samples=6)
          .over(["flow", "partner"]).alias("value_usd_12m"),
        pl.col("total_usd").rolling_sum(window_size=12, min_samples=6)
          .over(["flow", "partner"]).alias("total_usd_12m"),
    ]).with_columns(
        (pl.col("value_usd_12m") / pl.col("total_usd_12m")).alias("share_12m")
    )

    if min_months > 1:
        keep = (out.group_by(["flow", "partner"]).len()
                   .filter(pl.col("len") >= min_months)
                   .select("flow", "partner"))
        out = out.join(keep, on=["flow", "partner"])

    PATHS.panel.mkdir(parents=True, exist_ok=True)
    dest = PATHS.panel / "trade_shares.parquet"
    out.write_parquet(dest, compression="zstd")
    log.info("wrote %d partner-months -> %s", out.height, dest)
    return out


#: Headline monthly series for the wide panel. Left side is the column name.
PANEL_SERIES: dict[str, str] = {
    # prices
    "cpi_chained":            "NSO.CPI.CHAINED.HEADLINE",
    "cpi_yoy_chained":        "NSO.CPI.CHAINED_YOY.HEADLINE",
    "cpi_mom_published":      "NSO.CPI.MOM.HEADLINE",
    "cpi_yoy_published":      "NSO.CPI.YOY.HEADLINE",
    # fiscal
    "budget_revenue_month":   "NSO.FISCAL.REV.TOTAL.MONTH",
    "budget_revenue_ytd":     "NSO.FISCAL.REV.TOTAL.YTD",
    "budget_expend_month":    "NSO.FISCAL.EXP.TOTAL.MONTH",
    "budget_expend_ytd":      "NSO.FISCAL.EXP.TOTAL.YTD",
    "budget_capex_ytd":       "NSO.FISCAL.EXP.DEVINVEST.YTD",
    "budget_current_ytd":     "NSO.FISCAL.EXP.CURRENT.YTD",
    "budget_interest_ytd":    "NSO.FISCAL.EXP.INTEREST.YTD",
    "public_investment_ytd":  "NSO.INVEST.PUBLIC.YTD",
    # external
    "trade_balance_month":    "NSO.TRADE.BALANCE.MONTH",
    "trade_balance_ytd":      "NSO.TRADE.BALANCE.YTD",
    "trade_turnover_month":   "NSO.TRADE.TURNOVER.MONTH",
    "fdi_disbursed_ytd":      "NSO.FDI.DISBURSED.YTD",
    "usd_index_mom":          "NSO.FX.USD_INDEX.MOM",
    # monetary (only populated for issues that still carried a banking section)
    "credit_growth_ytd":      "NSO.MONEY.CREDIT.GROWTH_YTD",
    "m2_growth_ytd":          "NSO.MONEY.M2.GROWTH_YTD",
}


def monthly_panel() -> pl.DataFrame:
    """Pivot the headline series into one row per month."""
    frames = [store.read(ds, "M") for ds in
              ("nso_narrative", "nso_cpi", "cpi_chained", "imf_exchange_rates")]
    frames = [f for f in frames if not f.is_empty()]
    if not frames:
        log.warning("nothing to build a panel from")
        return pl.DataFrame()
    df = pl.concat(frames, how="diagonal_relaxed")

    wanted = {v: k for k, v in PANEL_SERIES.items()}
    df = (df.filter(pl.col("series_id").is_in(list(wanted)))
            .sort("vintage", descending=True, nulls_last=True)
            .unique(subset=["series_id", "date"], keep="first")
            .with_columns(pl.col("series_id").replace_strict(wanted, default=None)
                          .alias("column")))
    if df.is_empty():
        log.warning("none of the panel series are present yet")
        return pl.DataFrame()

    wide = (df.pivot(on="column", index="date", values="value", aggregate_function="first")
              .sort("date"))
    # VND/USD from IMF, if collected
    fx = store.read("imf_exchange_rates", "M")
    if not fx.is_empty():
        fx = (fx.filter(pl.col("measure") == "XDC_USD")
                .sort("vintage", descending=True, nulls_last=True)
                .unique(subset=["date"], keep="first")
                .select("date", pl.col("value").alias("vnd_per_usd")))
        wide = wide.join(fx, on="date", how="left")

    PATHS.panel.mkdir(parents=True, exist_ok=True)
    dest = PATHS.panel / "vn_macro_monthly.parquet"
    wide.write_parquet(dest, compression="zstd")
    log.info("wrote %d months x %d columns -> %s",
             wide.height, len(wide.columns), dest)
    return wide
