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
GITHUB_EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "manual")

def broadcast_line_message(text_message):
    """發送 LINE 廣播並印出詳細回應"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("【錯誤】未讀取到 LINE_CHANNEL_ACCESS_TOKEN！請檢查 GitHub Secrets！", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    payload = {"messages": [{"type": "text", "text": text_message}]}
    
    try:
        print(f"正在發送 LINE 訊息 (字數: {len(text_message)})...", flush=True)
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"LINE API 回應狀態碼: {res.status_code}", flush=True)
        print(f"LINE API 回應內文: {res.text}", flush=True)
        if res.status_code == 200:
            print("🎉 LINE 廣播發送成功！", flush=True)
    except Exception as e:
        print(f"發送 LINE 訊息例外: {e}", flush=True)

def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def check_training_courses():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行職安卡課程檢查...", flush=True)
    print(f"當前觸發事件: {GITHUB_EVENT_NAME}", flush=True)
    
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL
    })

    try:
        # 1. Session & Auth Token
        session.get(MAIN_PAGE_URL, timeout=10)
        auth_res = session.post(
            AUTH_TOKEN_URL, 
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        print(f"authToken API 狀態碼: {auth_res.status_code}", flush=True)
        
        if auth_res.status_code in [200, 201]:
            data = auth_res.json()
            token = data.get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})
                print("成功取得授權 Token！", flush=True)

        # 2. 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
        print(f"getTrainingList API 狀態碼: {res.status_code}", flush=True)
        
        if res.status_code == 200:
            json_data = res.json()
            training_list = json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data

            today_str = datetime.now().strftime("%Y/%m/%d")
            available_courses = []

            for item in training_list:
                total_capacity = safe_int(item.get("numberOfPeopleSignUp", 0))
                signed_up = safe_int(item.get("numberOfPeople", 0))
                remaining = total_capacity - signed_up
                raw_date = str(item.get("trDate", "")).replace("-", "/")

                if raw_date >= today_str and remaining > 0:
                    item["remaining"] = remaining
                    item["total_capacity"] = total_capacity
                    item["signed_up"] = signed_up
                    available_courses.append(item)

            print(f"分析完成：共找到 {len(available_courses)} 筆可報名課程。", flush=True)

            # 3. 發送判斷 logic
            is_manual_trigger = (GITHUB_EVENT_NAME in ["workflow_dispatch", "manual"])

            if available_courses:
                msg = f"🚨【臺灣職安卡】發現可報名課程！\n\n"
                for c in available_courses[:5]:
                    msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                    msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                    msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                    msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                    msg += "------------------------------\n"
                msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                broadcast_line_message(msg)

            elif is_manual_trigger:
                msg = "⚙️【臺灣職安卡】監控系統已成功連線！\n\n" \
                      "目前官網暫無可報名課程。\n" \
                      "系統已進入背景輪詢模式（每 30 分鐘自動檢查），有釋出名額將會第一時間通知您！"
                broadcast_line_message(msg)

            else:
                print("排程觸發且無可報名課程，保持靜默不發送。", flush=True)

    except Exception as e:
        print(f"抓取課程資料時發生錯誤: {e}", flush=True)

if __name__ == "__main__":
    check_training_courses()
