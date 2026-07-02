import os
import asyncio
import logging
import json
import pytz
import httpx
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    AsyncApiClient,
    AsyncMessagingApi,
    AsyncMessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
    QuickReply,
    QuickReplyItem,
    MessageAction,
    DatetimePickerAction,
    ImageMessage
)
from linebot.v3.messaging.exceptions import UnauthorizedException
from linebot.v3.webhooks import (
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
    ImageMessageContent
)
from fastapi.staticfiles import StaticFiles

from src.hohsin_api import HohsinAPI
from src.monitor import HohsinMonitor
from src.tr_api import TaiwanRailwayAPI
from src.tr_stations import TR_STATIONS
from src.tr_monitor import TaiwanRailwayMonitor
from src.persistence import save_tasks_to_file, load_tasks_from_file
from src.ai_chat import ai_reply

# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LineBot")

# 全域通知機器人實例預定義，防止 NameError
line_bot_api_notify = None
LINE_QUOTA_EXHAUSTED = False
LINE_TOKEN_REFRESH_LOCK = asyncio.Lock()

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
LINE_CHANNEL_ID = os.getenv("LINE_CHANNEL_ID")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
# 備援通知機器人 Token
LINE_NOTIFY_ACCESS_TOKEN = os.getenv("LINE_NOTIFY_ACCESS_TOKEN")

if not LINE_CHANNEL_SECRET or (not LINE_CHANNEL_ACCESS_TOKEN and not LINE_CHANNEL_ID):
    logger.error("錯誤：未設定 LINE_CHANNEL_SECRET 或 LINE_CHANNEL_ACCESS_TOKEN（或 LINE_CHANNEL_ID）")
    logger.info("這是不影響系統啟動的警告，請在 .env 中補上設定。")
elif not LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_ID:
    logger.warning("未設定 LINE_CHANNEL_ACCESS_TOKEN，將在需要時嘗試向 LINE 取得新 token。")

# 初始化 FastAPI 與 LINE SDK (主機器人)
app = FastAPI()

# 修正：掛載靜態檔案目錄，以便提供 QR Code 圖片
static_dir = os.path.join(os.getcwd(), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
async_api_client = AsyncApiClient(configuration)
line_bot_api = AsyncMessagingApi(async_api_client)
line_bot_blob = AsyncMessagingApiBlob(async_api_client)   # 下載使用者傳來的圖片

# 初始化備援通知機器人 (如果有的話)
line_bot_api_notify = None
if LINE_NOTIFY_ACCESS_TOKEN:
    config_notify = Configuration(access_token=LINE_NOTIFY_ACCESS_TOKEN)
    async_api_client_notify = AsyncApiClient(config_notify)
    line_bot_api_notify = AsyncMessagingApi(async_api_client_notify)
    logger.info("備援通知機器人已就緒。")

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

# --- 訊息安全發送層 (解決 429 熔斷問題) ---

async def refresh_line_channel_access_token(expected_token: Optional[str] = None) -> bool:
    """嘗試以 Channel ID/Secret 重新取得 LINE Channel Access Token"""
    global LINE_CHANNEL_ACCESS_TOKEN, configuration, async_api_client, line_bot_api
    if not LINE_CHANNEL_ID or not LINE_CHANNEL_SECRET:
        logger.error("無法刷新 LINE Channel Access Token：缺少 LINE_CHANNEL_ID 或 LINE_CHANNEL_SECRET")
        return False

    async with LINE_TOKEN_REFRESH_LOCK:
        if expected_token and LINE_CHANNEL_ACCESS_TOKEN != expected_token:
            return True

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.line.me/oauth2/v2.1/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": LINE_CHANNEL_ID,
                        "client_secret": LINE_CHANNEL_SECRET
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
        except httpx.HTTPError as e:
            logger.error(f"刷新 LINE access token 失敗: {e}")
            return False

        if response.status_code != 200:
            logger.error(f"刷新 LINE access token 失敗: {response.status_code} {response.text}")
            return False

        try:
            payload = response.json()
        except ValueError as e:
            logger.error(f"刷新 LINE access token 失敗: 回應解析錯誤 {e}")
            return False

        new_token = payload.get("access_token")
        if not new_token:
            logger.error("刷新 LINE access token 失敗: 回應缺少 access_token")
            return False

        if new_token == LINE_CHANNEL_ACCESS_TOKEN:
            return True

        if async_api_client:
            try:
                await async_api_client.close()
            except Exception as e:
                logger.warning(f"關閉舊 LINE client 失敗: {e}")

        LINE_CHANNEL_ACCESS_TOKEN = new_token
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        async_api_client = AsyncApiClient(configuration)
        line_bot_api = AsyncMessagingApi(async_api_client)
        logger.info("LINE channel access token 已更新")
        return True

def is_unauthorized_error(err: Exception) -> bool:
    """判斷是否為 LINE token 失效造成的 401"""
    if isinstance(err, UnauthorizedException):
        return True
    status = getattr(err, "status", None)
    if status == 401:
        return True
    http_resp = getattr(err, "http_resp", None)
    if http_resp is not None and getattr(http_resp, "status", None) == 401:
        return True
    msg = str(err).lower()
    return "401" in msg or "invalid_token" in msg or "unauthorized" in msg

async def safe_reply(reply_token: str, messages: list, user_id: str = None):
    """具備熔斷保護的回覆函式"""
    global LINE_QUOTA_EXHAUSTED
    if LINE_QUOTA_EXHAUSTED:
        logger.warning(f"[熔斷中] 拒絕向 {user_id} 發送回覆訊息")
        return
    
    try:
        await line_bot_api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))
    except Exception as e:
        if is_unauthorized_error(e):
            logger.warning(f"LINE access token 失效，嘗試刷新: {e}")
            current_token = LINE_CHANNEL_ACCESS_TOKEN
            if await refresh_line_channel_access_token(current_token):
                try:
                    await line_bot_api.reply_message(ReplyMessageRequest(reply_token=reply_token, messages=messages))
                except Exception as retry_err:
                    if "429" in str(retry_err) or "limit" in str(retry_err).lower():
                        LINE_QUOTA_EXHAUSTED = True
                        logger.error("!!! LINE 配額耗盡，啟動熔斷器 !!!")
                    else:
                        logger.error(f"Reply 重試失敗: {retry_err}")
            else:
                logger.error("無法刷新 LINE access token，Reply 失敗")
        elif "429" in str(e) or "limit" in str(e).lower():
            LINE_QUOTA_EXHAUSTED = True
            logger.error("!!! LINE 配額耗盡，啟動熔斷器 !!!")
        else:
            logger.error(f"Reply 失敗: {e}")

async def safe_push(user_id: str, messages: list):
    """具備熔斷保護的推播函式"""
    global LINE_QUOTA_EXHAUSTED
    if LINE_QUOTA_EXHAUSTED:
        logger.warning(f"[熔斷中] 拒絕向 {user_id} 發送推播訊息")
        return
    
    try:
        client = line_bot_api_notify if line_bot_api_notify else line_bot_api
        await client.push_message(PushMessageRequest(to=user_id, messages=messages))
    except Exception as e:
        if is_unauthorized_error(e):
            if client is line_bot_api:
                logger.warning(f"LINE access token 失效，嘗試刷新: {e}")
                current_token = LINE_CHANNEL_ACCESS_TOKEN
                if await refresh_line_channel_access_token(current_token):
                    try:
                        await line_bot_api.push_message(PushMessageRequest(to=user_id, messages=messages))
                    except Exception as retry_err:
                        if "429" in str(retry_err) or "limit" in str(retry_err).lower():
                            LINE_QUOTA_EXHAUSTED = True
                            logger.error("!!! LINE 配額耗盡，啟動熔斷器 !!!")
                        else:
                            logger.error(f"Push 重試失敗: {retry_err}")
                else:
                    logger.error("無法刷新 LINE access token，Push 失敗")
            else:
                logger.error(f"Push 失敗: {e}")
        elif "429" in str(e) or "limit" in str(e).lower():
            LINE_QUOTA_EXHAUSTED = True
            logger.error("!!! LINE 配額耗盡，啟動熔斷器 !!!")
        else:
            logger.error(f"Push 失敗: {e}")

class LineNotifier:
    """專門為 LINE 打造的通知模組"""
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def send_message(self, text: str):
        if "🎉 搶票成功" in text:
            msg = create_success_card(text)
        else:
            title = "⚠️ 系統通知" if "❌" in text or "⚠️" in text else "📢 狀態更新"
            color = DANGER_COLOR if "❌" in text or "⚠️" in text else THEME_COLOR
            card_dict = create_base_flex_card(title, [{"type": "text", "text": text, "wrap": True, "size": "sm"}])
            card_dict["header"]["backgroundColor"] = color
            msg = FlexMessage(alt_text=title, contents=FlexContainer.from_dict(card_dict))
        
        await safe_push(self.user_id, [msg])

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
    WAITING_FOR_SHIFT = "waiting_for_shift"
    WAITING_FOR_MANUAL_SHIFT_TIME = "waiting_for_manual_shift_time"
    # 台鐵專用狀態
    WAITING_FOR_TRA_ID = "waiting_for_tra_id"
    WAITING_FOR_TRA_PASSWORD = "waiting_for_tra_password"
    WAITING_FOR_TRA_SAVE_CHOICE = "waiting_for_tra_save_choice"
    WAITING_FOR_START_TIME = "waiting_for_start_time"
    WAITING_FOR_END_TIME = "waiting_for_end_time"

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
            "action": {"type": "message", "label": "🚌 和欣客運", "text": "客運:hohsin"},
            "style": "primary", "color": THEME_COLOR
        },
        {
            "type": "button",
            "action": {"type": "message", "label": "🚆 台灣鐵路", "text": "客運:tra"},
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

def create_times_quick_reply(selected_date: str, bus_type: str = "hohsin"):
    """建立時段選擇的 Quick Reply (根據業者區分時段)"""
    if bus_type == "hohsin":
        all_times = [
            ("00:00~03:00", "03:00"), ("03:00~06:00", "06:00"), 
            ("06:00~09:00", "09:00"), ("09:00~12:00", "12:00"),
            ("12:00~15:00", "15:00"), ("15:00~18:00", "18:00"), 
            ("18:00~21:00", "21:00"), ("21:00~23:59", "23:59")
        ]
    else:
        # 台鐵時段：採用台灣旅客最熟悉的四大區分，既簡潔又專業
        all_times = [
            ("🌅 凌晨 (00-06)", "06:00", "00:00~06:00"),
            ("☀️ 上午 (06-12)", "12:00", "06:00~12:00"),
            ("☕ 下午 (12-18)", "18:00", "12:00~18:00"),
            ("🌙 晚上 (18-24)", "23:59", "18:00~23:59"),
            ("📅 全天 (00-24)", "23:59", "00:00~23:59")
        ]
    
    # 強制獲取台灣時間
    tw_tz = pytz.timezone('Asia/Taipei')
    now_tw = datetime.now(tw_tz)
    
    is_today = selected_date == now_tw.strftime("%Y-%m-%d")
    
    # 截止基準：台灣現在 + 30 分鐘 (台鐵訂票較嚴格)
    deadline_time = now_tw + timedelta(minutes=30)
    deadline_str = deadline_time.strftime("%H:%M")

    items = []
    if bus_type == "hohsin":
        for display, end_time in all_times:
            if is_today and deadline_str > end_time: continue
            items.append(QuickReplyItem(action=MessageAction(label=display, text=f"時段:{display}")))
    else:
        for label, end_time, real_val in all_times:
            if is_today and deadline_str > end_time: continue
            items.append(QuickReplyItem(action=MessageAction(label=label, text=f"時段:{real_val}")))
    
    if not items:
        return None
        
    return QuickReply(items=items)

def create_precise_time_carousel(prefix: str, selected_date: str, min_time: str = "00:00"):
    """建立 30 分鐘一格的精確時間輪播選單"""
    tw_tz = pytz.timezone('Asia/Taipei')
    now_tw = datetime.now(tw_tz)
    is_today = selected_date == now_tw.strftime("%Y-%m-%d")
    
    # 截止基準：台灣現在 + 30 分鐘
    deadline_time = now_tw + timedelta(minutes=30)
    deadline_str = deadline_time.strftime("%H:%M")
    
    # 產生所有 30 分鐘間隔
    times = []
    h, m = 0, 0
    while h < 24:
        t_str = f"{h:02d}:{m:02d}"
        
        # 過濾邏輯
        skip = False
        if is_today and t_str < deadline_str:
            skip = True
        if t_str < min_time:
            skip = True
            
        if not skip:
            times.append(t_str)
            
        m += 30
        if m >= 60:
            m = 0
            h += 1
            
    if prefix == "結束" and (not is_today or "23:59" >= deadline_str): 
        times.append("23:59")

    bubbles = []
    chunk_size = 4
    for i in range(0, len(times), chunk_size):
        chunk = times[i:i + chunk_size]
        buttons = []
        for t in chunk:
            buttons.append({
                "type": "button",
                "action": {"type": "message", "label": t, "text": f"{prefix}:{t}"},
                "style": "secondary", "margin": "sm", "height": "sm"
            })
        
        bubble = create_base_flex_card(f"⏱️ 選擇{prefix}時間", buttons)
        bubbles.append(bubble)
        if len(bubbles) == 12: break # LINE 限制 12 張

    return FlexMessage(alt_text=f"請選擇{prefix}時間", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

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
        bus_type = "hohsin" if isinstance(m, HohsinMonitor) else "tra"
        bus_name = "和欣" if bus_type == "hohsin" else "台鐵"
        from_name = get_station_name(m.from_station, bus_type)
        to_name = get_station_name(m.to_station, bus_type)
        
        bubble = create_base_flex_card(f"📡 任務 #{i+1} ({bus_name})", [
            {"type": "text", "text": f"📍 {from_name} ➡️ {to_name}", "weight": "bold", "size": "sm"},
            {"type": "text", "text": f"📅 {m.travel_date}", "size": "xs"},
            {"type": "text", "text": f"⏰ {m.start_time}~{m.end_time}", "size": "xs"},
            {"type": "separator", "margin": "xs"},
            {"type": "text", "text": f"🔄 已嘗試：{m.attempt_count} 次", "size": "xxs", "color": "#aaaaaa"},
            {"type": "text", "text": f"⏱️ 最後檢查：{m.last_check_time or '準備中'}", "size": "xxs", "color": "#aaaaaa"}
        ], [
            {"type": "button", "action": {"type": "message", "label": "🛑 停止任務", "text": f"取消任務:{i}"}, "style": "secondary", "color": DANGER_COLOR, "height": "sm"}
        ])
        bubbles.append(bubble)
    return FlexMessage(alt_text="📡 您的任務清單", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

def create_shifts_carousel(schedules: List[Dict[str, Any]]):
    """建立班次選擇輪播卡片 (方案 B)"""
    bubbles = []
    # 每 4 個班次一組
    chunk_size = 4
    for i in range(0, len(schedules), chunk_size):
        chunk = schedules[i:i + chunk_size]
        buttons = []
        for s in chunk:
            raw_time = s.get("intoStationDepartureTime", "??:??")
            # 修正：如果時間包含 T (ISO 格式)，只取 HH:MM
            time = raw_time.split("T")[1][:5] if "T" in raw_time else raw_time
            vacant = s.get("vacantSeats", 0)
            schedule_id = s.get("dailyScheduleId")
            
            label = f"{time} ({'有票' if vacant > 0 else '無票'})"
            buttons.append({
                "type": "button",
                "action": {"type": "message", "label": label, "text": f"班次:{schedule_id}|{time}"},
                "style": "primary" if vacant > 0 else "secondary",
                "margin": "sm", "height": "sm"
            })
            
        bubble = create_base_flex_card("🚌 選擇特定班次", buttons)
        bubble["body"]["contents"].insert(0, {"type": "text", "text": "請選擇您要「精確監控」的班次：", "size": "xs", "color": "#666666"})
        
        # 加入手動輸入按鈕
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "button", "action": {"type": "message", "label": "⌨️ 找不到？手動輸入時間", "text": "班次:手動輸入"}, "style": "link", "color": "#666666"}
            ]
        }
        bubbles.append(bubble)
        if len(bubbles) == 12: break

    return FlexMessage(alt_text="🚌 選擇班次", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))

def create_confirm_cancel_quick_reply(idx: int):
    """建立停止任務的二次確認 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 是，確定停止", text=f"確認取消:是:{idx}")),
        QuickReplyItem(action=MessageAction(label="❌ 否，繼續監控", text="確認取消:否"))
    ])

def get_station_name(stn_id: str, bus_type: str = "hohsin") -> str:
    """根據業者類型獲取車站名稱"""
    if bus_type == "hohsin":
        for s in STATIONS_CACHE:
            if s["id"] == stn_id:
                return s["operatingName"]
    else:
        # 台鐵
        return TR_STATIONS.get(stn_id, stn_id)
    return stn_id

# --- FastAPI 路由 ---

@app.post("/callback")
async def callback(request: Request):
    """LINE Webhook 接收點"""
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        # 修正：標準 WebhookHandler 是同步的
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
    
    # 修正：如果已經選了精確班次，就不需要去切分 time_range，直接使用選定的班次時間
    if state.get("shift_time"):
        time_parts = [state["shift_time"], state["shift_time"]]
    else:
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
                    {"type": "text", "text": state.get("shift_time") or time_range, "size": "xs", "color": "#666666", "flex": 5}
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
            manual_seats=state.get("manual_seats"),
            target_schedule_id=state.get("target_schedule_id")
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
    
    # 同步到檔案
    save_tasks_to_file(running_tasks)
    
    async def run_and_cleanup():
        try: await monitor.run()
        finally:
            # 只有在任務非正在運行（即成功完成或手動停止）時才移除
            if not monitor.is_running:
                if user_id in running_tasks and monitor in running_tasks[user_id]:
                    running_tasks[user_id].remove(monitor)
                    save_tasks_to_file(running_tasks) # 更新檔案
    
    asyncio.create_task(run_and_cleanup())
    state["step"] = States.IDLE
    return FlexMessage(alt_text="🚀 任務啟動成功", contents=FlexContainer.from_dict(card_dict))

async def recover_all_tasks():
    """系統啟動時恢復所有監控任務"""
    saved_tasks = load_tasks_from_file()
    if not saved_tasks:
        return
    
    logger.info(f"偵測到 {len(saved_tasks)} 個未完成任務，正在準備恢復...")
    
    for task in saved_tasks:
        user_id = task["user_id"]
        bus_type = task["bus_type"]
        p = task["params"]
        
        # 準備狀態與參數
        try:
            if bus_type == "hohsin":
                monitor = HohsinMonitor(
                    from_station=p["from_station"],
                    to_station=p["to_station"],
                    travel_date=p["travel_date"],
                    start_time=p["start_time"],
                    end_time=p["end_time"],
                    notifier=LineNotifier(user_id),
                    user_phone=p["user_phone"],
                    user_password=p["user_password"],
                    manual_seats=p.get("manual_seats"),
                    target_schedule_id=p.get("target_schedule_id")
                )
            else:
                monitor = TaiwanRailwayMonitor(
                    from_station=p["from_station"],
                    to_station=p["to_station"],
                    travel_date=p["travel_date"],
                    start_time=p["start_time"],
                    end_time=p["end_time"],
                    notifier=LineNotifier(user_id),
                    user_id_no=p["user_id_no"],
                    user_password=p["user_password"]
                )
            
            monitor.num_tickets = p.get("num_tickets", 1)
            monitor.attempt_count = task.get("attempt_count", 0)
            monitor.last_check_time = task.get("last_check_time")
            
            if user_id not in running_tasks: running_tasks[user_id] = []
            running_tasks[user_id].append(monitor)
            
            async def run_and_cleanup_internal(m=monitor, uid=user_id):
                try: await m.run()
                finally:
                    if not m.is_running:
                        if uid in running_tasks and m in running_tasks[uid]:
                            running_tasks[uid].remove(m)
                            save_tasks_to_file(running_tasks)

            asyncio.create_task(run_and_cleanup_internal())
        except Exception as e:
            logger.error(f"恢復任務時發生錯誤 ({bus_type}): {e}")

    logger.info("所有任務恢復指令已發出。")

async def handle_my_tickets(user_id: str, reply_token: str):
    """獲取並顯示使用者的最近車票。"""
    users = load_users()
    if user_id not in users:
        await safe_reply(reply_token, [TextMessage(text="請先點擊『開始搶票』並完成一次查詢，讓系統記錄您的帳號。")], user_id)
        return

    user_info = users[user_id]
    # 修正：根據實例結構，帳密可能存在 'hohsin' 鍵值下或直接在 user_info 下
    hohsin_creds = user_info.get("hohsin", user_info)
    phone = hohsin_creds.get("phone") or hohsin_creds.get("username")
    password = hohsin_creds.get("password")

    if not phone or not password:
        await safe_reply(reply_token, [TextMessage(text="找不到您的和欣帳密。請先執行『開始搶票』並選擇和欣來輸入帳號。")], user_id)
        return

    api = HohsinAPI()
    try:
        if await api.login(phone, password):
            orders = await api.get_my_orders()
            if not orders:
                await safe_reply(reply_token, [TextMessage(text="📭 您目前沒有進行中的訂單。")], user_id)
                return

            # 只顯示前 10 筆已付款且未搭乘的票
            ticket_bubbles = []
            for order in orders[:5]:
                for t in order.get("tickets", []):
                    # 過濾出有效的票 (含付款、取票、驗票中)
                    status = t.get("xActionDescription", "")
                    if any(x in status for x in ["付款", "取票", "驗票"]):
                        departure = t.get("intoStationDepartureTime", "").replace("T", " ")
                        bubble = {
                            "type": "bubble",
                            "size": "mega",
                            "header": {
                                "type": "box", "layout": "vertical", "contents": [
                                    {"type": "text", "text": "🎫 和欣客運 電子車票", "weight": "bold", "color": "#FFFFFF", "size": "sm"}
                                ], "backgroundColor": "#00B900"
                            },
                            "body": {
                                "type": "box", "layout": "vertical", "contents": [
                                    {"type": "text", "text": f"{t.get('intoStationOperatingName')} → {t.get('outofStationOperatingName')}", "weight": "bold", "size": "xl"},
                                    {"type": "text", "text": f"時間：{departure}", "size": "sm", "color": "#666666", "margin": "md"},
                                    {"type": "text", "text": f"座號：{t.get('seatNo')} 號 ({t.get('cabinLevel')})", "size": "sm", "color": "#666666"},
                                    {"type": "separator", "margin": "lg"},
                                    {"type": "text", "text": f"車票編號：{t.get('ticketNo')}", "size": "xs", "color": "#999999", "margin": "md"}
                                ]
                            },
                            "footer": {
                                "type": "box", "layout": "vertical", "contents": [
                                    {
                                        "type": "button",
                                        "action": {
                                            "type": "postback",
                                            "label": "顯示 QR Code",
                                            "data": f"action=show_qrcode&ticket_no={t.get('ticketNo')}&ticket_id={t.get('id')}"
                                        },
                                        "style": "primary", "color": "#00B900"
                                    }
                                ]
                            }
                        }
                        ticket_bubbles.append(bubble)

            if not ticket_bubbles:
                await safe_reply(reply_token, [TextMessage(text="找不到已付款的有效車票。")], user_id)
            else:
                carousel = {"type": "carousel", "contents": ticket_bubbles[:10]}
                await safe_reply(reply_token, [FlexMessage(alt_text="您的車票列表", contents=FlexContainer.from_dict(carousel))], user_id)
        else:
            await safe_reply(reply_token, [TextMessage(text="和欣登入失敗，請檢查帳密設定。")], user_id)
    except Exception as e:
        logger.error(f"獲取車票發生錯誤: {e}")
        await safe_reply(reply_token, [TextMessage(text=f"系統錯誤：{str(e)}")], user_id)
    finally:
        await api.close()

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    logger.info(f"收到來自 {user_id} 的訊息: {text}")
    
    async def process_msg():
        # 2. 查詢車票指令
        if text in ["我的車票", "查詢車票", "ticket", "Ticket"]:
            await handle_my_tickets(user_id, event.reply_token)
            return

        # 初始化狀態
        if user_id not in user_states:
            user_states[user_id] = {"step": States.IDLE}
            
        state = user_states[user_id]
        users = load_users()

        # 1. 啟動指令
        if text in ["搶票", "/start", "開始"]:
            state["step"] = States.WAITING_FOR_BUS
            await safe_reply(event.reply_token, [create_bus_card()])
            return

        # 1.5 查詢/取消任務
        if text in ["查詢", "查詢任務", "取消"]:
            tasks = running_tasks.get(user_id, [])
            if not tasks:
                reply = FlexMessage(alt_text="無進行中任務", contents=FlexContainer.from_dict(create_base_flex_card("📡 任務管理", [{"type": "text", "text": "您目前沒有正在執行的搶票任務。", "size": "sm"}])))
                await safe_reply(event.reply_token, [reply], user_id)
                return
            await safe_reply(event.reply_token, [create_task_list_carousel(tasks)], user_id)
            return



        # 1.6 發起取消確認
        if text.startswith("取消任務:"):
            idx = int(text.split(":", 1)[1])
            if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
                m = running_tasks[user_id][idx]
                bus_type = "hohsin" if isinstance(m, HohsinMonitor) else "tra"
                from_name = get_station_name(m.from_station, bus_type)
                to_name = get_station_name(m.to_station, bus_type)
                
                info_text = f"📍 路線：{from_name} ➡️ {to_name}\n📅 日期：{m.travel_date}\n⏰ 時段：{m.start_time}~{m.end_time}"
                confirm_msg = TextMessage(
                    text=f"⚠️ 您確定要「停止」此監控任務嗎？\n\n{info_text}",
                    quick_reply=create_confirm_cancel_quick_reply(idx)
                )
                await safe_reply(event.reply_token, [confirm_msg], user_id)
            else:
                await safe_reply(event.reply_token, [TextMessage(text="❌ 找不到該任務，可能已被系統自動終止。")], user_id)
            return

        # 1.7 執行最終取消
        if text.startswith("確認取消:是:"):
            idx = int(text.split(":", 2)[2])
            if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
                m = running_tasks[user_id].pop(idx)
                m.stop() 
                save_tasks_to_file(running_tasks)
                bus_type = "hohsin" if isinstance(m, HohsinMonitor) else "tra"
                from_name = get_station_name(m.from_station, bus_type)
                to_name = get_station_name(m.to_station, bus_type)
                reply = FlexMessage(alt_text="任務已停止", contents=FlexContainer.from_dict(create_base_flex_card("🛑 停止成功", [{"type": "text", "text": f"已成功停止：\n{m.travel_date} {from_name}➡️{to_name}", "wrap": True, "size": "sm"}])))
                await safe_reply(event.reply_token, [reply], user_id)
            else:
                await safe_reply(event.reply_token, [TextMessage(text="⚠️ 停止失敗：找不到該任務，可能已由系統完成或已手動移除。")], user_id)
            return

        if text == "確認取消:否":
            await safe_reply(event.reply_token, [TextMessage(text="👌 好的，監控將繼續執行！")], user_id)
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
                await safe_reply(event.reply_token, [create_login_hint_card(bus_name, masked_id)])
            else:
                state["step"] = States.WAITING_FOR_PHONE if bus_type == "hohsin" else States.WAITING_FOR_TRA_ID
                await safe_reply(event.reply_token, [create_login_hint_card(bus_name)])
            return

        # 2.1 選擇是否使用儲存帳密
        if state["step"] == States.WAITING_FOR_CREDENTIAL_CHOICE and text.startswith("帳密:"):
            bus_type = state["bus"]
            if text == "帳密:使用儲存":
                creds = users[user_id][bus_type]
                state["phone"] = creds.get("phone") or creds.get("username")
                state["password"] = creds["password"]
                state["step"] = States.WAITING_FOR_ROUTE_CHOICE
                favs = users.get(user_id, {}).get(f"favorites_{bus_type}", [])
                await safe_reply(event.reply_token, [create_route_choice_card(bool(favs))])
            else:
                state["step"] = States.WAITING_FOR_PHONE if bus_type == "hohsin" else States.WAITING_FOR_TRA_ID
                label = "和欣手機" if bus_type == "hohsin" else "身分證字號"
                contents = [{"type": "text", "text": f"請輸入新的【{label}】：", "size": "sm"}]
                await safe_reply(event.reply_token, [FlexMessage(alt_text="重新輸入", contents=FlexContainer.from_dict(create_base_flex_card("🔒 帳號重設", contents)))])
            return

        # 2.2 輸入手機 (和欣)
        if state["step"] == States.WAITING_FOR_PHONE:
            state["phone"] = text
            state["step"] = States.WAITING_FOR_PASSWORD
            contents = [{"type": "text", "text": f"📱 手機：{text}\n\n請輸入【和欣客運密碼】：", "wrap": True, "size": "sm"}]
            await safe_reply(event.reply_token, [FlexMessage(alt_text="輸入密碼", contents=FlexContainer.from_dict(create_base_flex_card("🔒 密碼設定", contents)))])
            return

        # 2.2.1 輸入身分證 (台鐵)
        if state["step"] == States.WAITING_FOR_TRA_ID:
            state["phone"] = text.upper() # 台鐵身分證轉大寫
            state["step"] = States.WAITING_FOR_TRA_PASSWORD
            contents = [{"type": "text", "text": f"🆔 身分證：{state['phone']}\n\n請輸入【台鐵會員密碼】：", "wrap": True, "size": "sm"}]
            await safe_reply(event.reply_token, [FlexMessage(alt_text="輸入密碼", contents=FlexContainer.from_dict(create_base_flex_card("🔒 鐵路密碼", contents)))])
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
            await safe_reply(event.reply_token, [FlexMessage(alt_text="記憶選擇", contents=FlexContainer.from_dict(create_base_flex_card("💾 隱私設定", contents, footer)))])
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
            favs = users.get(user_id, {}).get(f"favorites_{bus_type}", [])
            await safe_reply(event.reply_token, [create_route_choice_card(bool(favs))])
            return

        # 2.5 選擇路線方式
        if state["step"] == States.WAITING_FOR_ROUTE_CHOICE:
            bus_type = state.get("bus", "hohsin")
            if text == "路線:常用":
                favs = users.get(user_id, {}).get(f"favorites_{bus_type}", [])
                if not favs:
                    await safe_reply(event.reply_token, [create_route_choice_card(False)])
                else:
                    await safe_reply(event.reply_token, [create_favorites_carousel(favs)])
                return
            elif text == "路線:全新":
                state["step"] = States.WAITING_FOR_FROM
                if bus_type == "hohsin":
                    await init_stations()
                    await safe_reply(event.reply_token, [create_stations_carousel(STATIONS_CACHE, "上車")])
                else:
                    # 台鐵車站選擇：將字典轉為 list
                    tr_list = [{"id": k, "operatingName": v} for k, v in TR_STATIONS.items()]
                    # 只取前面比較熱門的幾張卡片
                    await safe_reply(event.reply_token, [create_stations_carousel(tr_list, "上車")])
                return

        # 2.6 選擇常用路線
        if state["step"] == States.WAITING_FOR_ROUTE_CHOICE and text.startswith("常用路線:"):
            bus_type = state.get("bus", "hohsin")
            idx = int(text.split(":")[1])
            fav = users[user_id][f"favorites_{bus_type}"][idx]
            state.update({"from_stn": fav["from"], "to_stn": fav["to"], "from_stn_name": fav["name"].split("-")[0], "to_stn_name": fav["name"].split("-")[1], "is_favorite_route": True, "step": States.WAITING_FOR_DATE})
            
            contents = [{"type": "text", "text": f"⭐ 已選常用路線：\n{fav['name']}\n\n請點擊下方按鈕選擇乘車日期。", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇日期", 
                contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)),
                quick_reply=create_date_picker_quick_reply()
            )
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 2.7 刪除常用路線
        if state["step"] == States.WAITING_FOR_ROUTE_CHOICE and text.startswith("刪除路線:"):
            bus_type = state.get("bus", "hohsin")
            fav_key = f"favorites_{bus_type}"
            idx = int(text.split(":")[1])
            if user_id in users and fav_key in users[user_id] and 0 <= idx < len(users[user_id][fav_key]):
                removed = users[user_id][fav_key].pop(idx)
                save_users(users)
                msg = f"✅ 已刪除常用路線：\n{removed['name']}"
            else:
                msg = "❌ 刪除失敗，找不到該路線。"
            
            await safe_reply(event.reply_token, [TextMessage(text=msg)])
            # 重新顯示常用清單
            favs = users.get(user_id, {}).get(fav_key, [])
            if not favs:
                await safe_reply(event.reply_token, [create_route_choice_card(False)])
            else:
                await safe_reply(event.reply_token, [create_favorites_carousel(favs)])
            return

        # 3. 選擇上車站
        if state["step"] == States.WAITING_FOR_FROM and text.startswith("上車:"):
            stn_id = text.split(":")[1]
            bus_type = state.get("bus", "hohsin")
            state.update({"from_stn": stn_id, "from_stn_name": get_station_name(stn_id, bus_type), "is_favorite_route": False, "step": States.WAITING_FOR_TO})
            
            if bus_type == "hohsin":
                await safe_reply(event.reply_token, [create_stations_carousel(STATIONS_CACHE, "下車")])
            else:
                tr_list = [{"id": k, "operatingName": v} for k, v in TR_STATIONS.items()]
                await safe_reply(event.reply_token, [create_stations_carousel(tr_list, "下車")])
            return

        # 4. 選擇下車站
        if state["step"] == States.WAITING_FOR_TO and text.startswith("下車:"):
            stn_id = text.split(":")[1]
            bus_type = state.get("bus", "hohsin")
            state.update({"to_stn": stn_id, "to_stn_name": get_station_name(stn_id, bus_type), "step": States.WAITING_FOR_DATE})
            
            contents = [{"type": "text", "text": f"📍 路線：{state['from_stn_name']} ➡️ {state['to_stn_name']}\n\n請點擊下方按鈕選擇乘車日期。", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇日期", 
                contents=FlexContainer.from_dict(create_base_flex_card("📅 日期設定", contents)),
                quick_reply=create_date_picker_quick_reply()
            )
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 6.1 台鐵：選擇出發時間
        if state["step"] == States.WAITING_FOR_START_TIME and text.startswith("出發:"):
            start_t = text.split(":", 1)[1]
            state.update({"start_time": start_t, "step": States.WAITING_FOR_END_TIME})
            card = create_precise_time_carousel("結束", state["date"], min_time=start_t)
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 6.2 台鐵：選擇結束時間
        if state["step"] == States.WAITING_FOR_END_TIME and text.startswith("結束:"):
            end_t = text.split(":", 1)[1]
            state.update({
                "end_time": end_t,
                "time_range": f"{state['start_time']}~{end_t}",
                "step": States.WAITING_FOR_COUNT
            })
            contents = [{"type": "text", "text": f"⏰ 已選時段：{state['time_range']}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇張數", 
                contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)),
                quick_reply=create_ticket_count_quick_reply()
            )
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 6. 選擇時段 (原和欣流程 -> 改為方案 B 班次選擇 + 班表補完)
        if state["step"] == States.WAITING_FOR_TIME and text.startswith("時段:"):
            state["time_range"] = text[3:]
            time_parts = state["time_range"].split("~")
            
            async def fetch_full_shifts_and_reply():
                try:
                    # 1. 取得今日即時有票的班次
                    realtime_schedules = await global_api.get_schedules(
                        state["from_stn"], state["to_stn"], state["date"], time_parts[0], time_parts[1]
                    )
                    
                    # 2. 取得未來參考班次 (假設 7 天後一定有參考價值)
                    orig_date = datetime.strptime(state["date"], "%Y-%m-%d")
                    ref_date = (orig_date + timedelta(days=7)).strftime("%Y-%m-%d")
                    ref_schedules = await global_api.get_schedules(
                        state["from_stn"], state["to_stn"], ref_date, time_parts[0], time_parts[1]
                    )
                    
                    # 3. 合併班次 (以時間為 Key)
                    # 結構: { "18:15": { "dailyScheduleId": 123, "vacantSeats": 5, "is_real": True } }
                    merged = {}
                    
                    # 先填入參考班次 (預設客滿)
                    for s in ref_schedules:
                        t = s.get("intoStationDepartureTime", "").split("T")[1][:5]
                        merged[t] = {
                            "id": s.get("dailyScheduleId"),
                            "time": t,
                            "vacant": 0,
                            "is_real": False
                        }
                    
                    # 用即時資料覆蓋或更新
                    for s in realtime_schedules:
                        t = s.get("intoStationDepartureTime", "").split("T")[1][:5]
                        merged[t] = {
                            "id": s.get("dailyScheduleId"),
                            "time": t,
                            "vacant": s.get("vacantSeats", 0),
                            "is_real": True
                        }
                    
                    # 排序
                    final_list = sorted(merged.values(), key=lambda x: x["time"])

                    if not final_list:
                        # 如果真的連參考都抓不到
                        contents = [{"type": "text", "text": f"⚠️ 該時段 ({state['time_range']}) 查無班次資訊。", "wrap": True, "size": "sm"}]
                        footer = [{"type": "button", "action": {"type": "message", "label": "⌨️ 手動輸入精確時間", "text": "班次:手動輸入"}, "style": "primary", "color": THEME_COLOR}]
                        await safe_reply(event.reply_token, [FlexMessage(alt_text="無班次", contents=FlexContainer.from_dict(create_base_flex_card("🚌 班次提醒", contents, footer)))])
                        return

                    # 4. 建立輪播選單
                    bubbles = []
                    chunk_size = 4
                    for i in range(0, len(final_list), chunk_size):
                        chunk = final_list[i:i + chunk_size]
                        buttons = []
                        for s in chunk:
                            label = f"{s['time']} ({'有票' if s['vacant'] > 0 else '客滿'})"
                            # 如果是即時有的，傳 ID；如果是補位的，傳時間
                            val = f"{s['id']}|{s['time']}" if s['is_real'] else f"手動|{s['time']}"
                            buttons.append({
                                "type": "button",
                                "action": {"type": "message", "label": label, "text": f"班次:{val}"},
                                "style": "primary" if s['vacant'] > 0 else "secondary",
                                "margin": "sm", "height": "sm"
                            })
                        bubble = create_base_flex_card("🚌 選擇班次", buttons)
                        bubble["body"]["contents"].insert(0, {"type": "text", "text": "綠色可直接訂，灰色將開啟死守監控：", "size": "xs", "color": "#666666"})
                        bubbles.append(bubble)

                    state["step"] = States.WAITING_FOR_SHIFT
                    await safe_reply(event.reply_token, [FlexMessage(alt_text="選擇班次", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))])

                except Exception as e:
                    logger.error(f"獲取完整班次失敗: {e}")
                    await safe_reply(event.reply_token, [TextMessage(text=f"❌ 查詢發生錯誤: {e}")])

            asyncio.create_task(fetch_full_shifts_and_reply())
            return

        # 6.3 處理班次選擇 (方案 B)
        if state["step"] == States.WAITING_FOR_SHIFT and text.startswith("班次:"):
            choice = text.split(":", 1)[1]
            
            if choice == "手動輸入":
                state["step"] = States.WAITING_FOR_MANUAL_SHIFT_TIME
                contents = [{"type": "text", "text": "⌨️ 請輸入您要監控的【精確時間】。\n(例如: 10:15)", "wrap": True, "size": "sm", "weight": "bold"}]
                await safe_reply(event.reply_token, [FlexMessage(alt_text="輸入時間", contents=FlexContainer.from_dict(create_base_flex_card("⌨️ 時間輸入", contents)))])
                return

            parts = choice.split("|")
            # 修正：處理 '手動|17:50' 這種格式
            sched_id = None if parts[0] == "手動" else int(parts[0])
            state.update({
                "target_schedule_id": sched_id,
                "shift_time": parts[1],
                "step": States.WAITING_FOR_COUNT
            })
            
            # 關鍵修正：如果是建議班次（無 ID），則將時間區間縮小到該分鐘，實現精確監控
            if sched_id is None:
                state["time_range"] = f"{state['shift_time']}~{state['shift_time']}"
            
            contents = [{"type": "text", "text": f"⏰ 已選班次：{state['shift_time']}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇張數", 
                contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)),
                quick_reply=create_ticket_count_quick_reply()
            )
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 6.4 處理手動班次時間輸入
        if state["step"] == States.WAITING_FOR_MANUAL_SHIFT_TIME:
            # 簡單驗證時間格式 HH:MM
            import re
            if not re.match(r"^\d{2}:\d{2}$", text):
                await safe_reply(event.reply_token, [TextMessage(text="❌ 格式錯誤，請使用 HH:MM (例如 10:15)")])
                return
                
            state.update({
                "shift_time": text,
                "time_range": f"{text}~{text}", # 精確鎖定該時間
                "target_schedule_id": None,     # 清除特定 ID，改用時間鎖定
                "step": States.WAITING_FOR_COUNT
            })
            
            contents = [{"type": "text", "text": f"⏰ 已設定精確監控：{text}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
            card = FlexMessage(
                alt_text="選擇張數", 
                contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)),
                quick_reply=create_ticket_count_quick_reply()
            )
            await safe_reply(event.reply_token, [card], user_id)
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
            await safe_reply(event.reply_token, [card], user_id)
            return

        # 8. 選擇選位模式
        if state["step"] == States.WAITING_FOR_SEAT_MODE and text.startswith("選位:"):
            if text == "選位:自動":
                state.update({"seat_mode": "auto", "manual_seats": None})
            else:
                state["step"] = States.WAITING_FOR_MANUAL_SEATS
                contents = [{"type": "text", "text": "⌨️ 請於下方輸入您指定的座號。\n(多個請用逗號隔開，例如: 5 或 1,2)", "wrap": True, "size": "sm", "weight": "bold"}]
                await safe_reply(event.reply_token, [FlexMessage(alt_text="輸入座號", contents=FlexContainer.from_dict(create_base_flex_card("⌨️ 座號輸入", contents)))])
                return

            if state.get("is_favorite_route"):
                await safe_reply(event.reply_token, [start_monitor_task(user_id, state, users)])
            else:
                state["step"] = States.WAITING_FOR_SAVE_ROUTE
                contents = [{"type": "text", "text": "🤖 已選擇自動選位。\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
                card = FlexMessage(
                    alt_text="存為常用", 
                    contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)),
                    quick_reply=create_save_route_quick_reply()
                )
                await safe_reply(event.reply_token, [card], user_id)
            return

        # 9. 輸入手動座號
        if state["step"] == States.WAITING_FOR_MANUAL_SEATS:
            try:
                seats = [int(s.strip()) for s in text.replace("，", ",").split(",")]
                state.update({"manual_seats": seats, "seat_mode": "manual"})
                if state.get("is_favorite_route"):
                    await safe_reply(event.reply_token, [start_monitor_task(user_id, state, users)])
                else:
                    state["step"] = States.WAITING_FOR_SAVE_ROUTE
                    contents = [{"type": "text", "text": f"✅ 已指定座位：{seats}\n\n最後，是否將此路線存為常用？", "wrap": True, "size": "sm"}]
                    card = FlexMessage(
                        alt_text="存為常用", 
                        contents=FlexContainer.from_dict(create_base_flex_card("💾 路線儲存", contents)),
                        quick_reply=create_save_route_quick_reply()
                    )
                    await safe_reply(event.reply_token, [card], user_id)
            except:
                contents = [{"type": "text", "text": "❌ 格式錯誤！\n請重新輸入數字（例如: 5 或 1,2）：", "color": DANGER_COLOR, "size": "sm"}]
                await safe_reply(event.reply_token, [FlexMessage(alt_text="格式錯誤", contents=FlexContainer.from_dict(create_base_flex_card("⚠️ 輸入錯誤", contents)))])
            return

        # 10. 儲存常用路線並啟動監控
        if state["step"] == States.WAITING_FOR_SAVE_ROUTE and text.startswith("存路線:"):
            bus_type = state.get("bus", "hohsin")
            fav_key = f"favorites_{bus_type}"
            if text == "存路線:是":
                if user_id not in users: users[user_id] = {}
                if fav_key not in users[user_id]: users[user_id][fav_key] = []
                if not any(f["from"] == state["from_stn"] and f["to"] == state["to_stn"] for f in users[user_id][fav_key]):
                    users[user_id][fav_key].append({"from": state["from_stn"], "to": state["to_stn"], "name": f"{state['from_stn_name']}-{state['to_stn_name']}"})
                    save_users(users)
            await safe_reply(event.reply_token, [start_monitor_task(user_id, state, users)])
            return

        # 💬 阿龜 AI 聊天（落空分支）：以上搶票指令／流程都沒對到 → 交給阿龜自然對話／回答問題。
        # 需要文字輸入的步驟（手機/密碼/座號/身分證）都在前面就 return 了，
        # 所以這裡不會攔到密碼、也不會打斷搶票流程。
        try:
            reply_text = await ai_reply(user_id, text)
        except Exception as e:
            logger.error(f"阿龜聊天失敗: {e}")
            reply_text = "抱歉，我剛剛恍神了一下，再說一次好嗎？"
        await safe_reply(event.reply_token, [TextMessage(text=reply_text)], user_id)
        return

    # 正式排程執行非同步邏輯
    asyncio.create_task(process_msg())


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    """使用者傳圖片 → 阿龜用視覺模型看懂／OCR，回覆說明。"""
    user_id = event.source.user_id
    msg_id = event.message.id

    async def process_img():
        try:
            from src.ai_tools import vision_describe
            image_bytes = await line_bot_blob.get_message_content(message_id=msg_id)
            desc = await vision_describe(bytes(image_bytes))
        except Exception as e:
            logger.error(f"看圖失敗: {e}")
            desc = "抱歉，我剛剛看圖的時候恍神了，再傳一次好嗎？"
        await safe_reply(event.reply_token, [TextMessage(text=desc)], user_id)

    asyncio.create_task(process_img())


@handler.add(PostbackEvent)
def handle_postback(event):
    """處理 LINE Postback 事件 (包含日期選擇與 QR Code 生成)"""
    user_id = event.source.user_id
    data = event.postback.data
    
    async def process_postback():
        # 處理 QR Code 請求
        if data.startswith("action=show_qrcode"):
            from urllib.parse import parse_qs
            params = parse_qs(data)
            ticket_no = params.get("ticket_no", [None])[0]
            ticket_id = params.get("ticket_id", [None])[0]
            
            if not ticket_no or not ticket_id:
                await safe_reply(event.reply_token, [TextMessage(text="❌ 無法獲取車票資訊。")], user_id)
                return

            logger.info(f"收到 QR Code 生成請求: 票號 {ticket_no}, ID {ticket_id}")

            try:
                static_dir = os.path.join(os.getcwd(), "static", "qrcodes")
                os.makedirs(static_dir, exist_ok=True)
                
                qr_filename = f"official_{ticket_no}.png"
                qr_path = os.path.join(static_dir, qr_filename)
                
                is_official = False
                if not os.path.exists(qr_path):
                    api = HohsinAPI()
                    users = load_users()
                    user_info = users.get(user_id, {})
                    hohsin_creds = user_info.get("hohsin", user_info)
                    phone = hohsin_creds.get("phone") or hohsin_creds.get("username")
                    password = hohsin_creds.get("password")
                    
                    if await api.login(phone, password):
                        qr_bytes = await api.get_resilient_qrcode(int(ticket_id))
                        if qr_bytes:
                            with open(qr_path, "wb") as f: f.write(qr_bytes)
                            is_official = True
                        else:
                            url_legacy = f"https://www.ebus.com.tw/etc/QRCode/10?TicketNo={ticket_no}"
                            resp = await api.client.get(url_legacy)
                            if resp.status_code == 200:
                                with open(qr_path, "wb") as f: f.write(resp.content)
                                is_official = True
                            else:
                                raise Exception("官方 QR 暫時無法取得")
                        await api.close()
                    else:
                        await api.close()
                        raise Exception("登入失敗")

                base_url = "https://my-hohsin-bot.duckdns.org"
                image_url = f"{base_url}/static/qrcodes/{qr_filename}?t={int(datetime.now().timestamp())}"
                status_text = "✅ QR 圖檔已取得"
                await safe_push(user_id, [
                    TextMessage(text=f"{status_text}\n編號：{ticket_no}"),
                    ImageMessage(original_content_url=image_url, preview_image_url=image_url)
                ])
            except Exception as e:
                logger.error(f"QR 失敗: {e}")
                await safe_push(user_id, [TextMessage(text=f"❌ 獲取失敗：{str(e)}")])
            return

        if user_id not in user_states: return
        state = user_states[user_id]
        
        if state["step"] == States.WAITING_FOR_DATE and event.postback.data == "action=select_date":
            selected_date = event.postback.params['date']
            state["date"] = selected_date
            bus_type = state.get("bus", "hohsin")
            if bus_type == "tra":
                card = create_precise_time_carousel("出發", selected_date)
                await safe_reply(event.reply_token, [card], user_id)
            else:
                times_qr = create_times_quick_reply(selected_date, bus_type)
                if times_qr:
                    contents = [{"type": "text", "text": f"📅 已選日期：{selected_date}\n\n最後一步，請選擇乘車時段：", "wrap": True, "size": "sm"}]
                    card = FlexMessage(alt_text="選擇時段", contents=FlexContainer.from_dict(create_base_flex_card("⏰ 時段設定", contents)), quick_reply=times_qr)
                else:
                    contents = [{"type": "text", "text": f"📅 日期：{selected_date}\n⚠️ 時段皆已截止。", "wrap": True, "size": "sm", "color": DANGER_COLOR}]
                    card = FlexMessage(alt_text="無可用時段", contents=FlexContainer.from_dict(create_base_flex_card("⚠️ 無可用時段", contents)), quick_reply=create_date_picker_quick_reply())
                await safe_reply(event.reply_token, [card], user_id)

    # 排程執行（只排一次；先前重複排了兩次會導致每個 postback 執行兩遍）
    asyncio.create_task(process_postback())


# --- 啟動設定 ---
@app.on_event("startup")
async def startup_event():
    # 預先載入車站清單
    await init_stations()
    # 恢復之前的任務
    await recover_all_tasks()
    # 啟動阿龜的提醒排程（到時間主動 push）
    try:
        from src.ai_tools import reminder_loop
        asyncio.create_task(reminder_loop())
    except Exception as e:
        logger.error(f"提醒排程啟動失敗: {e}")
    logger.info("LINE Bot 伺服器已啟動！")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("正在關閉伺服器，同步運行中的任務...")
    save_tasks_to_file(running_tasks)
    logger.info("任務同步完成。")

if __name__ == "__main__":
    import uvicorn
    # 本地測試時可以使用 ngrok 將 8000 port 對外暴露
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    print("這是一個 FastAPI 應用，請使用以下指令啟動：")
    print("uvicorn line_bot:app --host 0.0.0.0 --port 8000")
