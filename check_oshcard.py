import os
import time
from datetime import datetime
import requests
import urllib3

# 關閉不安全 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAIN_PAGE_URL = "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/OSC/api/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/OSC/api/public/applyOnline/getTrainingList"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def broadcast_line_message(text_message):
    """使用 LINE Messaging API 發送廣播"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("【錯誤】未讀取到 LINE_CHANNEL_ACCESS_TOKEN！", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    payload = {"messages": [{"type": "text", "text": text_message}]}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("🎉 LINE 廣播發送成功！", flush=True)
        else:
            print(f"LINE 發送失敗，狀態碼: {res.status_code}", flush=True)
    except Exception as e:
        print(f"發送 LINE 訊息例外: {e}", flush=True)

def check_training_courses():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行職安卡課程檢查...", flush=True)
    
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL
    })

    try:
        # 建立 Session 並取得 Auth Token
        session.get(MAIN_PAGE_URL, timeout=10)
        auth_res = session.post(
            AUTH_TOKEN_URL, 
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        
        if auth_res.status_code in [200, 201]:
            data = auth_res.json()
            token = data.get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})

        # 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
        if res.status_code == 200:
            json_data = res.json()
            training_list = json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data

            today_str = datetime.now().strftime("%Y/%m/%d")
            available_courses = []

            for item in training_list:
                total_capacity = item.get("numberOfPeople", 0)
                signed_up = item.get("numberOfPeopleSignUp", 0)
                remaining = total_capacity - signed_up
                tr_date = item.get("trDate", "")

                # 關鍵過濾條件：必須有剩餘名額 (remaining > 0) 且開課日期不早於今天
                if remaining > 0 and tr_date >= today_str:
                    item["remaining"] = remaining
                    available_courses.append(item)

            print(f"目前偵測到 {len(available_courses)} 筆可報名課程。", flush=True)

            # 只有當真的有空位時才發送 LINE 通知
            if available_courses:
                msg = f"🚨【臺灣職安卡】發現可報名課程！\n\n"
                for c in available_courses[:5]:  # 最多列出前 5 筆
                    msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                    msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                    msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                    msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} / {c.get('numberOfPeople', 0)}\n"
                    msg += "------------------------------\n"
                
                msg += "\n🔗 立即前往報名：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                
                broadcast_line_message(msg)
            else:
                print("目前所有未來課程均已額滿，不發送通知。", flush=True)

    except Exception as e:
        print(f"抓取課程資料時發生錯誤: {e}", flush=True)

if __name__ == "__main__":
    check_training_courses()
