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
from src.tr_api import TaiwanRailwayAPI
from src.tr_stations import TR_STATIONS
from src.tr_monitor import TaiwanRailwayMonitor

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

def create_success_card(text: str):
    """將搶票成功的純文字轉換為超級精緻的金色通知卡片"""
    # 預期 text 格式：🎉 搶票成功！\n日期：...\n班次：...\n張數：...\n座位：...
    lines = text.split("\n")
    title = lines[0] if len(lines) > 0 else "🎉 搶票成功"
    
    details = []
    for line in lines[1:]:
        if "：" in line:
            key, val = line.split("：", 1)
            details.append({
                "type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": key, "size": "sm", "color": "#aaaaaa", "flex": 2},
                    {"type": "text", "text": val, "size": "sm", "color": "#111111", "flex": 4, "weight": "bold"}
                ]
            })

    card = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#FFD700", # 金色
            "contents": [{"type": "text", "text": "🎊 訂票成功確認函", "color": "#8B4513", "weight": "bold", "size": "md", "align": "center"}]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "恭喜您！系統已成功完成訂位：", "size": "xs", "color": "#666666"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "contents": details},
                {"type": "separator", "margin": "lg"},
                {"type": "text", "text": "請記得於規定時間內前往官網或車站取票付款，祝您旅途愉快！", "size": "xxs", "color": "#aaaaaa", "wrap": True, "margin": "md"}
            ]
        }
    }
    return FlexMessage(alt_text="🎊 搶票成功通知", contents=FlexContainer.from_dict(card))

class LineNotifier:
    """專門為 LINE 打造的通知模組"""
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def send_message(self, text: str):
        """非同步發送訊息 (自動辨識成功訊息並轉為卡片)"""
        try:
            if "🎉 搶票成功" in text:
                msg = create_success_card(text)
            else:
                # 錯誤或其他提示則使用一般的紅/綠卡片
                title = "⚠️ 系統通知" if "❌" in text or "⚠️" in text else "📢 狀態更新"
                color = DANGER_COLOR if "❌" in text or "⚠️" in text else THEME_COLOR
                
                card_dict = create_base_flex_card(title, [{"type": "text", "text": text, "wrap": True, "size": "sm"}])
                card_dict["header"]["backgroundColor"] = color
                
                msg = FlexMessage(alt_text=title, contents=FlexContainer.from_dict(card_dict))

            req = PushMessageRequest(to=self.user_id, messages=[msg])
            line_bot_api.push_message(req)
        except Exception as e:
            logger.error(f"LINE 推播失敗: {e}")
            # 回退機制：萬一 Flex 失敗，發送純文字
            try:
                line_bot_api.push_message(PushMessageRequest(to=self.user_id, messages=[TextMessage(text=text)]))
            except: pass

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
    # 台鐵專用狀態
    WAITING_FOR_TRA_ID = "waiting_for_tra_id"
    WAITING_FOR_TRA_PASSWORD = "waiting_for_tra_password"
    WAITING_FOR_TRA_SAVE_CHOICE = "waiting_for_tra_save_choice"

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
    """客運/鐵路選擇卡片"""
    contents = [{"type": "text", "text": "👋 歡迎使用自動搶票系統！\n請選擇您要服務的業者：", "wrap": True, "size": "sm", "color": "#666666"}]
    footer = [
        {
            "type": "button",
            "action": {"type": "message", "label": "🚌 和欣客運", "text": "客運:和欣"},
            "style": "primary", "color": THEME_COLOR
        },
        {
            "type": "button",
            "action": {"type": "message", "label": "🚆 台灣鐵路", "text": "客運:台鐵"},
            "style": "secondary", "margin": "sm"
        }
    ]
    return FlexMessage(alt_text="請選擇業者", contents=FlexContainer.from_dict(create_base_flex_card("🎫 搶票中心", contents, footer)))

def create_login_hint_card(bus_name: str, masked_phone: str = None):
    """帳密輸入/確認卡片"""
    bus_label = "和欣手機" if "和欣" in bus_name else "身分證字號"
    if masked_phone:
        title = "👋 歡迎回來"
        contents = [{"type": "text", "text": f"偵測到您已儲存【{bus_name}】帳號：\n📱 {masked_phone}\n\n是否直接載入？", "wrap": True, "size": "sm"}]
        footer = [
            {"type": "button", "action": {"type": "message", "label": "✅ 使用儲存帳密", "text": "帳密:使用儲存"}, "style": "primary", "color": THEME_COLOR},
            {"type": "button", "action": {"type": "message", "label": "🔄 輸入新帳密", "text": "帳密:輸入全新"}, "style": "link", "color": "#666666"}
        ]
    else:
        title = "🔒 安全驗證"
        contents = [{"type": "text", "text": f"為確保【{bus_name}】訂票成功，請於下方對話框輸入您的「{bus_label}」。", "wrap": True, "size": "sm", "weight": "bold"}]
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
    """建立車站的輪播選單 (Carousel) - 整合精簡版"""
    bubbles = []
    chunk_size = 3
    for i in range(0, len(stations), chunk_size):
        chunk = stations[i:i + chunk_size]
        buttons = []
        for s in chunk:
            buttons.append({
                "type": "button",
                "action": {"type": "message", "label": s["operatingName"], "text": f"{step_prefix}:{s['id']}"},
                "style": "secondary", "margin": "sm", "height": "sm"
            })
        
        bubble = create_base_flex_card(f"📍 選擇{step_prefix}站", buttons)
        bubbles.append(bubble)
        if len(bubbles) == 12: break

    return FlexMessage(alt_text=f"請選擇{step_prefix}站", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

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

def create_seat_mode_quick_reply():
    """建立選位模式的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="🤖 自動選位", text="選位:自動")),
        QuickReplyItem(action=MessageAction(label="⌨️ 手動指定", text="選位:手動"))
    ])

def create_save_route_quick_reply():
    """建立儲存路線選擇的 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="💾 是，存為常用", text="存路線:是")),
        QuickReplyItem(action=MessageAction(label="❌ 否，不需要", text="存路線:否"))
    ])

def create_task_list_carousel(tasks):
    """建立任務清單的輪播選單"""
    bubbles = []
    for i, m in enumerate(tasks):
        bus_name = "和欣" if isinstance(m, HohsinMonitor) else "台鐵"
        bubble = create_base_flex_card(f"📡 任務 #{i+1} ({bus_name})", [
            {"type": "text", "text": f"📍 {m.from_station} ➡️ {m.to_station}", "weight": "bold", "size": "sm"},
            {"type": "text", "text": f"📅 {m.travel_date}", "size": "xs"},
            {"type": "text", "text": f"⏰ {m.start_time}~{m.end_time}", "size": "xs"}
        ], [
            {"type": "button", "action": {"type": "message", "label": "🛑 停止任務", "text": f"取消任務:{i}"}, "style": "secondary", "color": DANGER_COLOR, "height": "sm"}
        ])
        bubbles.append(bubble)
    return FlexMessage(alt_text="📡 您的任務清單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

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
    """輔助函式：將啟動資訊包裝成精緻卡片，並執行背景任務"""
    bus_type = state.get("bus", "hohsin")
    bus_name = "和欣客運" if bus_type == "hohsin" else "台灣鐵路"
    
    from_name = state["from_stn_name"]
    to_name = state["to_stn_name"]
    time_range = state["time_range"]
    time_parts = time_range.split("~")
    
    contents = [
        {
            "type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": from_name, "weight": "bold", "size": "md"},
                {"type": "text", "text": "➡️", "size": "sm", "color": "#aaaaaa", "align": "center"},
                {"type": "text", "text": to_name, "weight": "bold", "size": "md"}
            ], "alignItems": "center"
        },
        {"type": "separator", "margin": "md"},
        {
            "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm", "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "📅 日期", "size": "xs", "color": "#aaaaaa", "flex": 2},
                    {"type": "text", "text": state['date'], "size": "xs", "color": "#666666", "flex": 5}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "⏰ 時段", "size": "xs", "color": "#aaaaaa", "flex": 2},
                    {"type": "text", "text": time_range, "size": "xs", "color": "#666666", "flex": 5}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🎫 張數", "size": "xs", "color": "#aaaaaa", "flex": 2},
                    {"type": "text", "text": f"{state['num_tickets']} 張", "size": "xs", "color": "#666666", "flex": 5}
                ]},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "🤖 模式", "size": "xs", "color": "#aaaaaa", "flex": 2},
                    {"type": "text", "text": "手動指定" if state['seat_mode']=='manual' else "自動最優", "size": "xs", "color": THEME_COLOR, "flex": 5}
                ]}
            ]
        }
    ]

    if state['seat_mode'] == 'manual' and state.get('manual_seats'):
        contents[2]["contents"].append({
            "type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "💺 座號", "size": "xs", "color": "#aaaaaa", "flex": 2},
                {"type": "text", "text": str(state['manual_seats']), "size": "xs", "color": "#666666", "flex": 5}
            ]
        })

    card_dict = create_base_flex_card(f"🚀 {bus_name}任務已啟動", contents)
    card_dict["footer"] = {
        "type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "💡 提示：輸入「查詢」可控管任務", "size": "xxs", "color": "#aaaaaa", "align": "center"}
        ]
    }
    
    # 啟動監控
    if bus_type == "hohsin":
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
    else:
        monitor = TaiwanRailwayMonitor(
            from_station=state["from_stn"],
            to_station=state["to_stn"],
            travel_date=state["date"],
            start_time=time_parts[0],
            end_time=time_parts[1],
            notifier=LineNotifier(user_id),
            user_id_no=state["phone"],
            user_password=state["password"]
        )
    
    monitor.num_tickets = state["num_tickets"]
    
    if user_id not in running_tasks: running_tasks[user_id] = []
    running_tasks[user_id].append(monitor)
    
    async def run_and_cleanup():
        try: await monitor.run()
        finally:
            if user_id in running_tasks and monitor in running_tasks[user_id]:
                running_tasks[user_id].remove(monitor)
    
    asyncio.create_task(run_and_cleanup())
    state["step"] = States.IDLE
    return FlexMessage(alt_text="🚀 任務啟動成功", contents=FlexContainer.from_dict(card_dict))

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

    # 2. 選擇業者
    if state["step"] == States.WAITING_FOR_BUS and text.startswith("客運:"):
        bus_type = text.split(":")[1]
        state["bus"] = bus_type
        
        bus_name = "和欣客運" if bus_type == "hohsin" else "台灣鐵路"
        if user_id in users and bus_type in users[user_id]:
            state["step"] = States.WAITING_FOR_CREDENTIAL_CHOICE
            stored_id = users[user_id][bus_type].get("phone") or users[user_id][bus_type].get("username")
            masked_id = stored_id[:-4] + "****" if stored_id and len(stored_id) >= 4 else "****"
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_login_hint_card(bus_name, masked_id)]))
        else:
            state["step"] = States.WAITING_FOR_PHONE if bus_type == "hohsin" else States.WAITING_FOR_TRA_ID
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_login_hint_card(bus_name)]))
        return

    # 2.1 選擇是否使用儲存帳密
    if state["step"] == States.WAITING_FOR_CREDENTIAL_CHOICE and text.startswith("帳密:"):
        bus_type = state["bus"]
        if text == "帳密:使用儲存":
            creds = users[user_id][bus_type]
            state["phone"] = creds.get("phone") or creds.get("username")
            state["password"] = creds["password"]
            state["step"] = States.WAITING_FOR_ROUTE_CHOICE
            favs = users.get(user_id, {}).get("favorites", [])
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_route_choice_card(bool(favs))]))
        else:
            state["step"] = States.WAITING_FOR_PHONE if bus_type == "hohsin" else States.WAITING_FOR_TRA_ID
            label = "和欣手機" if bus_type == "hohsin" else "身分證字號"
            contents = [{"type": "text", "text": f"請輸入新的【{label}】：", "size": "sm"}]
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="重新輸入", contents=FlexContainer.from_dict(create_base_flex_card("🔒 帳號重設", contents)))]))
        return

    # 2.2 輸入手機 (和欣)
    if state["step"] == States.WAITING_FOR_PHONE:
        state["phone"] = text
        state["step"] = States.WAITING_FOR_PASSWORD
        contents = [{"type": "text", "text": f"📱 手機：{text}\n\n請輸入【和欣客運密碼】：", "wrap": True, "size": "sm"}]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="輸入密碼", contents=FlexContainer.from_dict(create_base_flex_card("🔒 密碼設定", contents)))]))
        return

    # 2.2.1 輸入身分證 (台鐵)
    if state["step"] == States.WAITING_FOR_TRA_ID:
        state["phone"] = text.upper() # 台鐵身分證轉大寫
        state["step"] = States.WAITING_FOR_TRA_PASSWORD
        contents = [{"type": "text", "text": f"🆔 身分證：{state['phone']}\n\n請輸入【台鐵會員密碼】：", "wrap": True, "size": "sm"}]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="輸入密碼", contents=FlexContainer.from_dict(create_base_flex_card("🔒 鐵路密碼", contents)))]))
        return

    # 2.3 輸入密碼 (通用處理)
    if state["step"] in [States.WAITING_FOR_PASSWORD, States.WAITING_FOR_TRA_PASSWORD]:
        state["password"] = text
        state["step"] = States.WAITING_FOR_SAVE_CHOICE if state["bus"] == "hohsin" else States.WAITING_FOR_TRA_SAVE_CHOICE
        contents = [{"type": "text", "text": "密碼已接收。\n請問是否要儲存此帳密，方便下次自動登入？", "wrap": True, "size": "sm"}]
        footer = [
            {"type": "button", "action": {"type": "message", "label": "💾 是，記住帳密", "text": "記憶:是"}, "style": "primary", "color": THEME_COLOR},
            {"type": "button", "action": {"type": "message", "label": "❌ 否，不要記住", "text": "記憶:否"}, "style": "link", "color": "#666666"}
        ]
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[FlexMessage(alt_text="記憶選擇", contents=FlexContainer.from_dict(create_base_flex_card("💾 隱私設定", contents, footer)))]))
        return

    # 2.4 選擇是否儲存
    if state["step"] in [States.WAITING_FOR_SAVE_CHOICE, States.WAITING_FOR_TRA_SAVE_CHOICE] and text.startswith("記憶:"):
        bus_type = state["bus"]
        if text == "記憶:是":
            if user_id not in users: users[user_id] = {}
            # 統一儲存結構
            users[user_id][bus_type] = {"phone" if bus_type=="hohsin" else "username": state["phone"], "password": state["password"]}
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
            if state["bus"] == "hohsin":
                asyncio.create_task(init_stations())
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_stations_carousel(STATIONS_CACHE, "上車")]))
            else:
                # 台鐵車站選擇：將字典轉為 list
                tr_list = [{"id": k, "operatingName": v} for k, v in TR_STATIONS.items()]
                # 只取前面比較熱門的幾張卡片
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[create_stations_carousel(tr_list, "上車")]))
            return

    # 2.6 選擇常用路線
    if state["step"] == States.WAITING_FOR_ROUTE_CHOICE and text.startswith("常用路線:"):
        idx = int(text.split(":")[1])
        fav = users[user_id]["favorites"][idx]
        state.update({"from_stn": fav["from"], "to_stn": fav["to"], "from_stn_name": fav["name"].split("-")[0], "to_stn_name": fav["name"].split("-")[1], "is_favorite_route": True, "step": States.WAITING_FOR_DATE})
        
        contents = [{"type": "text", "text": f"⭐ 已選常用路線：\n{fav['name']}\n\n請點擊下方按鈕選擇乘車日期。", "wrap": True, "size": "sm"}]
        card = FlexMessage(
            alt_text="選擇日期", 
            contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)),
            quick_reply=create_date_picker_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
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
        card = FlexMessage(
            alt_text="選擇日期", 
            contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)),
            quick_reply=create_date_picker_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        return

    # 6. 選擇時段
    if state["step"] == States.WAITING_FOR_TIME and text.startswith("時段:"):
        state.update({"time_range": text[3:], "step": States.WAITING_FOR_COUNT})
        
        contents = [{"type": "text", "text": f"⏰ 已選時段：{state['time_range']}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
        card = FlexMessage(
            alt_text="選擇張數", 
            contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)),
            quick_reply=create_ticket_count_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        return

    # 7. 選擇張數
    if state["step"] == States.WAITING_FOR_COUNT and text.startswith("張數:"):
        state.update({"num_tickets": int(text.split(":")[1]), "step": States.WAITING_FOR_SEAT_MODE})
        
        contents = [{"type": "text", "text": f"🎫 購票張數：{state['num_tickets']} 張\n\n請問您要使用自動選位還是手動指定？", "wrap": True, "size": "sm"}]
        card = FlexMessage(
            alt_text="選位模式", 
            contents=FlexContainer.from_dict(create_base_flex_card("🤖 選位設定", contents)),
            quick_reply=create_seat_mode_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
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
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[start_monitor_task(user_id, state, users)]))
        else:
            state["step"] = States.WAITING_FOR_SAVE_ROUTE
            contents = [{"type": "text", "text": "🤖 已選擇自動選位。\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="存為常用", 
                contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)),
                quick_reply=create_save_route_quick_reply()
            )
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        return

    # 9. 輸入手動座號
    if state["step"] == States.WAITING_FOR_MANUAL_SEATS:
        try:
            seats = [int(s.strip()) for s in text.replace("，", ",").split(",")]
            state.update({"manual_seats": seats, "seat_mode": "manual"})
            if state.get("is_favorite_route"):
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[start_monitor_task(user_id, state, users)]))
            else:
                state["step"] = States.WAITING_FOR_SAVE_ROUTE
                contents = [{"type": "text", "text": f"✅ 已指定座位：{seats}\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
                card = FlexMessage(
                    alt_text="存為常用", 
                    contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)),
                    quick_reply=create_save_route_quick_reply()
                )
                line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
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
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[start_monitor_task(user_id, state, users)]))
        return


@handler.add(PostbackEvent)
def handle_postback(event):
    """處理 LINE Date Picker 回傳的資料 (全卡片化)"""
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
            contents = [{"type": "text", "text": f"📅 已選日期：{selected_date}\n\n最後一步，請選擇乘車時段：", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇時段", 
                contents=FlexContainer.from_dict(create_base_flex_card("⏰ 時段設定", contents)),
                quick_reply=times_qr
            )
        else:
            state["step"] = States.WAITING_FOR_DATE
            contents = [{"type": "text", "text": f"📅 日期：{selected_date}\n⚠️ 抱歉，該日期的時段皆已截止，請選擇其他日期。", "wrap": True, "size": "sm", "color": DANGER_COLOR}]
            card = FlexMessage(
                alt_text="時段已截止", 
                contents=FlexContainer.from_dict(create_base_flex_card("⚠️ 無可用時段", contents)),
                quick_reply=create_date_picker_quick_reply()
            )

        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))


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
