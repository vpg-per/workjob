"""
run_candle_analysis.py
──────────────────────
Entry-point script.  Calls stock_candle_processor.process() for every
symbol / interval / scoring-flag combination defined in RUN_MATRIX below.

After ALL intervals finish, collects bias_change_info from each result and
sends ONE combined Telegram alert (via AlertManager.send_chart_alert) that
includes a multi-timeframe agreement flag.

Multi-Timeframe Agreement logic
────────────────────────────────
  STRONG BULL  : 15m AND 30m both flipped Bullish  (regardless of 1h)
  STRONG BEAR  : 15m AND 30m both flipped Bearish
  CONFIRMED ↑  : 15m flipped Bullish AND 1h current bias is Bullish
  CONFIRMED ↓  : 15m flipped Bearish AND 1h current bias is Bearish
  WEAK         : only one timeframe changed, others unchanged or conflicting
  NO CHANGE    : no bias flip detected on any timeframe

Edit RUN_MATRIX to add or remove symbols, intervals, or toggle scoring flags.

Usage
─────
    python run_candle_analysis.py

Output
──────
  Console  : indicator snapshot + pattern hits + frequency table per combo
             + one combined bias-change summary at the end
  Telegram : one combined alert if any bias change detected
  CSV      : <SYMBOL>_<INTERVAL>_analysis.csv  (one file per combination,
             if save_csv=True inside the process() calls)
"""

import gc
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Locate stock_candle_processor in the same directory ──────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from stock_candle_processor import ServiceManager, process

# ── AlertManager (optional — gracefully skipped if unavailable) ───────────────
try:
    _AM_PATH = _HERE / "alertManager.py"
    _am_spec   = importlib.util.spec_from_file_location("alertManager", _AM_PATH)
    _am_module = importlib.util.module_from_spec(_am_spec)
    _am_spec.loader.exec_module(_am_module)
    AlertManager = _am_module.AlertManager
    _ALERT_MANAGER_AVAILABLE = True
except Exception as _am_err:
    print(f"[WARN] AlertManager not loaded ({_am_err}). Alerts disabled.")
    AlertManager = None
    _ALERT_MANAGER_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Run matrix
# ──────────────────────────────────────────────────────────────────────────────
# Each entry: (symbol, interval, calc_macd, calc_rsi)
#
# calc_macd / calc_rsi:
#   True  → compute score (good for intraday: 15m, 30m)
#   False → skip scoring (sensible for higher timeframes: 1h, 4h where
#           short history limits MACD/RSI reliability)
#
# Add rows freely — a single ServiceManager instance is reused for all calls.
# ──────────────────────────────────────────────────────────────────────────────

RUN_MATRIX: list[tuple[str, str, bool, bool]] = [
    # symbol   interval   calc_macd  calc_rsi
    # ─────── ─────────  ─────────  ────────
    ("SPY",   "15m",     True,      True),
    ("SPY",   "30m",     True,      True),
    ("SPY",   "1h",      False,     False),
    ("IWM",   "15m",     True,      True),
    ("IWM",   "30m",     True,      True),
    ("IWM",   "1h",      False,     False),
    ("QQQ",   "15m",     True,      True),
    ("QQQ",   "30m",     True,      True),
    ("QQQ",   "1h",      False,     False),
    ("GLD",   "15m",     True,      True),
    ("GLD",   "30m",     True,      True),
    ("GLD",   "1h",      False,     False),
]

# Set to True to also run 4h bars (only 30 days of data available from Yahoo)
INCLUDE_4H = False
if INCLUDE_4H:
    for sym in ("SPY", "QQQ", "IWM", "GLD"):
        RUN_MATRIX.append((sym, "4h", False, False))


# ──────────────────────────────────────────────────────────────────────────────
# Multi-timeframe alert builder
# ──────────────────────────────────────────────────────────────────────────────

# Intervals considered for multi-timeframe agreement (in priority order)
_MTF_INTERVALS = ("15m", "30m", "1h")

# Emoji map
_BIAS_EMOJI = {
    "Bullish": "🟢",
    "Bearish": "🔴",
    "Neutral": "⚪",
}

def _bias_emoji(bias: str) -> str:
    for key, emoji in _BIAS_EMOJI.items():
        if key in bias:
            return emoji
    return "⚪"


def build_combined_alert(
    symbol:  str,
    results: dict[tuple[str, str], object],
) -> str | None:
    """
    Inspects bias_change_info from every completed interval result for `symbol`.
    Returns a formatted alert string if at least one interval flipped, else None.

    Multi-Timeframe Agreement flag (mtf_agreement):
      "🔥 STRONG BULL"   — 15m AND 30m both flipped Bullish
      "🔥 STRONG BEAR"   — 15m AND 30m both flipped Bearish
      "✅ CONFIRMED BULL" — 15m flipped Bullish AND 1h current bias is Bullish
      "✅ CONFIRMED BEAR" — 15m flipped Bearish AND 1h current bias is Bearish
      "⚠️  WEAK / MIXED"  — only one TF changed, or TFs conflict
      (no flag line)      — no flip detected on any TF
    """
    # Gather info per interval — only for MTF intervals
    interval_infos: dict[str, dict] = {}
    for interval in _MTF_INTERVALS:
        df = results.get((symbol, interval))
        if df is None:
            continue
        info = getattr(df, "attrs", {}).get("bias_change_info")
        if info:
            # Also attach the current (last) overall_bias regardless of change
            info["current_bias"] = str(df["overall_bias"].iloc[-1]) if "overall_bias" in df.columns else "N/A"
            interval_infos[interval] = info

    if not interval_infos:
        return None  # no MTF intervals ran

    any_changed = any(v["changed"] for v in interval_infos.values())
    if not any_changed:
        return None  # nothing flipped — no alert needed

    # ── Determine multi-timeframe agreement flag ──────────────────────────────
    info_15 = interval_infos.get("15m")
    info_30 = interval_infos.get("30m")
    info_1h = interval_infos.get("1h")

    mtf_flag = ""

    flipped_15 = info_15["changed"]  if info_15 else False
    flip_15_dir = info_15["flag"]    if info_15 else 0     # 1=bull, -1=bear
    flipped_30 = info_30["changed"]  if info_30 else False
    flip_30_dir = info_30["flag"]    if info_30 else 0
    bias_1h_cur = info_1h["current_bias"] if info_1h else ""

    if flipped_15 and flipped_30 and flip_15_dir == flip_30_dir:
        if flip_15_dir == 1:
            mtf_flag = "🔥 STRONG BULL — 15m & 30m both flipped Bullish"
        else:
            mtf_flag = "🔥 STRONG BEAR — 15m & 30m both flipped Bearish"
    elif flipped_15:
        if flip_15_dir == 1 and "Bullish" in bias_1h_cur:
            mtf_flag = "✅ CONFIRMED BULL — 15m flipped Bullish, 1h trend Bullish"
        elif flip_15_dir == -1 and "Bearish" in bias_1h_cur:
            mtf_flag = "✅ CONFIRMED BEAR — 15m flipped Bearish, 1h trend Bearish"
        else:
            mtf_flag = "⚠️  WEAK / MIXED — 15m flipped but other TFs not aligned"
    elif flipped_30:
        if flip_30_dir == 1 and "Bullish" in bias_1h_cur:
            mtf_flag = "✅ CONFIRMED BULL — 30m flipped Bullish, 1h trend Bullish"
        elif flip_30_dir == -1 and "Bearish" in bias_1h_cur:
            mtf_flag = "✅ CONFIRMED BEAR — 30m flipped Bearish, 1h trend Bearish"
        else:
            mtf_flag = "⚠️  WEAK / MIXED — 30m flipped but other TFs not aligned"

    # ── Build message ─────────────────────────────────────────────────────────
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"📊 {symbol} — Bias Change Alert",
        f"🕐 {now_str}",
        "",
    ]

    for interval in _MTF_INTERVALS:
        info = interval_infos.get(interval)
        if info is None:
            continue
        cur  = info["current_bias"]
        em   = _bias_emoji(cur)
        if info["changed"]:
            lines.append(
                f"{em} {interval:>3s}  FLIPPED: {info['prev_bias']} → {info['last_bias']}"
                f"  (close: {info['last_close']})"
            )
        else:
            lines.append(
                f"{em} {interval:>3s}  No change  [current: {cur}]"
            )

    if mtf_flag:
        lines.append("")
        lines.append(f"► MTF Agreement: {mtf_flag}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'═'*70}")
    print(f"  Multi-Symbol Candlestick Analyser")
    print(f"  Started : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Jobs    : {len(RUN_MATRIX)}")
    print(f"{'═'*70}")

    # One shared ServiceManager — avoids re-loading the module per call
    sm = ServiceManager()

    # One shared AlertManager — single combined alert sent after all intervals
    am = None
    if _ALERT_MANAGER_AVAILABLE:
        try:
            am = AlertManager()
            print("  AlertManager   : ready")
        except Exception as _e:
            print(f"  AlertManager   : init failed ({_e}) — alerts disabled")

    results: dict[tuple[str, str], object] = {}
    failed:  list[tuple[str, str]] = []

    for idx, (symbol, interval, calc_macd, calc_rsi) in enumerate(RUN_MATRIX, 1):
        key = (symbol, interval)
        print(f"\n[{idx}/{len(RUN_MATRIX)}]", end="")

        t0 = time.time()
        df = process(
            sm        = sm,
            symbol    = symbol,
            interval  = interval,
            calc_macd = calc_macd,
            calc_rsi  = calc_rsi,
            save_csv  = False,
        )
        elapsed = time.time() - t0

        if df is not None:
            results[key] = df
            print(f"  ✔  {symbol} {interval} completed in {elapsed:.1f}s")
        else:
            failed.append(key)
            print(f"  ✖  {symbol} {interval} failed after {elapsed:.1f}s")

        # Be polite to Yahoo Finance rate limits between requests
        if idx < len(RUN_MATRIX):
            time.sleep(0.5)

        gc.collect()

    # ── Combined multi-timeframe alert ────────────────────────────────────────
    # Group results by symbol and send one alert per symbol
    symbols_in_run = dict.fromkeys(sym for sym, *_ in RUN_MATRIX)  # ordered, deduped

    print(f"\n{'─'*70}")
    print("  BIAS CHANGE SUMMARY")
    print(f"{'─'*70}")

    for symbol in symbols_in_run:
        alert_msg = build_combined_alert(symbol, results)

        if alert_msg:
            print(f"\n{alert_msg}")
            if am is not None:
                try:
                    am.send_chart_alert(alert_msg)
                    print(f"\n  ✔  Telegram alert sent for {symbol}")
                except Exception as alert_exc:
                    print(f"\n  ✖  Telegram alert failed for {symbol}: {alert_exc}")
        else:
            # Summarise current bias for each interval without an alert
            print(f"\n  {symbol}  — no bias flip detected")
            for interval in _MTF_INTERVALS:
                df = results.get((symbol, interval))
                if df is not None and "overall_bias" in df.columns:
                    cur = df["overall_bias"].iloc[-1]
                    print(f"    {interval:>3s}  current bias: {cur}")

    # ── Final summary ────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  COMPLETED : {datetime.now():%H:%M:%S}")
    print(f"  Succeeded : {len(results)} / {len(RUN_MATRIX)}")
    if failed:
        print(f"  Failed    : {[f'{s} {i}' for s, i in failed]}")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
