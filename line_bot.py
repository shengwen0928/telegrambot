import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

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
# 結構: { "user_id": {"step": "waiting_for_from", "bus": "hohsin", ...} }
user_states: Dict[str, Dict[str, Any]] = {}

# 追蹤運行中的任務: { "user_id": [monitor_obj1, monitor_obj2, ...] }
running_tasks: Dict[str, List[HohsinMonitor]] = {}

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

# 定義狀態機
class States:
    IDLE = "idle"
    WAITING_FOR_BUS = "waiting_for_bus"
    WAITING_FOR_CREDENTIAL_CHOICE = "waiting_for_credential_choice"
    WAITING_FOR_PHONE = "waiting_for_phone"
    WAITING_FOR_PASSWORD = "waiting_for_password"
    WAITING_FOR_SAVE_CHOICE = "waiting_for_save_choice"
    WAITING_FOR_ROUTE_CHOICE = "waiting_for_route_choice"
    WAITING_FOR_FROM = "waiting_for_from"
    WAITING_FOR_TO = "waiting_for_to"
    WAITING_FOR_DATE = "waiting_for_date"
    WAITING_FOR_TIME = "waiting_for_time"
    WAITING_FOR_COUNT = "waiting_for_count"
    WAITING_FOR_SEAT_MODE = "waiting_for_seat_mode"
    WAITING_FOR_MANUAL_SEATS = "waiting_for_manual_seats"
    WAITING_FOR_SAVE_ROUTE = "waiting_for_save_route"

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

def create_route_choice_quick_reply(favorites=None):
    """建立選擇路線方式的 Quick Reply"""
    items = []
    if favorites:
        items.append(QuickReplyItem(action=MessageAction(label="⭐ 常用站點", text="路線:常用")))
    items.append(QuickReplyItem(action=MessageAction(label="🔍 選擇新站點", text="路線:全新")))
    return QuickReply(items=items)

def create_favorites_carousel(favorites):
    """建立常用站點輪播卡片 (包含選擇與刪除按鈕)"""
    bubbles = []
    for i, fav in enumerate(favorites):
        bubbles.append({
            "type": "bubble",
            "size": "micro",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": fav["name"], "weight": "bold", "align": "center"},
                    {
                        "type": "button", 
                        "action": {"type": "message", "label": "選此路線", "text": f"常用路線:{i}"}, 
                        "style": "primary", 
                        "margin": "md",
                        "height": "sm"
                    },
                    {
                        "type": "button", 
                        "action": {"type": "message", "label": "🗑️ 刪除", "text": f"刪除路線:{i}"}, 
                        "style": "secondary", 
                        "margin": "sm",
                        "height": "sm"
                    }
                ]
            }
        })
    return FlexMessage(alt_text="常用站點管理選單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

def create_bus_quick_reply():
    """建立客運選擇的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🚌 和欣客運", text="客運:和欣"))
    ])

def create_seat_mode_quick_reply():
    """建立選位模式 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🤖 自動選位", text="選位:自動")),
        QuickReplyItem(action=MessageAction(label="⌨️ 手動輸入", text="選位:手動"))
    ])

def create_save_route_quick_reply():
    """建立是否儲存常用路線的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="💾 儲存為常用", text="存路線:是")),
        QuickReplyItem(action=MessageAction(label="跳過", text="存路線:否"))
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

def create_ticket_count_quick_reply():
    """建立購買張數的 Quick Reply"""
    items = []
    for i in range(1, 5): # 支援 1-4 張
        items.append(QuickReplyItem(action=MessageAction(label=f"{i} 張", text=f"張數:{i}")))
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
        user_states[user_id] = {"step": States.IDLE}
        
    state = user_states[user_id]
    users = load_users()

    # 1. 啟動指令
    if text in ["搶票", "/start", "開始"]:
        state["step"] = States.WAITING_FOR_BUS
        reply = TextMessage(
            text="歡迎使用自動搶票機器人！\n請先選擇您要搶票的客運：",
            quick_reply=create_bus_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 1.5 查詢/取消任務
    if text in ["查詢", "查詢任務", "取消"]:
        tasks = running_tasks.get(user_id, [])
        if not tasks:
            reply = TextMessage(text="您目前沒有正在執行的搶票任務。")
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
            return
        
        bubbles = []
        for i, m in enumerate(tasks):
            bubbles.append({
                "type": "bubble",
                "size": "micro",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {"type": "text", "text": f"📅 {m.travel_date}", "weight": "bold", "size": "sm"},
                        {"type": "text", "text": f"📍 {m.from_station} ➡️ {m.to_station}", "size": "xs"},
                        {"type": "text", "text": f"⏰ {m.start_time}~{m.end_time}", "size": "xs"},
                        {
                            "type": "button",
                            "action": {"type": "message", "label": "❌ 取消此任務", "text": f"取消任務:{i}"},
                            "style": "secondary", "margin": "md", "color": "#ff4d4f", "height": "sm"
                        }
                    ]
                }
            })
        reply = FlexMessage(alt_text="目前任務清單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 1.6 執行取消
    if text.startswith("取消任務:"):
        idx = int(text.split(":")[1])
        if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
            m = running_tasks[user_id].pop(idx)
            m.stop() # 停止監控循環
            reply = TextMessage(text=f"🛑 已停止任務：{m.travel_date} {m.from_station}➡️{m.to_station}")
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
            return

    # 2. 選擇客運
    if state["step"] == States.WAITING_FOR_BUS and text == "客運:和欣":
        state["bus"] = "hohsin"
        
        # 檢查是否有和欣客運的儲存帳密
        if user_id in users and "hohsin" in users[user_id]:
            state["step"] = States.WAITING_FOR_CREDENTIAL_CHOICE
            masked_phone = users[user_id]["hohsin"].get("phone", "")
            if len(masked_phone) >= 4:
                masked_phone = masked_phone[:-4] + "****"
            reply = TextMessage(
                text=f"您有儲存的【和欣客運】帳號 ({masked_phone})。\n請問要使用該帳號，還是輸入新帳密？",
                quick_reply=create_credential_choice_quick_reply()
            )
        else:
            state["step"] = States.WAITING_FOR_PHONE
            reply = TextMessage(text="為確保訂票成功，請輸入您的【和欣客運手機號碼】：")
            
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.1 選擇是否使用儲存帳密
    if state["step"] == States.WAITING_FOR_CREDENTIAL_CHOICE and text.startswith("帳密:"):
        if text == "帳密:使用儲存":
            state["phone"] = users[user_id]["hohsin"]["phone"]
            state["password"] = users[user_id]["hohsin"]["password"]
            
            # 進入「選擇路線方式」
            state["step"] = States.WAITING_FOR_ROUTE_CHOICE
            favs = users.get(user_id, {}).get("favorites", [])
            reply = TextMessage(
                text="✅ 已載入帳密！\n請問您要使用 **常用站點** 還是 **全新搜尋**？",
                quick_reply=create_route_choice_quick_reply(favs)
            )
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        else:
            state["step"] = States.WAITING_FOR_PHONE
            reply = TextMessage(text="請輸入您的【和欣客運手機號碼】：")
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.2 輸入手機
    if state["step"] == States.WAITING_FOR_PHONE:
        state["phone"] = text
        state["step"] = States.WAITING_FOR_PASSWORD
        reply = TextMessage(text="請輸入您的【和欣客運密碼】：")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.3 輸入密碼
    if state["step"] == States.WAITING_FOR_PASSWORD:
        state["password"] = text
        state["step"] = States.WAITING_FOR_SAVE_CHOICE
        reply = TextMessage(
            text="請問您是否要將此帳密儲存起來，方便下次自動登入？",
            quick_reply=create_save_choice_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.4 選擇是否儲存
    if state["step"] == States.WAITING_FOR_SAVE_CHOICE and text.startswith("記憶:"):
        if text == "記憶:是":
            if user_id not in users: users[user_id] = {}
            users[user_id]["hohsin"] = {"phone": state["phone"], "password": state["password"]}
            save_users(users)
        
        state["step"] = States.WAITING_FOR_ROUTE_CHOICE
        favs = users.get(user_id, {}).get("favorites", [])
        reply = TextMessage(text="✅ 設定完成！\n請問您要使用 **常用站點** 還是 **全新搜尋**？", quick_reply=create_route_choice_quick_reply(favs))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 2.5 選擇路線方式
    if state["step"] == States.WAITING_FOR_ROUTE_CHOICE:
        if text == "路線:常用":
            favs = users.get(user_id, {}).get("favorites", [])
            if not favs:
                reply = TextMessage(text="您目前沒有常用站點紀錄，請選擇「全新搜尋」來建立一筆！", quick_reply=create_route_choice_quick_reply())
            else:
                reply = create_favorites_carousel(favs)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
            return
        elif text == "路線:全新":
            state["step"] = States.WAITING_FOR_FROM
            asyncio.create_task(init_stations())
            reply = TextMessage(text="🔍 全新搜尋\n請問您的 **上車站** 是哪裡？")
            msgs = [reply, create_stations_carousel(STATIONS_CACHE, "上車")]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=msgs))
            return

    # 2.7 刪除常用路線
    if text.startswith("刪除路線:"):
        idx = int(text.split(":")[1])
        if user_id in users and "favorites" in users[user_id]:
            if 0 <= idx < len(users[user_id]["favorites"]):
                removed = users[user_id]["favorites"].pop(idx)
                save_users(users)
                reply = TextMessage(
                    text=f"🗑️ 已刪除常用路線：{removed['name']}\n\n您可以繼續使用其他常用站點，或選擇全新搜尋。",
                    quick_reply=create_route_choice_quick_reply(users[user_id]["favorites"])
                )
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
                return

    # 2.6 選擇常用路線
    if state["step"] == States.WAITING_FOR_ROUTE_CHOICE and text.startswith("常用路線:"):
        idx = int(text.split(":")[1])
        fav = users[user_id]["favorites"][idx]
        state["from_stn"] = fav["from"]
        state["to_stn"] = fav["to"]
        state["from_stn_name"] = fav["name"].split("-")[0]
        state["to_stn_name"] = fav["name"].split("-")[1]
        
        state["step"] = States.WAITING_FOR_DATE
        reply = TextMessage(text=f"⭐ 已選常用路線：{fav['name']}\n\n請選擇您要哪一天的車票：", quick_reply=create_date_picker_quick_reply())
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 3. 選擇上車站
    if state["step"] == States.WAITING_FOR_FROM and text.startswith("上車:"):
        stn_id = text.split(":")[1]
        state["from_stn"] = stn_id
        state["from_stn_name"] = get_station_name(stn_id)
        state["step"] = States.WAITING_FOR_TO
        reply = create_stations_carousel(STATIONS_CACHE, "下車")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 4. 選擇下車站
    if state["step"] == States.WAITING_FOR_TO and text.startswith("下車:"):
        stn_id = text.split(":")[1]
        state["to_stn"] = stn_id
        state["to_stn_name"] = get_station_name(stn_id)
        state["step"] = States.WAITING_FOR_DATE
        reply = TextMessage(text=f"📍 路線：{state['from_stn_name']} ➡️ {state['to_stn_name']}\n\n請選擇乘車日期：", quick_reply=create_date_picker_quick_reply())
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 6. 選擇時段
    if state["step"] == States.WAITING_FOR_TIME and text.startswith("時段:"):
        state["time_range"] = text[3:]
        state["step"] = States.WAITING_FOR_COUNT
        reply = TextMessage(text=f"⏰ 時段：{state['time_range']}\n\n請問購買幾張票？", quick_reply=create_ticket_count_quick_reply())
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 7. 選擇張數
    if state["step"] == States.WAITING_FOR_COUNT and text.startswith("張數:"):
        state["num_tickets"] = int(text.split(":")[1])
        state["step"] = States.WAITING_FOR_SEAT_MODE
        reply = TextMessage(text=f"🎫 張數：{state['num_tickets']} 張\n\n請問選位方式？", quick_reply=create_seat_mode_quick_reply())
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 8. 選擇選位模式
    if state["step"] == States.WAITING_FOR_SEAT_MODE and text.startswith("選位:"):
        if text == "選位:自動":
            state["seat_mode"] = "auto"
            state["manual_seats"] = None
            state["step"] = States.WAITING_FOR_SAVE_ROUTE
            reply = TextMessage(text="🤖 已選擇自動選位。\n最後，是否將此路線存為常用？", quick_reply=create_save_route_quick_reply())
        else:
            state["step"] = States.WAITING_FOR_MANUAL_SEATS
            reply = TextMessage(text="⌨️ 請輸入您指定的座號 (多個請用逗號隔開，例如: 5 或 1,2)：")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 9. 輸入手動座號
    if state["step"] == States.WAITING_FOR_MANUAL_SEATS:
        try:
            seats = [int(s.strip()) for s in text.replace("，", ",").split(",")]
            state["manual_seats"] = seats
            state["seat_mode"] = "manual"
            state["step"] = States.WAITING_FOR_SAVE_ROUTE
            reply = TextMessage(text=f"✅ 已指定座位：{seats}\n\n最後，是否將此路線存為常用？", quick_reply=create_save_route_quick_reply())
        except:
            reply = TextMessage(text="❌ 格式錯誤，請輸入數字 (例如: 5 或 1,2)：")
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        return

    # 10. 儲存常用路線並啟動監控
    if state["step"] == States.WAITING_FOR_SAVE_ROUTE and text.startswith("存路線:"):
        from_name = state["from_stn_name"]
        to_name = state["to_stn_name"]
        
        if text == "存路線:是":
            if "favorites" not in users.get(user_id, {}): 
                if user_id not in users: users[user_id] = {}
                users[user_id]["favorites"] = []
            # 檢查是否已存在
            exists = any(f["from"] == state["from_stn"] and f["to"] == state["to_stn"] for f in users[user_id]["favorites"])
            if not exists:
                users[user_id]["favorites"].append({
                    "from": state["from_stn"],
                    "to": state["to_stn"],
                    "name": f"{from_name}-{to_name}"
                })
                save_users(users)

        # 啟動監控
        time_parts = state["time_range"].split("~")
        summary = (
            "🚀 **搶票任務已啟動！**\n\n"
            f"📍 {from_name} ➡️ {to_name}\n"
            f"📅 {state['date']}\n"
            f"⏰ {state['time_range']}\n"
            f"🎫 {state['num_tickets']} 張 ({'手動' if state['seat_mode']=='manual' else '自動'})\n"
            f"{'💺 指定：' + str(state['manual_seats']) if state['seat_mode']=='manual' else ''}"
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=summary)]))
        
        monitor = HohsinMonitor(
            from_station=state["from_stn"],
            to_station=state["to_stn"],
            travel_date=state["date"],
            start_time=time_parts[0],
            end_time=time_parts[1],
            notifier=LineNotifier(user_id),
            user_phone=state["phone"],
            user_password=state["password"],
            manual_seats=state.get("manual_seats")
        )
        monitor.num_tickets = state["num_tickets"]
        
        # 紀錄任務
        if user_id not in running_tasks: running_tasks[user_id] = []
        running_tasks[user_id].append(monitor)
        
        async def run_and_cleanup():
            try:
                await monitor.run()
            finally:
                if user_id in running_tasks and monitor in running_tasks[user_id]:
                    running_tasks[user_id].remove(monitor)
        
        asyncio.create_task(run_and_cleanup())
        state["step"] = States.IDLE
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
