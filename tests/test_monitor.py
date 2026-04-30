import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from src.monitor import HohsinMonitor

@pytest.mark.asyncio
async def test_monitor_flow():
    """測試監控流程，模擬成功發現餘票並訂票的情境。"""
    
    # 建立監控器實例
    monitor = HohsinMonitor(
        from_station="G03",
        to_station="B01",
        travel_date="2026-05-01",
        start_time="10:00",
        end_time="12:00"
    )

    # 模擬 API 與 Notifier
    mock_api = AsyncMock()
    mock_notifier = AsyncMock()
    
    monitor.api = mock_api
    monitor.notifier = mock_notifier

    # 模擬登入成功
    mock_api.login.return_value = True

    # 模擬班次查詢結果
    mock_schedules = [
        {
            "dailyScheduleId": 12345,
            "departureTime": "11:00",
            "vacantSeats": 1,
            "intoStationId": "G03",
            "outofStationId": "B01"
        }
    ]
    mock_api.get_schedules.return_value = mock_schedules

    # 模擬座位圖 (有一個空位 ticketId=None)
    mock_seating_plan = [
        {"seatNo": 1, "ticketId": 8168602},
        {"seatNo": 2, "ticketId": None}, # 空位
        {"seatNo": 3, "ticketId": 8168603}
    ]
    mock_api.get_seating_plans.return_value = mock_seating_plan

    # 模擬訂位成功
    mock_api.book_ticket.return_value = {"success": True, "result": "Order123"}

    # 執行監控 (我們需要控制迴圈，所以只跑一兩次)      
    # 這裡可以透過 patch asyncio.sleep 來加速並在適當時機停止
    with patch("asyncio.sleep", AsyncMock()):
        await monitor.run()

    # 驗證步驟
    mock_api.login.assert_called()
    mock_api.get_schedules.assert_called_with("G03", "B01", "2026-05-01", "10:00", "12:00")
    mock_api.get_seating_plans.assert_called_with(12345, "G03", "B01", travel_date="2026-05-01", start_time="10:00", end_time="12:00")
    mock_api.book_ticket.assert_called_with(mock_schedules[0], 2)    
    # 驗證通知
    assert mock_notifier.send_message.call_count >= 2 # 啟動通知 + 成功通知
    last_call_args = mock_notifier.send_message.call_args[0][0]
    assert "🎉 搶票成功" in last_call_args

@pytest.mark.asyncio
async def test_monitor_no_tickets():
    """測試無餘票時的情況。"""
    monitor = HohsinMonitor("G03", "B01", "2026-05-01")
    
    mock_api = AsyncMock()
    monitor.api = mock_api
    monitor.notifier = AsyncMock()

    mock_api.login.return_value = True
    
    # 第一輪沒票，第二輪手動停止
    mock_api.get_schedules.return_value = [{"vacantSeats": 0, "departureTime": "09:00"}]

    # 建立一個可以用來停止 monitor 的任務
    async def stop_monitor_later():
        await asyncio.sleep(0.1)
        monitor.is_running = False

    with patch("asyncio.sleep", AsyncMock(side_effect=[None, None, asyncio.CancelledError()])):
        # 由於 run 是死迴圈，我們需要另一個方式停止它
        # 這裡我們模擬第二次 sleep 時拋出 CancelledError 或是手動設置 is_running
        asyncio.create_task(stop_monitor_later())
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass

    mock_api.get_schedules.assert_called()
    mock_api.book_ticket.assert_not_called()
