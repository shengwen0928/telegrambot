import httpx
import pytest
import os
import sys

# 將 src 目錄加入 path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.ocr_engine import OCREngine

def test_ocr_recognition():
    """測試 OCR 引擎是否能正確辨識從和欣客運下載的驗證碼。"""
    engine = OCREngine()
    # 和欣客運驗證碼圖片網址
    url = "https://www.ebus.com.tw/Common/GetCaptchaImage"
    
    # 建立一個不檢查 SSL 的 client (避免測試環境 SSL 問題)
    with httpx.Client(verify=False) as client:
        response = client.get(url)
        assert response.status_code == 200
        image_bytes = response.content
        assert len(image_bytes) > 0
        
        # 進行辨識
        result = engine.classify(image_bytes)
        print(f"\n[OCR 測試結果] 辨識出的驗證碼為: {result}")
        
        # 驗證結果格式：應為 4 位字元 (通常是數字或字母)
        assert len(result) == 4
        assert result.isalnum()
