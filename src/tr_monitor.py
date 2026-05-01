import asyncio
import logging
from typing import Optional, List
from .tr_api import TaiwanRailwayAPI

logger = logging.getLogger("TR_Monitor")

class TaiwanRailwayMonitor:
    """台鐵搶票監控器。"""
    
    def __init__(
        self,
        from_station: str,
        to_station: str,
        travel_date: str,
        start_time: str,
        end_time: str,
        notifier: any,
        user_id_no: str,
        user_password: str,
        train_no: Optional[str] = None
    ):
        self.api = TaiwanRailwayAPI()
        self.from_station = from_station
        self.to_station = to_station
        self.travel_date = travel_date
        self.start_time = start_time
        self.end_time = end_time
        self.notifier = notifier
        self.user_id_no = user_id_no
        self.user_password = user_password
        self.train_no = train_no # 指定車次 (選填)
        self.is_running = True
        self.num_tickets = 1

    def stop(self):
        self.is_running = False

    async def run(self):
        """主監控循環 (優先使用訪客模式達成最高搶票效率)。"""
        logger.info(f"開始監控台鐵 (訪客模式)：{self.travel_date} {self.from_station}->{self.to_station}")
        
        # 1. 嘗試初始化 Session
        await self.api.init_session(mode="quick")

        retry_count = 0
        while self.is_running:
            try:
                # 在訪客模式下，我們直接調用快速訂票端點
                # 如果訂票成功，會回傳 True
                logger.info(f"正在執行台鐵快速搶票嘗試... (重試第 {retry_count+1} 次)")
                
                success = await self.api.guest_book_ticket(
                    pid=self.user_id_no,
                    from_stn=self.from_station,
                    to_stn=self.to_station,
                    date=self.travel_date,
                    start_time=self.start_time,
                    end_time=self.end_time
                )
                
                if success:
                    msg = f"🎊 台鐵訪客訂票成功！\n身分證：{self.user_id_no[:3]}******{self.user_id_no[-1]}\n日期：{self.travel_date}\n區間：{self.from_station} -> {self.to_station}\n請儘速至台鐵官網「查詢訂票紀錄」並付款。"
                    await self.notifier.send_message(msg)
                    self.is_running = False
                    break
                
                retry_count += 1
                # 模擬真實人類操作間隔，避免被封 IP
                # 台鐵建議間隔 10 秒以上
                await asyncio.sleep(12) 
                
            except Exception as e:
                logger.error(f"台鐵監控異常: {e}")
                await asyncio.sleep(30)

        await self.api.close()

