
from __future__ import annotations

import warnings
import math
from datetime import datetime, timedelta, time as dtime
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

try:
    from scipy.signal import argrelextrema
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False
    print("[key_levels] scipy not found — falling back to fractal swing detection. "
          "Install with: pip install scipy")

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

    # ── 30-minute Opening Range (current day RTH: 9:30–10:00) ─────────────
    or_cutoff = dtime(10, 0)
    rth_or = df[
        (df["bar_date"] == curr_day) &
        (df["bar_time"] >= RTH_OPEN) &
        (df["bar_time"] < or_cutoff)
    ]
    or_high_30 = float(rth_or["high"].max()) if not rth_or.empty else float("nan")
    or_low_30  = float(rth_or["low"].min())  if not rth_or.empty else float("nan")

    return {
        "prev_day_high"        : prev_day_high,
        "prev_day_low"         : prev_day_low,
        "prev_day_close"       : prev_day_close,
        "premarket_high"       : premarket_high,
        "premarket_low"        : premarket_low,
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
    df:       pd.DataFrame,
    n_levels: int = 2,
) -> dict[str, list[float]]:
    """
    Classic floor pivot points (P, R1/R2, S1/S2) from the prior bar's OHLC.
    Returns the `n_levels` nearest levels each side of the current close.

    Returns
    ───────
    {"support": [float, ...], "resistance": [float, ...], "method": "pivot"}
    """
    close = float(df["close"].iloc[-1])
    return _pivot_levels(df, close, n_levels)



def _pivot_levels(
    df:       pd.DataFrame,
    close:    float,
    n_levels: int,
) -> dict[str, list[float]]:
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
    df:          pd.DataFrame,
    sm           = None,
    symbol:      Optional[str] = None,
    n_levels:    int           = 2,
    swing_left:  int           = 3,
    swing_right: int           = 3,
) -> dict:
    close = float(df["close"].iloc[-1])

    result: dict = {
        "prev_day_high"        : None,
        "prev_day_low"         : None,
        "prev_day_close"       : None,
        "premarket_high"       : None,
        "premarket_low"        : None,
        "opening_range_high_30": None,
        "opening_range_low_30" : None,
        "support"              : [],
        "resistance"           : [],
        "swing_highs"          : [],
        "swing_lows"           : [],
        "sr_method"            : "pivot",
        "current_price"        : close,
    }

    # ── Session anchors ────────────────────────────────────────────────────────
    if sm is not None and symbol:
        try:
            session = get_session_levels(sm, symbol)
            result.update(session)

            est_now = datetime.now(ZoneInfo("America/New_York"))
            now_str = est_now.strftime("%H:%M")
            if est_now.hour >= 11:
                pivot_vals = [
                    val for val in [
                        result["prev_day_high"], 
                        result["prev_day_low"], 
                        result["premarket_high"], 
                        result["premarket_low"], 
                        result["opening_range_high_30"], 
                        result["opening_range_low_30"]
                    ] 
                    if val is not None
                ]
                pivot_valarr = sorted(pivot_vals)
                result["support"] = sorted(list(set([val for val in pivot_valarr if val < close])))[-3:]
                result["resistance"] = sorted(list(set(val for val in pivot_valarr if val > close)))[:3]
                result["prev_day_high"]=result["prev_day_low"]=result["premarket_high"]=result["premarket_low"]= \
                    result["opening_range_high_30"]=result["opening_range_low_30"]=float("nan")

        except Exception as e:
            print(f"[key_levels] Session levels unavailable: {e}")

    # # ── Pivot S/R — nearest n_levels each side ────────────────────────────────
    # sr = find_support_resistance(df, n_levels=n_levels)
    # result["support"]    = sr["support"]
    # result["resistance"] = sr["resistance"]

    # # ── Swings — nearest n_levels above and below current price ───────────────
    # df = find_swing_highs_lows(df, left=swing_left, right=swing_right)

    # all_swing_highs = sorted(df["swing_high"].dropna().tolist())
    # all_swing_lows  = sorted(df["swing_low"].dropna().tolist())

    # # nearest n_levels swing highs ABOVE price (ascending — closest first)
    # result["swing_highs"] = [p for p in all_swing_highs if p > close][:n_levels]
    # # nearest n_levels swing lows BELOW price (descending — closest first)
    # result["swing_lows"]  = sorted([p for p in all_swing_lows if p < close], reverse=True)[:n_levels]

    return result

# ──────────────────────────────────────────────────────────────────────────────
# NEW: Build directional targets block for Telegram
# ──────────────────────────────────────────────────────────────────────────────

def build_levels_line(symbol: str, results: dict) -> list[str]:
    """
    Returns up to 2 compact lines with the nearest S/R levels from the 15m frame.
      S {nearest_support:.2f}  ►  {price:.2f}  ►  R {nearest_resistance:.2f}
      PDH {pdh:.2f}  PDL {pdl:.2f}  OR30 {orl:.2f}–{orh:.2f}
    """
    kl = {}
    for iv in ("15m", "30m"):
        df_iv = results.get((symbol, iv))
        if df_iv is not None:
            kl = getattr(df_iv, "attrs", {}).get("key_levels", {})
            if kl:
                break
    if not kl:
        return []

    price = kl.get("current_price", 0.0)
    sup   = kl.get("support",    [])
    res   = kl.get("resistance", [])

    s_str = f"S {sup[len(sup)-1]:.2f}  " if sup else ""
    r_str = f"  R {res[0]:.2f}" if res else ""
    line1 = f"📌 {s_str}► {price:.2f}{r_str}"

    pdh = kl.get("prev_day_high")
    pdl = kl.get("prev_day_low")
    pmh = kl.get("premarket_high")
    pml = kl.get("premarket_low")
    orh = kl.get("opening_range_high_30")
    orl = kl.get("opening_range_low_30")

    parts = []
    if not math.isnan(pdl) and not math.isnan(pdh):
        parts.append(f"PD {pdl:.2f}–{pdh:.2f}")
    if not math.isnan(pml) and not math.isnan(pmh):
        parts.append(f"PM {pml:.2f}–{pmh:.2f}")
    if not math.isnan(orl) and not math.isnan(orh):
        parts.append(f"OR30 {orl:.2f}–{orh:.2f}")
    line2 = ("📅 " + "  |  ".join(parts)) if parts else ""

    return [line1] + ([line2] if line2 else [])


def attach_key_levels(
    df:          "pd.DataFrame",
    sm           = None,
    symbol:      str = "",
    n_levels:    int = 2,
    swing_left:  int = 3,
    swing_right: int = 3,
) -> "pd.DataFrame":
    levels = find_key_levels(
        df          = df,
        sm          = sm,
        symbol      = symbol,
        n_levels    = n_levels,
        swing_left  = swing_left,
        swing_right = swing_right,
    )
    # Persist on the DataFrame so callers (e.g. build_combined_alert) can read them
    df.attrs["key_levels"] = levels

    #df = find_swing_highs_lows(df, left=swing_left, right=swing_right)
    return df