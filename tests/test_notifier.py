import pytest
import respx
import httpx
import os
from src.notifier import TelegramNotifier

@pytest.mark.asyncio
async def test_send_message_success():
    """測試發送訊息成功的情境。"""
    token = "fake_token"
    chat_id = "fake_chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    with respx.mock:
        # 模擬 Telegram API 回傳成功
        respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))
        
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
        result = await notifier.send_message("測試訊息")
        
        assert result["ok"] is True
        assert result["result"]["message_id"] == 1

@pytest.mark.asyncio
async def test_send_message_failure():
    """測試發送訊息失敗的情境。"""
    token = "fake_token"
    chat_id = "fake_chat_id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    with respx.mock:
        # 模擬 Telegram API 回傳錯誤
        respx.post(url).mock(return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request"}))
        
        notifier = TelegramNotifier(token=token, chat_id=chat_id)
        result = await notifier.send_message("測試訊息")
        
        assert result["ok"] is False
        assert result["description"] == "Bad Request"

@pytest.mark.asyncio
async def test_default_env_loading(monkeypatch):
    """測試是否正確從環境變數讀取。"""
    monkeypatch.setenv("TG_BOT_TOKEN", "env_token")
    monkeypatch.setenv("TG_CHAT_ID", "env_chat_id")
    
    notifier = TelegramNotifier()
    assert notifier.token == "env_token"
    assert notifier.chat_id == "env_chat_id"
    assert "env_token" in notifier.url
