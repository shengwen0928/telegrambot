import asyncio
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.hohsin_api import HohsinAPI
from src.monitor import HohsinMonitor

# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("TGBot")

# 讀取環境變數
load_dotenv()
BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("錯誤：未設定 TG_BOT_TOKEN")
    exit(1)

# 初始化 Bot 與 Dispatcher
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 為了防止重複初始化 API 造成太多連線，我們建立一個全域的 API 實例來抓車站清單
# 實際搶票時 monitor 內部會自己開一個獨立的
global_api = HohsinAPI()
STATIONS_CACHE = []

# 定義狀態機
class BookingFlow(StatesGroup):
    waiting_for_bus = State()
    waiting_for_from = State()
    waiting_for_to = State()
    waiting_for_date = State()
    waiting_for_time = State()

# --- 輔助函式：建立 Inline Keyboard ---

def create_bus_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚌 和欣客運", callback_data="bus_hohsin")]
    ])
    return keyboard

def create_stations_keyboard(stations, prefix=""):
    """動態建立車站多列表單 (3欄)"""
    buttons = []
    row = []
    for s in stations:
        # callback_data 格式: from_G03 或 to_B01
        row.append(InlineKeyboardButton(text=s["operatingName"], callback_data=f"{prefix}_{s['id']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def create_dates_keyboard():
    """建立未來 16 天的日期選單 (4x4)"""
    buttons = []
    row = []
    today = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    
    for i in range(16):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        display_str = f"{target_date.strftime('%m/%d')} ({weekdays[target_date.weekday()]})"
        
        row.append(InlineKeyboardButton(text=display_str, callback_data=f"date_{date_str}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def create_times_keyboard():
    """建立和欣標準的 8 個時段選單"""
    times = [
        ("00:00~03:00", "00:00_03:00"),
        ("03:00~06:00", "03:00_06:00"),
        ("06:00~09:00", "06:00_09:00"),
        ("09:00~12:00", "09:00_12:00"),
        ("12:00~15:00", "12:00_15:00"),
        ("15:00~18:00", "15:00_18:00"),
        ("18:00~21:00", "18:00_21:00"),
        ("21:00~23:59", "21:00_23:59")
    ]
    buttons = []
    row = []
    for display, val in times:
        row.append(InlineKeyboardButton(text=display, callback_data=f"time_{val}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- 處理流程 ---

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """處理 /start 指令，啟動選單"""
    await state.clear()
    await message.answer("歡迎使用自動搶票機器人！\n請選擇您要搶票的客運：", reply_markup=create_bus_keyboard())
    await state.set_state(BookingFlow.waiting_for_bus)

@dp.callback_query(BookingFlow.waiting_for_bus, F.data.startswith("bus_"))
async def process_bus(callback: CallbackQuery, state: FSMContext):
    """處理客運選擇"""
    # 目前只有和欣
    global STATIONS_CACHE
    if not STATIONS_CACHE:
        await callback.message.answer("正在載入車站清單，請稍候...")
        STATIONS_CACHE = await global_api.get_stations()

    await callback.message.edit_text("🚌 已選擇：和欣客運\n\n請問您的 **上車站** 是哪裡？", reply_markup=create_stations_keyboard(STATIONS_CACHE, "from"))
    await state.set_state(BookingFlow.waiting_for_from)

@dp.callback_query(BookingFlow.waiting_for_from, F.data.startswith("from_"))
async def process_from_station(callback: CallbackQuery, state: FSMContext):
    """處理上車站選擇"""
    station_id = callback.data.split("_")[1]
    station_name = next((s["operatingName"] for s in STATIONS_CACHE if s["id"] == station_id), station_id)
    
    await state.update_data(from_station_id=station_id, from_station_name=station_name)
    
    await callback.message.edit_text(f"📍 上車站：{station_name}\n\n請問您的 **下車站** 是哪裡？", reply_markup=create_stations_keyboard(STATIONS_CACHE, "to"))
    await state.set_state(BookingFlow.waiting_for_to)

@dp.callback_query(BookingFlow.waiting_for_to, F.data.startswith("to_"))
async def process_to_station(callback: CallbackQuery, state: FSMContext):
    """處理下車站選擇"""
    station_id = callback.data.split("_")[1]
    station_name = next((s["operatingName"] for s in STATIONS_CACHE if s["id"] == station_id), station_id)
    
    data = await state.get_data()
    from_name = data["from_station_name"]
    
    await state.update_data(to_station_id=station_id, to_station_name=station_name)
    
    await callback.message.edit_text(f"📍 路線：{from_name} ➡️ {station_name}\n\n請問您要哪一天的車票？", reply_markup=create_dates_keyboard())
    await state.set_state(BookingFlow.waiting_for_date)

@dp.callback_query(BookingFlow.waiting_for_date, F.data.startswith("date_"))
async def process_date(callback: CallbackQuery, state: FSMContext):
    """處理日期選擇"""
    selected_date = callback.data.split("_")[1]
    await state.update_data(travel_date=selected_date)
    
    data = await state.get_data()
    
    await callback.message.edit_text(f"📅 日期：{selected_date}\n📍 路線：{data['from_station_name']} ➡️ {data['to_station_name']}\n\n最後一步，請選擇您要的 **乘車時段**：", reply_markup=create_times_keyboard())
    await state.set_state(BookingFlow.waiting_for_time)

@dp.callback_query(BookingFlow.waiting_for_time, F.data.startswith("time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    """處理時段選擇並啟動監控"""
    time_data = callback.data.split("_")
    start_time = time_data[1]
    end_time = time_data[2]
    
    data = await state.get_data()
    from_id = data["from_station_id"]
    to_id = data["to_station_id"]
    travel_date = data["travel_date"]
    
    # 建立最終確認訊息
    summary = (
        "✅ **搶票任務已建立並開始背景監控！**\n\n"
        f"🚌 客運：和欣客運\n"
        f"📍 路線：{data['from_station_name']} -> {data['to_station_name']}\n"
        f"📅 日期：{travel_date}\n"
        f"⏰ 時段：{start_time} ~ {end_time}\n\n"
        "💡 提示：您可以再次輸入 /start 建立另一筆任務。"
    )
    
    await callback.message.edit_text(summary, parse_mode="Markdown")
    await state.clear()
    
    # 在背景啟動搶票監控器 (使用我們之前辛苦修復好的核心邏輯)
    logger.info(f"啟動背景搶票任務: {from_id}->{to_id}, {travel_date}, {start_time}~{end_time}")
    
    monitor = HohsinMonitor(
        from_station=from_id,
        to_station=to_id,
        travel_date=travel_date,
        start_time=start_time,
        end_time=end_time
    )
    
    # 創建背景任務，這樣 bot 可以繼續服務其他訊息
    asyncio.create_task(monitor.run())

# --- 啟動機器人 ---

async def main():
    logger.info("Telegram 互動機器人已啟動！請在 Telegram 中輸入 /start 開始使用。")
    # 清除潛在的 Webhook 殘留
    await bot.delete_webhook(drop_pending_updates=True)
    # 開始監聽訊息
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("機器人已停止。")
