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
        """主監控循環。"""
        logger.info(f"開始監控台鐵：{self.travel_date} {self.from_station}->{self.to_station}")
        
        # 1. 嘗試登入 (台鐵會員登入可增加成功率)
        is_logged_in = False
        if self.user_id_no and self.user_password:
            is_logged_in = await self.api.login(self.user_id_no, self.user_password)
            if not is_logged_in:
                await self.notifier.send_message("⚠️ 台鐵會員登入失敗，將以訪客身分嘗試監控。")

        # 初始化 Session
        await self.api.init_session()

        retry_count = 0
        while self.is_running:
            try:
                # 1. 搜尋班次
                trains = await self.api.query_trains(
                    self.from_station, self.to_station, self.travel_date, self.start_time, self.end_time
                )
                
                if not trains:
                    logger.info("未找到符合條件的班次，等待下次查詢...")
                    await asyncio.sleep(15)
                    continue

                target_train = None
                # 如果有指定車次，找指定車次；否則找任何有位的
                for train in trains:
                    if self.train_no:
                        if train['train_no'] == self.train_no and train['has_seats']:
                            target_train = train
                            break
                    elif train['has_seats']:
                        target_train = train
                        break
                
                if target_train:
                    logger.info(f"發現可用班次：{target_train['train_no']}，嘗試訂票...")
                    # 2. 執行訂票
                    success = await self.api.book_ticket(
                        self.user_id_no, target_train['train_no'], 
                        self.from_station, self.to_station, self.travel_date
                    )
                    
                    if success:
                        msg = f"🎊 台鐵搶票成功！\n車次：{target_train['train_no']}\n日期：{self.travel_date}\n區間：{self.from_station} -> {self.to_station}\n請儘速至台鐵官網付款。"
                        await self.notifier.send_message(msg)
                        self.is_running = False
                        break
                    else:
                        logger.warning(f"班次 {target_train['train_no']} 訂票嘗試失敗")
                
                retry_count += 1
                if retry_count % 10 == 0:
                    logger.info(f"台鐵監控中... 已重試 {retry_count} 次")

                # 模擬間隔 (台鐵刷票建議 10-20 秒)
                await asyncio.sleep(12) 
                
            except Exception as e:
                logger.error(f"台鐵監控異常: {e}")
                await asyncio.sleep(30)

        await self.api.close()

