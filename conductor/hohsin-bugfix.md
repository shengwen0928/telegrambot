# 和欣客運 API 座位圖 (403/空陣列) 偵錯計畫

## 問題背景
目前在呼叫 `get_schedules` 時能成功取得班次列表，且 `vacantSeats` 顯示有餘票（例如 12 個位子）。
但在隨後呼叫 `/web/schedules/{schedule_id}/seatingplans` 獲取詳細座位圖時：
- 若使用 `DEFAULT_TOKEN`，會回傳 `403 Forbidden (At least one of these permissions must be granted)`。
- 若使用登入後的 `access_token`，會回傳 `200 OK`，但座位資料為空陣列 `[]`。

用戶也確認官網上該班次（台北總站 -> 台南轉運站）確實有空位，且懷疑是否與「時間區間」參數設定有關。

## 核心目標
找出 `get_seating_plans` 正確的呼叫方式，使其能回傳包含 `seatNo` 與 `status` 的完整座位列表。

## 偵錯步驟

### 階段 1：API 參數盲測與文件比對
1. 撰寫一個獨立的 Python 測試腳本 (`test_api_playground.py`)。
2. 嘗試在 `get_seating_plans` 的 Query String 中加入更多參數（如 `departureDate`, `departureTime`, `beginDepartureTime`, `endDepartureTime`）。
3. 測試是否需要將請求改為 `POST` 並帶上 Payload，或者是否有其他隱藏的 API 端點（例如 `/web/orders/seatingplans`）。

### 階段 2：Token 與 Cookie 深度模擬
1. 觀察登入時伺服器回傳的 `Set-Cookie`。
2. 確保 `httpx.AsyncClient` 完整繼承了這些 Cookie 並在呼叫 `seatingplans` 時帶上。
3. 測試 Header 中的 `Origin`、`Referer` 是否必須與欲查詢的班次時間/起訖站完全吻合。

### 階段 3：實際套用與整合測試
1. 找到正確的 API 呼叫格式後，將其整合回 `src/hohsin_api.py`。
2. 執行 `pytest` 確保單元測試與整合測試通過。
3. 執行 `python main.py` 驗證監控器是否能成功解析座位並推進到訂票階段。

## 預期成果
- 成功解析出座位圖，並從中找出未被佔用的 `seatNo`。
- 成功觸發 `book_ticket` 訂票 API。

> **注意：** 由於 Plan Mode 的安全限制，我無法直接在此模式下執行測試腳本。待您核准此計畫並退出 Plan Mode 後，我們將立即展開階段 1 的實測！