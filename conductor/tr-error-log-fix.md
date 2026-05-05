# 台鐵訂票錯誤訊息擷取修復計畫

## 1. 問題分析
根據使用者回報與代碼檢查，發現目前的 `tr_api.py` 中的錯誤判斷邏輯存在嚴重瑕疵：
```python
if "身分證" in error_resp.text:
    logger.error("訂票失敗：身分證字號無效或不符合台鐵格式。")
```
因為台鐵的查詢頁面 HTML 原本就包含 `<label>身分證字號</label>` 標籤，所以當伺服器因任何原因（例如最常見的 OCR 驗證碼錯誤）回傳 302 並跳轉回查詢頁面時，這段邏輯必定會被觸發，導致日誌錯誤地顯示「身分證無效」，誤導了除錯方向。

## 2. 修復步驟

### 2.1 引入 HTML 解析
在 `guest_book_ticket` 中使用 `BeautifulSoup` 正確解析跳轉後的錯誤頁面，而不是直接比對整個 HTML 的字串。

### 2.2 精確擷取錯誤提示
台鐵 Spring MVC 通常會將錯誤訊息放在特定 class 中。
修改 `tr_api.py` 的錯誤擷取代碼：
```python
soup = BeautifulSoup(error_resp.text, 'html.parser')
alert = soup.find('div', {'class': 'alert'})
error_span = soup.find('span', {'class': 'error'})
                
if error_span:
    err_msg = error_span.text.strip()
    logger.error(f"訂票失敗，伺服器提示: {err_msg}")
elif alert:
    err_msg = alert.text.strip()
    logger.error(f"訂票失敗，伺服器警告: {err_msg}")
else:
    logger.error("訂票失敗，跳轉回查詢頁面且原因不明 (可能是驗證碼錯誤)。")
```

### 2.3 預期結果
- 日誌將不再錯誤回報「身分證無效」。
- 如果是驗證碼辨識錯誤，日誌會精確印出官方的錯誤提示（如「圖形驗證碼輸入錯誤」）。
- 此修改不會改變原有的重試邏輯，但能讓使用者與開發者清楚知道失敗的真正原因。
