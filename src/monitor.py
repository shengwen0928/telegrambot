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
        max_retries: int = 5
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
        """
        self.api = HohsinAPI()
        self.notifier = TelegramNotifier()
        self.from_station = from_station
        self.to_station = to_station
        self.travel_date = travel_date
        self.start_time = start_time
        self.end_time = end_time
        self.max_retries = max_retries
        self.is_running = False

    async def _login_with_retry(self) -> bool:
        """嘗試登入，具備重試機制。"""
        for i in range(self.max_retries):
            try:
                logger.info(f"正在嘗試登入... (第 {i+1} 次)")
                if await self.api.login():
                    logger.info("登入成功！")
                    return True
            except Exception as e:
                logger.error(f"登入發生異常: {str(e)}")
            
            wait_time = random.uniform(2, 5)
            await asyncio.sleep(wait_time)
        
        return False

    async def _auto_book(self, schedule: Dict[str, Any]) -> bool:
        """執行自動選位與訂票。"""
        try:
            schedule_id = schedule["dailyScheduleId"]
            departure_time = schedule["departureTime"]
            logger.info(f"發現可用班次: {departure_time}，正在獲取座位圖...")
            
            # 1. 獲取座位圖
            seating_plans = await self.api.get_seating_plans(schedule_id)
            
            # 2. 尋找第一個空位 (status 為 0 通常代表空位，具體依 API 回傳為準)
            # 根據常見 API 邏輯，result 列表中的物件包含 seatNo
            # 我們假設 status=0 是空位，或是直接找沒有被佔用的序號
            vacant_seat = None
            for seat in seating_plans:
                if seat.get("status") == 0:
                    vacant_seat = seat["seatNo"]
                    break
            
            if vacant_seat is None:
                logger.warning(f"班次 {departure_time} 雖然顯示有餘票，但座位圖中無可用空位。")
                return False
            
            # 3. 執行訂票
            logger.info(f"選定座位: {vacant_seat}，執行訂位...")
            result = await self.api.book_ticket(schedule, vacant_seat)
            
            if result.get("success") or result.get("result"):
                msg = f"🎉 搶票成功！\n日期：{self.travel_date}\n班次：{departure_time}\n座位：{vacant_seat}"
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
        start_msg = f"🚀 監控啟動\n路線：{self.from_station} -> {self.to_station}\n日期：{self.travel_date}\n範圍：{self.start_time} - {self.end_time}"
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
                
                # 2. 檢查有無餘票 (通常是 check vacantCount > 0)
                target_schedule = None
                for s in schedules:
                    vacant_count = s.get("vacantCount", 0)
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
