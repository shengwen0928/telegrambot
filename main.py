import asyncio
import os
import signal
import logging
from dotenv import load_dotenv
from src.monitor import HohsinMonitor

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Main")

async def main():
    load_dotenv()
    
    # 讀取配置
    from_stn = os.getenv("FROM_STATION", "G03")
    to_stn = os.getenv("TO_STATION", "B01")
    date = os.getenv("TRAVEL_DATE")
    start_t = os.getenv("START_TIME", "00:00")
    end_t = os.getenv("END_TIME", "23:59")
    
    if not date:
        logger.error("錯誤：未在 .env 中設置 TRAVEL_DATE")
        return

    monitor = HohsinMonitor(
        from_station=from_stn,
        to_station=to_stn,
        travel_date=date,
        start_time=start_t,
        end_time=end_t
    )
    
    # 設定優雅退出 (僅在非 Windows 環境或特定情況下可用，但在 Windows 上 asyncio 有時需要不同處理)
    # 這裡我們使用簡單的變數控制
    def stop_monitor():
        logger.info("正在停止監控...")
        monitor.is_running = False

    # 在 Windows 上，signal.add_signal_handler 可能不穩定，我們嘗試捕捉
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_monitor)
            except NotImplementedError:
                # Windows ProactorEventLoop 不支持 add_signal_handler
                pass
    except Exception:
        pass

    try:
        await monitor.run()
    except asyncio.CancelledError:
        logger.info("任務被取消")
    except Exception as e:
        logger.error(f"執行過程中發生未預期錯誤: {e}")
    finally:
        logger.info("程式已停止。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("偵測到 Ctrl+C，結束程式。")
