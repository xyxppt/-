import os
import json
import time
from datetime import datetime
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAIN_PAGE_URL = "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/OSC/api/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/OSC/api/public/applyOnline/getTrainingList"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SEEN_FILE = "seen_courses.json"

def load_seen_courses():
    """讀取記憶庫檔案"""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"讀取記憶檔失敗: {e}", flush=True)
    return set()

def save_seen_courses(seen_set):
    """更新記憶庫檔案"""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_set), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入記憶檔失敗: {e}", flush=True)

def send_discord_log(title, description, color=3066993, fields=None):
    """傳送系統運作診斷至 Discord"""
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": "臺灣職安卡 24H 雲端監控系統"}
    }
    if fields:
        embed["fields"] = fields
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"Discord 傳送失敗: {e}", flush=True)

def broadcast_line_message(text_message):
    """發送 LINE 通報"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return False, "未設定 LINE Token"
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    try:
        res = requests.post(url, headers=headers, json={"messages": [{"type": "text", "text": text_message}]}, timeout=10)
        if res.status_code == 200:
            return True, "200 OK（發送成功）"
        else:
            return False, f"HTTP {res.status_code} ({res.text})"
    except Exception as e:
        return False, f"連線例外: {str(e)}"

def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def check_training_courses():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    seen_courses = load_seen_courses()
    
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL
    })

    line_status_str = "未發現新課程（LINE 保持靜音）"

    try:
        # 1. Auth Token 驗證
        session.get(MAIN_PAGE_URL, timeout=10)
        auth_res = session.post(AUTH_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        if auth_res.status_code in [200, 201]:
            token = auth_res.json().get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})

        # 2. 抓取官網課程
        res = session.get(TRAINING_LIST_URL, timeout=10)
        json_data = res.json() if res.status_code == 200 else {}
        training_list = json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data

        today_str = datetime.now().strftime("%Y/%m/%d")
        all_available = []
        new_courses = []

        for item in training_list:
            total_capacity = safe_int(item.get("numberOfPeopleSignUp", 0))
            signed_up = safe_int(item.get("numberOfPeople", 0))
            remaining = total_capacity - signed_up
            raw_date = str(item.get("trDate", "")).replace("-", "/")
            organizer = item.get("organizerName", "未知單位")

            # 課程獨一無二的識別 key
            course_key = f"{raw_date}_{organizer}"

            if raw_date >= today_str and remaining > 0:
                item["remaining"] = remaining
                item["total_capacity"] = total_capacity
                item["signed_up"] = signed_up
                item["key"] = course_key
                all_available.append(item)

                # 比對記憶庫：如果不在 seen_courses 裡面，代表是新課程！
                if course_key not in seen_courses:
                    new_courses.append(item)

        # 3. 只有有「新課程」時才發送 LINE 通知
        if new_courses:
            line_msg = f"🚨【臺灣職安卡】發現全新釋出課程！\n\n"
            for c in new_courses[:5]:
                line_msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                line_msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                line_msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                line_msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                line_msg += "------------------------------\n"
                seen_courses.add(c["key"]) # 把新課程寫入歷史紀錄
                
            line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
            
            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ 已推播 {len(new_courses)} 筆新課程至 LINE ({status_desc})"
            save_seen_courses(seen_courses)

        # 4. 傳送系統診斷回 Discord
        fields = [
            {"name": "💬 LINE 推播狀態", "value": line_status_str, "inline": False},
            {"name": "📊 官網課程即時統計", "value": f"全網總課程: {len(training_list)} 筆 | 開放報名中: {len(all_available)} 筆 | 本次新釋出: {len(new_courses)} 筆", "inline": False}
        ]

        if new_courses:
            summary = "".join([f"• **{c.get('trDate')}** | {c.get('organizerName')} (剩 {c.get('remaining')} 人)\n" for c in new_courses[:3]])
            fields.append({"name": "🆕 新發現課程摘要", "value": summary, "inline": False})

        send_discord_log(
            title="🎯 發現全新課程！" if new_courses else "✅ 系統監控正常（無新課程）",
            description=f"**執行時間**：{current_time}",
            color=3447003 if new_courses else 3066993,
            fields=fields
        )

    except Exception as e:
        send_discord_log(
            title="🚨 系統連線或解析失敗",
            description=f"**執行時間**：{current_time}\n**錯誤資訊**：`{str(e)}`",
            color=15158332
        )

if __name__ == "__main__":
    check_training_courses()
