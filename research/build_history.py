#!/usr/bin/env python3
"""Build the auditable Anthropic pre-IPO perpetual history package.

The production oracle uses live executable order books. Historical order-book
snapshots are not available from every venue for the full listing period, so
this research package uses completed UTC daily candles for a separate sanity
check. It never feeds the production oracle or liquidation engine.

Primary sources:
* OKX public daily-candle API.
* Bitget public daily-candle API.
* Binance public futures API, with the official Binance Data Collection archive
  as a jurisdiction-safe fallback.

Raw contract prices are not directly comparable. Each close is multiplied by
the venue's published/inferred nominal share basis. In particular, OKX changed
the ANTHROPIC contract scale by 10:1 at 08:06 UTC on 2026-06-30. The rebase is
value-neutral: pre-change prices use 1 billion units and subsequent prices use
10 billion units. The exact transition is visible in OKX's one-minute candles;
the daily close on the transition date is post-rebase.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib
import nbformat
import pandas as pd
import requests

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_DIR = ROOT / "research"
DEFAULT_OUTPUT = RESEARCH_DIR
USER_AGENT = {"User-Agent": "anthropic-valuation-research/1.0"}

OKX_ENDPOINT = "https://www.okx.com/api/v5/market/history-candles"
BITGET_ENDPOINT = "https://api.bitget.com/api/v2/mix/market/history-candles"
BINANCE_ENDPOINT = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_ARCHIVE = "https://data.binance.vision/data/futures/um"

OKX_MARKET = "ANTHROPIC-USDT-SWAP"
BITGET_MARKET = "ANTHROPICUSDT"
BINANCE_MARKET = "ANTHROPICUSDT"

ONE_BILLION = 1_000_000_000
TEN_BILLION = 10_000_000_000
OKX_REBASE_AT = datetime(2026, 6, 30, 8, 6, tzinfo=UTC)
BINANCE_LISTING_MONTH = date(2026, 6, 1)
VENUES = ("OKX", "Bitget", "Binance")


def utc_date(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).date()


def as_float(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"expected a non-negative finite number, received {value!r}")
    return number


def request_json(session: requests.Session, url: str, params: dict[str, Any]) -> Any:
    response = session.get(url, params=params, headers=USER_AGENT, timeout=30)
    if response.status_code == 451:
        raise PermissionError(f"{url} is unavailable in this jurisdiction")
    response.raise_for_status()
    return response.json()


def okx_basis_for_daily_close(day: date) -> int:
    """Return the basis applicable to that day's completed closing observation."""

    return TEN_BILLION if day >= OKX_REBASE_AT.date() else ONE_BILLION


def fetch_okx(session: requests.Session, as_of: date) -> list[dict[str, Any]]:
    """Download all completed OKX UTC daily candles, paging backward."""

    rows: dict[int, list[Any]] = {}
    cursor: int | None = None
    while True:
        params: dict[str, Any] = {
            "instId": OKX_MARKET,
            "bar": "1Dutc",
            "limit": "300",
        }
        if cursor is not None:
            params["after"] = str(cursor)
        payload = request_json(session, OKX_ENDPOINT, params)
        if payload.get("code") != "0":
            raise RuntimeError(f"OKX error: {payload}")
        page = payload.get("data") or []
        if not page:
            break
        for record in page:
            rows[int(record[0])] = record
        oldest = min(int(record[0]) for record in page)
        if len(page) < 300 or cursor == oldest:
            break
        cursor = oldest

    normalized: list[dict[str, Any]] = []
    for timestamp_ms, record in sorted(rows.items()):
        day = utc_date(timestamp_ms)
        # OKX's final field is 1 only after the candle is complete.
        if day >= as_of or str(record[8]) != "1":
            continue
        basis = okx_basis_for_daily_close(day)
        normalized.append(
            candle_row(
                day=day,
                venue="OKX",
                market=OKX_MARKET,
                raw=record[1:5],
                quote_volume=record[7],
                trade_count=None,
                basis=basis,
                mixed_basis=day == OKX_REBASE_AT.date(),
                source_url=(f"{OKX_ENDPOINT}?instId={OKX_MARKET}&bar=1Dutc&limit=300"),
            )
        )
    return normalized


def fetch_bitget(session: requests.Session, as_of: date) -> list[dict[str, Any]]:
    """Download completed Bitget UTC daily candles, paging backward if needed."""

    rows: dict[int, list[Any]] = {}
    # Bitget selects daily candles by their closing boundary. Ask through the
    # end of `as_of` and then retain only candles whose UTC opening date is
    # strictly earlier than the exclusive cutoff.
    end_ms = (
        int(
            datetime.combine(
                as_of + timedelta(days=1), datetime.min.time(), UTC
            ).timestamp()
            * 1000
        )
        - 1
    )
    while True:
        params = {
            "symbol": BITGET_MARKET,
            "productType": "USDT-FUTURES",
            "granularity": "1Dutc",
            "limit": "200",
            "endTime": str(end_ms),
        }
        payload = request_json(session, BITGET_ENDPOINT, params)
        if payload.get("code") != "00000":
            raise RuntimeError(f"Bitget error: {payload}")
        page = payload.get("data") or []
        if not page:
            break
        for record in page:
            rows[int(record[0])] = record
        oldest = min(int(record[0]) for record in page)
        if len(page) < 200 or oldest >= end_ms:
            break
        end_ms = oldest - 1

    normalized: list[dict[str, Any]] = []
    for timestamp_ms, record in sorted(rows.items()):
        day = utc_date(timestamp_ms)
        if day >= as_of:
            continue
        normalized.append(
            candle_row(
                day=day,
                venue="Bitget",
                market=BITGET_MARKET,
                raw=record[1:5],
                quote_volume=record[6],
                trade_count=None,
                basis=ONE_BILLION,
                mixed_basis=False,
                source_url=(
                    f"{BITGET_ENDPOINT}?symbol={BITGET_MARKET}"
                    "&productType=USDT-FUTURES&granularity=1Dutc"
                ),
            )
        )
    return normalized


def verify_archive_checksum(
    session: requests.Session, url: str, payload: bytes
) -> None:
    checksum = session.get(f"{url}.CHECKSUM", headers=USER_AGENT, timeout=30)
    if checksum.status_code == 404:
        return
    checksum.raise_for_status()
    expected = checksum.text.split()[0].lower()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RuntimeError(f"Binance archive checksum mismatch for {url}")


def read_binance_zip(session: requests.Session, url: str) -> list[dict[str, str]]:
    response = session.get(url, headers=USER_AGENT, timeout=30)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    verify_archive_checksum(session, url, response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise RuntimeError(f"unexpected Binance archive contents for {url}")
        text = archive.read(names[0]).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def month_starts(first: date, end_exclusive: date) -> list[date]:
    current = first.replace(day=1)
    result = []
    while current < end_exclusive.replace(day=1):
        result.append(current)
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return result


def fetch_binance_archive(
    session: requests.Session, as_of: date
) -> list[dict[str, Any]]:
    """Read checksum-verified official monthly/daily Binance archives."""

    records: dict[int, dict[str, str]] = {}
    covered_months: set[tuple[int, int]] = set()
    for month in month_starts(BINANCE_LISTING_MONTH, as_of):
        name = f"{BINANCE_MARKET}-1d-{month:%Y-%m}.zip"
        url = f"{BINANCE_ARCHIVE}/monthly/klines/{BINANCE_MARKET}/1d/{name}"
        month_rows = read_binance_zip(session, url)
        if month_rows:
            covered_months.add((month.year, month.month))
            for record in month_rows:
                records[int(record["open_time"])] = record

    day = BINANCE_LISTING_MONTH
    while day < as_of:
        if (day.year, day.month) not in covered_months:
            name = f"{BINANCE_MARKET}-1d-{day:%Y-%m-%d}.zip"
            url = f"{BINANCE_ARCHIVE}/daily/klines/{BINANCE_MARKET}/1d/{name}"
            for record in read_binance_zip(session, url):
                records[int(record["open_time"])] = record
        day += timedelta(days=1)

    normalized = []
    for timestamp_ms, record in sorted(records.items()):
        day = utc_date(timestamp_ms)
        if day >= as_of:
            continue
        normalized.append(
            candle_row(
                day=day,
                venue="Binance",
                market=BINANCE_MARKET,
                raw=(
                    record["open"],
                    record["high"],
                    record["low"],
                    record["close"],
                ),
                quote_volume=record["quote_volume"],
                trade_count=record["count"],
                basis=ONE_BILLION,
                mixed_basis=False,
                source_url=(f"{BINANCE_ARCHIVE}/monthly/klines/{BINANCE_MARKET}/1d/"),
            )
        )
    return normalized


def fetch_binance_api(session: requests.Session, as_of: date) -> list[dict[str, Any]]:
    start_ms = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)
    end_ms = (
        int(datetime.combine(as_of, datetime.min.time(), UTC).timestamp() * 1000) - 1
    )
    records: dict[int, list[Any]] = {}
    while start_ms <= end_ms:
        page = request_json(
            session,
            BINANCE_ENDPOINT,
            {
                "symbol": BINANCE_MARKET,
                "interval": "1d",
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": "1000",
            },
        )
        if not page:
            break
        for record in page:
            records[int(record[0])] = record
        newest = max(int(record[0]) for record in page)
        if len(page) < 1000 or newest <= start_ms:
            break
        start_ms = newest + 86_400_000

    normalized = []
    for timestamp_ms, record in sorted(records.items()):
        day = utc_date(timestamp_ms)
        if day >= as_of:
            continue
        normalized.append(
            candle_row(
                day=day,
                venue="Binance",
                market=BINANCE_MARKET,
                raw=record[1:5],
                quote_volume=record[7],
                trade_count=record[8],
                basis=ONE_BILLION,
                mixed_basis=False,
                source_url=(f"{BINANCE_ENDPOINT}?symbol={BINANCE_MARKET}&interval=1d"),
            )
        )
    return normalized


def fetch_binance(session: requests.Session, as_of: date) -> list[dict[str, Any]]:
    try:
        rows = fetch_binance_api(session, as_of)
        if rows:
            return rows
    except (PermissionError, requests.RequestException):
        pass
    return fetch_binance_archive(session, as_of)


def candle_row(
    *,
    day: date,
    venue: str,
    market: str,
    raw: Any,
    quote_volume: Any,
    trade_count: Any,
    basis: int,
    mixed_basis: bool,
    source_url: str,
) -> dict[str, Any]:
    open_price, high_price, low_price, close_price = map(as_float, raw)
    # Round only to precision far beyond any venue tick, then store the derived
    # company valuation as whole USD. This prevents binary-float artifacts from
    # making network and offline regeneration produce different CSV hashes.
    open_price, high_price, low_price, close_price = (
        round(value, 8) for value in (open_price, high_price, low_price, close_price)
    )
    return {
        "date": day.isoformat(),
        "venue": venue,
        "market": market,
        "raw_open": open_price,
        "raw_high": high_price,
        "raw_low": low_price,
        "raw_close": close_price,
        "nominal_share_basis": basis,
        "implied_valuation_close_usd": round(close_price * basis),
        "quote_volume_usd": round(as_float(quote_volume), 8),
        "trade_count": "" if trade_count is None else int(trade_count),
        "mixed_basis_daily_ohlc": mixed_basis,
        "source_url": source_url,
    }


def make_wide(daily: pd.DataFrame) -> pd.DataFrame:
    values = daily.pivot(
        index="date", columns="venue", values="implied_valuation_close_usd"
    )
    volumes = daily.pivot(index="date", columns="venue", values="quote_volume_usd")
    wide = pd.DataFrame(index=values.index)
    for venue in VENUES:
        wide[f"{venue.lower()}_valuation_usd"] = values.get(venue)
        wide[f"{venue.lower()}_quote_volume_usd"] = volumes.get(venue)
    valuation_columns = [f"{venue.lower()}_valuation_usd" for venue in VENUES]
    volume_columns = [f"{venue.lower()}_quote_volume_usd" for venue in VENUES]
    wide["available_venues"] = wide[valuation_columns].notna().sum(axis=1)
    wide["daily_close_median_usd"] = wide[valuation_columns].median(axis=1)
    wide["cross_venue_spread_bps"] = (
        wide[valuation_columns].max(axis=1) / wide[valuation_columns].min(axis=1) - 1
    ) * 10_000
    wide["aggregate_quote_volume_usd"] = wide[volume_columns].sum(axis=1, min_count=1)
    wide["median_daily_return"] = wide["daily_close_median_usd"].pct_change(
        fill_method=None
    )
    return wide.reset_index()


def annualized_volatility(returns: pd.Series) -> float:
    clean = returns.dropna()
    return float(clean.std(ddof=1) * math.sqrt(365)) if len(clean) > 1 else math.nan


def compute_summary(wide: pd.DataFrame, as_of: date) -> dict[str, Any]:
    all_three = wide[wide["available_venues"] == 3].copy()
    if all_three.empty:
        raise RuntimeError("no dates contain all three venues")
    all_three["median_daily_return"] = all_three["daily_close_median_usd"].pct_change(
        fill_method=None
    )
    returns = all_three["median_daily_return"].dropna()
    max_index = returns.abs().idxmax()
    max_move = float(all_three.loc[max_index, "median_daily_return"])
    start_value = float(all_three.iloc[0]["daily_close_median_usd"])
    end_value = float(all_three.iloc[-1]["daily_close_median_usd"])

    latest_30_start = pd.Timestamp(as_of - timedelta(days=30), tz="UTC")
    dated = all_three.assign(date=pd.to_datetime(all_three["date"], utc=True))
    latest_30 = dated[dated["date"] >= latest_30_start]

    valuation_columns = [f"{venue.lower()}_valuation_usd" for venue in VENUES]
    venue_returns = all_three[valuation_columns].pct_change(fill_method=None)
    correlations = venue_returns.corr()
    latest_30_volume = {
        venue: float(latest_30[f"{venue.lower()}_quote_volume_usd"].sum())
        for venue in VENUES
    }
    latest_30_total = sum(latest_30_volume.values())

    return {
        "data_cutoff_exclusive_utc": as_of.isoformat(),
        "common_history_start": str(all_three.iloc[0]["date"]),
        "common_history_end": str(all_three.iloc[-1]["date"]),
        "common_observations": len(all_three),
        "first_median_valuation_usd": round(start_value),
        "latest_median_valuation_usd": round(end_value),
        "common_period_return": end_value / start_value - 1,
        "annualized_realized_volatility": annualized_volatility(returns),
        "median_absolute_daily_move": float(returns.abs().median()),
        "largest_daily_move": max_move,
        "largest_daily_move_date": str(all_three.loc[max_index, "date"]),
        "median_cross_venue_spread_bps": float(
            all_three["cross_venue_spread_bps"].median()
        ),
        "p95_cross_venue_spread_bps": float(
            all_three["cross_venue_spread_bps"].quantile(0.95)
        ),
        "maximum_cross_venue_spread_bps": float(
            all_three["cross_venue_spread_bps"].max()
        ),
        "median_aggregate_daily_quote_volume_usd": float(
            all_three["aggregate_quote_volume_usd"].median()
        ),
        "latest_30d_average_quote_volume_usd": float(
            latest_30["aggregate_quote_volume_usd"].mean()
        ),
        "latest_30d_total_quote_volume_usd": float(
            latest_30["aggregate_quote_volume_usd"].sum()
        ),
        "latest_30d_quote_volume_by_venue_usd": latest_30_volume,
        "latest_30d_quote_volume_share": {
            venue: value / latest_30_total for venue, value in latest_30_volume.items()
        },
        "latest_close_by_venue_usd": {
            venue: round(float(all_three.iloc[-1][column]))
            for venue, column in zip(VENUES, valuation_columns, strict=True)
        },
        "daily_return_correlations": {
            left: {
                # Correlation uses vectorized floating-point math whose final
                # bit can differ by BLAS/runtime. Twelve decimals is far beyond
                # the source-data precision and keeps JSON builds canonical.
                right: round(
                    float(
                        correlations.loc[
                            f"{left.lower()}_valuation_usd",
                            f"{right.lower()}_valuation_usd",
                        ]
                    ),
                    12,
                )
                for right in VENUES
            }
            for left in VENUES
        },
        "methodology": (
            "Completed UTC daily closes, normalized by venue nominal share basis; "
            "daily median is a historical diagnostic, not the live impact-price oracle"
        ),
    }


def chart_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titleweight": "bold",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.alpha": 0.22,
            "grid.linewidth": 0.8,
            "legend.frameon": False,
            # Matplotlib otherwise salts SVG element IDs per process, making
            # visually identical offline rebuilds produce noisy file diffs.
            "svg.hashsalt": "anthropic-oracle",
        }
    )


def save_figure(fig: plt.Figure, directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        directory / f"{name}.png",
        dpi=180,
        bbox_inches="tight",
        metadata={"Software": "anthropic-oracle"},
    )
    svg_path = directory / f"{name}.svg"
    fig.savefig(
        svg_path,
        bbox_inches="tight",
        metadata={"Creator": "anthropic-oracle", "Date": None},
    )
    # Matplotlib emits spaces before newlines inside SVG path data. Removing
    # them keeps repository whitespace checks useful without changing the art.
    svg_path.write_text(
        "\n".join(line.rstrip() for line in svg_path.read_text().splitlines()) + "\n"
    )
    plt.close(fig)


def create_charts(wide: pd.DataFrame, directory: Path) -> None:
    chart_style()
    frame = wide.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    colors = {"OKX": "#2563eb", "Bitget": "#ea580c", "Binance": "#16a34a"}

    fig, axis = plt.subplots(figsize=(11.5, 6.2))
    for venue in VENUES:
        axis.plot(
            frame["date"],
            frame[f"{venue.lower()}_valuation_usd"] / 1e12,
            label=venue,
            color=colors[venue],
            linewidth=1.45,
            alpha=0.88,
        )
    plotted_median = frame["daily_close_median_usd"].where(
        frame["available_venues"] >= 2
    )
    axis.plot(
        frame["date"],
        plotted_median / 1e12,
        label="Daily median",
        color="#111827",
        linewidth=2.3,
        linestyle=(0, (4, 2)),
        zorder=5,
    )
    axis.axvline(pd.Timestamp(OKX_REBASE_AT), color="#6b7280", linewidth=1)
    axis.annotate(
        "OKX 10:1 scale change\n(value-neutral)",
        xy=(pd.Timestamp(OKX_REBASE_AT), 0.10),
        xycoords=("data", "axes fraction"),
        xytext=(7, 0),
        textcoords="offset points",
        va="bottom",
        color="#4b5563",
        fontsize=9,
    )
    latest = frame[frame["available_venues"] >= 2].iloc[-1]
    axis.annotate(
        f"${latest['daily_close_median_usd'] / 1e12:.3f}T",
        xy=(latest["date"], latest["daily_close_median_usd"] / 1e12),
        xytext=(7, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        fontweight="bold",
    )
    axis.set_title(
        "Anthropic pre-IPO perpetuals imply a common valuation path", loc="left"
    )
    axis.set_ylabel("Implied company valuation ($ trillions)")
    axis.set_xlabel("Completed UTC daily close")
    axis.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    axis.legend(ncol=4, loc="upper right")
    fig.text(
        0.01,
        0.01,
        "Sources: official OKX and Bitget APIs; official Binance Data Collection. "
        "Daily median is diagnostic and is not the live oracle.",
        color="#4b5563",
        fontsize=8.5,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    save_figure(fig, directory, "implied-valuation-history")

    common = frame[frame["available_venues"] == 3].copy()
    common["return"] = common["daily_close_median_usd"].pct_change(fill_method=None)
    common["rolling_vol"] = common["return"].rolling(
        14, min_periods=7
    ).std() * math.sqrt(365)
    bar_colors = [
        "#16a34a" if value >= 0 else "#dc2626" for value in common["return"].fillna(0)
    ]
    fig, axes = plt.subplots(
        2, 1, figsize=(11.5, 7.2), sharex=True, height_ratios=(1.2, 1)
    )
    axes[0].bar(common["date"], common["return"] * 100, color=bar_colors, width=0.82)
    axes[0].axhline(0, color="#6b7280", linewidth=0.8)
    axes[0].set_title("Daily price discovery is volatile but continuous", loc="left")
    axes[0].set_ylabel("Median daily return (%)")
    largest = common.loc[common["return"].abs().idxmax()]
    largest_move = largest["return"] * 100
    axes[0].annotate(
        f"{largest_move:+.1f}%",
        xy=(largest["date"], largest_move),
        xytext=(0, -18 if largest_move < 0 else 8),
        textcoords="offset points",
        ha="center",
        va="top" if largest_move < 0 else "bottom",
        fontsize=9,
        fontweight="bold",
    )
    axes[1].plot(
        common["date"], common["rolling_vol"] * 100, color="#7c3aed", linewidth=2
    )
    axes[1].set_ylabel("14-day realized vol\n(annualized, %)")
    axes[1].set_xlabel("Completed UTC daily close")
    axes[1].xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.text(
        0.01,
        0.01,
        "Return and volatility use the median of the three normalized venue closes.",
        color="#4b5563",
        fontsize=8.5,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    save_figure(fig, directory, "daily-volatility")

    fig, axes = plt.subplots(
        2, 1, figsize=(11.5, 7.2), sharex=True, height_ratios=(1.1, 1)
    )
    volume_bottom = pd.Series(0.0, index=common.index)
    for venue in VENUES:
        values = common[f"{venue.lower()}_quote_volume_usd"].fillna(0) / 1e6
        axes[0].bar(
            common["date"],
            values,
            bottom=volume_bottom,
            width=0.82,
            color=colors[venue],
            label=venue,
        )
        volume_bottom += values
    axes[0].set_title(
        "Liquidity is observable and cross-venue disagreement is measurable", loc="left"
    )
    axes[0].set_ylabel("Reported quote volume ($m)")
    axes[0].legend(ncol=3, loc="upper right")
    axes[1].plot(
        common["date"],
        common["cross_venue_spread_bps"],
        color="#111827",
        linewidth=1.7,
    )
    axes[1].fill_between(
        common["date"],
        common["cross_venue_spread_bps"],
        color="#9ca3af",
        alpha=0.18,
    )
    median_spread = common["cross_venue_spread_bps"].median()
    axes[1].axhline(median_spread, color="#6b7280", linewidth=1, linestyle="--")
    axes[1].annotate(
        f"Median {median_spread:.0f}bps",
        xy=(common.iloc[-1]["date"], median_spread),
        xytext=(-4, 5),
        textcoords="offset points",
        ha="right",
        va="bottom",
        fontsize=8.5,
        color="#4b5563",
    )
    axes[1].set_ylabel("Max/min close spread (bps)")
    axes[1].set_xlabel("Completed UTC daily close")
    axes[1].xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.text(
        0.01,
        0.01,
        "Volume is the venues' reported daily USDT quote volume; it is not audited economic volume.",
        color="#4b5563",
        fontsize=8.5,
    )
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    save_figure(fig, directory, "liquidity-and-dispersion")


def write_notebook(path: Path, as_of: date) -> None:
    notebook = nbformat.v4.new_notebook(
        metadata={
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.12"},
        }
    )
    notebook.cells = [
        nbformat.v4.new_markdown_cell(
            "# Anthropic pre-IPO perpetual history\n\n"
            f"Reproducible analysis of completed UTC daily candles through "
            f"{as_of - timedelta(days=1)}. The production oracle uses live impact "
            "prices; this notebook is a separate historical sanity check."
        ),
        nbformat.v4.new_code_cell(
            "import importlib\n"
            "import sys\n"
            "from datetime import date\n"
            "from pathlib import Path\n"
            "\n"
            "import pandas as pd\n"
            "from IPython.display import Image, display\n"
            "\n"
            "ROOT = Path.cwd()\n"
            'if not (ROOT / "research").exists():\n'
            "    ROOT = ROOT.parent\n"
            "sys.path.insert(0, str(ROOT))\n"
            'history = importlib.import_module("research.build_history")\n'
            'daily = pd.read_csv(ROOT / "research/data/anthropic_perp_daily.csv")\n'
            "wide = history.make_wide(daily)\n"
            f'summary = history.compute_summary(wide, date.fromisoformat("{as_of.isoformat()}"))\n'
            "wide.tail()"
        ),
        nbformat.v4.new_code_cell(
            'pd.Series(summary).drop("daily_return_correlations").to_frame("value")'
        ),
        nbformat.v4.new_code_cell(
            'pd.DataFrame(summary["daily_return_correlations"]).round(3)'
        ),
        nbformat.v4.new_code_cell(
            'charts = ROOT / "research/charts"\n'
            "history.create_charts(wide, charts)\n"
            "for name in [\n"
            '    "implied-valuation-history",\n'
            '    "daily-volatility",\n'
            '    "liquidity-and-dispersion",\n'
            "]:\n"
            '    display(Image(filename=charts / f"{name}.png"))'
        ),
        nbformat.v4.new_markdown_cell(
            "## Interpretation limits\n\n"
            "Daily close medians demonstrate cross-venue price discovery and are "
            "not a substitute for the live 30-second impact-price oracle. Reported "
            "volume may include market-maker and other non-directional turnover."
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, path)


def write_outputs(
    rows: list[dict[str, Any]], output: Path, as_of: date
) -> dict[str, Any]:
    data_dir = output / "data"
    charts_dir = output / "charts"
    data_dir.mkdir(parents=True, exist_ok=True)
    daily = pd.DataFrame(rows).sort_values(["date", "venue"]).reset_index(drop=True)
    if daily.empty:
        raise RuntimeError("no historical rows were collected")
    # Nullable integer dtype preserves blank trade counts for venues that do
    # not report the field while keeping Binance counts byte-for-byte stable
    # after an offline CSV round-trip.
    daily["trade_count"] = pd.to_numeric(daily["trade_count"], errors="coerce").astype(
        "Int64"
    )
    daily["nominal_share_basis"] = daily["nominal_share_basis"].astype("int64")
    daily["implied_valuation_close_usd"] = daily["implied_valuation_close_usd"].astype(
        "int64"
    )
    daily_path = data_dir / "anthropic_perp_daily.csv"
    daily.to_csv(daily_path, index=False, float_format="%.8f")

    wide = make_wide(daily)
    wide_path = data_dir / "anthropic_perp_daily_wide.csv"
    wide.to_csv(wide_path, index=False, float_format="%.8f")
    summary = compute_summary(wide, as_of)
    summary["normalized_daily_csv_sha256"] = hashlib.sha256(
        daily_path.read_bytes()
    ).hexdigest()
    (data_dir / "historical_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    create_charts(wide, charts_dir)
    write_notebook(output / "anthropic_perp_history.ipynb", as_of)
    return summary


def read_offline(path: Path, as_of: date) -> list[dict[str, Any]]:
    daily = pd.read_csv(path)
    daily = daily[pd.to_datetime(daily["date"]).dt.date < as_of]
    return daily.to_dict(orient="records")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default=datetime.now(UTC).date().isoformat(),
        help="exclusive UTC cutoff; default is today",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="regenerate analysis from the committed normalized daily CSV",
    )
    args = parser.parse_args(argv)
    as_of = date.fromisoformat(args.as_of)

    if args.offline:
        rows = read_offline(
            args.output_dir / "data" / "anthropic_perp_daily.csv", as_of
        )
    else:
        with requests.Session() as session:
            rows = [
                *fetch_okx(session, as_of),
                *fetch_bitget(session, as_of),
                *fetch_binance(session, as_of),
            ]
    summary = write_outputs(rows, args.output_dir, as_of)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
