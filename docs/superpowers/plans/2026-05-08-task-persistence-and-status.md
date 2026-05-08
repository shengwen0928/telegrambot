# 搶票任務持久化與詳細狀態顯示 實作計畫

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計畫。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 實作任務持久化（存入檔案，重啟自動恢復）與詳細監控狀態顯示（嘗試次數、最後檢查時間）。

**架構：** 
1. **數據追蹤**：修改 `HohsinMonitor` 與 `TaiwanRailwayMonitor` 類別，加入 `attempt_count` 與 `last_check_time` 屬性，並在監控循環中更新。
2. **持久化層**：新增 `src/persistence.py` 封裝對 `tasks.json` 的讀寫操作。
3. **邏輯整合**：修改 `line_bot.py`，在任務啟動/停止時同步更新持久化檔案。
4. **自動恢復**：在 Line Bot 啟動時讀取檔案並重新分派背景監控任務。
5. **UI 更新**：更新任務清單卡片，顯示嘗試次數與最後檢查時間。

**技術棧：** Python, JSON, asyncio

---

## 修改文件列表
- `src/monitor.py`: 更新 `HohsinMonitor` 類別。
- `src/tr_monitor.py`: 更新 `TaiwanRailwayMonitor` 類別。
- `src/persistence.py`: (新建立) 任務持久化工具。
- `line_bot.py`: 整合持久化邏輯、恢復任務、更新 UI。

---

### 任務 1：更新監控類別以追蹤狀態

**文件：**
- 修改：`src/monitor.py`
- 修改：`src/tr_monitor.py`

- [ ] **步驟 1：修改 `src/monitor.py` 中的 `HohsinMonitor`**
  - 在 `__init__` 中新增 `self.attempt_count = 0` 與 `self.last_check_time = None`。
  - 在 `run` 函數的 `while` 迴圈開始處，遞增 `self.attempt_count` 並更新 `self.last_check_time = datetime.now().strftime("%H:%M:%S")`。

- [ ] **步驟 2：修改 `src/tr_monitor.py` 中的 `TaiwanRailwayMonitor`**
  - 同樣新增屬性並在 `run` 迴圈中更新。

- [ ] **步驟 3：Commit**
```bash
git add src/monitor.py src/tr_monitor.py
git commit -m "feat(monitor): 增加嘗試次數與最後檢查時間的追蹤"
```

---

### 任務 2：建立持久化工具 (PersistenceManager)

**文件：**
- 建立：`src/persistence.py`

- [ ] **步驟 1：實作 `src/persistence.py`**
  - 實作 `save_tasks(tasks_data)` 與 `load_tasks()`。
  - 任務資料結構預計包含：`user_id`, `bus_type`, `params` (監控器所需的初始化參數)。

```python
import json
import os

TASKS_FILE = "tasks.json"

def save_tasks_to_file(running_tasks_dict):
    """將所有正在運行的任務參數序列化並存檔"""
    data = []
    for user_id, monitors in running_tasks_dict.items():
        for m in monitors:
            task_info = {
                "user_id": user_id,
                "bus_type": "hohsin" if "Hohsin" in m.__class__.__name__ else "tra",
                "params": {
                    "from_station": m.from_station,
                    "to_station": m.to_station,
                    "travel_date": m.travel_date,
                    "start_time": m.start_time,
                    "end_time": m.end_time,
                    "num_tickets": getattr(m, "num_tickets", 1)
                }
            }
            # 針對和欣的額外參數
            if task_info["bus_type"] == "hohsin":
                task_info["params"].update({
                    "user_phone": m.user_phone,
                    "user_password": m.user_password,
                    "manual_seats": m.manual_seats,
                    "target_schedule_id": m.target_schedule_id
                })
            else:
                # 針對台鐵的額外參數
                task_info["params"].update({
                    "user_id_no": m.user_id_no,
                    "user_password": m.user_password
                })
            data.append(task_info)
    
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_tasks_from_file():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []
```

- [ ] **步驟 2：Commit**
```bash
git add src/persistence.py
git commit -m "feat(persistence): 新增任務持久化工具"
```

---

### 任務 3：在 `line_bot.py` 中整合持久化與恢復邏輯

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：修改 `start_monitor_task` 函數**
  - 在啟動任務後調用 `save_tasks_to_file(running_tasks)`。

- [ ] **步驟 2：修改「取消任務」與任務完成邏輯**
  - 在 `running_tasks[user_id].remove(monitor)` 或 `pop` 後，同樣調用 `save_tasks_to_file(running_tasks)`。

- [ ] **步驟 3：實作 `recover_all_tasks()` 並在啟動時調用**
  - 讀取 `tasks.json`，循環建立監控實例並 `asyncio.create_task`。

- [ ] **步驟 4：更新 `create_task_list_carousel` 的 UI**
  - 在卡片內容中加入：
    - `{"type": "text", "text": f"🔄 已嘗試：{m.attempt_count} 次", "size": "xs", "color": "#aaaaaa"}`
    - `{"type": "text", "text": f"⏱️ 最後檢查：{m.last_check_time or '尚未開始'}", "size": "xs", "color": "#aaaaaa"}`

- [ ] **步驟 5：執行語法檢查與初步驗證**

- [ ] **步驟 6：Commit**
```bash
git add line_bot.py
git commit -m "feat(line-bot): 整合任務持久化與詳細狀態顯示"
```
