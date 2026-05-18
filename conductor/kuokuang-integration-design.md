# 國光客運整合設計規格 (Kuo-Kuang Integration Design Spec)

## 背景與動機 (Background & Motivation)
使用者希望在現有支援和欣客運、台鐵的搶票系統中，新增支援「國光客運」的訂票功能。此設計將規劃如何安全、模組化地整合國光客運。

## 範圍與影響 (Scope & Impact)
- **API 通訊**: 解析並呼叫國光客運的查詢與訂票端點。
- **監控機制**: 定時輪詢並自動觸發訂票。
- **使用者介面**: Line Bot 與 Telegram Bot 的選單與指令擴充。
- **任務狀態儲存**: 任務持久化模組需支援新的客運類型。
- **不影響**: 既有的和欣客運與台鐵功能不受影響。

## 擬定解決方案 (Proposed Solution)
採用**獨立模組設計 (Independent Module Design)**，與現有架構對齊。

### 1. API 層 (`src/kuokuang_api.py`)
- **`KuoKuangAPI` 類別**: 負責處理 HTTP Requests。
- **核心方法**:
  - `search_tickets(from_station, to_station, date, time)`: 查詢可用車次。
  - `book_ticket(schedule_id, id_number, phone, name)`: 執行訂票。訂票過程需提供身分證字號、手機號碼與中文姓名。
- **錯誤處理與驗證碼**: 若遇到驗證碼需求，將串接現有的 `OCREngine` 進行圖形辨識處理。

### 2. 監控層 (`src/kuokuang_monitor.py`)
- **`KuoKuangMonitor` 類別**: 負責業務邏輯的輪詢。
- **核心流程**:
  1. 初始化時接收使用者條件與憑證 (身分證、電話、姓名)。
  2. 定期調用 `KuoKuangAPI.search_tickets` 檢查空位。
  3. 發現空位後，自動調用 `KuoKuangAPI.book_ticket`。
  4. 將結果 (成功/失敗) 透過 `TelegramNotifier` / `LineNotifier` 發送給使用者。

### 3. Bot 與持久化整合
- **Persistence (`src/persistence.py`)**:
  - 任務資料結構需擴充，增加 `service_type` (如 `hohsin`, `tra`, `kuokuang`) 欄位或新增對應的儲存邏輯，以區分不同客運的監控任務。
- **Bots (`line_bot.py`, `tg_bot.py`)**:
  - 增加國光客運的進入點 (按鈕或 `/kuokuang` 指令)。
  - 對話流程：選擇起訖站 -> 選擇日期與時間 -> 收集訂票人資訊 (身分證、手機、姓名) -> 啟動 Monitor 背景任務。

## 替代方案考慮 (Alternatives Considered)
- **統一介面重構 (Unified Interface)**: 將所有客運 API 抽象為共用介面。雖然長期架構較佳，但在目前階段開發成本較高，且可能引入影響既有功能的風險。因此選擇快速且安全的獨立模組設計。

## 驗證與測試 (Verification & Testing)
1. 實作 API 單元測試 (`tests/test_kuokuang_api.py`)，使用 mock 測試查詢與訂票流程。
2. 手動測試: 透過 Bot 實際發起一個國光客運的監控任務，驗證查詢是否正確運行。
