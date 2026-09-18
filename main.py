import os
import base64
import gc
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
from flask import Flask, render_template, request
from dataManager import ServiceManager
from alertManager import AlertManager
from supresrange import SupportResistanceByInputInterval
from csPattern import csPattern
from sectorperformance import refresh_sector_chart
from dayTrendAlert import day_trend_alert_bp
from stockAnalysis import stock_analysis_bp
import time

app = Flask(__name__)
app.register_blueprint(day_trend_alert_bp)
app.register_blueprint(stock_analysis_bp)
# Shared singletons — never store DataFrames on these
g_message = []
objMgr = ServiceManager()
altMgr = AlertManager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def process_stocksignal(symbol="SPY"):
    """Fetch + analyse one symbol; return a minimal DataFrame slice."""
    global g_message
    df = objMgr.analyze_stockdata(symbol)
    altMgr.prepare_crsovr_message(df)
    g_message = altMgr.get_message()
    return df


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
# This route is referred as auto buysell indicator
@app.route("/csPattern")
def CandleStickPattern():
    # Parse symbol list
    raw = request.args.get('symbol', type=str)
    if raw:
        stocksymbols = [raw.upper()]
    else:
        env_sym = os.getenv("CUSTOM_ALERT_SYMBOL", "")
        stocksymbols = [s.strip() for s in env_sym.split(",") if s.strip()] or ['SPY']

    allsymbols_data = []
    for symbol in stocksymbols:
        # Load any existing open order from DB before constructing csPattern
        dbrecval = altMgr.GetStockOrderRecordfromDB(symbol, 'Open')

        cs = csPattern()
        if dbrecval is not None:
            cs.openorderon5m = dbrecval

        # analyze_stockcandlesLTF already frees its DataFrames internally
        cs.analyze_stockcandlesLTF(symbol)

        open_order  = cs.openorderon5m
        close_order = cs.closeorderon5m

        # ---- open signal ----
        if open_order is not None and close_order is None:
            existing = altMgr.GetStockOrderRecordusingUnixTime(
                symbol,
                str(open_order['unixtime']),
                str(open_order['hour']),
                str(open_order['minute'])
            )
            if existing is None:
                altMgr.AddOpenStockOrderRecordtoDB(open_order)
                # Only alert on genuinely new candles (first detection)
                if open_order['updatedTriggerTime'] == open_order['unixtime']:
                    allsymbols_data.append(
                        f"Symbol: {open_order['symbol']} "
                        f"Time: {open_order['hour']}:{open_order['minute']} "
                        f"Pattern: {open_order['cspattern']}, "
                        f"open price: {open_order['stockprice']}, "
                        f"stoploss: {open_order['stoploss']}, "
                        f"profittarget: {open_order['profittarget']}"
                    )

        # ---- close signal ----
        if close_order is not None:
            if open_order is not None:
                altMgr.AddOpenStockOrderRecordtoDB(open_order, "OpenClose")
            allsymbols_data.append(
                f"Symbol: {close_order['symbol']} "
                f"Time: {close_order['hour']}:{close_order['minute']} "
                f"Pattern: {close_order['cspattern']}, "
                f"close price: {close_order['stockprice']}"
            )
            cs.openorderon5m  = None
            cs.closeorderon5m = None

        # Free everything for this symbol immediately
        del cs, open_order, close_order, dbrecval

    resultdata = ",".join(allsymbols_data)
    if allsymbols_data:
        altMgr.send_chart_alert(resultdata)

    del allsymbols_data
    gc.collect()
    return resultdata if resultdata else "done!"


# This route is indicated as pre market pattern usually executes before 9AM
@app.route("/marketPattern")
def marketPattern():
    stocksymbols    = ['NQ%3DF', 'RTY%3DF', 'GC%3DF']
    allsymbols_data = []

    for symbol in stocksymbols:
        cs  = csPattern()
        ret = cs.analyze_stockcandlesHTF(symbol)   # frees DataFrames internally
        if ret is not None:
            allsymbols_data.append(ret)
        del cs
        gc.collect()

    sentmsg = "done!"
    if allsymbols_data:
        resultdata = ", ".join(str(item) for item in allsymbols_data)
        altMgr.send_chart_alert(resultdata)
        sentmsg = resultdata

    del allsymbols_data
    gc.collect()
    return sentmsg


@app.route("/scalpPattern")
def ScalpPattern():
    symbol   = request.args.get('symbol', default='SPY', type=str).upper()
    interval = request.args.get('interval', default='15m', type=str)

    scalper = SupportResistanceByInputInterval(symbol, interval, days_back=2)
    summary = scalper.get_scalping_summary()

    if not summary:
        del scalper
        gc.collect()
        return "<h1>Error: Could not generate analysis.</h1>", 500

    image_buffer       = scalper.plot_15min_chart(bars_to_show=96)
    chart_image_base64 = base64.b64encode(image_buffer.getvalue()).decode('utf-8')
    image_buffer.close()

    del scalper, image_buffer
    gc.collect()

    return render_template('./scalp.html', summary=summary, chart_image=chart_image_base64)

# This route is used for showing the sector behavior
@app.route("/sectorPerformance")
def SectorPerformanceGet():
    datarefresh = request.args.get('datarefresh', default='TRUE', type=str).upper()
    if datarefresh=="TRUE":
        refresh_sector_chart()
    image_buffer = altMgr.GetSectorChartFromDB()
    if image_buffer is not None:
        altMgr.send_photo_alert(image_buffer)
        chart_image_base64 = base64.b64encode(image_buffer.getvalue()).decode('utf-8')
        image_buffer.close()

    del image_buffer
    gc.collect()

    return render_template('./sectorperformance.html', page_title="Sector Performance", chart_image=chart_image_base64)

@app.route("/bkOutInvoke")
def BkOutInvoke():
    return render_template('./second.html')


@app.route('/rangePattern')
def RangePattern():
    dt  = datetime.now()
    fmt = '%Y-%m-%d %H:%M:%S %z'
    pmst_dt  = datetime.strptime(f"{dt.date()} 4:00:00 -0400",  fmt)
    regst_dt = datetime.strptime(f"{dt.date()} 9:30:00 -0400",  fmt)
    reget_dt = datetime.strptime(f"{dt.date()} 10:00:00 -0400", fmt)

    if int(datetime.now().timestamp()) < reget_dt.timestamp():
        reget_dt = datetime.now().astimezone(reget_dt.tzinfo)

    allsymbols_data = []
    pm_data = rg_data = ""

    for ss in ['SPY']:
        df = objMgr.download_stock_data(
            ss,
            startPeriod=pmst_dt.timestamp(),
            endPeriod=reget_dt.timestamp(),
            interval="15m"
        )
        if df is None:
            continue

        # Compute pre-market summary — only scalar aggregates kept
        mask_pm = df['unixtime'] <= int(regst_dt.timestamp()) - 1
        if mask_pm.any():
            pm_data = (
                f"O:{df.loc[mask_pm,  'open'].iloc[0]:.2f}, "
                f"C:{df.loc[mask_pm,  'close'].iloc[-1]:.2f}, "
                f"L:{df.loc[mask_pm,  'low'].min():.2f}, "
                f"H:{df.loc[mask_pm,  'high'].max():.2f}"
            )

        # Compute regular-session summary
        mask_rg = df['unixtime'] >= int(regst_dt.timestamp()) - 1
        if mask_rg.any():
            rg_data = (
                f"O:{df.loc[mask_rg, 'open'].iloc[0]:.2f}, "
                f"C:{df.loc[mask_rg, 'close'].iloc[-1]:.2f}, "
                f"L:{df.loc[mask_rg, 'low'].min():.2f}, "
                f"H:{df.loc[mask_rg, 'high'].max():.2f}"
            )

        del df          # free the full frame right away
        gc.collect()

        allsymbols_data.append(
            f'{{ "symbol": "{ss}", "pmdata": "{{{pm_data}}}", "rgdata": "{{{rg_data}}}" }}'
        )

    resultdata = ",".join(allsymbols_data)
    del allsymbols_data

    if pm_data or rg_data:
        altMgr.send_chart_alert(resultdata)

    altMgr.DelOldRecordsFromDB()
    gc.collect()
    return resultdata


@app.route("/inputsym")
def inputsym():
    global g_message
    g_message = []
    return render_template('./inputsym.html')


@app.route("/dayTrend")
def dayTrend():
    global g_message
    g_message = []
    symbol = request.args.get('symbol', default='', type=str).upper()
    return render_template('./dayTrend.html', symbol=symbol)


@app.route('/returnPattern')
def ReturnPattern():
    global g_message
    g_message = []
    altMgr.set_message(g_message)

    symbol       = request.args.get('symbol', default='', type=str).upper()
    stocksymbols = [symbol] if symbol else ['GLD', 'QQQ', 'IWM']

    # Process each symbol, stream-concat into a single result; never hold
    # more than one symbol's DataFrame in memory at once
    frames = []
    for ss in stocksymbols:
        df_stock = process_stocksignal(ss)
        frames.append(df_stock)
        del df_stock
        gc.collect()

    df_allsymbols = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    del frames
    gc.collect()

    if g_message:
        sentmsg = altMgr.send_chart_alert(g_message)
        print(sentmsg)

    result = df_allsymbols.to_json(orient='records', index=False)
    del df_allsymbols
    gc.collect()
    return result


@app.route("/")
def default():
    return render_template('./default.html')


if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=80)