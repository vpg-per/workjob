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
from gitalertmanager import AlertManager
from dataManager import ServiceManager
from stock_candle_processor import process

# ──────────────────────────────────────────────────────────────────────────────
# Run matrix
# ──────────────────────────────────────────────────────────────────────────────
RUN_MATRIX: list[tuple[str, str, bool, bool]] = [
    # symbol   interval   calc_macd  calc_rsi
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

def _build_targets_block(symbol: str, mtf_direction: int, results: dict) -> list[str]:
    """
    Builds the price target lines for the Telegram alert.

    mtf_direction :  1 = bullish flip detected
                    -1 = bearish flip detected
                     0 = no directional flip (skip targets)

    Pulls key_levels from the 15m DataFrame (most granular intraday frame).
    Falls back to 30m if 15m levels are unavailable.

    Bullish layout:
        🎯 Targets (resistance above):  R1 … R2 … R3
        🛡 Stop guide (support below):  S1 … S2
        📅 Session levels relevant to long bias

    Bearish layout:
        🎯 Targets (support below):     S1 … S2 … S3
        🛡 Stop guide (resistance above): R1 … R2
        📅 Session levels relevant to short bias
    """
    if mtf_direction == 0:
        return []

    # Prefer 15m levels; fall back to 30m
    kl = {}
    for iv in ("15m", "30m"):
        df_iv = results.get((symbol, iv))
        if df_iv is not None:
            kl = getattr(df_iv, "attrs", {}).get("key_levels", {})
            if kl:
                kl["_source_interval"] = iv
                break

    if not kl:
        return []  # key_levels not computed (attach_key_levels not wired yet)

    price   = kl.get("current_price", 0.0)
    sup     = kl.get("support",    [])   # already sorted descending (nearest first)
    res     = kl.get("resistance", [])   # already sorted ascending  (nearest first)
    src_iv  = kl.get("_source_interval", "15m")

    lines = ["", f"📌 Key Levels  [{src_iv} frame | price {price:.2f}]"]
    sep   = "   " + "─" * 34

    if mtf_direction == 1:
        # ── BULLISH: targets are resistance above, stops are support below ──

        lines.append(sep)
        lines.append("🎯 Upside Targets (resistance):")
        if res:
            for i, r in enumerate(res[:4], 1):
                dist_pct = (r - price) / price * 100
                lines.append(f"   R{i}  {r:.2f}   (+{dist_pct:.1f}%)")
        else:
            lines.append("   — no resistance levels found")

        lines.append(sep)
        lines.append("🛡 Stop Guide (support below):")
        if sup:
            for i, s in enumerate(sup[:3], 1):
                dist_pct = (price - s) / price * 100
                lines.append(f"   S{i}  {s:.2f}   (-{dist_pct:.1f}%)")
        else:
            lines.append("   — no support levels found")

        # Session anchors relevant to a LONG trade
        lines.append(sep)
        lines.append("📅 Session Anchors (long context):")
        _append_bullish_session_lines(lines, kl, price)

    else:
        # ── BEARISH: targets are support below, stops are resistance above ──

        lines.append(sep)
        lines.append("🎯 Downside Targets (support):")
        if sup:
            for i, s in enumerate(sup[:4], 1):
                dist_pct = (price - s) / price * 100
                lines.append(f"   S{i}  {s:.2f}   (-{dist_pct:.1f}%)")
        else:
            lines.append("   — no support levels found")

        lines.append(sep)
        lines.append("🛡 Stop Guide (resistance above):")
        if res:
            for i, r in enumerate(res[:3], 1):
                dist_pct = (r - price) / price * 100
                lines.append(f"   R{i}  {r:.2f}   (+{dist_pct:.1f}%)")
        else:
            lines.append("   — no resistance levels found")

        # Session anchors relevant to a SHORT trade
        lines.append(sep)
        lines.append("📅 Session Anchors (short context):")
        _append_bearish_session_lines(lines, kl, price)

    lines.append(sep)
    return lines


def _fmt_anchor(label: str, val: float | None, price: float, direction: str = "") -> str | None:
    """
    Format a single session anchor line.
    direction: "above" | "below" | "" (no filter)
    Returns None if val is missing or filtered out.
    """
    if val is None or val != val:   # None or NaN
        return None
    if direction == "above" and val <= price:
        return None
    if direction == "below" and val >= price:
        return None
    dist_pct = (val - price) / price * 100
    sign = "+" if dist_pct >= 0 else ""
    return f"   {label:<22} {val:.2f}  ({sign}{dist_pct:.1f}%)"


def _append_bullish_session_lines(lines: list, kl: dict, price: float) -> None:
    """
    For a BULLISH signal, highlight session levels ABOVE price as breakout
    targets and levels BELOW price as potential support / stops.
    """
    pdh   = kl.get("prev_day_high")
    pdc   = kl.get("prev_day_close")
    pdl   = kl.get("prev_day_low")
    pmh   = kl.get("premarket_high")
    pml   = kl.get("premarket_low")
    orh15 = kl.get("opening_range_high_15")
    orl15 = kl.get("opening_range_low_15")
    orh30 = kl.get("opening_range_high_30")
    orl30 = kl.get("opening_range_low_30")

    # Above current price — potential breakout targets (most actionable for longs)
    breakout_anchors = [
        ("PDH  (breakout lvl)",  pdh,   "above"),
        ("OR High 15m",          orh15, "above"),
        ("OR High 30m",          orh30, "above"),
        ("PM High",              pmh,   "above"),
    ]
    # Below current price — potential support / stop zones
    support_anchors = [
        ("PDC  (prior close)",   pdc,   "below"),
        ("OR Low 15m",           orl15, "below"),
        ("OR Low 30m",           orl30, "below"),
        ("PM Low",               pml,   "below"),
        ("PDL  (stop ref)",      pdl,   "below"),
    ]

    added = False
    for label, val, direction in breakout_anchors:
        line = _fmt_anchor(label, val, price, direction)
        if line:
            lines.append(line)
            added = True

    if not added:
        lines.append("   (no session levels above price)")

    lines.append("   — — — — — — — — — — — — — — — —")
    lines.append("   Support / Stop zones below:")
    any_stop = False
    for label, val, direction in support_anchors:
        line = _fmt_anchor(label, val, price, direction)
        if line:
            lines.append(line)
            any_stop = True
    if not any_stop:
        lines.append("   (no session anchors below price)")


def _append_bearish_session_lines(lines: list, kl: dict, price: float) -> None:
    """
    For a BEARISH signal, highlight session levels BELOW price as breakdown
    targets and levels ABOVE price as potential resistance / stops.
    """
    pdh   = kl.get("prev_day_high")
    pdc   = kl.get("prev_day_close")
    pdl   = kl.get("prev_day_low")
    pmh   = kl.get("premarket_high")
    pml   = kl.get("premarket_low")
    orh15 = kl.get("opening_range_high_15")
    orl15 = kl.get("opening_range_low_15")
    orh30 = kl.get("opening_range_high_30")
    orl30 = kl.get("opening_range_low_30")

    # Below current price — potential breakdown targets (most actionable for shorts)
    breakdown_anchors = [
        ("PDL  (breakdown lvl)", pdl,   "below"),
        ("OR Low 15m",           orl15, "below"),
        ("OR Low 30m",           orl30, "below"),
        ("PM Low",               pml,   "below"),
        ("PDC  (prior close)",   pdc,   "below"),
    ]
    # Above current price — resistance / stop zones
    resistance_anchors = [
        ("OR High 15m",          orh15, "above"),
        ("OR High 30m",          orh30, "above"),
        ("PM High",              pmh,   "above"),
        ("PDH  (stop ref)",      pdh,   "above"),
    ]

    added = False
    for label, val, direction in breakdown_anchors:
        line = _fmt_anchor(label, val, price, direction)
        if line:
            lines.append(line)
            added = True

    if not added:
        lines.append("   (no session levels below price)")

    lines.append("   — — — — — — — — — — — — — — — —")
    lines.append("   Resistance / Stop zones above:")
    any_stop = False
    for label, val, direction in resistance_anchors:
        line = _fmt_anchor(label, val, price, direction)
        if line:
            lines.append(line)
            any_stop = True
    if not any_stop:
        lines.append("   (no session anchors above price)")


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

    mtf_flag      = ""
    mtf_direction = 0   # 1=bullish, -1=bearish, 0=mixed/none — drives target block

    if flipped_15 and flipped_30 and flip_15_dir == flip_30_dir:
        mtf_direction = flip_15_dir
        if flip_15_dir == 1:
            mtf_flag = "🔥 STRONG BULL — 15m & 30m both flipped Bullish"
        else:
            mtf_flag = "🔥 STRONG BEAR — 15m & 30m both flipped Bearish"

    elif flipped_15:
        if flip_15_dir == 1 and "Bullish" in bias_1h_cur:
            mtf_flag      = "✅ CONFIRMED BULL — 15m flipped Bullish, 1h trend Bullish"
            mtf_direction = 1
        elif flip_15_dir == -1 and "Bearish" in bias_1h_cur:
            mtf_flag      = "✅ CONFIRMED BEAR — 15m flipped Bearish, 1h trend Bearish"
            mtf_direction = -1
        else:
            mtf_flag      = "⚠️  WEAK / MIXED — 15m flipped but other TFs not aligned"
            mtf_direction = flip_15_dir   # still show targets, just weaker signal

    elif flipped_30:
        if flip_30_dir == 1 and "Bullish" in bias_1h_cur:
            mtf_flag      = "✅ CONFIRMED BULL — 30m flipped Bullish, 1h trend Bullish"
            mtf_direction = 1
        elif flip_30_dir == -1 and "Bearish" in bias_1h_cur:
            mtf_flag      = "✅ CONFIRMED BEAR — 30m flipped Bearish, 1h trend Bearish"
            mtf_direction = -1
        else:
            mtf_flag      = "⚠️  WEAK / MIXED — 30m flipped but other TFs not aligned"
            mtf_direction = flip_30_dir
    
    # ── Build header + TF rows ────────────────────────────────────────────────
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

    # ── Directional targets block ─────────────────────────────────────────────
    # Only appended when there is a clear bullish or bearish MTF direction.
    # For WEAK/MIXED signals mtf_direction is still set from the flipping TF
    # so targets are shown but the WEAK label above already caveats the signal.
    target_lines = _build_targets_block(symbol, mtf_direction, results)
    lines.extend(target_lines)
    
    df_15 = results.get((symbol, "15m"))
    if df_15 is not None:
        kl = getattr(df_15, "attrs", {}).get("key_levels", {})
        if kl:
            price = kl.get("current_price", 0)
            sup   = kl.get("support",   [])
            res   = kl.get("resistance", [])
            lines.append("")
            lines.append("📌 Key Levels (15m frame):")
            if res: lines.append(f"   R  {res[0]:.2f}" + (f"  {res[1]:.2f}" if len(res) > 1 else ""))
            lines.append(f"   ►  {price:.2f}  current")
            if sup: lines.append(f"   S  {sup[0]:.2f}" + (f"  {sup[1]:.2f}" if len(sup) > 1 else ""))

            # Session anchors
            pdh = kl.get("prev_day_high")
            pdl = kl.get("prev_day_low")
            orh = kl.get("opening_range_high_15")
            orl = kl.get("opening_range_low_15")
            pmh = kl.get("premarket_high")
            pml = kl.get("premarket_low")
            if pdh: lines.append(f"   PDH {pdh:.2f}  PDL {pdl:.2f}")
            if orh: lines.append(f"   OR  {orl:.2f} – {orh:.2f}")
            if pmh: lines.append(f"   PM  {pml:.2f} – {pmh:.2f}")

    return "\n".join(lines)


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
