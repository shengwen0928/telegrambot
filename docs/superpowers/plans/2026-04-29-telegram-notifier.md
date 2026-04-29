# Telegram 通知模組 實現計劃

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計劃。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 建立 Telegram 通知功能，當訂位成功或出錯時通知使用者。

**架構：** 建立 `TelegramNotifier` 類別，封裝 `httpx` 非同步發送請求到 Telegram Bot API 的邏輯。

**技術棧：** Python, httpx, python-dotenv, pytest, pytest-asyncio, respx

---

### 任務 1：環境準備

- [ ] **步驟 1：更新 `requirements.txt` 以包含測試工具**
- [ ] **步驟 2：執行安裝**

### 任務 2：實作 Telegram 通知類別

**文件：**
- 建立：`src/notifier.py`

- [ ] **步驟 1：建立 `src/notifier.py` 並實作 `TelegramNotifier`**

```python
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
```

### 任務 3：撰寫單元測試 (TDD)

**文件：**
- 測試：`tests/test_notifier.py`

- [ ] **步驟 1：編寫模擬測試程式碼**

```python
import pytest
import respx
import httpx
from src.notifier import TelegramNotifier

@pytest.mark.asyncio
async def test_send_message_success():
    token = "fake_token"
    chat_id = "fake_chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    with respx.mock:
        respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))
        
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
        result = await notifier.send_message("Hello World")
        
        assert result["ok"] is True
```

- [ ] **步驟 2：執行測試**
運行：`pytest tests/test_notifier.py`

### 任務 4：版本控制

- [ ] **步驟 1：執行 Git Commit**

```bash
git add .
git commit -m "feat: 實作 Telegram 通知模組與單元測試"
```
