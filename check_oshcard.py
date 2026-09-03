import os
import time
import requests

# 1. API 網址設定（修復 404 錯誤）
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/getTrainingList"

# 2. 從環境變數讀取 LINE Messaging API 的 Channel Access Token
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def broadcast_line_message(text_message):
    """使用 LINE Messaging API 的 Broadcast (廣播) 功能發送訊息給所有好友"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("【錯誤】未讀取到 LINE_CHANNEL_ACCESS_TOKEN，請檢查 GitHub Secrets！", flush=True)
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
        print(f"LINE API HTTP 狀態碼: {res.status_code}", flush=True)
        print(f"LINE API 回應訊息: {res.text}", flush=True)
        
        if res.status_code == 200:
            print("🎉 LINE 廣播發送成功！", flush=True)
        else:
            print("❌ LINE 廣播發送失敗，請對照上方的 HTTP 狀態碼與回應訊息排查。", flush=True)
    except Exception as e:
        print(f"發送 LINE 訊息時發生例外錯誤: {e}", flush=True)

def get_auth_token(session):
    """取得臺灣職安卡 API 的動態 JWT Token"""
    try:
        res = session.post(AUTH_TOKEN_URL, json={}, timeout=10)
        if res.status_code in [200, 201]:
            data = res.json()
            token = data.get("token") or data.get("authToken") or res.headers.get("Authorization")
            return token
    except Exception as e:
        print(f"取得職安卡 Token 失敗: {e}", flush=True)
    return None

def check_training_courses():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 開始執行職安卡課程檢查...", flush=True)
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
    })

    # 1. 取得 Authorization Token
    token = get_auth_token(session)
    if token:
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        session.headers.update({"Authorization": token})

    try:
        # 2. 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
        print(f"課程列表 API 狀態碼: {res.status_code}", flush=True)
        
        if res.status_code == 200:
            json_data = res.json()
            training_list = json_data.get("trainingList", [])
            print(f"成功擷取到 {len(training_list)} 筆課程資料。", flush=True)

            available_courses = []
            for item in training_list:
                total_capacity = item.get("numberOfPeople", 0)
                signed_up = item.get("numberOfPeopleSignUp", 0)

                # ========================================================
                # 測試時：改成 if True: (強迫發送通知測試 LINE 功能)
                # 正式時：保持 if signed_up < total_capacity: (有名額才發送)
                # ========================================================
                if True:  
                    remaining = total_capacity - signed_up
                    item["remaining"] = remaining
                    available_courses.append(item)

            # 3. 發送 LINE 廣播通知
            if available_courses:
                print(f"發現 {len(available_courses)} 門可報名/測試課程，準備發送 LINE 通知...", flush=True)
                
                # 避免測試時字數過長，只列出前 3 筆範例
                display_courses = available_courses[:3] if len(available_courses) > 3 else available_courses
                
                msg = f"🚨【臺灣職安卡】發現課程資訊！\n\n"
                for c in display_courses:
                    msg += f"📅 日期：{c.get('trDate')}\n"
                    msg += f"🏢 單位：{c.get('organizerName')}\n"
                    msg += f"📍 地點：{c.get('location')}\n"
                    msg += f"🎟️ 剩餘名額：{c.get('remaining')} / {c.get('numberOfPeople')}\n"
                    msg += "------------------------------\n"
                
                msg += "\n🔗 點此前往報名：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                
                broadcast_line_message(msg)
            else:
                print("檢查完成：目前所有課程皆已額滿，不發送通知。", flush=True)

    except Exception as e:
        print(f"抓取課程資料時發生錯誤: {e}", flush=True)

if __name__ == "__main__":
    check_training_courses()
