import pytest
import os
from src.hohsin_api import HohsinAPI

@pytest.mark.asyncio
async def test_hohsin_api_integration():
    """整合測試：登入、獲取車站清單、查詢餘票。"""
    api = HohsinAPI()
    try:
        # 1. 測試登入 (可能因為驗證碼辨識失敗需要重試)
        success = False
        for i in range(3):  # 最多嘗試 3 次辨識驗證碼
            print(f"登入嘗試 {i+1}...")
            if await api.login():
                success = True
                break
        
        assert success, "登入失敗（可能是驗證碼辨識多次失敗）"
        assert api.access_token is not None
        print("登入成功！")

        # 2. 測試獲取車站清單
        stations = await api.get_stations()
        assert isinstance(stations, list)
        assert len(stations) > 0
        print(f"成功獲取 {len(stations)} 個車站。")
        
        # 測試查詢: 台北總站 (G03) -> 臺南轉運站 (B01)
        from_id = "G03"
        to_id = "B01"
        print(f"測試查詢: 台北總站 ({from_id}) -> 臺南轉運站 ({to_id})")

        # 3. 測試查詢班次 (使用今天日期)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            schedules = await api.get_schedules(from_id, to_id, today)
            assert isinstance(schedules, list)
            print(f"班次查詢成功，回傳 {len(schedules)} 筆。")
        except Exception as e:
            print(f"班次查詢失敗: {e}")

    finally:
        await api.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_hohsin_api_integration())
