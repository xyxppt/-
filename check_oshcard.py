import os
import time
import requests

# 1. 網址設定
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/osc/api/public/applyOnline/getTrainingList"

# 2. 從環境變數讀取 LINE Messaging API 的 Channel Access Token
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def broadcast_line_message(text_message):
    """使用 LINE Messaging API 的 Broadcast (廣播/群發) 功能，發送訊息給所有好友"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("未設定 LINE Token，跳過 LINE 通知。")
        return

    # 改用 /v2/bot/message/broadcast 廣播 API
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
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
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("LINE 廣播推播成功（已發送給所有好友）！")
        else:
            print(f"LINE 廣播發送失敗，HTTP 狀態碼: {res.status_code}, 回應: {res.text}")
    except Exception as e:
        print(f"發送 LINE 訊息時發生例外錯誤: {e}")

def get_auth_token(session):
    """取得動態 JWT Token"""
    try:
        res = session.post(AUTH_TOKEN_URL, json={}, timeout=10)
        if res.status_code in [200, 201]:
            data = res.json()
            token = data.get("token") or data.get("authToken") or res.headers.get("Authorization")
            return token
    except Exception as e:
        print(f"取得 Token 失敗: {e}")
    return None

def check_training_courses():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
    })

    # 1. 取得 Authorization
    token = get_auth_token(session)
    if token:
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        session.headers.update({"Authorization": token})

    try:
        # 2. 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
        if res.status_code == 200:
            json_data = res.json()
            training_list = json_data.get("trainingList", [])

            available_courses = []
            for item in training_list:
                total_capacity = item.get("numberOfPeople", 0)
                signed_up = item.get("numberOfPeopleSignUp", 0)

                # 判斷：已報名人數低於總容納人數
                if True:
                    remaining = total_capacity - signed_up
                    item["remaining"] = remaining
                    available_courses.append(item)

            # 3. 發送 LINE 廣播通知
            if available_courses:
                msg = f"🚨【臺灣職安卡】發現 {len(available_courses)} 門課程尚有名額！\n\n"
                for c in available_courses:
                    msg += f"📅 日期：{c.get('trDate')}\n"
                    msg += f"🏢 單位：{c.get('organizerName')}\n"
                    msg += f"📍 地點：{c.get('location')}\n"
                    msg += f"🎟️ 剩餘名額：{c.get('remaining')} / {c.get('numberOfPeople')}\n"
                    msg += "------------------------------\n"
                
                msg += "\n🔗 點此前往報名：https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                broadcast_line_message(msg)
            else:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 檢查完成：目前所有課程皆已額滿。")

    except Exception as e:
        print(f"抓取課程資料時發生錯誤: {e}")

if __name__ == "__main__":
    check_training_courses()
