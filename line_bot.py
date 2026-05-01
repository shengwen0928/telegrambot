import os
import asyncio
import logging
import json
import pytz
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

# --- 統一樣式規範 ---
THEME_COLOR = "#00b900" # 和欣綠
DANGER_COLOR = "#ff4d4f" # 警示紅

def create_base_flex_card(title: str, contents: list, footer_buttons: list = None):
    """通用精緻卡片模板"""
    card = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "text", "text": title, "color": "#ffffff", "weight": "bold", "size": "md"}],
            "backgroundColor": THEME_COLOR
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": contents,
            "spacing": "md"
        }
    }
    if footer_buttons:
        card["footer"] = {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": footer_buttons
        }
    return card

# --- 輔助函式：建立 Flex Message ---

def create_bus_card():
    """客運選擇卡片"""
    contents = [{"type": "text", "text": "👋 歡迎使用自動搶票系統！\n請選擇您要服務的客運公司：", "wrap": True, "size": "sm", "color": "#666666"}]
    footer = [{
        "type": "button",
        "action": {"type": "message", "label": "🚌 和欣客運", "text": "客運:和欣"},
        "style": "primary", "color": THEME_COLOR
    }]
    return FlexMessage(alt_text="請選擇客運", contents=FlexContainer.from_dict(create_base_flex_card("🎫 搶票中心", contents, footer)))

def create_login_hint_card(bus_name: str, masked_phone: str = None):
    """帳密輸入/確認卡片"""
    if masked_phone:
        title = "👋 歡迎回來"
        contents = [{"type": "text", "text": f"偵測到您已儲存【{bus_name}】帳號：\n📱 {masked_phone}\n\n是否直接載入？", "wrap": True, "size": "sm"}]
        footer = [
            {"type": "button", "action": {"type": "message", "label": "✅ 使用儲存帳密", "text": "帳密:使用儲存"}, "style": "primary", "color": THEME_COLOR},
            {"type": "button", "action": {"type": "message", "label": "🔄 輸入新帳密", "text": "帳密:輸入全新"}, "style": "link", "color": "#666666"}
        ]
    else:
        title = "🔒 安全驗證"
        contents = [{"type": "text", "text": f"為確保【{bus_name}】訂票成功，請於下方對話框輸入您的「手機號碼」。", "wrap": True, "size": "sm", "weight": "bold"}]
        footer = None
    return FlexMessage(alt_text="帳密確認", contents=FlexContainer.from_dict(create_base_flex_card(title, contents, footer)))

def create_route_choice_card(has_favorites: bool):
    """搜尋模式卡片"""
    contents = [{"type": "text", "text": "✅ 帳密已就緒！\n請選擇您的搜尋模式：", "wrap": True, "size": "sm"}]
    footer = []
    if has_favorites:
        footer.append({"type": "button", "action": {"type": "message", "label": "⭐ 常用站點", "text": "路線:常用"}, "style": "primary", "color": THEME_COLOR})
    footer.append({"type": "button", "action": {"type": "message", "label": "🔍 全新搜尋", "text": "路線:全新"}, "style": "secondary", "margin": "sm"})
    return FlexMessage(alt_text="選擇路線方式", contents=FlexContainer.from_dict(create_base_flex_card("🛰️ 搜尋設定", contents, footer)))

def create_favorites_carousel(favorites):
    """建立精緻漂亮的常用站點輪播卡片"""
    bubbles = []
    for i, fav in enumerate(favorites):
        stn_parts = fav["name"].split("-")
        from_n = stn_parts[0] if len(stn_parts) > 0 else "起點"
        to_n = stn_parts[1] if len(stn_parts) > 1 else "終點"

        bubble = create_base_flex_card("⭐ 常用路線", [
            {
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": from_n, "weight": "bold", "size": "lg", "flex": 0},
                    {"type": "text", "text": "➡️", "size": "sm", "color": "#aaaaaa", "align": "center", "gravity": "center"},
                    {"type": "text", "text": to_n, "weight": "bold", "size": "lg", "flex": 0}
                ], "justifyContent": "space-between", "alignItems": "center"
            }
        ], [
            {"type": "button", "action": {"type": "message", "label": "立刻搶票", "text": f"常用路線:{i}"}, "style": "primary", "color": THEME_COLOR, "height": "sm"},
            {"type": "button", "action": {"type": "message", "label": "🗑️ 刪除", "text": f"刪除路線:{i}"}, "style": "link", "color": DANGER_COLOR, "height": "sm"}
        ])
        bubbles.append(bubble)
    return FlexMessage(alt_text="⭐ 您的常用清單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

def create_stations_carousel(stations, step_prefix="上車"):
    """分頁式的車站選擇卡片"""
    bubbles = []
    chunk_size = 5 # 縮小一點讓卡片更精簡
    for i in range(0, len(stations), chunk_size):
        chunk = stations[i:i + chunk_size]
        btns = []
        for s in chunk:
            btns.append({"type": "button", "action": {"type": "message", "label": s["operatingName"], "text": f"{step_prefix}:{s['id']}"}, "style": "secondary", "margin": "xs", "height": "sm"})
        
        bubble = create_base_flex_card(f"📍 選擇{step_prefix}站", btns)
        bubbles.append(bubble)
        if len(bubbles) == 10: break

    return FlexMessage(alt_text=f"請選擇{step_prefix}站", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

def create_summary_card(state):
    """最終啟動確認卡片"""
    from_n, to_n = state["from_stn_name"], state["to_stn_name"]
    contents = [
        {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": f"📍 路線：{from_n} ➡️ {to_n}", "weight": "bold", "size": "sm"},
            {"type": "text", "text": f"📅 日期：{state['date']}", "size": "sm", "margin": "sm"},
            {"type": "text", "text": f"⏰ 時段：{state['time_range']}", "size": "sm"},
            {"type": "text", "text": f"🎫 張數：{state['num_tickets']} 張", "size": "sm"},
            {"type": "text", "text": f"🤖 模式：{'手動指定' if state['seat_mode']=='manual' else '自動最優'}", "size": "sm", "color": THEME_COLOR}
        ]}
    ]
    return create_base_flex_card("🚀 搶票任務已啟動", contents)

def create_task_list_carousel(tasks):
    """運行中任務的精緻選單"""
    bubbles = []
    for i, m in enumerate(tasks):
        bubble = create_base_flex_card("📡 監控中", [
            {"type": "text", "text": f"📅 {m.travel_date}", "weight": "bold"},
            {"type": "text", "text": f"📍 {m.from_station} ➡️ {m.to_station}", "size": "sm"},
            {"type": "text", "text": f"⏰ {m.start_time}~{m.end_time}", "size": "sm"}
        ], [
            {"type": "button", "action": {"type": "message", "label": "🛑 停止任務", "text": f"取消任務:{i}"}, "style": "primary", "color": DANGER_COLOR, "height": "sm"}
        ])
        bubbles.append(bubble)
    return FlexMessage(alt_text="目前任務清單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

# --- 其他 Quick Reply 保持現狀，因為它們是輸入輔助 ---
def create_credential_choice_quick_reply():
    """建立是否使用儲存帳密的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 使用儲存帳密", text="帳密:使用儲存")),
        QuickReplyItem(action=MessageAction(label="🔄 輸入新帳密", text="帳密:輸入全新"))
    ])

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

def create_times_quick_reply(selected_date: str):
    """建立時段選擇的 Quick Reply (強制台灣時區判定)"""
    all_times = [
        ("00:00~03:00", "03:00"), ("03:00~06:00", "06:00"), 
        ("06:00~09:00", "09:00"), ("09:00~12:00", "12:00"),
        ("12:00~15:00", "15:00"), ("15:00~18:00", "18:00"), 
        ("18:00~21:00", "21:00"), ("21:00~23:59", "23:59")
    ]
    
    # 強制獲取台灣時間
    tw_tz = pytz.timezone('Asia/Taipei')
    now_tw = datetime.now(tw_tz)
    
    is_today = selected_date == now_tw.strftime("%Y-%m-%d")
    
    # 計算「台灣現在時間 + 1 小時」作為訂票截止基準
    deadline_time = now_tw + timedelta(hours=1)
    deadline_str = deadline_time.strftime("%H:%M")

    items = []
    for display, end_time in all_times:
        # 如果選的是今天，且「台灣現在+1小時」已經超過時段「結束時間」，則隱藏
        if is_today and deadline_str > end_time:
            continue
        items.append(QuickReplyItem(action=MessageAction(label=display, text=f"時段:{display}")))
    
    if not items:
        return None
        
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

def start_monitor_task(user_id, state, users):
    """輔助函式：封裝啟動監控任務的邏輯，避免重複代碼"""
    from_name = state["from_stn_name"]
    to_name = state["to_stn_name"]
    time_parts = state["time_range"].split("~")
    
    summary = (
        "🚀 **搶票任務已啟動！**\n\n"
        f"📍 {from_name} ➡️ {to_name}\n"
        f"📅 {state['date']}\n"
        f"⏰ {state['time_range']}\n"
        f"🎫 {state['num_tickets']} 張 ({'手動' if state['seat_mode']=='manual' else '自動'})\n"
        f"{'💺 指定：' + str(state['manual_seats']) if state['seat_mode']=='manual' else ''}"
    )
    
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
    return TextMessage(text=summary)

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
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_bus_card()]))
        return

    # 1.5 查詢/取消任務
    if text in ["查詢", "查詢任務", "取消"]:
        tasks = running_tasks.get(user_id, [])
        if not tasks:
            reply = FlexMessage(alt_text="無進行中任務", contents=FlexContainer.from_dict(create_base_flex_card("📡 任務管理", [{"type": "text", "text": "您目前沒有正在執行的搶票任務。", "size": "sm"}])))
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
            return
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_task_list_carousel(tasks)]))
        return

    # 1.6 執行取消
    if text.startswith("取消任務:"):
        idx = int(text.split(":")[1])
        if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
            m = running_tasks[user_id].pop(idx)
            m.stop() 
            reply = FlexMessage(alt_text="任務已停止", contents=FlexContainer.from_dict(create_base_flex_card("🛑 停止成功", [{"type": "text", "text": f"已成功停止：\n{m.travel_date} {m.from_station}➡️{m.to_station}", "wrap": True, "size": "sm"}])))
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
            return

    # 2. 選擇客運
    if state["step"] == States.WAITING_FOR_BUS and text == "客運:和欣":
        state["bus"] = "hohsin"
        if user_id in users and "hohsin" in users[user_id]:
            state["step"] = States.WAITING_FOR_CREDENTIAL_CHOICE
            masked_phone = users[user_id]["hohsin"].get("phone", "")
            if len(masked_phone) >= 4:
                masked_phone = masked_phone[:-4] + "****"
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_login_hint_card("和欣客運", masked_phone)]))
        else:
            state["step"] = States.WAITING_FOR_PHONE
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_login_hint_card("和欣客運")]))
        return

    # 2.1 選擇是否使用儲存帳密
    if state["step"] == States.WAITING_FOR_CREDENTIAL_CHOICE and text.startswith("帳密:"):
        if text == "帳密:使用儲存":
            state["phone"] = users[user_id]["hohsin"]["phone"]
            state["password"] = users[user_id]["hohsin"]["password"]
            state["step"] = States.WAITING_FOR_ROUTE_CHOICE
            favs = users.get(user_id, {}).get("favorites", [])
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_route_choice_card(bool(favs))]))
        else:
            state["step"] = States.WAITING_FOR_PHONE
            contents = [{"type": "text", "text": "請輸入新的【和欣客運手機號碼】：", "size": "sm"}]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="輸入手機", contents=FlexContainer.from_dict(create_base_flex_card("🔒 帳號設定", contents)))]))
        return

    # 2.2 輸入手機
    if state["step"] == States.WAITING_FOR_PHONE:
        state["phone"] = text
        state["step"] = States.WAITING_FOR_PASSWORD
        contents = [{"type": "text", "text": f"📱 手機：{text}\n\n請輸入【和欣客運密碼】：", "wrap": True, "size": "sm"}]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="輸入密碼", contents=FlexContainer.from_dict(create_base_flex_card("🔒 密碼設定", contents)))]))
        return

    # 2.3 輸入密碼
    if state["step"] == States.WAITING_FOR_PASSWORD:
        state["password"] = text
        state["step"] = States.WAITING_FOR_SAVE_CHOICE
        contents = [{"type": "text", "text": "密碼已接收。\n請問是否要將此帳密儲存起來，方便下次自動登入？", "wrap": True, "size": "sm"}]
        footer = [
            {"type": "button", "action": {"type": "message", "label": "💾 是，記住帳密", "text": "記憶:是"}, "style": "primary", "color": THEME_COLOR},
            {"type": "button", "action": {"type": "message", "label": "❌ 否，不要記住", "text": "記憶:否"}, "style": "link", "color": "#666666"}
        ]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="記憶選擇", contents=FlexContainer.from_dict(create_base_flex_card("💾 隱私設定", contents, footer)))]))
        return

    # 2.4 選擇是否儲存
    if state["step"] == States.WAITING_FOR_SAVE_CHOICE and text.startswith("記憶:"):
        if text == "記憶:是":
            if user_id not in users: users[user_id] = {}
            users[user_id]["hohsin"] = {"phone": state["phone"], "password": state["password"]}
            save_users(users)
        state["step"] = States.WAITING_FOR_ROUTE_CHOICE
        favs = users.get(user_id, {}).get("favorites", [])
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_route_choice_card(bool(favs))]))
        return

    # 2.5 選擇路線方式
    if state["step"] == States.WAITING_FOR_ROUTE_CHOICE:
        if text == "路線:常用":
            favs = users.get(user_id, {}).get("favorites", [])
            if not favs:
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_route_choice_card(False)]))
            else:
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_favorites_carousel(favs)]))
            return
        elif text == "路線:全新":
            state["step"] = States.WAITING_FOR_FROM
            asyncio.create_task(init_stations())
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_stations_carousel(STATIONS_CACHE, "上車")]))
            return

    # 2.6 選擇常用路線
    if state["step"] == States.WAITING_FOR_ROUTE_CHOICE and text.startswith("常用路線:"):
        idx = int(text.split(":")[1])
        fav = users[user_id]["favorites"][idx]
        state.update({"from_stn": fav["from"], "to_stn": fav["to"], "from_stn_name": fav["name"].split("-")[0], "to_stn_name": fav["name"].split("-")[1], "is_favorite_route": True, "step": States.WAITING_FOR_DATE})
        
        contents = [{"type": "text", "text": f"⭐ 已選常用路線：\n{fav['name']}\n\n請點擊下方按鈕選擇乘車日期。", "wrap": True, "size": "sm"}]
        card = FlexMessage(alt_text="選擇日期", contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="點此選日期：", quick_reply=create_date_picker_quick_reply())]))
        return

    # 3. 選擇上車站
    if state["step"] == States.WAITING_FOR_FROM and text.startswith("上車:"):
        stn_id = text.split(":")[1]
        state.update({"from_stn": stn_id, "from_stn_name": get_station_name(stn_id), "is_favorite_route": False, "step": States.WAITING_FOR_TO})
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_stations_carousel(STATIONS_CACHE, "下車")]))
        return

    # 4. 選擇下車站
    if state["step"] == States.WAITING_FOR_TO and text.startswith("下車:"):
        stn_id = text.split(":")[1]
        state.update({"to_stn": stn_id, "to_stn_name": get_station_name(stn_id), "step": States.WAITING_FOR_DATE})
        
        contents = [{"type": "text", "text": f"📍 路線：{state['from_stn_name']} ➡️ {state['to_stn_name']}\n\n請點擊下方按鈕選擇乘車日期。", "wrap": True, "size": "sm"}]
        card = FlexMessage(alt_text="選擇日期", contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="點此選日期：", quick_reply=create_date_picker_quick_reply())]))
        return

    # 6. 選擇時段
    if state["step"] == States.WAITING_FOR_TIME and text.startswith("時段:"):
        state.update({"time_range": text[3:], "step": States.WAITING_FOR_COUNT})
        
        contents = [{"type": "text", "text": f"⏰ 已選時段：{state['time_range']}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
        card = FlexMessage(alt_text="選擇張數", contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="請選擇張數：", quick_reply=create_ticket_count_quick_reply())]))
        return

    # 7. 選擇張數
    if state["step"] == States.WAITING_FOR_COUNT and text.startswith("張數:"):
        state.update({"num_tickets": int(text.split(":")[1]), "step": States.WAITING_FOR_SEAT_MODE})
        
        contents = [{"type": "text", "text": f"🎫 購票張數：{state['num_tickets']} 張\n\n請問您要使用自動選位還是手動指定？", "wrap": True, "size": "sm"}]
        card = FlexMessage(alt_text="選位模式", contents=FlexContainer.from_dict(create_base_flex_card("🤖 選位設定", contents)))
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="請選擇方式：", quick_reply=create_seat_mode_quick_reply())]))
        return

    # 8. 選擇選位模式
    if state["step"] == States.WAITING_FOR_SEAT_MODE and text.startswith("選位:"):
        if text == "選位:自動":
            state.update({"seat_mode": "auto", "manual_seats": None})
        else:
            state["step"] = States.WAITING_FOR_MANUAL_SEATS
            contents = [{"type": "text", "text": "⌨️ 請於下方輸入您指定的座號。\n(多個請用逗號隔開，例如: 5 或 1,2)", "wrap": True, "size": "sm", "weight": "bold"}]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="輸入座號", contents=FlexContainer.from_dict(create_base_flex_card("⌨️ 座號輸入", contents)))]))
            return

        if state.get("is_favorite_route"):
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="任務啟動", contents=FlexContainer.from_dict(start_monitor_task(user_id, state, users)))]))
        else:
            state["step"] = States.WAITING_FOR_SAVE_ROUTE
            contents = [{"type": "text", "text": "🤖 已選擇自動選位。\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
            card = FlexMessage(alt_text="存為常用", contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)))
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="是否儲存？", quick_reply=create_save_route_quick_reply())]))
        return

    # 9. 輸入手動座號
    if state["step"] == States.WAITING_FOR_MANUAL_SEATS:
        try:
            seats = [int(s.strip()) for s in text.replace("，", ",").split(",")]
            state.update({"manual_seats": seats, "seat_mode": "manual"})
            if state.get("is_favorite_route"):
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="任務啟動", contents=FlexContainer.from_dict(start_monitor_task(user_id, state, users)))]))
            else:
                state["step"] = States.WAITING_FOR_SAVE_ROUTE
                contents = [{"type": "text", "text": f"✅ 已指定座位：{seats}\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
                card = FlexMessage(alt_text="存為常用", contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)))
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card, TextMessage(text="是否儲存？", quick_reply=create_save_route_quick_reply())]))
        except:
            contents = [{"type": "text", "text": "❌ 格式錯誤！\n請重新輸入數字（例如: 5 或 1,2）：", "color": DANGER_COLOR, "size": "sm"}]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="格式錯誤", contents=FlexContainer.from_dict(create_base_flex_card("⚠️ 輸入錯誤", contents)))]))
        return

    # 10. 儲存常用路線並啟動監控
    if state["step"] == States.WAITING_FOR_SAVE_ROUTE and text.startswith("存路線:"):
        if text == "存路線:是":
            if user_id not in users: users[user_id] = {}
            if "favorites" not in users[user_id]: users[user_id]["favorites"] = []
            if not any(f["from"] == state["from_stn"] and f["to"] == state["to_stn"] for f in users[user_id]["favorites"]):
                users[user_id]["favorites"].append({"from": state["from_stn"], "to": state["to_stn"], "name": f"{state['from_stn_name']}-{state['to_stn_name']}"})
                save_users(users)
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="任務啟動", contents=FlexContainer.from_dict(start_monitor_task(user_id, state, users)))]))
        return


@handler.add(PostbackEvent)
def handle_postback(event):
    """處理 LINE Date Picker 回傳的資料"""
    user_id = event.source.user_id
    if user_id not in user_states:
        return
        
    state = user_states[user_id]
    
    # 5. 處理日期選擇
    if state["step"] == States.WAITING_FOR_DATE and event.postback.data == "action=select_date":
        selected_date = event.postback.params['date'] # 格式 YYYY-MM-DD
        state["date"] = selected_date
        state["step"] = "waiting_for_time"

        times_qr = create_times_quick_reply(selected_date)
        if times_qr:
            reply = TextMessage(
                text=f"📅 已選擇日期：{selected_date}\n\n最後一步，請選擇乘車時段：",
                quick_reply=times_qr
            )
        else:
            # 如果是今天且已經過了 23:59
            state["step"] = States.WAITING_FOR_DATE # 退回日期選擇
            reply = TextMessage(
                text=f"📅 日期：{selected_date}\n⚠️ 抱歉，該日期的所有時段皆已過去，請選擇其他日期。",
                quick_reply=create_date_picker_quick_reply()
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
