import asyncio
import json
import httpx
from datetime import datetime
from src.hohsin_api import HohsinAPI

async def test_schedules():
    api = HohsinAPI()
    # 測試參數：高雄建國 (A01) -> 台北總站 (G03) 
    from_stn = "A01"
    to_stn = "G03"
    # 測試這週末
    dates = ["2026-05-08", "2026-05-09", "2026-05-10"]
    
    try:
        for date in dates:
            print(f"\n===== 測試日期: {date} =====")
            url = f"{api.BASE_URL}/web/schedules"
            params = {
                "intoStationId": from_stn,
                "outofStationId": to_stn,
                "departureDate": date.replace("-", "/"),
                "beginDepartureTime": "00:00",
                "endDepartureTime": "23:59",
                "isVacantOnly": "false"
            }
            headers = api.headers.copy()
            headers["Authorization"] = f"Bearer {api.DEFAULT_TOKEN}"
            
            res1 = await api.client.get(url, params=params, headers=headers)
            items1 = res1.json().get("result", {}).get("items", [])
            print(f"總班次數量 (isVacantOnly=false): {len(items1)}")

            params["isVacantOnly"] = "true"
            res2 = await api.client.get(url, params=params, headers=headers)
            items2 = res2.json().get("result", {}).get("items", [])
            print(f"總班次數量 (isVacantOnly=true): {len(items2)}")
            
            diff = [s for s in items1 if s.get("vacantSeats", 0) == 0]
            print(f"餘位為 0 的班次數量: {len(diff)}")
            for i, s in enumerate(diff[:5]):
                print(f"  - 無票班次 {i+1}: 時間={s.get('intoStationDepartureTime')}")
    except Exception as e:
        print(f"錯誤: {e}")
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test_schedules())
