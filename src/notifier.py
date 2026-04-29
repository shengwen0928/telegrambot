import httpx
import os
from dotenv import load_dotenv

# 讀取 .env 檔案
load_dotenv()

class TelegramNotifier:
    """Telegram 通知模組，用於發送即時訊息。"""
    
    def __init__(self, token=None, chat_id=None):
        """初始化，預設從環境變數讀取 Token 與 Chat ID。"""
        self.token = token or os.getenv("TG_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TG_CHAT_ID")
        self.url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def send_message(self, text: str):
        """非同步發送訊息。"""
        payload = {"chat_id": self.chat_id, "text": text}
        async with httpx.AsyncClient() as client:
            response = await client.post(self.url, json=payload)
            return response.json()
