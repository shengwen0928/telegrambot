import argparse
import asyncio
import os
import signal
import logging
import src.hohsin_api
from dotenv import load_dotenv
from src.monitor import HohsinMonitor

# 關鍵檢查：印出實體代碼路徑
print(f"!!! [載入檢查] HohsinAPI 檔案路徑: {src.hohsin_api.__file__}")

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Main")

async def main():
    load_dotenv()
    
    # 建立命令列參數解析器，並以 .env 環境變數為預設值 (確保既有功能 100% 不受影響)
    parser = argparse.ArgumentParser(description="和欣客運自動搶票程式")
    parser.add_argument("--from-station", type=str, default=os.getenv("FROM_STATION", "G03"), help="起點站 ID (如 G03)")
    parser.add_argument("--to-station", type=str, default=os.getenv("TO_STATION", "B01"), help="終點站 ID (如 B01)")
    parser.add_argument("--date", type=str, default=os.getenv("TRAVEL_DATE"), help="乘車日期 (如 2026-05-05)")
    parser.add_argument("--start", type=str, default=os.getenv("START_TIME", "00:00"), help="開始時間區間 (如 00:00)")
    parser.add_argument("--end", type=str, default=os.getenv("END_TIME", "23:59"), help="結束時間區間 (如 23:59)")
    
    args = parser.parse_args()

    from_stn = args.from_station
    to_stn = args.to_station
    date = args.date
    start_t = args.start
    end_t = args.end
    
    if not date:
        logger.error("錯誤：請在 .env 中設置 TRAVEL_DATE，或在執行時加上 --date 參數 (例如 --date 2026-05-05)")
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
