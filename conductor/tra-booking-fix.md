# 台鐵訂票失敗問題分析與計畫

## 現狀分析
1. 日誌顯示，訂票 POST 請求後，伺服器回傳 `302 Moved Temporarily` 並跳轉回查詢頁面 (`/tip001/tip121/query`)。
2. 這通常表示參數錯誤或驗證碼未通過。
3. 根據先前的 HTML 解析，驗證碼相關欄位名稱可能是 `g-recaptcha-response` (reCAPTCHA) 或其他未知的名稱，而非我先前設定的 `cvCode`。此外，行程參數中的部分欄位結構可能與實際不符。

## 計畫步驟
1. **離開 Plan 模式**。
2. **獲取完整表單結構**：編寫一個獨立指令碼 (`debug_tra_form.py`) 來下載完整的訂票頁面 HTML，並將其中 `<form id="queryForm">` 的所有 `input` 與 `select` 標籤的 `name` 導出，精準對照。
3. **驗證參數**：將提取出的正確參數名稱更新到 `src/tr_api.py` 的 `guest_book_ticket` payload 中。
4. **處理驗證碼欄位**：確認圖形驗證碼實際綁定的 `name` 是什麼（可能是 `verifyCode` 或是動態的），並更新提交邏輯。
5. **重構並測試**：執行真實的 POST 請求模擬，並將 `follow_redirects` 設為 False 以攔截跳轉訊息，找出真正的錯誤原因。
