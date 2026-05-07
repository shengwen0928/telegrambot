# Line Bot 停止任務二次確認機制 實作計畫

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐任務實現此計畫。步驟使用複選框（`- [ ]`）語法來跟踪進度。

**目標：** 在 Line Bot 中實作「停止任務」的二次確認機制，防止誤觸導致監控中斷。

**架構：** 
1. 新增 `create_confirm_cancel_quick_reply` 輔助函數來產生 Quick Reply 快捷按鈕。
2. 重構 `handle_message` 中的「取消任務」邏輯，改為發送確認請求而非直接停止。
3. 實作「確認取消:是」與「確認取消:否」的指令處理器，分別處理執行停止與取消操作。

**技術棧：** Python, FastAPI, Line Bot SDK v3

---

## 修改文件列表
- `line_bot.py`: 實作輔助函數、修改指令處理器、整合二次確認流程。

---

### 任務 1：新增二次確認輔助函數

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：在 `line_bot.py` 中新增 `create_confirm_cancel_quick_reply` 函數**

```python
def create_confirm_cancel_quick_reply(idx: int):
    """建立停止任務的二次確認 Quick Reply"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="✅ 是，確定停止", text=f"確認取消:是:{idx}")),
        QuickReplyItem(action=MessageAction(label="❌ 否，繼續監控", text="確認取消:否"))
    ])
```

- [ ] **步驟 2：執行 Python 語法檢查**

運行：`python -m py_compile line_bot.py`
預期：PASS

- [ ] **步驟 3：Commit**

```bash
git add line_bot.py
git commit -m "feat(line-bot): 新增停止任務二次確認輔助函數"
```

---

### 任務 2：重構「取消任務」指令發起流程

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：修改 `handle_message` 中 `text.startswith("取消任務:")` 的邏輯**

```python
    # 1.6 發起取消確認
    if text.startswith("取消任務:"):
        idx = int(text.split(":", 1)[1])
        if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
            m = running_tasks[user_id][idx]
            bus_type = "hohsin" if isinstance(m, HohsinMonitor) else "tra"
            from_name = get_station_name(m.from_station, bus_type)
            to_name = get_station_name(m.to_station, bus_type)
            
            info_text = f"📍 路線：{from_name} ➡️ {to_name}\n📅 日期：{m.travel_date}\n⏰ 時段：{m.start_time}~{m.end_time}"
            confirm_msg = TextMessage(
                text=f"⚠️ 您確定要「停止」此監控任務嗎？\n\n{info_text}",
                quick_reply=create_confirm_cancel_quick_reply(idx)
            )
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[confirm_msg]))
        else:
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="❌ 找不到該任務，可能已被系統自動終止。")]))
        return
```

- [ ] **步驟 2：執行 Python 語法檢查**

運行：`python -m py_compile line_bot.py`
預期：PASS

- [ ] **步驟 3：Commit**

```bash
git add line_bot.py
git commit -m "feat(line-bot): 將直接停止任務重構為發起二次確認請求"
```

---

### 任務 3：實作最終確認指令處理器

**文件：**
- 修改：`line_bot.py`

- [ ] **步驟 1：在 `handle_message` 中新增處理「確認取消:是」與「確認取消:否」的邏輯**

```python
    # 1.7 執行最終取消
    if text.startswith("確認取消:是:"):
        idx = int(text.split(":", 2)[2])
        if user_id in running_tasks and 0 <= idx < len(running_tasks[user_id]):
            m = running_tasks[user_id].pop(idx)
            m.stop() 
            bus_type = "hohsin" if isinstance(m, HohsinMonitor) else "tra"
            from_name = get_station_name(m.from_station, bus_type)
            to_name = get_station_name(m.to_station, bus_type)
            reply = FlexMessage(alt_text="任務已停止", contents=FlexContainer.from_dict(create_base_flex_card("🛑 停止成功", [{"type": "text", "text": f"已成功停止：\n{m.travel_date} {from_name}➡️{to_name}", "wrap": True, "size": "sm"}])))
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[reply]))
        else:
            line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="⚠️ 停止失敗：找不到該任務，可能已由系統完成或已手動移除。")]))
        return

    if text == "確認取消:否":
        line_bot_api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="👌 好的，監控將繼續執行！")]))
        return
```

- [ ] **步驟 2：執行 Python 語法檢查並驗證整合性**

運行：`python -m py_compile line_bot.py`
預期：PASS

- [ ] **步驟 3：Commit**

```bash
git add line_bot.py
git commit -m "feat(line-bot): 實作最終取消指令處理邏輯"
```
