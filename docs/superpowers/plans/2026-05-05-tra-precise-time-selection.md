# 台鐵精確 30 分鐘分步時間選擇實現計畫

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計畫。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 在 `line_bot.py` 中為台鐵實作分步式的精確 30 分鐘時間選擇流程（先選出發時間，再選結束時間）。

**架構：**
1. 擴充 `States` 狀態機，新增 `WAITING_FOR_START_TIME` 與 `WAITING_FOR_END_TIME`。
2. 實作 `create_precise_time_carousel` 函式，產生每 30 分鐘一格的輪播卡片。
3. 更新 `handle_postback`：當台鐵日期選好後，導向 `WAITING_FOR_START_TIME`。
4. 更新 `handle_message`：處理「出發:HH:mm」與「結束:HH:mm」訊息，儲存時間並最終合併為 `time_range`。

**技術棧：** Python, FastAPI, Line Bot SDK v3.

---

### 任務 1：狀態機與 UI 輔助函式更新

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：擴充 States 類別**
在 `States` 類別中新增兩個狀態。
```python
class States:
    # ... 現有狀態 ...
    WAITING_FOR_START_TIME = "waiting_for_start_time"
    WAITING_FOR_END_TIME = "waiting_for_end_time"
```

- [ ] **步驟 2：實作 create_precise_time_carousel 函式**
新增一個產生輪播選單的函式。
```python
def create_precise_time_carousel(prefix: str, selected_date: str, min_time: str = "00:00"):
    """建立 30 分鐘一格的精確時間輪播選單"""
    tw_tz = pytz.timezone('Asia/Taipei')
    now_tw = datetime.now(tw_tz)
    is_today = selected_date == now_tw.strftime("%Y-%m-%d")
    deadline_str = (now_tw + timedelta(minutes=30)).strftime("%H:%M")
    
    # 產生所有 30 分鐘間隔
    times = []
    h, m = 0, 0
    while h < 24:
        t_str = f"{h:02d}:{m:02d}"
        if t_str == "00:00" and h == 24: break
        # 過濾邏輯
        if is_today and t_str < deadline_str: pass
        elif t_str < min_time: pass
        else:
            times.append(t_str)
        m += 30
        if m >= 60:
            m = 0
            h += 1
    if prefix == "結束": times.append("23:59")

    bubbles = []
    chunk_size = 4
    for i in range(0, len(times), chunk_size):
        chunk = times[i:i + chunk_size]
        buttons = []
        for t in chunk:
            buttons.append({
                "type": "button",
                "action": {"type": "message", "label": t, "text": f"{prefix}:{t}"},
                "style": "secondary", "margin": "sm", "height": "sm"
            })
        bubbles.append(create_base_flex_card(f"⏱️ 選擇{prefix}時間", buttons))
        if len(bubbles) == 12: break # LINE 限制 12 張

    return FlexMessage(alt_text=f"請選擇{prefix}時間", contents=FlexContainer.from_dict({"type": "carousel", "contents": bubbles}))
```

- [ ] **步驟 3：Commit**
```bash
git add line_bot.py
git commit -m "feat(line-bot): 擴充時間選擇狀態並新增精確時間輪播函式"
```

---

### 任務 2：日期選擇後的流程分流

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：更新 handle_postback 邏輯**
根據業者類型，決定是進入台鐵的精確選擇還是和欣的區塊選擇。
```python
    # 5. 處理日期選擇 (修改處)
    if state["step"] == States.WAITING_FOR_DATE and event.postback.data == "action=select_date":
        selected_date = event.postback.params['date']
        state["date"] = selected_date
        bus_type = state.get("bus", "hohsin")

        if bus_type == "tra":
            state["step"] = States.WAITING_FOR_START_TIME
            card = create_precise_time_carousel("出發", selected_date)
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        else:
            state["step"] = States.WAITING_FOR_TIME
            times_qr = create_times_quick_reply(selected_date, bus_type)
            # ... 原有發送和欣選單的代碼 ...
```

- [ ] **步驟 2：Commit**
```bash
git add line_bot.py
git commit -m "feat(line-bot): 實作日期選擇後的台鐵/和欣流程分流"
```

---

### 任務 3：實作出發與結束時間的訊息處理

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：處理「出發:HH:mm」訊息**
在 `handle_message` 中新增處理。
```python
    # 6.1 台鐵：選擇出發時間
    if state["step"] == States.WAITING_FOR_START_TIME and text.startswith("出發:"):
        start_t = text.split(":")[1]
        state.update({"start_time": start_t, "step": States.WAITING_FOR_END_TIME})
        card = create_precise_time_carousel("結束", state["date"], min_time=start_t)
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        return
```

- [ ] **步驟 2：處理「結束:HH:mm」訊息**
接收結束時間並合併回 `time_range`，然後跳轉至選擇張數。
```python
    # 6.2 台鐵：選擇結束時間
    if state["step"] == States.WAITING_FOR_END_TIME and text.startswith("結束:"):
        end_t = text.split(":")[1]
        state.update({
            "end_time": end_t,
            "time_range": f"{state['start_time']}~{end_t}",
            "step": States.WAITING_FOR_COUNT
        })
        contents = [{"type": "text", "text": f"⏰ 已選時段：{state['time_range']}\n\n請選擇欲購買的張數。", "wrap": True, "size": "sm"}]
        card = FlexMessage(
            alt_text="選擇張數", 
            contents=FlexContainer.from_dict(create_base_flex_card("🎫 購票張數", contents)),
            quick_reply=create_ticket_count_quick_reply()
        )
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[card]))
        return
```

- [ ] **步驟 3：Commit**
```bash
git add line_bot.py
git commit -m "feat(line-bot): 補全台鐵出發與結束時間的訊息處理邏輯"
```

---

### 任務 4：驗證與清理

- [ ] **步驟 1：手動測試流程**
1. 進入台鐵流程。
2. 選擇日期後，確認出現 30 分鐘一格的「出發時間」輪播。
3. 選擇出發後，確認出現從該時間點開始的「結束時間」輪播。
4. 選擇結束後，確認顯示正確的張數選擇卡片。
5. 確認和欣流程依然維持原狀。

- [ ] **步驟 2：清理與最終提交**
刪除所有臨時調試腳本。
```bash
git status
```
