import os
import time
import requests

# 1. API 網址設定
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/getTrainingList"

# 2. 從環境變數讀取 LINE Messaging API 的 Channel Access Token
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def broadcast_line_message(text_message):
    """使用 LINE Messaging API 的 Broadcast (廣播) 功能發送訊息"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("【錯誤】未讀取到 LINE_CHANNEL_ACCESS_TOKEN！", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    payload = {
        "messages": [
            {
                "type": "text",
                "text": text_message
            }
        ]
    }
    
    try:
        print("正在向所有好友發送 LINE 廣播通知...", flush=True)
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"LINE API 狀態碼: {res.status_code}", flush=True)
        print(f"LINE API 回應內容: {res.text}", flush=True)
    except Exception as e:
        print(f"發送 LINE 訊息時發生例外錯誤: {e}", flush=True)

def check_training_courses():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行職安卡課程檢查...", flush=True)
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Referer": "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
    })

    try:
        # 1. 取得 Authorization Token (必須使用 POST)
        auth_res = session.post(AUTH_TOKEN_URL, json={}, timeout=10)
        if auth_res.status_code in [200, 201]:
            token = auth_res.json().get("token") or auth_res.json().get("authToken")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})
                print("成功取得職安卡授權 Token！", flush=True)
        
        # 2. 抓取課程資料 (必須使用 POST 才能成功取得)
        res = session.post(TRAINING_LIST_URL, json={}, timeout=10)
        print(f"課程列表 API 狀態碼: {res.status_code}", flush=True)
        
        if res.status_code == 200:
            json_data = res.json()
            training_list = json_data.get("trainingList", [])
            print(f"成功擷取到 {len(training_list)} 筆課程資料。", flush=True)

            available_courses = []
            for item in training_list:
                total_capacity = item.get("numberOfPeople", 0)
                signed_up = item.get("numberOfPeopleSignUp", 0)

                # 強制設為 True 測試發送
                if True:  
                    remaining = total_capacity - signed_up
                    item["remaining"] = remaining
                    available_courses.append(item)

            # 3. 發送 LINE 廣播通知
            if available_courses:
                print(f"準備發送 LINE 通知...", flush=True)
                display_courses = available_courses[:3]
                
                msg = f"🚨【臺灣職安卡】測試通知！\n\n"
                for c in display_courses:
                    msg += f"📅 日期：{c.get('trDate')}\n"
                    msg += f"🏢 單位：{c.get('organizerName')}\n"
                    msg += f"📍 地點：{c.get('location')}\n"
                    msg += f"🎟️ 剩餘名額：{c.get('remaining')} / {c.get('numberOfPeople')}\n"
                    msg += "------------------------------\n"
                
                msg += "\n🔗 點此前往報名：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                
                broadcast_line_message(msg)

    except Exception as e:
        print(f"抓取課程資料時發生錯誤: {e}", flush=True)

if __name__ == "__main__":
    check_training_courses()
