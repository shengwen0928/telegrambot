import json
import os
import logging

logger = logging.getLogger("Persistence")
TASKS_FILE = "tasks.json"

def save_tasks_to_file(running_tasks_dict):
    """將所有正在運行的任務參數序列化並存檔"""
    data = []
    for user_id, monitors in running_tasks_dict.items():
        for m in monitors:
            # 判斷業者類型
            monitor_type = m.__class__.__name__
            bus_type = "hohsin" if "Hohsin" in monitor_type else "tra"
            
            task_info = {
                "user_id": user_id,
                "bus_type": bus_type,
                "attempt_count": getattr(m, "attempt_count", 0),
                "last_check_time": getattr(m, "last_check_time", None),
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
            if bus_type == "hohsin":
                task_info["params"].update({
                    "user_phone": getattr(m, "user_phone", None),
                    "user_password": getattr(m, "user_password", None),
                    "manual_seats": getattr(m, "manual_seats", None),
                    "target_schedule_id": getattr(m, "target_schedule_id", None)
                })
            else:
                # 針對台鐵的額外參數
                task_info["params"].update({
                    "user_id_no": getattr(m, "user_id_no", None),
                    "user_password": getattr(m, "user_password", None)
                })
            data.append(task_info)
    
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"已同步 {len(data)} 個任務至 {TASKS_FILE}")
    except Exception as e:
        logger.error(f"儲存任務檔案失敗: {e}")

def load_tasks_from_file():
    """從檔案讀取任務"""
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"讀取任務檔案失敗: {e}")
    return []
