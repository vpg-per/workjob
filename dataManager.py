import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import gc

# Only the columns needed after processing — avoids carrying dead weight
_FINAL_COLS_MACD = ['unixtime', 'nmonth', 'nday', 'hour', 'minute',
                    'macd', 'msignal', 'histogram', 'open', 'close', 'high', 'low',
                    'interval', 'symbol']
_FINAL_COLS_RSI  = ['unixtime', 'nmonth', 'nday', 'hour', 'minute',
                    'rsi', 'rsignal', 'crossover', 'open', 'close', 'high', 'low',
                    'interval', 'symbol']


class ServiceManager:
    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_stockdata(self, symbol):
        todayn     = datetime.now().strftime('%d')
        yesterdayn = (datetime.now() - timedelta(days=1)).strftime('%d')

        # 5m: keep only today's last 20 rows right after fetch
        data5m    = self.GetStockdata_Byinterval(symbol, "5m", indicatorList="macd")
        df_merged = data5m[data5m['nday'] == todayn].tail(20).copy()
        del data5m
        gc.collect()

        # Fetch 15m / 30m / 1h once, slice immediately after each use
        data15m = self.GetStockdata_Byinterval(symbol, "15m", indicatorList="macd")
        data30m = self.GetStockdata_Byinterval(symbol, "30m", indicatorList="macd")
        data1h  = self.GetStockdata_Byinterval(symbol, "1h",  indicatorList="macd")

        #data15m  = self.calculate_Buy_Sell_Values(data15m, data30m, 65)
        slice15  = data15m[data15m['nday'] == todayn].tail(12).copy()
        slice15 = self.calculate_TrendAlert(slice15)
        del data15m
        gc.collect()

        #data30m  = self.calculate_Buy_Sell_Values(data30m, data1h, 125)
        slice30  = data30m[data30m['nday'] == todayn].tail(8).copy()
        slice30 = self.calculate_TrendAlert(slice30)
        del data30m
        gc.collect()

        slice1h  = data1h[data1h['nday'] == todayn].tail(4).copy()
        slice1h = self.calculate_TrendAlert(slice1h)
        del data1h
        gc.collect()

        # 4h: only last 3 rows, then filter to today/yesterday
        data4h  = self.GetStockdata_Byinterval(symbol, "4h", indicatorList="macd").tail(3)
        slice4h = data4h[(data4h['nday'] == todayn) | (data4h['nday'] == yesterdayn)].copy()
        slice4h = self.calculate_TrendAlert(slice4h)
        if len(slice4h) == 0:
            slice4h = data4h.copy()
        del data4h
        gc.collect()

        df_merged = pd.concat(
            [df_merged, slice4h, slice1h, slice30, slice15 ],
            ignore_index=True
        )
        del slice15, slice30, slice1h, slice4h
        gc.collect()

        return df_merged

    def GetStockdata_Byinterval(self, symbol, interval="1d", indicatorList="macd"):
        stPeriod  = int((datetime.now() - timedelta(days=4)).timestamp())
        endPeriod = datetime.now()
        rem = endPeriod.minute % 5
        endPeriod = endPeriod.replace(minute=endPeriod.minute - rem, second=0, microsecond=0)

        df = self.download_stock_data(symbol, stPeriod, endPeriod.timestamp(), interval)
        if df is None:
            print("Failed to fetch data. Please check your internet connection.")
            return None

        # ---- interval-specific trimming / resampling ----
        if interval == "5m":
            valid_min = {"00","05","10","15","20","25","30","35","40","45","50","55"}
            mask = (df['unixtime'] <= endPeriod.timestamp()) & df['minute'].isin(valid_min)
            df   = df.loc[mask].copy()

        elif interval == "15m":
            rem15 = endPeriod.minute % 15
            ep    = endPeriod.replace(minute=endPeriod.minute - rem15, second=0, microsecond=0).timestamp() - 1
            df    = df.loc[(df['unixtime'] <= ep) & df['minute'].isin({"00","15","30","45"})].copy()

        elif interval == "30m":
            rem30 = endPeriod.minute % 30
            ep    = endPeriod.replace(minute=endPeriod.minute - rem30, second=0, microsecond=0).timestamp() - 1
            df    = df.loc[(df['unixtime'] <= ep) & df['minute'].isin({"00","30"})].copy()

        elif interval == "1h":
            df = (
                df.resample('1h', origin='epoch')
                .agg({'unixtime':'first','open':'first','high':'max','low':'min','close':'last'})
                .dropna()
            )
            ep = endPeriod.replace(minute=0, second=0, microsecond=0).timestamp()
            df = self._attach_dt_cols(df)
            df = df[df['unixtime'] <= ep].copy()

        elif interval == "4h":
            df  = df[df['minute'].isin({"00"})].copy()
            rem4 = endPeriod.hour % 4
            ep   = endPeriod.replace(
                hour=endPeriod.hour - rem4, minute=0, second=0, microsecond=0
            ).timestamp() - 1
            df = df[df['unixtime'] <= ep].copy()
            df = (
                df.resample('4h', origin='epoch',offset='3h', closed='right', label='right')
                .agg({'unixtime':'first','open':'first','high':'max','low':'min','close':'last'})
                .dropna()
            )
            df = self._attach_dt_cols(df)

        # ---- compute indicators in-place (no copy) ----
        if "macd" in indicatorList:
            df = self._calculate_macd_inplace(df)
        if "rsi" in indicatorList:
            df = self._calculate_rsi_inplace(df)

        # ---- drop every column we don't need ----
        want = _FINAL_COLS_MACD if "macd" in indicatorList else _FINAL_COLS_RSI
        keep = [c for c in want if c in df.columns]
        size = len(df)
        if size > 4:
            size = 4
        df_sel = df[keep].tail(5).copy()
        del df
        gc.collect()

        sym_clean        = symbol.replace("%3DF", "")
        df_sel['interval'] = pd.Categorical([interval]  * len(df_sel))
        df_sel['symbol']   = pd.Categorical([sym_clean] * len(df_sel))

        return df_sel

    def download_stock_data(self, symbol, startPeriod, endPeriod, interval="1d"):
        """Fetch OHLCV from Yahoo Finance. Returns DataFrame with tz-aware index."""
        if interval in ("4h", "1h"):
            interval = "30m"

        url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        params = {
            'period1':        int(startPeriod),
            'period2':        int(endPeriod),
            'interval':       interval,
            'includePrePost': 'true',
        }
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data   = resp.json()
            result = data['chart']['result'][0]
            quotes = result['indicators']['quote'][0]

            # Keep timestamps as int64 for pd.to_datetime — int32 overflows
            # silently producing NaT, which cascades into NaN for all derived
            # datetime columns.  Downcast to int32 only AFTER derivation.
            ts_arr = np.asarray(result['timestamp'], dtype='int64')

            df = pd.DataFrame({
                'unixtime': ts_arr,
                'open':  np.round(np.asarray(quotes['open'],  dtype='float32'), 2),
                'high':  np.round(np.asarray(quotes['high'],  dtype='float32'), 2),
                'low':   np.round(np.asarray(quotes['low'],   dtype='float32'), 2),
                'close': np.round(np.asarray(quotes['close'], dtype='float32'), 2),
            })
            df.dropna(inplace=True)
            df.reset_index(drop=True, inplace=True)

            # Derive datetime columns while unixtime is still int64
            ts = (
                pd.to_datetime(df['unixtime'], unit='s')
                .dt.tz_localize('UTC')
                .dt.tz_convert('America/New_York')
            )
            df.index      = ts
            df.index.name = 'timestamp'
            df['rec_dt'] = ts.dt.date.values
            # Now safe to downcast unixtime to int32
            df['unixtime'] = df['unixtime'].astype('int32')
            df = self._attach_dt_cols(df)
            del ts

            return df

        except requests.exceptions.RequestException as e:
            print(f"Error fetching data: {e}")
        except KeyError as e:
            print(f"Error parsing data: {e}")
        return None

    def calculate_TrendAlert(self, dfcur):
        dfcur['crossover'] = '0'
        if dfcur is None or dfcur.empty or len(dfcur) < 2:
            return dfcur
        
        bullish_score = 0
        bearish_score = 0
        last_row, last_but_second_row = dfcur.iloc[-1], dfcur.iloc[-2]
        if (last_row['macd'] > 0 and last_row['msignal'] > 0):
            bullish_score += 1
        elif (last_row['macd'] < 0 and last_row['msignal'] < 0):
            bearish_score += 1

        if (last_row['histogram'] > 0.1):
            bullish_score += 1
        elif (last_row['histogram'] < -0.1):
            bearish_score += 1

        if (last_row['macd'] > last_but_second_row['macd']):
            bullish_score += 1
        elif (last_row['macd'] < last_but_second_row['macd']):
            bearish_score += 1

        dfcur.loc[dfcur.index[-1], 'crossover'] = str(bullish_score-bearish_score)
        return dfcur

    def calculate_RSITrendAlert(self, dfcur):
        dfcur['rsicrossover'] = '0'
        if dfcur is None or dfcur.empty or len(dfcur) < 2:
            return dfcur
        
        bullish_score = 0
        bearish_score = 0
        last_row, last_but_second_row = dfcur.iloc[-1], dfcur.iloc[-2]
        
        score = 0.0
    
        # 1. RSI Level (strongest weight)
        if last_row['rsi'] < 30:
            score += 40      # Strongly oversold
        elif last_row['rsi'] < 40:
            score += 20
        elif last_row['rsi'] > 70:
            score -= 40      # Overbought
        elif last_row['rsi'] > 60:
            score -= 20
            
        # 2. RSI vs RSI_SMA (momentum)
        if last_row['rsi'] > last_row['rsignal']:
            score += 25
        else:
            score -= 25
        
        # 3. RSI crossing RSI_SMA
        if last_but_second_row['rsi'] < last_row['rsignal'] and last_row['rsi'] > last_row['rsignal']:
            score += 15   # Bullish crossover
        elif last_but_second_row['rsi'] > last_row['rsignal'] and last_row['rsi'] < last_row['rsignal']:
            score -= 15   # Bearish crossover
            
        # 4. RSI around 50 midline
        if 50 < last_row['rsi'] < 60:
            score += 10
        elif 40 < last_row['rsi'] < 50:
            score -= 10
        
        # Clamp score
        score = max(min(score, 100), -100)
        
        # Interpretation
        if score >= 50:
            bias = "strong bullish"
        elif score >= 20:
            bias = "bullish"
        elif score <= -50:
            bias = "strong bearish"
        elif score <= -20:
            bias = "bearish"
        else:
            bias = "neutral"

        dfcur.loc[dfcur.index[-1], 'rsicrossover'] = bias
        return dfcur

    def calculate_Buy_Sell_Values(self, dfcur, dfhtf, lookupmins):
        dfcur = dfcur.copy()
        dfcur['buyval']    = np.float32(0)
        dfcur['sellval']   = np.float32(0)
        dfcur['stoploss']  = np.float32(0)
        dfcur['crossover'] = 'Neutral'

        lookupts = int((datetime.now() - timedelta(minutes=lookupmins)).timestamp())
        dfcur    = dfcur[dfcur['unixtime'].astype('int32') >= lookupts].copy()

        if not dfcur.empty:
            dfhtf_trim = dfhtf[dfhtf['unixtime'].astype('int32') >= lookupts]

            # Rolling 9-bar mean + lag histogram — computed only on the trimmed slice
            dfcur['ninemaval']      = dfcur['close'].rolling(window=9).mean().round(2)
            dfcur['histogram_prev'] = dfcur['histogram'].shift(1)

            last_row   = dfcur.iloc[-1]
            hist_cur   = float(last_row['histogram'])
            hist_prev  = float(last_row['histogram_prev'])
            change     = False
            crossval   = 'Neutral'

            if hist_cur >= 0 and hist_prev <= 0 and hist_prev != hist_cur:
                change, crossval = True, 'Bullish'
            elif hist_cur <= 0 and hist_prev >= 0 and hist_prev != hist_cur:
                change, crossval = True, 'Bearish'

            if change and not dfhtf_trim.empty:
                last_htf = dfhtf_trim.iloc[-1]
                idx = dfcur.index[-1]
                dfcur.loc[idx, 'crossover'] = crossval
                dfcur.loc[idx, 'buyval']    = float(last_row['ninemaval'])
                dfcur.loc[idx, 'sellval']   = float(last_htf['close'])
                dfcur.loc[idx, 'stoploss']  = float(last_htf['open'])

            dfcur.drop(columns=['histogram_prev', 'ninemaval'], inplace=True)
            del dfhtf_trim

        return dfcur

    def identify_candlestick_patterns(self, data):
        """Identifies common candlestick patterns in the data."""
        if len(data) < 3:
            return data

        data['pattern']   = 'NA'
        data['pattern2c'] = 'NA'
        data['pattern3c'] = 'NA'

        opens  = data['open'].to_numpy(dtype='float32')
        highs  = data['high'].to_numpy(dtype='float32')
        lows   = data['low'].to_numpy(dtype='float32')
        closes = data['close'].to_numpy(dtype='float32')
        idx    = data.index

        for i in range(3, len(data)):
            o, h, l, c  = opens[i],   highs[i],   lows[i],   closes[i]
            o1, h1, l1, c1 = opens[i-1], highs[i-1], lows[i-1], closes[i-1]
            body        = abs(c - o)
            price_range = h - l

            if price_range > 0:
                ratio = body / price_range
                if ratio < 0.1:
                    data.loc[idx[i], 'pattern'] = 'Dj'
                elif ratio > 0.95:
                    data.loc[idx[i], 'pattern'] = 'UM' if c > o else 'EM'

            if c > o and l > l1 and c > o1 and price_range > 0 and body / price_range > 0.95:
                data.loc[idx[i], 'pattern2c'] = 'UE'
            elif c < o and h < h1 and c < o1 and price_range > 0 and body / price_range > 0.95:
                data.loc[idx[i], 'pattern2c'] = 'EE'

            o2, c2 = opens[i-2], closes[i-2]
            o3, c3 = opens[i-3], closes[i-3]
            if (c3 < o3 and c2 > o2 and c1 > o1 and c2 > c3 and c1 > c2
                    and c < o and o > c1 and c < o3):
                data.loc[idx[i], 'pattern3c'] = 'Ul3LS'
            if (c3 > o3 and c2 < o2 and c1 < o1 and c2 < c3 and c1 < c2
                    and c > o and o < c1 and c > o3):
                data.loc[idx[i], 'pattern3c'] = 'Ea3LS'

        return data

    def calculate_bollinger_bands(self, df, period=20, std_dev=2):
        mid    = df['close'].rolling(window=period).mean()
        stddev = df['close'].rolling(window=period).std()
        df['midbnd'] = mid.round(2).astype('float32')
        df['ubnd']   = (mid + std_dev * stddev).round(2).astype('float32')
        df['lbnd']   = (mid - std_dev * stddev).round(2).astype('float32')
        del mid, stddev
        return df

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_dt_cols(df):
        """Re-attach nmonth/nday/hour/minute from unixtime after a resample.
        Must use int64 for pd.to_datetime — int32 overflows and produces NaT.
        Downcasts unixtime to int32 after derivation to save memory.
        """
        dt_ny = (
            pd.to_datetime(df['unixtime'].astype('int64'), unit='s')
            .dt.tz_localize('UTC')
            .dt.tz_convert('America/New_York')
        )
        df['rec_dt'] = dt_ny.dt.date
        df['nmonth'] = dt_ny.dt.strftime('%m').astype('category')
        df['nday']   = dt_ny.dt.strftime('%d').astype('category')
        df['hour']   = dt_ny.dt.strftime('%H').astype('category')
        df['minute'] = dt_ny.dt.strftime('%M').astype('category')
        df['unixtime'] = df['unixtime'].astype('int32')
        del dt_ny
        return df

    @staticmethod
    def _calculate_macd_inplace(df, fast=12, slow=26, signal=9):
        """MACD in-place; uses float64 for ewm accuracy, stores float32."""
        close     = df['close'].astype('float64')
        ema_fast  = close.ewm(span=fast,   adjust=False).mean()
        ema_slow  = close.ewm(span=slow,   adjust=False).mean()
        macd_line = ema_fast - ema_slow
        sig_line  = macd_line.ewm(span=signal, adjust=False).mean()

        df['macd']      = macd_line.round(2).astype('float32')
        df['msignal']   = sig_line.round(2).astype('float32')
        df['histogram'] = (macd_line - sig_line).round(2).astype('float32')
        del close, ema_fast, ema_slow, macd_line, sig_line
        return df

    # Backward-compat alias
    def calculate_macd(self, df, fast=12, slow=26, signal=9):
        return self._calculate_macd_inplace(df, fast, slow, signal)

    @staticmethod
    def _calculate_rsi_inplace(df, period=14):
        """RSI + signal + crossover in-place; float32 output."""
        diff = df['close'].astype('float64').diff()
        gain = diff.clip(lower=0)
        loss = (-diff).clip(lower=0)
        del diff

        alpha    = 1.0 / period
        avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
        avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
        del gain, loss

        rs  = avg_gain / avg_loss
        del avg_gain, avg_loss

        rsi     = (100 - (100 / (1 + rs))).round(2).astype('float32')
        rsignal = rsi.astype('float64').ewm(span=period).mean().round(2).astype('float32')
        del rs

        rsi_prev     = rsi.shift(1)
        rsignal_prev = rsignal.shift(1)
        bullish      = (rsi > rsignal) & (rsi_prev < rsignal_prev)
        bearish      = (rsi < rsignal) & (rsi_prev > rsignal_prev)

        df['rsi']       = rsi
        df['rsignal']   = rsignal
        df['crossover'] = np.where(bullish, "Bullish", np.where(bearish, "Bearish", "Neutral"))

        del rsi, rsignal, rsi_prev, rsignal_prev, bullish, bearish
        return df

    # Backward-compat alias
    def calculate_rsi(self, df, period=14):
        return self._calculate_rsi_inplace(df, period)
