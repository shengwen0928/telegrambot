import sys
import os
from bs4 import BeautifulSoup

def apply_fix():
    path = 'src/tr_api.py'
    with open(path, 'rb') as f:
        content = f.read().decode('utf-8')
    
    start = content.find('async def guest_book_ticket')
    end = content.find('async def query_trains')
    
    if start == -1 or end == -1:
        print("Error: Could not find function boundaries")
        return

    new_fn = """    async def guest_book_ticket(self, pid: str, from_stn: str, to_stn: str, date: str, start_time: str, end_time: str, num_tickets: int = 1) -> bool:
        \"\"\"
        使用「快速訂票 (訪客模式)」直接訂票。
        \"\"\"
        try:
            # --- 重要：Session 連貫性機制 ---
            # 直接在 query 頁面獲取 CSRF 與 Token，並保持 client cookies
            url_query = f"{self.BASE_URL}/tip001/tip121/query"
            resp_query = await self.client.get(url_query)
            resp_query.raise_for_status()
            
            soup_query = BeautifulSoup(resp_query.text, 'html.parser')
            csrf = soup_query.find('input', {'name': '_csrf'})
            self.csrf_token = csrf['value'] if csrf else ""
            
            token_input = soup_query.find('input', {'name': 'quickTipToken'})
            self.complete_token = token_input['value'] if token_input else ""
            
            at_input = soup_query.find('input', {'name': 'action-token'})
            self.action_token = at_input['value'] if at_input else ""

            # 更新擬真 Headers
            self.client.headers.update({
                "Referer": url_query,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Origin": "https://tip.railway.gov.tw"
            })
            
            # 2. 獲取驗證碼 (最多嘗試 3 次辨識出 6 碼)
            captcha_text = ""
            for _ in range(3):
                # 使用同一個 client 以確保帶上 query 頁面的 Cookies
                img_resp = await self.client.get(f"{self.BASE_URL}/player/picture")
                if img_resp.status_code == 200:
                    c = self.ocr.classify(img_resp.content)
                    import re
                    c = re.sub(r'[^a-zA-Z0-9]', '', c)
                    if len(c) == 6:
                        captcha_text = c
                        break
            
            if not captcha_text:
                logger.error("無法獲取有效的 6 碼驗證碼，停止本次重試。")
                return False

            url_book = f"{self.BASE_URL}/tip001/tip121/bookingTicket"
            from_full = f"{from_stn}-{TR_STATIONS.get(from_stn, '')}"
            to_full = f"{to_stn}-{TR_STATIONS.get(to_stn, '')}"
            
            # 完整模擬官方 Payload
            payload = {
                "_csrf": self.csrf_token,
                "custIdTypeEnum": "PERSON_ID",
                "_custIdTypeEnum": "on",
                "pid": pid.upper(),
                "startStation": from_full,
                "endStation": to_full,
                "tripType": "ONEWAY",
                "orderType": "BY_TIME",
                "normalQty": str(num_tickets),
                "wheelChairQty": "0",
                "parentChildQty": "0",
                "ticketOrderParamList[0].tripNo": "TRIP1",
                "ticketOrderParamList[0].rideDate": date.replace("-", "/"),
                "ticketOrderParamList[0].startOrEndTime": "true",
                "ticketOrderParamList[0].startTime": start_time,
                "ticketOrderParamList[0].endTime": end_time,
                "ticketOrderParamList[0].chgSeat": "true",
                "_ticketOrderParamList[0].chgSeat": "on",
                "ticketOrderParamList[0].seatPref": "NONE",
                "ticketOrderParamList[0].trainTypeList": ["11", "1", "2", "3", "4", "5"],
                "_ticketOrderParamList[0].trainTypeList": ["on"] * 6,
                "g-recaptcha-response": captcha_text,
                "verifyType": "text",
                "isSecondVerify": "true",
                "quickTipToken": self.complete_token,
                "action-token": self.action_token,
                "action-name": "submit_form"
            }
            
            import asyncio, random
            await asyncio.sleep(random.uniform(0.5, 1.2))
            
            # 執行訂票 POST (禁用自動重定向以觀察狀態)
            resp = await self.client.post(url_book, data=payload, follow_redirects=False)
            
            if "訂票成功" in resp.text or "成功代碼" in resp.text:
                logger.info("恭喜！台鐵訪客訂票成功！")
                return True
            
            if resp.status_code == 302:
                location = resp.headers.get("Location", "")
                if "query" not in location and ("tip121" in location or "tip123" in location):
                    logger.info(f"檢測到成功跳轉: {location}")
                    return True
                
                # 抓取真正的錯誤訊息
                error_url = location if location.startswith("http") else f"https://tip.railway.gov.tw{location}"
                err_resp = await self.client.get(error_url)
                err_soup = BeautifulSoup(err_resp.text, 'html.parser')
                
                errors = [s.text.strip() for s in err_soup.find_all('span', class_='error')]
                alerts = [div.text.strip() for div in err_soup.find_all('div', class_='alert') if "認明本公司" not in div.text]

                if errors:
                    logger.error(f"訂票失敗，官方錯誤: {', '.join(errors)}")
                elif alerts:
                    logger.error(f"訂票失敗，官方警告: {', '.join(alerts)}")
                else:
                    logger.error("訂票失敗，跳轉回查詢頁面 (原因：驗證碼不符)，將繼續自動重試。")

            return False
        except Exception as e:
            logger.error(f"訪客訂票發生異常: {e}")
            return False

\n"""
    
    new_content = content[:start] + new_fn + content[end:]
    with open(path, 'wb') as f:
        f.write(new_content.encode('utf-8'))
    print("Success: applied TRA Session fix")

if __name__ == "__main__":
    apply_fix()
