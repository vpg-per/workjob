import os
import requests
import io
from pathlib import Path 

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
