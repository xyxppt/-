import os
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
GITHUB_EVENT_NAME = os.getenv("GITHUB_EVENT_NAME", "manual")

def send_discord_log(title, description, color=3066993, fields=None):
    """傳送 Embed 嵌入卡片至 Discord"""
    if not DISCORD_WEBHOOK_URL:
        print("【提示】未設定 DISCORD_WEBHOOK_URL，跳過 Discord 發送。", flush=True)
        return

    embed = {
        "title": title,
        "description": description,
        "color": color, # 3066993: 綠色, 15158332: 紅色, 3447003: 藍色
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": "臺灣職安卡 GitHub Actions 監控系統"}
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"發送 Discord 訊息失敗: {e}", flush=True)

def broadcast_line_message(text_message):
    """傳送重大通知至 LINE"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("【錯誤】未設定 LINE_CHANNEL_ACCESS_TOKEN！", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    payload = {"messages": [{"type": "text", "text": text_message}]}
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"發送 LINE 訊息例外: {e}", flush=True)

def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def check_training_courses():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{current_time}] 開始執行職安卡課程檢查...", flush=True)
    
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
        
        if auth_res.status_code in [200, 201]:
            data = auth_res.json()
            token = data.get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})

        # 2. 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
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

            is_manual = (GITHUB_EVENT_NAME in ["workflow_dispatch", "manual"])
            trigger_type = "手動點擊執行" if is_manual else "背景 30 分鐘自動排程"

            if available_courses:
                line_msg = f"🚨【臺灣職安卡】發現可報名課程！\n\n"
                discord_fields = []

                for c in available_courses[:5]:
                    course_info = f"📅 日期: {c.get('trDate')}\n📍 地點: {c.get('location')}\n🎟️ 剩餘: {c.get('remaining')} 人 (已報名 {c.get('signed_up')}/{c.get('total_capacity')})"
                    line_msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                    line_msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                    line_msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                    line_msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                    line_msg += "------------------------------\n"
                    
                    discord_fields.append({
                        "name": f"🏢 {c.get('organizerName')}",
                        "value": course_info,
                        "inline": False
                    })

                line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
                
                # 有名額：LINE 與 Discord 同時通知
                broadcast_line_message(line_msg)
                send_discord_log(
                    title="🎉 發現可報名課程！",
                    description=f"**觸發模式**：{trigger_type}\n**檢測時間**：{current_time}",
                    color=3447003,
                    fields=discord_fields
                )
            else:
                # 無名額：僅回傳 Discord 心跳記錄
                send_discord_log(
                    title="✅ 系統健康檢查：連線正常",
                    description=f"**觸發模式**：{trigger_type}\n**檢測時間**：{current_time}\n**目前狀態**：官網暫無釋出名額，持續監控中。",
                    color=3066993
                )
                if is_manual:
                    broadcast_line_message("⚙️【臺灣職安卡】監控系統已成功連線！\n目前官網暫無可報名課程，已開始在背景監控。")

    except Exception as e:
        error_msg = f"抓取課程資料時發生錯誤: {e}"
        print(error_msg, flush=True)
        send_discord_log(
            title="⚠️ 系統運作異常",
            description=f"**時間**：{current_time}\n**錯誤內容**：`{error_msg}`",
            color=15158332
        )

if __name__ == "__main__":
    check_training_courses()
