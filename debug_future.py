import asyncio
from datetime import datetime, timedelta
from src.hohsin_api import HohsinAPI

async def test_future_timetable():
    api = HohsinAPI()
    from_stn = "A01"
    to_stn = "G03"
    
    # 測試 14 天後 (通常票才剛開或還沒滿)
    future_date = (datetime.now() + timedelta(days=13)).strftime("%Y-%m-%d")
    
    print(f"--- 測試未來日期 (日期: {future_date}) ---")
    try:
        schedules = await api.get_schedules(from_stn, to_stn, future_date, "00:00", "23:59")
        print(f"總班次數量: {len(schedules)}")
        
        times = sorted([s.get("intoStationDepartureTime").split("T")[1][:5] for s in schedules])
        print(f"所有班次時間: {times}")
        
    except Exception as e:
        print(f"錯誤: {e}")
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test_future_timetable())
