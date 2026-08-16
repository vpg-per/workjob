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

