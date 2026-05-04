# 分離常用站點儲存邏輯計畫

## 目標
解決和欣客運與台灣鐵路的「常用路線」在列表中混雜顯示的問題，提升使用者體驗。

## 修改範圍
檔案：`line_bot.py`

## 實作步驟
1. **修改狀態存取邏輯**：
   將所有讀取與寫入 `users[user_id]["favorites"]` 的地方，改為動態鍵值 `favorites_{bus_type}`。
   - `bus_type` 可從 `state.get("bus")` 或直接從 `bus_type` 變數取得。
2. **具體修改點**：
   - **Step 2.1 (使用儲存帳密)**：讀取常用路線時，改用 `f"favorites_{bus_type}"`。
   - **Step 2.4 (記憶選擇)**：讀取常用路線時，改用 `f"favorites_{bus_type}"`。
   - **Step 2.5 (選擇路線方式)**：讀取常用路線時，改用 `f"favorites_{state['bus']}"`。
   - **Step 2.6 (選擇常用路線)**：提取選中路線時，改用 `users[user_id][f"favorites_{state['bus']}"][idx]`。
   - **Step 10 (儲存常用路線)**：儲存路線時，寫入至 `users[user_id][f"favorites_{bus_type}"]`。

## 預期結果
當使用者進入「和欣客運」流程並選擇「常用站點」時，只會看到以前儲存的和欣路線；進入「台灣鐵路」流程時，則只會看到台鐵的路線。
