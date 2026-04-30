import asyncio
import random
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from .hohsin_api import HohsinAPI
from .notifier import TelegramNotifier

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("HohsinMonitor")

class HohsinMonitor:
    """和欣客運搶票監控引擎。"""

    def __init__(
        self,
        from_station: str,
        to_station: str,
        travel_date: str,
        start_time: str = "00:00",
        end_time: str = "23:59",
        max_retries: int = 5,
        notifier = None,
        user_phone: Optional[str] = None,
        user_password: Optional[str] = None
    ):
        """
        初始化監控器。
        
        Args:
            from_station: 起點站 ID (如 G03)。
            to_station: 終點站 ID (如 B01)。
            travel_date: 乘車日期 (YYYY-MM-DD)。
            start_time: 開始時間篩選 (HH:mm)。
            end_time: 結束時間篩選 (HH:mm)。
            max_retries: 登入或 API 失敗時的最大重試次數。
            notifier: 自訂通知模組，預設為 TelegramNotifier。
            user_phone: 用戶手機號碼 (用於覆蓋 .env)。
            user_password: 用戶密碼 (用於覆蓋 .env)。
        """
        self.api = HohsinAPI()
        self.notifier = notifier if notifier else TelegramNotifier()
        self.from_station = from_station
        self.to_station = to_station
        self.travel_date = travel_date
        self.start_time = start_time
        self.end_time = end_time
        self.max_retries = max_retries
        self.is_running = False
        self.user_phone = user_phone
        self.user_password = user_password

    async def _login_with_retry(self) -> bool:
        """嘗試登入，具備重試機制。"""
        for i in range(self.max_retries):
            try:
                logger.info(f"正在嘗試登入... (第 {i+1} 次)")
                if await self.api.login(self.user_phone, self.user_password):
                    logger.info("登入成功！")
                    return True
            except Exception as e:
                logger.error(f"登入發生異常: {str(e)}")
            
            wait_time = random.uniform(2, 5)
            await asyncio.sleep(wait_time)
        
        return False

    async def _auto_book(self, schedule: Dict[str, Any], num_tickets: int = 1) -> bool:
        """執行自動選位與訂票，支援多張票與自定義座位偏好。"""
        try:
            schedule_id = schedule["dailyScheduleId"]
            from_name = await self.api.get_station_name(schedule["intoStationId"])
            to_name = await self.api.get_station_name(schedule["outofStationId"])
            
            departure_time = schedule.get("intoStationDepartureTime", "未知時間")
            logger.info(f"發現可用班次: [{schedule_id}] {from_name} -> {to_name} ({departure_time})，預計訂購 {num_tickets} 張")
            
            # 1. 獲取座位圖
            seating_plans = await self.api.get_seating_plans(
                schedule_id, 
                schedule["intoStationId"], 
                schedule["outofStationId"],
                travel_date=self.travel_date,
                start_time=self.start_time,
                end_time=self.end_time
            )
            
            # 2. 定義座位偏好邏輯
            all_vacant = [seat["seatNo"] for seat in seating_plans if seat.get("ticketId") is None]
            selected_seats = []

            if num_tickets == 2:
                # 兩張票的連號優先級 (括號內為一組)
                pair_groups = [
                    (3, 4), (6, 7), (9, 10), (13, 14), (16, 17), (19, 20), (22, 23), (25, 26), # 第一優先
                    (1, 2), (28, 29) # 第二優先
                ]
                
                for p1, p2 in pair_groups:
                    if p1 in all_vacant and p2 in all_vacant:
                        selected_seats = [p1, p2]
                        break
                
                if not selected_seats:
                    logger.warning(f"班次 {departure_time} 雖有餘票，但無指定連號座位，跳過。")
                    return False

            elif num_tickets == 1:
                # 單張票優先級
                priority_single = [5, 1, 2]
                for s in priority_single:
                    if s in all_vacant:
                        selected_seats = [s]
                        break
                
                # 如果優先位都沒了，隨便抓一個
                if not selected_seats and all_vacant:
                    selected_seats = [all_vacant[0]]
                
                if not selected_seats:
                    logger.warning(f"班次 {departure_time} 座位圖中無任何可用空位。")
                    return False
            else:
                # 3張票以上暫不實作特殊邏輯，直接抓前 N 個
                if len(all_vacant) >= num_tickets:
                    selected_seats = all_vacant[:num_tickets]
                else:
                    logger.warning(f"班次 {departure_time} 餘票不足。")
                    return False

            # 3. 執行訂票
            logger.info(f"選定座位: {selected_seats}，執行訂位...")
            result = await self.api.book_ticket(schedule, selected_seats)
            
            if result.get("success") or result.get("result"):
                msg = f"🎉 搶票成功！\n日期：{self.travel_date}\n班次：{departure_time}\n張數：{num_tickets}\n座位：{', '.join(map(str, selected_seats))}"
                logger.info(msg)
                await self.notifier.send_message(msg)
                return True
            else:
                logger.error(f"訂位失敗: {result}")
                await self.notifier.send_message(f"❌ 訂位失敗！回應：{result}")
                
        except Exception as e:
            logger.error(f"自動訂位過程發生錯誤: {str(e)}")
            await self.notifier.send_message(f"⚠️ 自動訂位出錯: {str(e)}")
            
        return False

    async def run(self):
        """啟動監控循環。"""
        from_name = await self.api.get_station_name(self.from_station)
        to_name = await self.api.get_station_name(self.to_station)
        
        start_msg = f"🚀 監控啟動\n路線：{from_name} ({self.from_station}) -> {to_name} ({self.to_station})\n日期：{self.travel_date}\n範圍：{self.start_time} - {self.end_time}"
        logger.info(start_msg)
        await self.notifier.send_message(start_msg)

        if not await self._login_with_retry():
            error_msg = "❌ 登入失敗多次，監控停止。"
            logger.error(error_msg)
            await self.notifier.send_message(error_msg)
            return

        self.is_running = True
        
        while self.is_running:
            try:
                # 1. 查詢班次
                schedules = await self.api.get_schedules(
                    self.from_station,
                    self.to_station,
                    self.travel_date,
                    self.start_time,
                    self.end_time
                )
                
                # 2. 檢查有無餘票 (使用正確欄位 vacantSeats)
                target_schedule = None
                for s in schedules:
                    vacant_count = s.get("vacantSeats", 0)
                    if vacant_count > 0:
                        target_schedule = s
                        break

                
                if target_schedule:
                    success = await self._auto_book(target_schedule)
                    if success:
                        self.is_running = False
                        break
                else:
                    logger.info(f"目前無餘票，等待中...")

            except Exception as e:
                logger.error(f"監控循環發生錯誤: {str(e)}")
                # 如果是 Token 過期 (401)，嘗試重新登入
                if "401" in str(e):
                    logger.info("偵測到 Token 過期，嘗試重新登入...")
                    if not await self._login_with_retry():
                        self.is_running = False
                        await self.notifier.send_message("❌ 重新登入失敗，監控停止。")

            # 隨機延遲 1-3 秒
            await asyncio.sleep(random.uniform(1, 3))

        logger.info("監控結束。")
