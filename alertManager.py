import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from time import gmtime, strftime
from zoneinfo import ZoneInfo
import os
import io
import psycopg2, psycopg2.extras

class AlertManager:
    def __init__(self):
        
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        
        self._message = []
        self.token = os.getenv("TELE_TOKEN")
        self.chat_id = os.getenv("TELE_CHAT_ID")

    def prepare_crsovr_message(self, df):
        # This alert is initiated for 15 or 30 minute time frame only

        arr_interval = [ "15m", "30m"]
        for i in range(len(arr_interval)):
            df_sel_rows = df[df['interval'] == arr_interval[i]]
            for date, row in df_sel_rows.tail(1).iterrows():
                if (( row['crossover'] == "Bullish") | ( row['crossover'] == "Bearish")):
                    if (self.isExistsinDB(row) == False):
                        message = ""
                        if (row['crossover'] == "Bullish" and  float( row['buyval']) > 0):
                            message = (f"{row['symbol']} Buy signal on {row['interval']} consider trade at {row['buyval']}:{row['sellval']}:{row['stoploss']}")
                        elif (row['crossover'] == "Bearish" and float( row['buyval']) > 0):
                            message = (f"{row['symbol']} Sell signal on {row['interval']} consider trade at {row['buyval']}:{row['sellval']}:{row['stoploss']}")
                        if (len(message) > 0):
                            self._message.append( message )
                            self.AddRecordtoDB(row)

        return

    def send_chart_alert(self, s_message):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage?chat_id={self.chat_id}&text={s_message}"
        return requests.get(url).json()
    
    def send_photo_alert(self, image_buffer: io.BytesIO,filename:     str = "sp.png", set_title:str = ""):
        image_buffer.seek(0)
        data  = {"chat_id": self.chat_id, "caption": set_title, "parse_mode": "HTML"}
        files = {"photo": (filename, image_buffer, "image/png")}        
        
        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        resp = requests.post(url, data=data, files=files, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            print(f"[Telegram] ✓ Photo sent successfully " )        
        return 

    def get_message(self):
        return self._message
    
    def set_message(self, new_message):
        self._message = new_message

    def isExistsinDB(self, row):
        retval=False
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        try:
            dtlookupval = f"{row['nmonth']}-{row['nday']} {row['hour']}:{row['minute']}"
            with psycopg2.connect(conn_string) as conn:
                # Open a cursor to perform database operations
                with conn.cursor() as cur:
                    cur.execute("Select \"triggerTime\", \"interval\", \"crossover\" from rsicrossover where \"triggerTime\"=%s and \"interval\"=%s and \"stocksymbol\"=%s and \"NotificationSent\"=True; ", (dtlookupval, row['interval'], row['symbol'],))
                    if (cur.rowcount > 0 ):
                        retval = True
                cur.close()
            conn.close()
            return retval
            
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")

    def AddRecordtoDB(self, row):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        try:
            dttimeval = f"{row['nmonth']}-{row['nday']} {row['hour']}:{row['minute']}"
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO rsicrossover (\"triggerTime\", \"interval\", \"crossover\", \"stocksymbol\", \"Open\", \"Close\", \"Low\", \"High\", \"NotificationSent\", \"rsiVal\", \"signal\", \"midbnd\", \"ubnd\", \"lbnd\") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                        (dttimeval, row['interval'], row['crossover'], row['symbol'], row['open'], row['close'], row['low'], row['high'], "TRUE", row['macd'], row['msignal'], row['buyval'], row['sellval'], row['stoploss'])
                    )
        
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")
        return

    def DelOldRecordsFromDB(self):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        try:
            nowdt = datetime.now().date()- timedelta(days=1)
            dttimeval = f"%{nowdt.strftime('%m')}-{nowdt.strftime('%d')}%"
            delete_sql = "DELETE FROM rsicrossover WHERE \"triggerTime\" like %s;"
            
            lookupts = int((datetime.now()- timedelta(hours=8)).timestamp())
            delete_sql1 = f"DELETE FROM stockorder WHERE CAST(triggerTime AS INTEGER) < {lookupts};"
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(delete_sql, (dttimeval,))
                
                with conn.cursor() as cur1:
                    cur1.execute(delete_sql1)
        
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")
        return

    def AddOpenStockOrderRecordtoDB(self, row, transstate="Open"):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        try:
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    #cur.execute("Select * from stockorder where \"symbol\"=%s and \"OrderType\"=%s and \"transstate\"='Open' and \"triggerTime\"=%s; ", (row['symbol'], row['cspattern'], row['unixtime'],))
                    cur.execute("Select * from stockorder where symbol=%s and OrderType=%s and transstate='Open'; ", (row['symbol'], row['cspattern'],))
                    if (cur.rowcount <= 0 ):
                        cur.execute(
                            "INSERT INTO stockorder (triggerTime, symbol, OrderType, stockprice, stoploss, profittarget, hour, minute,transstate,updatedTriggerTime) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                            (row['unixtime'], row['symbol'], row['cspattern'], row['stockprice'], row['stoploss'], row['profittarget'], row['hour'], row['minute'], "Open", row['updatedTriggerTime'])
                        )
                    else:
                        if (transstate == "Open"):
                            cur.execute(
                                "UPDATE stockorder SET hour=%s, minute=%s, profittarget=%s, stoploss=%s, updatedTriggerTime=%s WHERE triggerTime=%s and symbol=%s and OrderType=%s and transstate='Open';", (row['hour'], row['minute'], row['profittarget'], row['stoploss'], row['updatedTriggerTime'], str(row['unixtime']), row['symbol'], row['cspattern'],)
                            )
                        else:
                            cur.execute(
                                "UPDATE stockorder SET hour=%s, minute=%s, profittarget=%s, stoploss=%s, transstate=%s WHERE triggerTime=%s and symbol=%s and OrderType=%s;", (row['hour'], row['minute'], row['profittarget'], row['stoploss'], transstate, str(row['unixtime']), row['symbol'], row['cspattern'],)
                            )
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")
        return

    def GetStockOrderRecordfromDB(self, symbol, transstate="Open"):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        recdata = None
        try:
            with psycopg2.connect(conn_string) as conn:
                # Open a cursor to perform database operations
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("Select triggerTime, symbol, OrderType, stockprice, stoploss, profittarget, hour, minute, transstate, updatedTriggerTime from stockorder where symbol=%s and transstate=%s; ", (symbol, transstate,))
                    if (cur.rowcount > 0 ):
                        rows = cur.fetchall()
                        for row in rows:
                            recdata = {"symbol": row['symbol'], "stockprice": row['stockprice'], "cspattern": row['ordertype'],
                                "unixtime": row['triggertime'], 'stoploss': row['stoploss'], 'profittarget': row['profittarget'],
                                'hour': row['hour'], 'minute': row['minute'], 'transstate': row['transstate'], 'updatedTriggerTime': row['updatedtriggertime'] }

                cur.close()
            conn.close()
            return recdata
            
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")

    def GetStockOrderRecordusingUnixTime(self, symbol, unixtime, inphour, inpminute):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        recdata = None
        try:
            with psycopg2.connect(conn_string) as conn:
                # Open a cursor to perform database operations
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("Select triggerTime, symbol, OrderType, stockprice, stoploss, profittarget, hour, minute, transstate, updatedTriggerTime from stockorder where symbol=%s and hour=%s and minute=%s ; ", (symbol, inphour, inpminute,))
                    if (cur.rowcount > 0 ):
                        rows = cur.fetchall()
                        for row in rows:
                            recdata = {"symbol": row['symbol'], "stockprice": row['stockprice'], "cspattern": row['ordertype'],
                                "unixtime": row['triggertime'], 'stoploss': row['stoploss'], 'profittarget': row['profittarget'],
                                'hour': row['hour'], 'minute': row['minute'], 'transstate': row['transstate'], 'updatedTriggerTime': row['updatedtriggertime'] }

                cur.close()
            conn.close()
            print(f"recdata: {recdata}")
            return recdata
            
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")

    def AddCloseStockOrderRecordtoDB(self, row):
        conn_string = os.getenv("DATABASE_URL")
        conn = None
        try:
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO stockorder (triggerTime, symbol, OrderType, stockprice, stoploss, profittarget, hour, minute,transstate) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);",
                        (row['unixtime'], row['symbol'], row['cspattern'], row['stockprice'], row['stoploss'], row['profittarget'], row['hour'], row['minute'], "Close")
                    )
        except psycopg2.Error as e:
            print(f"Error connecting to or querying the database: {e}")
        return


