import asyncio
import os
import httpx
import json
from dotenv import load_dotenv
import logging

# 設定基礎日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("APIDebug")

async def debug_flow():
    load_dotenv()
    user_name = os.getenv("USER_PHONE")
    password = os.getenv("USER_PASSWORD")
    
    # 車站與日期設定 (測試用)
    from_id = os.getenv("FROM_STATION", "G03")
    to_id = os.getenv("TO_STATION", "B01")
    date = os.getenv("TRAVEL_DATE")
    
    if not date:
        logger.error("請在 .env 中設置 TRAVEL_DATE")
        return

    # 1. 建立 Client 並獲取基礎 Cookie
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.ebus.com.tw",
        "Referer": "https://www.ebus.com.tw/"
    }
    
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        logger.info("--- 步驟 1: 造訪首頁與獲取驗證碼 ---")
        await client.get("https://www.ebus.com.tw/Home/LogIn")
        
        # 獲取驗證碼 (這裡簡單模擬，實際上我們需要 OCR，但我們可以直接拿 Token)
        # 為了節省時間，我們直接執行 login 邏輯
        from src.hohsin_api import HohsinAPI
        api = HohsinAPI()
        
        logger.info("--- 步驟 2: 執行正式登入 ---")
        login_success = await api.login()
        if not login_success:
            logger.error("登入失敗，中止診斷。")
            return
        
        logger.info(f"登入成功！Access Token 前 10 碼: {api.access_token[:10]}...")
        
        logger.info("--- 步驟 3: 查詢班次 (取得區間資料) ---")
        # 使用 00:00 - 23:59 區間
        schedules = await api.get_schedules(from_id, to_id, date, "00:00", "23:59")
        
        if not schedules:
            logger.error("找不到班次，請確認日期與起訖站。")
            return
        
        # 抓第一個有票的班次進行測試
        target = None
        for s in schedules:
            if s.get("vacantSeats", 0) > 0:
                target = s
                break
        
        if not target:
            target = schedules[0]
            logger.warning(f"目前所有班次皆無餘票，將針對第一個班次 [{target['dailyScheduleId']}] 進行強制測試。")
        else:
            logger.info(f"選定測試班次: [{target['dailyScheduleId']}] 時間: {target['scheduleDepartureTime']} 餘票: {target['vacantSeats']}")

        logger.info("--- 步驟 4: 座位圖參數組合測試 ---")
        
        sch_id = target['dailyScheduleId']
        dep_date = date.replace("-", "/") # 有些 API 用斜線
        
        test_cases = [
            ("CASE A: 僅 ID", {}),
            ("CASE B: ID + 車站", {"intoStationId": from_id, "outofStationId": to_id}),
            ("CASE C: ID + 車站 + 日期 (斜線)", {"intoStationId": from_id, "outofStationId": to_id, "departureDate": dep_date}),
            ("CASE D: ID + 車站 + 搜尋區間", {
                "intoStationId": from_id, 
                "outofStationId": to_id, 
                "beginDepartureTime": "00:00", 
                "endDepartureTime": "23:59"
            })
        ]
        
        url = f"https://api.ebus.com.tw/web/schedules/{sch_id}/seatingplans"
        
        for name, params in test_cases:
            logger.info(f"正在執行 {name}...")
            try:
                # 測試使用會員 Token
                resp = await api.client.get(url, params=params)
                logger.info(f"結果: {resp.status_code} | 內容長度: {len(resp.text)}")
                if resp.status_code == 200:
                    data = resp.json()
                    res_val = data.get("result")
                    if res_val:
                        count = len(res_val.get("items", []) if isinstance(res_val, dict) else res_val)
                        logger.info(f"成功！獲取到 {count} 個座位數據。")
                        if count > 0:
                            logger.info(f"樣例數據: {res_val[0] if isinstance(res_val, list) else res_val.get('items')[0]}")
                            logger.info(">>> 診斷結論：此參數組合有效！")
                    else:
                        logger.warning("回應成功但 result 為 null/空。")
                else:
                    logger.error(f"失敗回應: {resp.text[:200]}")
            except Exception as e:
                logger.error(f"請求發生異常: {e}")

        await api.close()

if __name__ == "__main__":
    asyncio.run(debug_flow())
