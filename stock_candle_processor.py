"""
stock_candle_processor.py
─────────────────────────
Library module — all functions, no executable code at module level.

Provides:
  • fetch_ohlcv(sm, symbol, interval)           — multi-interval OHLCV fetch
  • build_strategy(calc_macd, calc_rsi)         — pandas-ta-classic Strategy
  • run_strategy(df, calc_macd, calc_rsi)       — run Strategy, return enriched df
  • calculate_macd_score(df)                    — mirrors ServiceManager.calculate_TrendAlert
  • calculate_rsi_score(df)                     — mirrors ServiceManager.calculate_RSITrendAlert
  • add_candle_signal(df, cdl_cols)             — plain-English CDL labels per row
  • add_overall_bias(df)                        — majority-vote composite signal
  • print_summary(df, cdl_cols, symbol, interval)
  • process(sm, symbol, interval, calc_macd, calc_rsi, save_csv)  — full pipeline

Supported intervals  : "15m", "30m", "1h", "4h"
Supported symbols    : any ticker valid on Yahoo Finance (SPY, QQQ, IWM, GLD …)
calc_macd / calc_rsi : bool flags — set False to skip scoring for higher timeframes

Candlestick patterns (26 total)
─────────────────────────────────
  Single (9) : Hammer, Inverted Hammer, Shooting Star, Hanging Man,
               Doji, Dragonfly Doji, Gravestone Doji, Marubozu, Spinning Top
  Two    (7) : Engulfing, Harami, Harami Cross, Dark Cloud Cover, Piercing,
               Counterattack, Belt Hold
  Three (10) : Morning Star, Evening Star, Morning Doji Star, Evening Doji Star,
               3 White Soldiers, 3 Black Crows, 3 Inside, 3 Outside,
               Abandoned Baby, Breakaway

Install
───────
    pip install pandas-ta-classic pandas numpy requests
"""

from __future__ import annotations

import gc
import importlib.util
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

warnings.filterwarnings("ignore", category=FutureWarning)

# ──────────────────────────────────────────────────────────────────────────────
# Safe import of dataManager  (immune to circular-import / Windows __pycache__)
# ──────────────────────────────────────────────────────────────────────────────
_DM_PATH = Path(__file__).resolve().parent / "dataManager.py"
if not _DM_PATH.exists():
    raise FileNotFoundError(
        f"dataManager.py not found next to this script.\nExpected: {_DM_PATH}"
    )
_dm_spec   = importlib.util.spec_from_file_location("dataManager", _DM_PATH)
_dm_module = importlib.util.module_from_spec(_dm_spec)
sys.modules.setdefault("dataManager", _dm_module)
_dm_spec.loader.exec_module(_dm_module)
ServiceManager = _dm_module.ServiceManager


# ──────────────────────────────────────────────────────────────────────────────
# Interval metadata
# ──────────────────────────────────────────────────────────────────────────────

# Valid minutes per interval (used for boundary alignment)
_INTERVAL_MINUTES: dict[str, set[str]] = {
    "15m": {"00", "15", "30", "45"},
    "30m": {"00", "30"},
    "1h":  {"00"},
    "4h":  {"00"},
}

# Lookback window (days) per interval — enough bars for MACD/RSI warmup
_LOOKBACK_DAYS: dict[str, int] = {
    "15m": 4,
    "30m": 7,
    "1h":  14,
    "4h":  30,
}

# Minute modulus per interval for end-period alignment
_INTERVAL_MOD: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h":  60,
    "4h":  240,
}


# ──────────────────────────────────────────────────────────────────────────────
# Pattern lists
# ──────────────────────────────────────────────────────────────────────────────

SINGLE_CANDLE_PATTERNS: list[str] = [
    # "hammer",           # Bullish reversal at bottom
    # "invertedhammer",   # Bullish reversal (needs confirmation)
    # "shootingstar",     # Bearish reversal at top
    # "hangingman",       # Bearish reversal at top
    # "doji",             # Indecision / potential reversal
    # "dragonflydoji",    # Bullish reversal doji
    # "gravestonedoji",   # Bearish reversal doji
    # "marubozu",         # Strong trend candle (no wicks)
    # "spinningtop",      # Indecision candle
]

TWO_CANDLE_PATTERNS: list[str] = [
    "engulfing",        # Bull/bear engulf prior candle — most-referenced pattern
    "harami",           # Inside bar reversal
    "haramicross",      # Harami with doji — stronger signal
    "darkcloudcover",   # Bearish: gap up then close below midpoint
    "piercing",         # Bullish: gap down then close above midpoint
    #"counterattack",    # Two opposite candles closing at same price
    #"belthold",         # Trend continuation, no shadow on one side
]

THREE_CANDLE_PATTERNS: list[str] = [
    "morningstar",      # Bullish reversal: down → small body → strong up
    "eveningstar",      # Bearish reversal: up → small body → strong down
    "morningdojistar",  # Morning Star with doji middle — stronger signal
    "eveningdojistar",  # Evening Star with doji middle — stronger signal
    "3whitesoldiers",   # Three consecutive bullish candles
    "3blackcrows",      # Three consecutive bearish candles
    "3inside",          # Three Inside Up / Down
    "3outside",         # Three Outside Up / Down
    "abandonedbaby",    # Rare high-conviction gap reversal
    "breakaway",        # Breakaway pattern (5-bar, triggered on 3rd)
]

ALL_PATTERNS: list[str] = (
    SINGLE_CANDLE_PATTERNS + TWO_CANDLE_PATTERNS + THREE_CANDLE_PATTERNS
)

# CDL column → (bullish label, bearish label)
_LABEL_MAP: dict[str, tuple[str, str]] = {
    "CDL_HAMMER":          ("Hammer ↑",           "Hammer ↑"),
    "CDL_INVERTEDHAMMER":  ("Inv. Hammer ↑",       "Inv. Hammer ↑"),
    "CDL_SHOOTINGSTAR":    ("Shooting Star ↓",     "Shooting Star ↓"),
    "CDL_HANGINGMAN":      ("Hanging Man ↓",       "Hanging Man ↓"),
    "CDL_DOJI":            ("Doji ↔",              "Doji ↔"),
    "CDL_DRAGONFLYDOJI":   ("Dragonfly Doji ↑",    "Dragonfly Doji ↑"),
    "CDL_GRAVESTONEDOJI":  ("Gravestone Doji ↓",   "Gravestone Doji ↓"),
    "CDL_MARUBOZU":        ("Bull Marubozu ↑",     "Bear Marubozu ↓"),
    "CDL_SPINNINGTOP":     ("Spinning Top ↔",      "Spinning Top ↔"),
    "CDL_ENGULFING":       ("Bull Engulfing ↑",    "Bear Engulfing ↓"),
    "CDL_HARAMI":          ("Bull Harami ↑",       "Bear Harami ↓"),
    "CDL_HARAMICROSS":     ("Harami Cross ↑",      "Harami Cross ↓"),
    "CDL_DARKCLOUDCOVER":  ("Dark Cloud Cover ↓",  "Dark Cloud Cover ↓"),
    "CDL_PIERCING":        ("Piercing Line ↑",     "Piercing Line ↑"),
    "CDL_COUNTERATTACK":   ("Counterattack ↑",     "Counterattack ↓"),
    "CDL_BELTHOLD":        ("Belt Hold ↑",         "Belt Hold ↓"),
    "CDL_MORNINGSTAR":     ("Morning Star ↑",      "Morning Star ↑"),
    "CDL_EVENINGSTAR":     ("Evening Star ↓",      "Evening Star ↓"),
    "CDL_MORNINGDOJISTAR": ("Morn. Doji Star ↑",   "Morn. Doji Star ↑"),
    "CDL_EVENINGDOJISTAR": ("Eve. Doji Star ↓",    "Eve. Doji Star ↓"),
    "CDL_3WHITESOLDIERS":  ("3 White Soldiers ↑",  "3 White Soldiers ↑"),
    "CDL_3BLACKCROWS":     ("3 Black Crows ↓",     "3 Black Crows ↓"),
    "CDL_3INSIDE":         ("3 Inside Up ↑",       "3 Inside Down ↓"),
    "CDL_3OUTSIDE":        ("3 Outside Up ↑",      "3 Outside Down ↓"),
    "CDL_ABANDONEDBABY":   ("Abandoned Baby ↑",    "Abandoned Baby ↓"),
    "CDL_BREAKAWAY":       ("Breakaway ↑",         "Breakaway ↓"),
}

_SINGLE_SET = {f"CDL_{p.upper()}" for p in SINGLE_CANDLE_PATTERNS}
_TWO_SET    = {f"CDL_{p.upper()}" for p in TWO_CANDLE_PATTERNS}


# ──────────────────────────────────────────────────────────────────────────────
# Step 1 – Fetch OHLCV for any supported interval
# ──────────────────────────────────────────────────────────────────────────────

def fetch_ohlcv(sm: ServiceManager, symbol: str, interval: str) -> pd.DataFrame:
    """
    Fetches pre/post-market OHLCV data for `symbol` at `interval` using
    ServiceManager.download_stock_data (which already passes includePrePost=true).

    Applies the same boundary alignment and resampling used in
    ServiceManager.GetStockdata_Byinterval for each interval, but retains the
    full history (GetStockdata_Byinterval truncates to 5 rows via .tail(5)).

    Supported intervals: "15m", "30m", "1h", "4h"
    """
    if interval not in _INTERVAL_MINUTES:
        raise ValueError(
            f"Unsupported interval '{interval}'. "
            f"Choose from: {list(_INTERVAL_MINUTES)}"
        )

    days     = _LOOKBACK_DAYS[interval]
    start_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    end_dt   = datetime.now()

    # Align end_dt to interval boundary (mirrors GetStockdata_Byinterval)
    mod = _INTERVAL_MOD[interval]
    if interval in ("15m", "30m"):
        rem    = end_dt.minute % mod
        end_dt = end_dt.replace(minute=end_dt.minute - rem, second=0, microsecond=0)
    elif interval == "1h":
        end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    elif interval == "4h":
        rem    = end_dt.hour % 4
        end_dt = end_dt.replace(
            hour=end_dt.hour - rem, minute=0, second=0, microsecond=0
        )

    # download_stock_data maps "1h"/"4h" → "30m" internally, so we always
    # receive 30-minute bars; post-download resampling matches GetStockdata_Byinterval
    df = sm.download_stock_data(symbol, start_ts, end_dt.timestamp(), interval=interval)
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {symbol} @ {interval}")

    ep = end_dt.timestamp() - 1

    if interval == "15m":
        df = df.loc[
            (df["unixtime"] <= ep) & df["minute"].isin({"00", "15", "30", "45"})
        ].copy()

    elif interval == "30m":
        df = df.loc[
            (df["unixtime"] <= ep) & df["minute"].isin({"00", "30"})
        ].copy()

    elif interval == "1h":
        # Resample 30-minute bars → 1-hour (mirrors GetStockdata_Byinterval)
        df = (
            df.resample("1h", origin="epoch")
            .agg({"unixtime": "first", "open": "first",
                  "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        df = sm._attach_dt_cols(df)
        df = df[df["unixtime"] <= ep].copy()

    elif interval == "4h":
        df = df[df["minute"].isin({"00"})].copy()
        df = df[df["unixtime"] <= ep].copy()
        df = (
            df.resample("4h", origin="epoch", offset="3h",
                        closed="right", label="right")
            .agg({"unixtime": "first", "open": "first",
                  "high": "max", "low": "min", "close": "last"})
            .dropna()
        )
        df = sm._attach_dt_cols(df)

    df.reset_index(drop=True, inplace=True)

    if df.empty:
        raise RuntimeError(
            f"No bars left after alignment for {symbol} @ {interval}"
        )
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 2 – pandas-ta-classic Strategy
# ──────────────────────────────────────────────────────────────────────────────

def build_strategy(calc_macd: bool = True, calc_rsi: bool = True) -> ta.Strategy:
    """
    Builds a pandas-ta-classic Strategy containing:
      • MACD (12/26/9)         — only if calc_macd=True
      • RSI  (14)              — only if calc_rsi=True
      • All 26 CDL patterns    — always included

    calc_macd / calc_rsi can be set False for higher timeframes (1h, 4h) where
    the indicator scoring is optional or too noisy on short history.
    """
    ta_list: list[dict] = []

    if calc_macd:
        ta_list.append({"kind": "macd", "fast": 12, "slow": 26, "signal": 9})
    if calc_rsi:
        ta_list.append({"kind": "rsi", "length": 14})

    # Candle patterns are always computed regardless of scoring flags
    ta_list.append({"kind": "cdl_pattern", "name": ALL_PATTERNS})

    name = f"CandleStrat_macd={calc_macd}_rsi={calc_rsi}"
    return ta.Strategy(
        name        = name,
        description = f"MACD={calc_macd}  RSI={calc_rsi}  CDL patterns=26",
        ta          = ta_list,
    )


def run_strategy(
    df: pd.DataFrame,
    calc_macd: bool = True,
    calc_rsi:  bool = True,
) -> Tuple[pd.DataFrame, list[str]]:
    """
    Executes the Strategy on df using df.ta.strategy().
    Returns (enriched_df, cdl_column_names).
    pandas-ta-classic requires float64 OHLCV.
    """
    result = df.copy()
    for col in ("open", "high", "low", "close"):
        result[col] = result[col].astype("float64")

    strat = build_strategy(calc_macd=calc_macd, calc_rsi=calc_rsi)
    result.ta.strategy(strat)

    cdl_cols = [c for c in result.columns if c.startswith("CDL_")]
    for c in cdl_cols:
        result[c] = result[c].fillna(0).astype("int16")

    return result, cdl_cols


# ──────────────────────────────────────────────────────────────────────────────
# Step 3 – MACD score  (mirrors ServiceManager.calculate_TrendAlert)
# ──────────────────────────────────────────────────────────────────────────────

def calculate_macd_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised replication of ServiceManager.calculate_TrendAlert across every
    row in the DataFrame.

    Score criteria (same as calculate_TrendAlert):
      +1 / -1  MACD & Signal both positive / negative
      +1 / -1  Histogram > 0.1  /  < -0.1
      +1 / -1  MACD rising / falling vs prior bar
    Net score ∈ {-3 … +3} → stored as 'macd_score' (int8)
    Text bias stored as 'macd_bias'.

    Skipped silently if MACD columns are absent (calc_macd=False was used).
    """
    required = {"MACD_12_26_9", "MACDh_12_26_9", "MACDs_12_26_9"}
    if not required.issubset(df.columns):
        return df  # scoring was disabled — leave df unchanged

    macd      = df["MACD_12_26_9"].astype("float64")
    hist      = df["MACDh_12_26_9"].astype("float64")
    sig       = df["MACDs_12_26_9"].astype("float64")
    macd_prev = macd.shift(1)

    bull = (
        ((macd > 0) & (sig > 0)).astype(int) +
        (hist > 0.1).astype(int) +
        (macd > macd_prev).astype(int)
    )
    bear = (
        ((macd < 0) & (sig < 0)).astype(int) +
        (hist < -0.1).astype(int) +
        (macd < macd_prev).astype(int)
    )
    score = (bull - bear).astype("int8")

    df["macd_score"] = score
    df["macd_bias"]  = [
        ("Strong Bull" if s >= 2 else
         "Bull"        if s == 1 else
         "Strong Bear" if s <= -2 else
         "Bear"        if s == -1 else
         "Neutral")
        for s in score
    ]
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 4 – RSI score  (mirrors ServiceManager.calculate_RSITrendAlert)
# ──────────────────────────────────────────────────────────────────────────────

def calculate_rsi_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorised replication of ServiceManager.calculate_RSITrendAlert across
    every row in the DataFrame.

    Score criteria (same weights as calculate_RSITrendAlert):
      RSI level    ±40 (extreme) / ±20 (moderate)
      RSI vs EMA   ±25
      Crossover    ±15
      Midline 50   ±10
    Clamped to [-100, +100] → stored as 'rsi_score' (float32)
    Text bias stored as 'rsi_bias'.

    Skipped silently if RSI_14 column is absent (calc_rsi=False was used).
    """
    if "RSI_14" not in df.columns:
        return df  # scoring was disabled — leave df unchanged

    rsi     = df["RSI_14"].astype("float64")
    rsig    = rsi.ewm(span=14, adjust=False).mean()   # EMA signal of RSI
    rsi_p   = rsi.shift(1)
    rsig_p  = rsig.shift(1)

    def _row(i: int) -> float:
        if i < 1:
            return 0.0
        r, rs  = rsi.iat[i],  rsig.iat[i]
        rp, rsp = rsi_p.iat[i], rsig_p.iat[i]
        s = 0.0
        if   r < 30: s += 40
        elif r < 40: s += 20
        elif r > 70: s -= 40
        elif r > 60: s -= 20
        s += 25 if r > rs else -25
        if   rp < rsp and r > rs: s += 15
        elif rp > rsp and r < rs: s -= 15
        if   50 < r < 60: s += 10
        elif 40 < r < 50: s -= 10
        return max(min(s, 100.0), -100.0)

    scores = [_row(i) for i in range(len(df))]
    df["rsi_score"] = pd.array(scores, dtype="float32")
    df["rsi_bias"]  = [
        ("Strong Bull" if s >= 50 else
         "Bull"        if s >= 20 else
         "Strong Bear" if s <= -50 else
         "Bear"        if s <= -20 else
         "Neutral")
        for s in scores
    ]
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 5 – Candle signal labels
# ──────────────────────────────────────────────────────────────────────────────

def add_candle_signal(df: pd.DataFrame, cdl_cols: list[str]) -> pd.DataFrame:
    """
    Adds 'candle_signal' (plain-English label for the strongest CDL pattern on
    each bar) and 'candle_group' (Single / Two-candle / Three-candle).
    When multiple patterns fire, the one with the highest absolute value wins.
    """
    def _label(row_slice: pd.Series) -> tuple[str, str]:
        active = {c: row_slice[c] for c in cdl_cols if row_slice[c] != 0}
        if not active:
            return "—", "—"
        strongest = max(active, key=lambda c: abs(active[c]))
        val = active[strongest]
        bull, bear = _LABEL_MAP.get(strongest, (strongest, strongest))
        label = bull if val > 0 else bear
        group = (
            "Single"      if strongest in _SINGLE_SET else
            "Two-candle"  if strongest in _TWO_SET    else
            "Three-candle"
        )
        return label, group

    pairs = [_label(row) for _, row in df[cdl_cols].iterrows()]
    df["candle_signal"] = [p[0] for p in pairs]
    df["candle_group"]  = [p[1] for p in pairs]
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 6 – Composite overall bias
# ──────────────────────────────────────────────────────────────────────────────

def add_overall_bias(df: pd.DataFrame) -> pd.DataFrame:
    """
    Majority-vote composite of whatever scoring columns are present
    (macd_bias, rsi_bias) plus candle_signal direction arrows.
    Result stored as 'overall_bias'.
    """
    def _vote(row) -> str:
        bull = bear = 0
        for col in ("macd_bias", "rsi_bias"):
            if col in row:
                v = str(row[col])
                if "Bull" in v:  bull += 1
                elif "Bear" in v: bear += 1
        sig = str(row.get("candle_signal", ""))
        if "↑" in sig:   bull += 1
        elif "↓" in sig: bear += 1
        if bull > bear:  return "⬆ Bullish"
        if bear > bull:  return "⬇ Bearish"
        return "↔ Neutral"

    df["overall_bias"] = df.apply(_vote, axis=1)
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 6b – Bias change detection
# ──────────────────────────────────────────────────────────────────────────────

def detect_bias_change(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compares overall_bias of the last fully-closed candle against its predecessor
    and sets 'bias_changed' flag column:

        1  → bias flipped TO Bullish  on the latest closed candle
       -1  → bias flipped TO Bearish  on the latest closed candle
        0  → no change (or Neutral on either side)

    Only the two most-recent rows are evaluated; all prior rows receive 0.
    This is designed for 15m, 30m, and 1h intervals.

    The function also returns a convenience dict via df.attrs['bias_change_info']
    containing the last two candles' metadata for alert message construction.
    """
    df["bias_changed"] = 0  # default: no change

    if len(df) < 2:
        df.attrs["bias_change_info"] = None
        return df

    prev_row = df.iloc[-2]
    last_row = df.iloc[-1]

    prev_bias = str(prev_row.get("overall_bias", ""))
    last_bias = str(last_row.get("overall_bias", ""))

    changed = False
    flag    = 0

    if prev_bias != last_bias:
        if "Bullish" in last_bias and "Bearish" in prev_bias:
            flag    = 1
            changed = True
        elif "Bearish" in last_bias and "Bullish" in prev_bias:
            flag    = -1
            changed = True
        # Neutral transitions are tracked but not flagged (flag stays 0)

    df.at[df.index[-1], "bias_changed"] = flag

    # Attach metadata for alert construction (accessible outside this function)
    df.attrs["bias_change_info"] = {
        "changed":    changed,
        "flag":       flag,
        "prev_bias":  prev_bias,
        "last_bias":  last_bias,
        "prev_time":  f"{prev_row.get('hour','?')}:{prev_row.get('minute','?')}",
        "last_time":  f"{last_row.get('hour','?')}:{last_row.get('minute','?')}",
        "prev_close": prev_row.get("close", "?"),
        "last_close": last_row.get("close", "?"),
        "rec_dt":     str(last_row.get("rec_dt", "")),
    }
    return df


# ──────────────────────────────────────────────────────────────────────────────
# Step 7 – Display
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(
    df:        pd.DataFrame,
    cdl_cols:  list[str],
    symbol:    str,
    interval:  str,
) -> None:
    """
    Prints three sections:
      1. Indicator snapshot — last 10 bars
      2. Candlestick pattern hits — rows where any pattern fired
      3. Pattern frequency table
    Only columns that actually exist in df are shown (handles disabled flags).
    """
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 230)
    pd.set_option("display.float_format", "{:.2f}".format)

    sep1 = "─" * 115
    sep2 = "═" * 115
    tag  = f"{symbol} @ {interval} | pre/post market"

    # ── 1. Indicator snapshot ────────────────────────────────────────────────
    snap_candidates = [
        "rec_dt", "hour", "minute",
        "open", "high", "low", "close",
        "MACD_12_26_9", "MACDh_12_26_9", "RSI_14",
        "macd_score", "macd_bias",
        "rsi_score",  "rsi_bias",
        "overall_bias",
    ]
    snap_cols = [c for c in snap_candidates if c in df.columns]

    print(f"\n{sep2}")
    print(f"  INDICATOR SNAPSHOT  |  {tag}  |  last 10 bars")
    print(sep2)
    print(df[snap_cols].tail(10).to_string(index=False))
    print(sep2)

   # ── 2. Pattern hit rows ──────────────────────────────────────────────────
    hit_candidates = [
        "rec_dt", "hour", "minute",
        "open", "high", "low", "close",
        "macd_score", "macd_bias",
        "rsi_score",  "rsi_bias",
        "candle_signal", "candle_group", "overall_bias",
    ]
    hit_cols  = [c for c in hit_candidates if c in df.columns]
    hit_mask  = (df[cdl_cols] != 0).any(axis=1)
    hits      = df.loc[hit_mask, hit_cols]

    print(f"\n{sep1}")
    print(f"  PATTERN HITS  |  {tag}  |  {len(hits)} signal(s) in window")
    print(sep1)
    if hits.empty:
        print("  No patterns detected in this window.")
    else:
        print(hits.tail(20).to_string(index=False))
    print(sep1)

    # ── 3. Frequency table ───────────────────────────────────────────────────
    freq = {c: int((df[c] != 0).sum()) for c in cdl_cols if (df[c] != 0).any()}
    if freq:
        freq_df = (
            pd.DataFrame.from_dict(freq, orient="index", columns=["fires"])
            .sort_values("fires", ascending=False)
        )
        # print(f"\n{sep1}")
        # print(f"  PATTERN FREQUENCY  |  {tag}")
        # print(sep1)
        # print(freq_df.to_string())
        # print(sep1)
    print()


# ──────────────────────────────────────────────────────────────────────────────
# Full pipeline
# ──────────────────────────────────────────────────────────────────────────────

def process(
    sm:        ServiceManager,
    symbol:    str,
    interval:  str,
    calc_macd: bool = True,
    calc_rsi:  bool = True,
    save_csv:  bool = True,
) -> Optional[pd.DataFrame]:
    """
    End-to-end pipeline for one symbol / interval combination.

    Parameters
    ----------
    sm         : ServiceManager instance (shared across calls)
    symbol     : Ticker string, e.g. "SPY", "QQQ", "IWM", "GLD"
    interval   : "15m" | "30m" | "1h" | "4h"
    calc_macd  : If True, compute MACD and macd_score / macd_bias.
                 Set False to skip for higher timeframes where MACD is noisy.
    calc_rsi   : If True, compute RSI and rsi_score / rsi_bias.
                 Set False to skip for higher timeframes where RSI is optional.
    save_csv   : If True, write results to <symbol>_<interval>_analysis.csv

    Returns the enriched DataFrame with bias_change_info stored in df.attrs.
    Alerting is handled by the caller after all intervals are processed.
    """
    tag = f"{symbol} @ {interval}"
    print(f"\n{'━'*60}")
    print(f"  [{datetime.now():%H:%M:%S}]  Processing {tag}")
    print(f"  MACD score={calc_macd}   RSI score={calc_rsi}")
    print(f"{'━'*60}")

    try:
        # 1. Fetch OHLCV
        df = fetch_ohlcv(sm, symbol, interval)
        print(f"  Rows : {len(df)}   "
              f"Range: {df['rec_dt'].iloc[0]} → {df['rec_dt'].iloc[-1]}")

        # 2. Strategy: MACD + RSI (conditional) + 26 CDL patterns
        df, cdl_cols = run_strategy(df, calc_macd=calc_macd, calc_rsi=calc_rsi)
        print(f"  Strategy done — {len(cdl_cols)} CDL cols added")

        # 3. MACD score (skipped internally if column absent)
        if calc_macd:
            df = calculate_macd_score(df)

        # 4. RSI score (skipped internally if column absent)
        if calc_rsi:
            df = calculate_rsi_score(df)

        # 5. Candle signal labels
        df = add_candle_signal(df, cdl_cols)

        # 6. Composite overall bias
        df = add_overall_bias(df)

        # 6b. Bias change detection (15m, 30m, 1h supported)
        #     Alerting is deferred to the caller (run_candle_analysis) so that
        #     all intervals can be collected first and ONE combined alert sent.
        if interval in ("15m", "30m", "1h"):
            df = detect_bias_change(df)
            info = df.attrs.get("bias_change_info")

            if info and info["changed"]:
                print(f"\n  ⚡ BIAS CHANGE DETECTED: {symbol} {interval}")
                print(f"     {info['prev_bias']} → {info['last_bias']}")
            else:
                bias_col = df["overall_bias"].iloc[-1] if "overall_bias" in df.columns else "N/A"
                print(f"  No bias change — current: {bias_col}")

        # 7. Console summary
        print_summary(df, cdl_cols, symbol, interval)

        # 8. Optional CSV export
        if save_csv:
            base_cols = [
                "rec_dt", "hour", "minute", "unixtime",
                "open", "high", "low", "close",
            ]
            score_cols = []
            if "MACD_12_26_9" in df.columns:
                score_cols += ["MACD_12_26_9", "MACDh_12_26_9", "MACDs_12_26_9",
                               "macd_score", "macd_bias"]
            if "RSI_14" in df.columns:
                score_cols += ["RSI_14", "rsi_score", "rsi_bias"]
            label_cols  = ["candle_signal", "candle_group", "overall_bias", "bias_changed"]
            save_cols   = [c for c in base_cols + score_cols + label_cols + cdl_cols
                           if c in df.columns]
            out = f"{symbol}_{interval}_analysis.csv"
            df[save_cols].to_csv(out, index=False)
            print(f"  Saved → {out}")

        return df

    except Exception as exc:
        print(f"  ✖  {tag} failed: {exc}")
        return None

    finally:
        gc.collect()
