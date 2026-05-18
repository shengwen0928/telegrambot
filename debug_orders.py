import asyncio
import os
import json
from src.hohsin_api import HohsinAPI
from dotenv import load_dotenv

async def dump_orders():
    load_dotenv()
    api = HohsinAPI()
    
    user = os.getenv("USER_PHONE")
    password = os.getenv("USER_PASSWORD")
    
    print(f"嘗試登入使用者: {user}")
    if await api.login(user, password):
        print("✅ 登入成功！正在獲取訂單...")
        orders = await api.get_my_orders()
        
        if not orders:
            print("📭 目前帳號下無訂單。請確認此帳號是否有已訂購但未搭乘的票。")
        else:
            print(f"📂 找到 {len(orders)} 筆訂單。正在獲取第一張票的詳細資料...")
            first_order = orders[0]
            tickets = first_order.get("tickets", [])
            if tickets:
                first_ticket_id = tickets[0].get("id")
                first_ticket_no = tickets[0].get("ticketNo")
                print(f"🎫 嘗試下載車票 ID: {first_ticket_id} (票號: {first_ticket_no}) 的官方 QR Code...")
                
                qr_bytes = await api.download_official_qrcode(first_ticket_id)
                if qr_bytes:
                    filename = f"official_qr_{first_ticket_no}.png"
                    with open(filename, "wb") as f:
                        f.write(qr_bytes)
                    print(f"✅ 下載成功！圖片已儲存為: {filename}")
                else:
                    print("❌ 下載失敗。官方 API 可能不支援此路徑，或需要特定參數。")
            else:
                print("⚠️ 訂單內沒有車票資訊。")
    else:
        print("❌ 登入失敗，請檢查帳密與驗證碼。")

if __name__ == "__main__":
    asyncio.run(dump_orders())
