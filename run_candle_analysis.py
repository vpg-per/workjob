"""
run_candle_analysis.py
──────────────────────
Entry-point script.  Calls stock_candle_processor.process() for every
symbol / interval / scoring-flag combination defined in RUN_MATRIX below.

After ALL intervals finish, collects bias_change_info from each result and
sends ONE combined Telegram alert (via AlertManager.send_chart_alert) that
includes a multi-timeframe agreement flag AND directional price targets.

Multi-Timeframe Agreement logic
────────────────────────────────
  STRONG BULL  : 15m AND 30m both flipped Bullish  (regardless of 1h)
  STRONG BEAR  : 15m AND 30m both flipped Bearish
  CONFIRMED ↑  : 15m flipped Bullish AND 1h current bias is Bullish
  CONFIRMED ↓  : 15m flipped Bearish AND 1h current bias is Bearish
  WEAK         : only one timeframe changed, others unchanged or conflicting
  NO CHANGE    : no bias flip detected on any timeframe

Directional Targets logic (NEW)
─────────────────────────────────
  When MTF bias is Bullish:
    Targets    → resistance levels ABOVE current price (ascending)
    Stop guide → support levels BELOW current price (nearest first)
    Session    → PDH, OR-High, PM-High highlighted as breakout targets

  When MTF bias is Bearish:
    Targets    → support levels BELOW current price (descending)
    Stop guide → resistance levels ABOVE current price (nearest first)
    Session    → PDL, OR-Low, PM-Low highlighted as breakdown targets

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
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from gitalertmanager import AlertManager
from dataManager import ServiceManager
from stock_candle_processor import ( process, attach_key_levels)

# ──────────────────────────────────────────────────────────────────────────────
# Run matrix
# ──────────────────────────────────────────────────────────────────────────────
RUN_MATRIX: list[tuple[str, str, bool, bool]] = [
    # symbol   interval   calc_macd  calc_rsi
    ("SPY",   "15m",     True,      True),
    ("SPY",   "30m",     True,      True),
    ("SPY",   "1h",      True,     True),
    ("QQQ",   "15m",     True,      True),
    ("QQQ",   "30m",     True,      True),
    ("QQQ",   "1h",      True,     True),
    ("IWM",   "15m",     True,      True),
    ("IWM",   "30m",     True,      True),
    ("IWM",   "1h",      True,     True),
    ("GLD",   "15m",     True,      True),
    ("GLD",   "30m",     True,      True),
    ("GLD",   "1h",      True,     True),
]

INCLUDE_4H = False
if INCLUDE_4H:
    for sym in ("SPY", "QQQ", "IWM", "GLD"):
        RUN_MATRIX.append((sym, "4h", False, False))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_db_row(info: dict) -> dict:
    from datetime import datetime as _dt
    raw_lt = info.get("lasttime") or info.get("last_time")
    if isinstance(raw_lt, str):
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
        "lasttime":   info.get("lasttime") or info.get("last_time"),
        "rec_dt":     info.get("rec_dt", raw_lt.date()),
        "nmonth":     raw_lt.month,
        "nday":       raw_lt.day,
        "hour":       raw_lt.hour,
        "minute":     raw_lt.minute,
        "last_time":  info.get("lasttime") or info.get("last_time"),
        "last_bias":  info.get("last_bias", ""),
        "last_close": info.get("last_close", 0.0),
        "flag":       info.get("flag", 0),
    }


_MTF_INTERVALS = ("15m", "30m", "1h")

_BIAS_EMOJI = {"Bullish": "🟢", "Bearish": "🔴", "Neutral": "⚪"}

def _bias_emoji(bias: str) -> str:
    for key, emoji in _BIAS_EMOJI.items():
        if key in bias:
            return emoji
    return "⚪"

def _is_bullish(bias: str) -> bool:
    return "Bullish" in bias

def _is_bearish(bias: str) -> bool:
    return "Bearish" in bias


# ──────────────────────────────────────────────────────────────────────────────
# NEW: Build directional targets block for Telegram
# ──────────────────────────────────────────────────────────────────────────────

def _build_levels_line(symbol: str, results: dict) -> list[str]:
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

    s_str = f"S {sup[0]:.2f}  " if sup else ""
    r_str = f"  R {res[0]:.2f}" if res else ""
    line1 = f"📌 {s_str}► {price:.2f}{r_str}"

    pdh = kl.get("prev_day_high")
    pdl = kl.get("prev_day_low")
    orh = kl.get("opening_range_high_30")
    orl = kl.get("opening_range_low_30")

    parts = []
    if pdh and pdl:
        parts.append(f"PDH {pdh:.2f}  PDL {pdl:.2f}")
    if orh and orl:
        parts.append(f"OR30 {orl:.2f}–{orh:.2f}")
    line2 = ("📅 " + "  |  ".join(parts)) if parts else ""

    return [line1] + ([line2] if line2 else [])


# ──────────────────────────────────────────────────────────────────────────────
# Updated: build_combined_alert — now includes directional targets
# ──────────────────────────────────────────────────────────────────────────────

def build_combined_alert(
    symbol:  str,
    results: dict[tuple[str, str], object],
) -> str | None:
    """
    Inspects bias_change_info from every completed interval result for `symbol`.
    Returns a formatted alert string if at least one interval flipped, else None.

    Includes directional price targets (resistance/support/session anchors)
    filtered by the MTF bias direction so the alert is immediately actionable.

    Multi-Timeframe Agreement flag (mtf_agreement):
      "🔥 STRONG BULL"    — 15m AND 30m both flipped Bullish
      "🔥 STRONG BEAR"    — 15m AND 30m both flipped Bearish
      "✅ CONFIRMED BULL"  — 15m flipped Bullish AND 1h trend Bullish
      "✅ CONFIRMED BEAR"  — 15m flipped Bearish AND 1h trend Bearish
      "⚠️  WEAK / MIXED"   — only one TF changed, or TFs conflict
      (no flag line)       — no flip detected on any TF
    """
    # ── Gather per-interval info ──────────────────────────────────────────────
    interval_infos: dict[str, dict] = {}
    for interval in _MTF_INTERVALS:
        df = results.get((symbol, interval))
        if df is None:
            continue
        info = getattr(df, "attrs", {}).get("bias_change_info")
        if info:
            info["current_bias"] = (
                str(df["overall_bias"].iloc[-1])
                if "overall_bias" in df.columns else "N/A"
            )
            interval_infos[interval] = info

    if not interval_infos:
        return None

    any_changed = any(v["changed"] for v in interval_infos.values())
    if not any_changed:
        return None

    # ── MTF Agreement flag ────────────────────────────────────────────────────
    info_15 = interval_infos.get("15m")
    info_30 = interval_infos.get("30m")
    info_1h = interval_infos.get("1h")

    flipped_15   = info_15["changed"]   if info_15 else False
    flip_15_dir  = info_15["flag"]      if info_15 else 0
    flipped_30   = info_30["changed"]   if info_30 else False
    flip_30_dir  = info_30["flag"]      if info_30 else 0
    bias_1h_cur  = info_1h["current_bias"] if info_1h else ""

    mtf_flag = ""

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
    
    # ── Build header + TF rows ────────────────────────────────────────────────
    est_now = datetime.now(ZoneInfo("America/New_York"))
    now_str = est_now.strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"📊 {symbol} — Bias Change Alert",
        f"🕐 {now_str}",
        "",
    ]

    for interval in _MTF_INTERVALS:
        info = interval_infos.get(interval)
        if info is None:
            continue
        cur = info["current_bias"]
        em  = _bias_emoji(cur)
        if info["changed"]:
            lines.append(
                f"{em} {interval:>3s}  FLIPPED: {info['prev_bias']} → {info['last_bias']}"
                f"  (close: {info['last_close']:.2f})"
            )
        else:
            lines.append(f"{em} {interval:>3s}  No change  [current: {cur}]")

    if mtf_flag:
        lines.append("")
        lines.append(f"► MTF Agreement: {mtf_flag}")

    # ── Compact key levels (2 lines max) ─────────────────────────────────────
    lines.extend(_build_levels_line(symbol, results))

    return "\n".join(lines)


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

    # Session anchors — one line per group
    def _f(key):  # format a value or return 'n/a'
        v = levels.get(key)
        return f"{v:.2f}" if v is not None else "n/a"

    print("  ── Session Anchors ──")
    print(f"    Prev Day   L {_f('prev_day_low')}  H {_f('prev_day_high')}  C {_f('prev_day_close')}")
    print(f"    Pre-Market L {_f('premarket_low')}  H {_f('premarket_high')}")
    print(f"    OR 30m     L {_f('opening_range_low_30')}  H {_f('opening_range_high_30')}")

    supports = sorted(levels.get("support", []))
    resistances = sorted(levels.get("resistance", []), reverse=True)
    sup_str = "  ".join(f"S {s:.2f}" for s in supports) if supports else "None"
    res_str = "  ".join(f"R {r:.2f}" for r in resistances) if resistances else "None"
    price_str = (f"C {levels['current_price']:.2f}")
    print(f"Pivots:    {sup_str} : {res_str}")

    # Swings
    sh = sorted(levels.get("swing_highs", []), reverse=True)[:5]
    sl = sorted(levels.get("swing_lows", []))[:5]
    sh_str = "  ".join(f"H {h:.2f}" for h in sh) if sh else "None"
    sl_str = "  ".join(f"L {l:.2f}" for l in sl) if sl else "None"
    print(f"Swings:    {sl_str} : {sh_str}")
    print(sep)


# ──────────────────────────────────────────────────────────────────────────────
# Runner  (unchanged from original except gc / failed list)
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'═'*70}")
    print(f"  Multi-Symbol Candlestick Analyser")
    print(f"  Started : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Jobs    : {len(RUN_MATRIX)}")
    print(f"{'═'*70}")

    sm     = ServiceManager()
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

        if idx < len(RUN_MATRIX):
            time.sleep(0.5)

    gc.collect()
    
    symbols_in_run = dict.fromkeys(sym for sym, *_ in RUN_MATRIX)

    for symbol in symbols_in_run:
        # Attach key levels using 15m if available (most granular)
        df_30m = results.get((symbol, "30m"))
        if df_30m is not None:
            df_key = attach_key_levels(
                df_30m,
                sm       = sm,
                symbol   = symbol,
                n_levels = 2,
            )
            levels = df_key.attrs.get("key_levels", {})
            if levels:
                print_key_levels(levels, symbol=symbol, interval="30m")
            else:
                print(f"  {symbol} — no key levels available")
        else:
            print(f"  {symbol} — 30m data not available for key levels")

    # ── Combined multi-timeframe alert (DB-gated) ─────────────────────────────
    symbols_in_run = dict.fromkeys(sym for sym, *_ in RUN_MATRIX)

    print(f"\n{'─'*70}")
    print("  BIAS CHANGE SUMMARY")
    print(f"{'─'*70}")

    for symbol in symbols_in_run:
        alert_msg = build_combined_alert(symbol, results)

        if not alert_msg:
            print(f"\n  {symbol}  — no bias flip detected")
            for interval in _MTF_INTERVALS:
                df = results.get((symbol, interval))
                if df is not None and "overall_bias" in df.columns:
                    cur = df["overall_bias"].iloc[-1]
                    print(f"    {interval:>3s}  current bias: {cur}")
            continue

        # ── DB gate ──────────────────────────────────────────────────────────
        new_flip_intervals: list[str] = []

        for interval in _MTF_INTERVALS:
            df = results.get((symbol, interval))
            if df is None:
                continue
            info = getattr(df, "attrs", {}).get("bias_change_info")
            if not info or not info.get("changed"):
                continue

            row = _build_db_row(info)

            if altMgr.isAlertExistsinDB(row, symbol, interval):
                print(f"  [DB] {symbol} {interval} flip already recorded — skipping")
            else:
                new_flip_intervals.append(interval)
                altMgr.AddAlertRecordtoDB(row, symbol, interval)
                print(f"  [DB] {symbol} {interval} new flip — recorded in DB")

        if new_flip_intervals:
            print(f"\n{alert_msg}")
            altMgr.send_chart_alert(alert_msg)
            print(f"\n  ✔  Telegram alert sent for {symbol} "
                  f"(new flips: {', '.join(new_flip_intervals)})")
        else:
            print(f"\n  {symbol}  — all flips already alerted (no Telegram message sent)")
            print(alert_msg)

    # ── Final summary ─────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  COMPLETED : {datetime.now():%H:%M:%S}")
    print(f"  Succeeded : {len(results)} / {len(RUN_MATRIX)}")
    if failed:
        print(f"  Failed    : {[f'{s} {i}' for s, i in failed]}")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
