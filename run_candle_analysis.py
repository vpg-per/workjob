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
import os
import gc
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
from gitalertmanager import AlertManager
from dataManager import ServiceManager
from stock_candle_processor import process

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
# Helper: build the `row` dict expected by AlertManager DB methods
# ──────────────────────────────────────────────────────────────────────────────

def _build_db_row(info: dict) -> dict:
    """
    Convert a bias_change_info dict (stored in df.attrs) into the flat `row`
    dict expected by AlertManager.isAlertExistsinDB / AddAlertRecordtoDB.

    bias_change_info is expected to contain at minimum:
        lasttime   – datetime-like or str of the bar's timestamp
        last_bias  – e.g. "Bullish"
        last_close – float closing price
        flag       – 1 (bull flip) or -1 (bear flip)
        rec_dt     – date portion used as the record date

    The keys nmonth / nday / hour / minute are derived here so gitalertmanager
    does not need to recompute them.
    """
    from datetime import datetime as _dt

    # lasttime may arrive as a datetime or as a string — normalise to datetime
    raw_lt = info.get("lasttime") or info.get("last_time")
    if isinstance(raw_lt, str):
        # Try common formats; fall back to "now" so we never crash
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                raw_lt = _dt.strptime(raw_lt, fmt)
                break
            except ValueError:
                pass
        else:
            raw_lt = _dt.now()

    if not isinstance(raw_lt, _dt):
        raw_lt = _dt.now()

    return {
        # used by isAlertExistsinDB lookup
        "lasttime": info.get("lasttime") or info.get("last_time"),
        "rec_dt":   info.get("rec_dt", raw_lt.date()),
        # decomposed for the dtlookupval string (kept for backward compat)
        "nmonth": raw_lt.month,
        "nday":   raw_lt.day,
        "hour":   raw_lt.hour,
        "minute": raw_lt.minute,
        # used by AddAlertRecordtoDB insert
        "last_time":  info.get("lasttime") or info.get("last_time"),
        "last_bias":  info.get("last_bias", ""),
        "last_close": info.get("last_close", 0.0),
        "flag":       info.get("flag", 0),
    }



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
                f"  (close: {info['last_close']:.2f})"
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

    sm = ServiceManager()
    altMgr = AlertManager()

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

    # ── Combined multi-timeframe alert (DB-gated) ─────────────────────────────
    # For each symbol:
    #   1. Check every flipped interval against the DB (isAlertExistsinDB).
    #   2. Only proceed with an alert if AT LEAST ONE interval flip is new
    #      (i.e. not already recorded in the DB).
    #   3. Insert new flip records (AddAlertRecordtoDB) and send ONE Telegram
    #      alert per symbol.  Intervals already in the DB are skipped silently.
    symbols_in_run = dict.fromkeys(sym for sym, *_ in RUN_MATRIX)  # ordered, deduped

    print(f"\n{'─'*70}")
    print("  BIAS CHANGE SUMMARY")
    print(f"{'─'*70}")

    for symbol in symbols_in_run:
        alert_msg = build_combined_alert(symbol, results)

        if not alert_msg:
            # No flip detected on any timeframe — print current biases only
            print(f"\n  {symbol}  — no bias flip detected")
            for interval in _MTF_INTERVALS:
                df = results.get((symbol, interval))
                if df is not None and "overall_bias" in df.columns:
                    cur = df["overall_bias"].iloc[-1]
                    print(f"    {interval:>3s}  current bias: {cur}")
            continue

        # ── DB gate: collect intervals that flipped AND are new ──────────────
        new_flip_intervals: list[str] = []

        for interval in _MTF_INTERVALS:
            df = results.get((symbol, interval))
            if df is None:
                continue
            info = getattr(df, "attrs", {}).get("bias_change_info")
            if not info or not info.get("changed"):
                continue  # no flip on this interval

            row = _build_db_row(info)

            if altMgr.isAlertExistsinDB(row, symbol, interval):
                print(f"  [DB] {symbol} {interval} flip already recorded — skipping")
            else:
                new_flip_intervals.append(interval)
                altMgr.AddAlertRecordtoDB(row, symbol, interval)
                print(f"  [DB] {symbol} {interval} new flip — recorded in DB")

        # ── Send Telegram only when there is at least one genuinely new flip ──
        if new_flip_intervals:
            print(f"\n{alert_msg}")
            altMgr.send_chart_alert(alert_msg)
            print(f"\n  ✔  Telegram alert sent for {symbol} "
                  f"(new flips: {', '.join(new_flip_intervals)})")
        else:
            print(f"\n  {symbol}  — all flips already alerted (no Telegram message sent)")
            # Still print the current biases for visibility
            print(alert_msg)

    # ── Final summary ────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  COMPLETED : {datetime.now():%H:%M:%S}")
    print(f"  Succeeded : {len(results)} / {len(RUN_MATRIX)}")
    if failed:
        print(f"  Failed    : {[f'{s} {i}' for s, i in failed]}")
    print(f"{'═'*70}\n")

if __name__ == "__main__":
    main()
