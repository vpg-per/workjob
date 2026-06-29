"""
key_levels.py
─────────────
Price-level identification for intraday trading targets.

Provides two groups of functions:

  GROUP A — Session Anchors  (require raw 1-min or 5-min data from ServiceManager)
  ─────────────────────────
  get_session_levels(sm, symbol)
      → prev_day_high, prev_day_low, prev_day_close (RTH)
        premarket_high, premarket_low
        opening_range_high_15, opening_range_low_15  (first 15-min bar)
        opening_range_high_30, opening_range_low_30  (first 30-min bar)

  GROUP B — Technical Levels  (operate on the enriched df from stock_candle_processor)
  ──────────────────────────
  find_swing_highs_lows(df, left=3, right=3)
      → df with columns: swing_high (price or NaN), swing_low (price or NaN)

  find_support_resistance(df, n_levels=5, tolerance=0.003, method="fractal")
      → dict with keys: support (list[float]), resistance (list[float])
        Each list is sorted and deduplicated within `tolerance` % of each other.

  find_key_levels(df, sm=None, symbol=None, n_levels=5)
      → Combined dict of all levels (session anchors + S/R + swings).
        If sm/symbol are None, session anchors are skipped.

Recommended Third-Party Libraries
──────────────────────────────────
  • pandas-ta-classic  (already installed) — pivot_points() for classic/woodie/camarilla
  • scipy              — signal.argrelextrema() for robust local min/max detection
  • mplfinance         — visualization of levels on candlestick charts
  • scikit-learn       — KMeans clustering for S/R zone grouping (optional)

Install:
    pip install scipy scikit-learn mplfinance
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta, time as dtime
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

# ── Optional imports (degrade gracefully if not installed) ─────────────────────
try:
    from scipy.signal import argrelextrema
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False
    print("[key_levels] scipy not found — falling back to fractal swing detection. "
          "Install with: pip install scipy")

try:
    from sklearn.cluster import KMeans
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    # KMeans clustering for S/R zones won't be available; fractal method used instead


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Regular trading hours (Eastern) — adjust if your ServiceManager uses UTC
RTH_OPEN  = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)
PRE_OPEN  = dtime(4, 0)   # typical pre-market start
PRE_CLOSE = RTH_OPEN      # pre-market ends at RTH open


# ──────────────────────────────────────────────────────────────────────────────
# GROUP A — Session Anchors
# ──────────────────────────────────────────────────────────────────────────────

def _parse_bar_time(row: pd.Series) -> dtime:
    """
    Build a time object from 'hour' and 'minute' columns (both stored as strings
    like '09', '30' in ServiceManager's _attach_dt_cols output).
    Falls back to integer coercion if already numeric.
    """
    h = int(str(row.get("hour",   "0")).strip())
    m = int(str(row.get("minute", "0")).strip())
    return dtime(h, m)


def get_session_levels(sm, symbol: str) -> dict:
    """
    Fetches a 5-day window of 5-minute bars (pre/post market included) and
    extracts the following levels for the MOST RECENTLY COMPLETED trading day:

    Returns
    ───────
    {
        "prev_day_high"        : float,
        "prev_day_low"         : float,
        "prev_day_close"       : float,   # last RTH close
        "premarket_high"       : float,
        "premarket_low"        : float,
        "opening_range_high_15": float,   # high of 9:30–9:45 RTH window
        "opening_range_low_15" : float,
        "opening_range_high_30": float,   # high of 9:30–10:00 RTH window
        "opening_range_low_30" : float,
    }

    Notes
    ─────
    • Uses 5-min bars so the opening range uses 3 bars (15m) or 6 bars (30m).
    • "Previous day" = the last date that has RTH data before today's RTH session.
    • Pre-market = bars where bar_time >= PRE_OPEN and bar_time < RTH_OPEN
      on the CURRENT trading date.
    • Opening range = first N minutes of the CURRENT RTH session.
    """
    from datetime import date as _date

    # ── Fetch 5-day window of 5-minute bars ──────────────────────────────────
    end_dt    = datetime.now()
    start_ts  = int((end_dt - timedelta(days=5)).timestamp())

    # ServiceManager.download_stock_data with interval "5m" or fall back to "15m"
    # Most Yahoo-based managers return 5m up to 60 days back
    df = None
    for try_interval in ("5m", "15m"):
        try:
            df = sm.download_stock_data(
                symbol, start_ts, end_dt.timestamp(), interval=try_interval
            )
            if df is not None and not df.empty:
                break
        except Exception:
            continue

    if df is None or df.empty:
        raise RuntimeError(
            f"[key_levels] Could not fetch intraday data for {symbol}. "
            "Check ServiceManager connectivity."
        )

    # ── Build a time column ───────────────────────────────────────────────────
    df = df.copy()
    df["bar_time"] = df.apply(_parse_bar_time, axis=1)
    df["bar_date"] = pd.to_datetime(df["rec_dt"]).dt.date

    today = _date.today()
    all_dates = sorted(df["bar_date"].unique())

    # Identify "previous RTH date" (last date before today that has RTH bars)
    rth_dates = [
        d for d in all_dates
        if not df[(df["bar_date"] == d) &
                  (df["bar_time"] >= RTH_OPEN) &
                  (df["bar_time"] < RTH_CLOSE)].empty
    ]

    if len(rth_dates) < 1:
        raise RuntimeError("[key_levels] Not enough RTH data to determine previous day.")

    # Previous day = last RTH date strictly before today (or last available)
    prev_dates = [d for d in rth_dates if d < today]
    prev_day   = prev_dates[-1] if prev_dates else rth_dates[-2] if len(rth_dates) >= 2 else rth_dates[-1]
    curr_day   = today if today in all_dates else rth_dates[-1]

    # ── Previous Day High / Low / RTH Close ──────────────────────────────────
    prev_rth = df[
        (df["bar_date"] == prev_day) &
        (df["bar_time"] >= RTH_OPEN) &
        (df["bar_time"] < RTH_CLOSE)
    ]

    prev_day_high  = float(prev_rth["high"].max())
    prev_day_low   = float(prev_rth["low"].min())
    prev_day_close = float(prev_rth["close"].iloc[-1]) if not prev_rth.empty else float("nan")

    # ── Pre-Market High / Low (current day) ──────────────────────────────────
    premarket = df[
        (df["bar_date"] == curr_day) &
        (df["bar_time"] >= PRE_OPEN) &
        (df["bar_time"] < PRE_CLOSE)
    ]

    premarket_high = float(premarket["high"].max()) if not premarket.empty else float("nan")
    premarket_low  = float(premarket["low"].min())  if not premarket.empty else float("nan")

    # ── Opening Range (current day RTH) ──────────────────────────────────────
    rth_today = df[
        (df["bar_date"] == curr_day) &
        (df["bar_time"] >= RTH_OPEN) &
        (df["bar_time"] < RTH_CLOSE)
    ].sort_values("bar_time")

    def _range_hl(bars: pd.DataFrame, minutes: int):
        """Slice the first `minutes` of RTH bars."""
        cutoff = (datetime.combine(datetime.today(), RTH_OPEN) + timedelta(minutes=minutes)).time()
        window = bars[bars["bar_time"] < cutoff]
        if window.empty:
            return float("nan"), float("nan")
        return float(window["high"].max()), float(window["low"].min())

    or_high_15, or_low_15 = _range_hl(rth_today, 15)
    or_high_30, or_low_30 = _range_hl(rth_today, 30)

    return {
        "prev_day_high"        : prev_day_high,
        "prev_day_low"         : prev_day_low,
        "prev_day_close"       : prev_day_close,
        "premarket_high"       : premarket_high,
        "premarket_low"        : premarket_low,
        "opening_range_high_15": or_high_15,
        "opening_range_low_15" : or_low_15,
        "opening_range_high_30": or_high_30,
        "opening_range_low_30" : or_low_30,
    }


# ──────────────────────────────────────────────────────────────────────────────
# GROUP B — Swing Highs / Lows
# ──────────────────────────────────────────────────────────────────────────────

def find_swing_highs_lows(
    df:    pd.DataFrame,
    left:  int = 3,
    right: int = 3,
) -> pd.DataFrame:
    """
    Identifies pivot swing highs and lows on the close or high/low series.

    Two methods are tried in order:
      1. scipy.signal.argrelextrema (preferred — more reliable)
      2. Fractal / Williams pivot (fallback — no scipy required)

    Parameters
    ──────────
    df    : Enriched DataFrame from stock_candle_processor.process()
    left  : Number of bars to the LEFT that must be lower (swing high) / higher (swing low)
    right : Number of bars to the RIGHT (look-forward — will be NaN on last `right` bars)

    Adds columns
    ────────────
    swing_high : price at confirmed swing high, NaN elsewhere
    swing_low  : price at confirmed swing low,  NaN elsewhere

    Note: The last `right` bars cannot be confirmed yet (no right-side data).
    In live trading, only use rows where swing_high / swing_low is not NaN.
    """
    df = df.copy()
    highs  = df["high"].values.astype("float64")
    lows   = df["low"].values.astype("float64")
    n      = len(df)

    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)

    if _SCIPY_AVAILABLE:
        # argrelextrema uses a rolling window of `order` bars on each side
        order = max(left, right)
        high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
        low_idx  = argrelextrema(lows,  np.less_equal,    order=order)[0]

        for i in high_idx:
            sh[i] = highs[i]
        for i in low_idx:
            sl[i] = lows[i]

    else:
        # Fractal (Williams) pivot: bar i is a swing high if it is the highest
        # among [i-left … i+right]; symmetric for swing low.
        for i in range(left, n - right):
            window_h = highs[i - left : i + right + 1]
            window_l = lows[i  - left : i + right + 1]
            if highs[i] == window_h.max():
                sh[i] = highs[i]
            if lows[i] == window_l.min():
                sl[i] = lows[i]

    df["swing_high"] = sh
    df["swing_low"]  = sl
    return df


# ──────────────────────────────────────────────────────────────────────────────
# GROUP B — Support / Resistance
# ──────────────────────────────────────────────────────────────────────────────

def find_support_resistance(
    df:        pd.DataFrame,
    n_levels:  int   = 5,
    tolerance: float = 0.003,
    method:    str   = "fractal",
) -> dict[str, list[float]]:
    """
    Derives support and resistance price levels from the OHLCV data.

    Parameters
    ──────────
    df         : DataFrame (ideally with swing_high / swing_low already added by
                 find_swing_highs_lows; otherwise computed on-the-fly from H/L)
    n_levels   : Maximum number of S/R levels to return per side
    tolerance  : Price proximity threshold for merging nearby levels (0.003 = 0.3%)
    method     : "fractal"  — use swing pivot points (default; always available)
                 "cluster"  — KMeans cluster swing prices (requires scikit-learn)
                 "pivot"    — Classic floor pivot points (P, R1-R3, S1-S3)
                              using the prior bar's OHLC

    Returns
    ───────
    {
        "support"   : [float, ...],   # ascending, deduplicated
        "resistance": [float, ...],   # ascending, deduplicated
        "method"    : str,
    }
    """
    close = df["close"].iloc[-1]
    method = method.lower()

    if method == "pivot":
        return _pivot_levels(df, close, n_levels)

    # ── Get swing prices (compute if not already in df) ───────────────────────
    if "swing_high" not in df.columns or "swing_low" not in df.columns:
        df = find_swing_highs_lows(df)

    raw_highs = df["swing_high"].dropna().values.tolist()
    raw_lows  = df["swing_low"].dropna().values.tolist()

    if method == "cluster" and _SKLEARN_AVAILABLE:
        support, resistance = _cluster_levels(
            raw_lows, raw_highs, close, n_levels, tolerance
        )
    else:
        if method == "cluster" and not _SKLEARN_AVAILABLE:
            print("[key_levels] scikit-learn not available — falling back to fractal method. "
                  "Install with: pip install scikit-learn")
        support, resistance = _fractal_levels(
            raw_lows, raw_highs, close, n_levels, tolerance
        )

    return {
        "support"   : support,
        "resistance": resistance,
        "method"    : method if (method != "cluster" or _SKLEARN_AVAILABLE) else "fractal",
    }


def _dedup(prices: list[float], tolerance: float) -> list[float]:
    """
    Merge prices within `tolerance` % of each other into a single representative
    (the mean of the cluster).  Input need not be sorted.
    """
    if not prices:
        return []
    prices = sorted(prices)
    groups: list[list[float]] = [[prices[0]]]
    for p in prices[1:]:
        if abs(p - groups[-1][-1]) / max(groups[-1][-1], 1e-9) <= tolerance:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [round(sum(g) / len(g), 4) for g in groups]


def _fractal_levels(
    raw_lows:  list[float],
    raw_highs: list[float],
    close:     float,
    n_levels:  int,
    tolerance: float,
) -> tuple[list[float], list[float]]:
    """Split swing prices into support (below close) and resistance (above close)."""
    all_prices   = raw_lows + raw_highs
    below        = [p for p in all_prices if p < close]
    above        = [p for p in all_prices if p > close]

    support      = sorted(_dedup(below, tolerance), reverse=True)[:n_levels]
    resistance   = sorted(_dedup(above, tolerance))[:n_levels]
    return support, resistance


def _cluster_levels(
    raw_lows:  list[float],
    raw_highs: list[float],
    close:     float,
    n_levels:  int,
    tolerance: float,
) -> tuple[list[float], list[float]]:
    """
    KMeans clustering on swing prices.
    Groups nearby prices into zones and returns the cluster centroids as levels.
    More robust than fractal on noisy intraday data — picks the n_levels most
    "agreed upon" prices across the full lookback window.
    """
    all_prices = np.array(raw_lows + raw_highs).reshape(-1, 1)
    if len(all_prices) < n_levels * 2:
        # Not enough data for clustering — fall back to fractal
        return _fractal_levels(raw_lows, raw_highs, close, n_levels, tolerance)

    k = min(n_levels * 2, len(all_prices))
    km = KMeans(n_clusters=k, n_init="auto", random_state=42)
    km.fit(all_prices)
    centroids = sorted(float(c[0]) for c in km.cluster_centers_)

    support    = sorted([p for p in centroids if p < close], reverse=True)[:n_levels]
    resistance = sorted([p for p in centroids if p > close])[:n_levels]
    return support, resistance


def _pivot_levels(
    df:       pd.DataFrame,
    close:    float,
    n_levels: int,
) -> dict[str, list[float]]:
    """
    Classic floor trader pivot points using the previous bar's OHLC.
    Returns up to n_levels support / resistance levels.

    P  = (H + L + C) / 3
    R1 = 2P − L,  R2 = P + (H − L),  R3 = H + 2(P − L)
    S1 = 2P − H,  S2 = P − (H − L),  S3 = L − 2(H − P)
    """
    if len(df) < 2:
        return {"support": [], "resistance": [], "method": "pivot"}

    prev = df.iloc[-2]
    H, L, C = float(prev["high"]), float(prev["low"]), float(prev["close"])
    P = (H + L + C) / 3.0

    R1 = 2 * P - L
    R2 = P + (H - L)
    R3 = H + 2 * (P - L)

    S1 = 2 * P - H
    S2 = P - (H - L)
    S3 = L - 2 * (H - P)

    resistance = sorted([r for r in (R1, R2, R3) if r > close])[:n_levels]
    support    = sorted([s for s in (S1, S2, S3) if s < close], reverse=True)[:n_levels]

    return {"support": support, "resistance": resistance, "method": "pivot"}


# ──────────────────────────────────────────────────────────────────────────────
# Combined entry-point
# ──────────────────────────────────────────────────────────────────────────────

def find_key_levels(
    df:       pd.DataFrame,
    sm=None,
    symbol:   Optional[str]  = None,
    n_levels: int             = 5,
    sr_method: str            = "fractal",
    swing_left:  int          = 3,
    swing_right: int          = 3,
    tolerance:   float        = 0.003,
) -> dict:
    """
    Master function — returns all key levels in one dict.

    Parameters
    ──────────
    df         : Enriched DataFrame from stock_candle_processor.process()
    sm         : ServiceManager instance (needed for session anchors; pass None to skip)
    symbol     : Ticker string (needed for session anchors)
    n_levels   : Max S/R levels per side
    sr_method  : "fractal" | "cluster" | "pivot"
    swing_left / swing_right : Fractal swing detection window
    tolerance  : Dedup tolerance for S/R merging

    Returns
    ───────
    {
        # Session anchors (empty if sm/symbol not provided)
        "prev_day_high"        : float | None,
        "prev_day_low"         : float | None,
        "prev_day_close"       : float | None,
        "premarket_high"       : float | None,
        "premarket_low"        : float | None,
        "opening_range_high_15": float | None,
        "opening_range_low_15" : float | None,
        "opening_range_high_30": float | None,
        "opening_range_low_30" : float | None,

        # Technical levels
        "support"              : list[float],
        "resistance"           : list[float],
        "swing_highs"          : list[float],   # confirmed swing highs
        "swing_lows"           : list[float],   # confirmed swing lows

        # Meta
        "sr_method"            : str,
        "current_price"        : float,
    }
    """
    result: dict = {
        "prev_day_high"        : None,
        "prev_day_low"         : None,
        "prev_day_close"       : None,
        "premarket_high"       : None,
        "premarket_low"        : None,
        "opening_range_high_15": None,
        "opening_range_low_15" : None,
        "opening_range_high_30": None,
        "opening_range_low_30" : None,
        "support"              : [],
        "resistance"           : [],
        "swing_highs"          : [],
        "swing_lows"           : [],
        "sr_method"            : sr_method,
        "current_price"        : float(df["close"].iloc[-1]),
    }

    # ── Session anchors ────────────────────────────────────────────────────────
    if sm is not None and symbol:
        try:
            session = get_session_levels(sm, symbol)
            result.update(session)
        except Exception as e:
            print(f"[key_levels] Session levels unavailable: {e}")

    # ── Swings ─────────────────────────────────────────────────────────────────
    df = find_swing_highs_lows(df, left=swing_left, right=swing_right)
    result["swing_highs"] = df["swing_high"].dropna().tolist()
    result["swing_lows"]  = df["swing_low"].dropna().tolist()

    # ── S/R ────────────────────────────────────────────────────────────────────
    sr = find_support_resistance(
        df,
        n_levels  = n_levels,
        tolerance = tolerance,
        method    = sr_method,
    )
    result["support"]   = sr["support"]
    result["resistance"] = sr["resistance"]
    result["sr_method"] = sr["method"]

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Pretty-print helper
# ──────────────────────────────────────────────────────────────────────────────

def print_key_levels(levels: dict, symbol: str = "", interval: str = "") -> None:
    """
    Formats and prints the dict returned by find_key_levels().
    """
    tag = f"{symbol} {interval}".strip()
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  KEY LEVELS  |  {tag}  |  price={levels['current_price']:.2f}")
    print(sep)

    # Session anchors
    anchor_keys = [
        ("prev_day_high",         "Prev Day High"),
        ("prev_day_low",          "Prev Day Low"),
        ("prev_day_close",        "Prev Day RTH Close"),
        ("premarket_high",        "Pre-Market High"),
        ("premarket_low",         "Pre-Market Low"),
        ("opening_range_high_15", "OR High (15m)"),
        ("opening_range_low_15",  "OR Low  (15m)"),
        ("opening_range_high_30", "OR High (30m)"),
        ("opening_range_low_30",  "OR Low  (30m)"),
    ]
    print("  ── Session Anchors ──")
    for key, label in anchor_keys:
        val = levels.get(key)
        print(f"    {label:<26} {val:.4f}" if val is not None else f"    {label:<26} n/a")

    # Resistance
    print(f"\n  ── Resistance ({levels['sr_method']}) ──")
    for r in sorted(levels.get("resistance", []), reverse=True):
        print(f"    R  {r:.4f}")

    # Current price
    print(f"    ►  {levels['current_price']:.4f}  ◄ current")

    # Support
    print(f"  ── Support ({levels['sr_method']}) ──")
    for s in levels.get("support", []):
        print(f"    S  {s:.4f}")

    # Swings
    sh = sorted(levels.get("swing_highs", []), reverse=True)[:5]
    sl = sorted(levels.get("swing_lows", []))[:5]
    print(f"\n  ── Recent Swing Highs (top 5) ──")
    for p in sh:
        print(f"    ▲  {p:.4f}")
    print(f"  ── Recent Swing Lows (top 5) ──")
    for p in sl:
        print(f"    ▼  {p:.4f}")
    print(sep)
