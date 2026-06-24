import os
import requests
import io
import numpy as np
from pathlib import Path
try:
    import psycopg2
except ImportError:
    psycopg2 = None  # type: ignore

class AlertManager:
    def __init__(self):
        
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            print("[DEBUG] python-dotenv is not installed!")
            pass
            
        self._message = []
        self.token = os.getenv("USER_SECRET")
        self.chat_id = os.getenv("USER_ID")

    def send_chart_alert(self, s_message):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage?chat_id={self.chat_id}&text={s_message}"
        return requests.get(url).json()
    
    def send_photo_alert(self, image_buffer: io.BytesIO,filename:     str = "sp.png", set_title = ""):
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
    
    def isAlertExistsinDB(self, row, symbol, interval):
        """Return True if a matching alert already exists in the DB, False otherwise.
        Falls back to False on any DB error so the caller can still send the alert.
        """
        if psycopg2 is None:
            print("[AlertManager] psycopg2 not installed — skipping DB check")
            return False
        conn_string = os.getenv("DATABASE_URL")
        try:
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT "stockalertid" FROM mtfstockalert WHERE "lasttime"=%s AND "interval"=%s AND "symbol"=%s AND "recorddate"=%s LIMIT 1;',
                        (row['lasttime'], interval, symbol, row['rec_dt']),
                    )
                    # fetchone() returns None when no row matches
                    return cur.fetchone() is not None
        except psycopg2.Error as e:
            print(f"[AlertManager] DB check error: {e}")
            return False

    def AddAlertRecordtoDB(self, row, symbol, interval):
        """Insert a new alert record into the DB.  No-op if psycopg2 is unavailable."""
        if psycopg2 is None:
            print("[AlertManager] psycopg2 not installed — skipping DB insert")
            return
        conn_string = os.getenv("DATABASE_URL")
        try:
            with psycopg2.connect(conn_string) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'INSERT INTO mtfstockalert '
                        '("symbol", "lasttime", "alerttype", "lastbias", "lastcloseprice", "recorddate", "interval") '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s);',
                        (symbol, row['last_time'], row['flag'],row['last_bias'], str(row['last_close']),row['rec_dt'], interval),
                    )
                conn.commit()
                print(f"[AlertManager] ✓ Alert record inserted for {symbol} {interval}")
        except psycopg2.Error as e:
            print(f"[AlertManager] DB insert error: {e}")
