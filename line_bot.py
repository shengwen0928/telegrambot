import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
    QuickReply,
    QuickReplyItem,
    MessageAction,
    DatetimePickerAction
)
from linebot.v3.webhooks import (
    MessageEvent,
    PostbackEvent,
    TextMessageContent
)

from src.hohsin_api import HohsinAPI
from src.monitor import HohsinMonitor

# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LineBot")

USER_DB_FILE = "users.json"

def load_users() -> Dict[str, Dict[str, str]]:
    if os.path.exists(USER_DB_FILE):
        try:
            with open(USER_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"讀取 users.json 失敗: {e}")
    return {}

def save_users(data: Dict[str, Dict[str, str]]):
    try:
        with open(USER_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"寫入 users.json 失敗: {e}")

# 讀取環境變數
load_dotenv()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    logger.error("錯誤：未設定 LINE_CHANNEL_SECRET 或 LINE_CHANNEL_ACCESS_TOKEN")
    logger.info("這是不影響系統啟動的警告，請在 .env 中補上設定。")

# 初始化 FastAPI 與 LINE SDK
app = FastAPI()
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
line_bot_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

class LineNotifier:
    """專門為 LINE 打造的通知模組"""
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def send_message(self, text: str):
        """非同步發送訊息 (使用 Push Message)"""
        try:
            req = PushMessageRequest(to=self.user_id, messages=[TextMessage(text=text)])
            line_bot_api.push_message(req)
        except Exception as e:
            logger.error(f"LINE 推播失敗: {e}")

# 簡單的記憶體狀態管理 (Production 建議改用 Redis)

# 結構: { "user_id": {"step": "waiting_for_from", "bus": "hohsin", "from_stn": "G03", "to_stn": "B01", "date": "2026-05-05"} }
user_states: Dict[str, Dict[str, Any]] = {}

# 全域的和欣 API 實例 (用來抓車站)
global_api = HohsinAPI()
STATIONS_CACHE = []

async def init_stations():
    global STATIONS_CACHE
    if not STATIONS_CACHE:
        try:
            STATIONS_CACHE = await global_api.get_stations()
        except Exception as e:
            logger.error(f"無法獲取車站清單: {e}")

# --- 輔助函式：建立 Flex Message ---

def create_credential_choice_quick_reply():
    """建立是否使用儲存帳密的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 使用儲存帳密", text="帳密:使用儲存")),
        QuickReplyItem(action=MessageAction(label="🔄 輸入新帳密", text="帳密:輸入全新"))
    ])

def create_save_choice_quick_reply():
    """建立是否記憶帳密的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="💾 是，記住帳密", text="記憶:是")),
        QuickReplyItem(action=MessageAction(label="❌ 否，不要記住", text="記憶:否"))
    ])

def create_bus_quick_reply():
    """建立客運選擇的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🚌 和欣客運", text="客運:和欣"))
    ])

def create_stations_carousel(stations, step_prefix="上車"):
    """建立車站的輪播選單 (Carousel)"""
    # 每個 Bubble (卡片) 最多只能放一定數量的按鈕，我們每張卡片放 3 個按鈕
    bubbles = []
    chunk_size = 3
    for i in range(0, len(stations), chunk_size):
        chunk = stations[i:i + chunk_size]
        buttons = []
        for s in chunk:
            buttons.append({
                "type": "button",
                "action": {
                    "type": "message",
                    "label": s["operatingName"],
                    "text": f"{step_prefix}:{s['id']}" # 訊息內容如 "上車:G03"
                },
                "style": "secondary",
                "margin": "sm"
            })
        
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": f"選擇{step_prefix}站",
                        "weight": "bold",
                        "size": "xl"
                    }
                ] + buttons
            }
        }
        bubbles.append(bubble)
        
        # LINE Carousel 最多支援 12 個 bubble (即 36 個站)
        if len(bubbles) == 12:
            break

    return FlexMessage(
        alt_text=f"請選擇{step_prefix}站",
        contents=FlexContainer.from_dict({
            "type": "carousel",
            "contents": bubbles
        })
    )

def create_date_picker_quick_reply():
    """建立日期選擇器的 Quick Reply"""
    today = datetime.now()
    max_date = today + timedelta(days=15) # 最多 16 天
    
    return QuickReply(items=[
        QuickReplyItem(action=DatetimePickerAction(
            label="📅 選擇乘車日期",
            data="action=select_date",
            mode="date",
            initial=today.strftime("%Y-%m-%d"),
            max=max_date.strftime("%Y-%m-%d"),
            min=today.strftime("%Y-%m-%d")
        ))
    ])

def create_times_quick_reply():
    """建立時段選擇的 Quick Reply"""
    times = [
        "00:00~03:00", "03:00~06:00", "06:00~09:00", "09:00~12:00",
        "12:00~15:00", "15:00~18:00", "18:00~21:00", "21:00~23:59"
    ]
    items = []
    for t in times:
        items.append(QuickReplyItem(action=MessageAction(label=t, text=f"時段:{t}")))
    return QuickReply(items=items)

def get_station_name(stn_id: str) -> str:
    for s in STATIONS_CACHE:
        if s["id"] == stn_id:
            return s["operatingName"]
    return stn_id

# --- FastAPI 路由 ---

@app.post("/callback")
async def callback(request: Request):
    """LINE Webhook 接收點"""
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        handler.handle(body_str, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return "OK"

# --- LINE 事件處理 ---

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    
    # 初始化狀態
    if user_id not in user_states:
        user_states[user_id] = {"step": "idle"}
        
    state = user_states[user_id]

    # 1. 啟動指令
    if text in ["搶票", "/start", "開始"]:
        state["step"] = "waiting_for_bus"
        reply = TextMessage(
            text="歡迎使用自動搶票機器人！\n請先選擇您要搶票的客運：",
            quick_reply=create_bus_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2. 選擇客運
    if state["step"] == "waiting_for_bus" and text == "客運:和欣":
        state["bus"] = "hohsin"
        
        # 檢查是否有和欣客運的儲存帳密
        users = load_users()
        if user_id in users and "hohsin" in users[user_id]:
            state["step"] = "waiting_for_credential_choice"
            masked_phone = users[user_id]["hohsin"].get("phone", "")
            if len(masked_phone) >= 4:
                masked_phone = masked_phone[:-4] + "****"
            reply = TextMessage(
                text=f"您有儲存的【和欣客運】帳號 ({masked_phone})。\n請問要使用該帳號，還是輸入新帳密？",
                quick_reply=create_credential_choice_quick_reply()
            )
        else:
            state["step"] = "waiting_for_phone"
            reply = TextMessage(text="為確保訂票成功，請輸入您的【和欣客運手機號碼】：")
            
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.1 選擇是否使用儲存帳密
    if state["step"] == "waiting_for_credential_choice" and text.startswith("帳密:"):
        if text == "帳密:使用儲存":
            users = load_users()
            state["phone"] = users[user_id]["hohsin"]["phone"]
            state["password"] = users[user_id]["hohsin"]["password"]
            
            state["step"] = "waiting_for_from"
            asyncio.create_task(init_stations())
            reply = TextMessage(
                text="✅ 已載入帳密！\n請問您的 **上車站** 是哪裡？",
                quick_reply=None # 清除快速回覆
            )
            # 因為只能回傳一個 ReplyMessageRequest，我們把兩個訊息放進去
            msgs = [reply, create_stations_carousel(STATIONS_CACHE, "上車")]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=msgs))
        else:
            state["step"] = "waiting_for_phone"
            reply = TextMessage(text="請輸入您的【和欣客運手機號碼】：")
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.2 輸入手機
    if state["step"] == "waiting_for_phone":
        state["phone"] = text
        state["step"] = "waiting_for_password"
        reply = TextMessage(text="請輸入您的【和欣客運密碼】\n(預設通常是身分證字號，英文字母大寫)：")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.3 輸入密碼
    if state["step"] == "waiting_for_password":
        state["password"] = text
        state["step"] = "waiting_for_save_choice"
        reply = TextMessage(
            text="請問您是否要將此帳密儲存起來，方便下次自動登入？\n(將安全儲存於本地伺服器)",
            quick_reply=create_save_choice_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.4 選擇是否儲存
    if state["step"] == "waiting_for_save_choice" and text.startswith("記憶:"):
        if text == "記憶:是":
            users = load_users()
            if user_id not in users:
                users[user_id] = {}
            users[user_id]["hohsin"] = {
                "phone": state["phone"],
                "password": state["password"]
            }
            save_users(users)
        
        state["step"] = "waiting_for_from"
        asyncio.create_task(init_stations())
        reply = TextMessage(text="✅ 帳密設定完成！\n請問您的 **上車站** 是哪裡？")
        msgs = [reply, create_stations_carousel(STATIONS_CACHE, "上車")]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=msgs))
        return

    # 3. 選擇上車站
    if state["step"] == "waiting_for_from" and text.startswith("上車:"):
        stn_id = text.split(":")[1]
        state["from_stn"] = stn_id
        state["step"] = "waiting_for_to"
        
        reply = create_stations_carousel(STATIONS_CACHE, "下車")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 4. 選擇下車站
    if state["step"] == "waiting_for_to" and text.startswith("下車:"):
        stn_id = text.split(":")[1]
        state["to_stn"] = stn_id
        state["step"] = "waiting_for_date"
        
        from_name = get_station_name(state["from_stn"])
        to_name = get_station_name(stn_id)
        
        reply = TextMessage(
            text=f"📍 路線：{from_name} ➡️ {to_name}\n\n請選擇您要哪一天的車票：",
            quick_reply=create_date_picker_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 6. 選擇時段並啟動監控
    if state["step"] == "waiting_for_time" and text.startswith("時段:"):
        time_range = text[3:] # ex: 00:00~03:00 (去掉前三個字 "時段:")
        start_t, end_t = time_range.split("~")
        
        state["start_time"] = start_t
        state["end_time"] = end_t
        
        from_name = get_station_name(state["from_stn"])
        to_name = get_station_name(state["to_stn"])
        travel_date = state["date"]
        
        summary = (
            "✅ 搶票任務已建立並開始背景監控！\n\n"
            f"🚌 客運：和欣客運\n"
            f"📍 路線：{from_name} -> {to_name}\n"
            f"📅 日期：{travel_date}\n"
            f"⏰ 時段：{start_t} ~ {end_t}\n\n"
            "💡 提示：您可以再次輸入「搶票」建立另一筆任務。"
        )
        
        reply = TextMessage(text=summary)
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        
        # 啟動背景搶票
        monitor = HohsinMonitor(
            from_station=state["from_stn"],
            to_station=state["to_stn"],
            travel_date=travel_date,
            start_time=start_t,
            end_time=end_t,
            notifier=LineNotifier(user_id),
            user_phone=state.get("phone"),
            user_password=state.get("password")
        )
        asyncio.create_task(monitor.run())
        
        # 清除狀態
        user_states[user_id] = {"step": "idle"}
        return

@handler.add(PostbackEvent)
def handle_postback(event):
    """處理 LINE Date Picker 回傳的資料"""
    user_id = event.source.user_id
    if user_id not in user_states:
        return
        
    state = user_states[user_id]
    
    # 5. 處理日期選擇
    if state["step"] == "waiting_for_date" and event.postback.data == "action=select_date":
        selected_date = event.postback.params['date'] # 格式 YYYY-MM-DD
        state["date"] = selected_date
        state["step"] = "waiting_for_time"
        
        reply = TextMessage(
            text=f"📅 已選擇日期：{selected_date}\n\n最後一步，請選擇乘車時段：",
            quick_reply=create_times_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))

# --- 啟動設定 ---
@app.on_event("startup")
async def startup_event():
    # 預先載入車站清單
    await init_stations()
    logger.info("LINE Bot 伺服器已啟動！")

if __name__ == "__main__":
    import uvicorn
    # 本地測試時可以使用 ngrok 將 8000 port 對外暴露
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    print("這是一個 FastAPI 應用，請使用以下指令啟動：")
    print("uvicorn line_bot:app --host 0.0.0.0 --port 8000")
