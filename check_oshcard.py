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
    """發送診斷日誌至 Discord (Embed 卡片)"""
    if not DISCORD_WEBHOOK_URL:
        print("【提示】未設定 DISCORD_WEBHOOK_URL，跳過 Discord 發送。", flush=True)
        return

    embed = {
        "title": title,
        "description": description,
        "color": color, # 3066993: 綠色, 15158332: 紅色, 3447003: 藍色
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "footer": {"text": "臺灣職安卡 系統全效能診斷監控"}
    }
    if fields:
        embed["fields"] = fields

    payload = {"embeds": [embed]}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"發送 Discord 訊息失敗: {e}", flush=True)

def broadcast_line_message(text_message):
    """發送 LINE 廣播並回傳執行結果與狀態細節"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return False, "未設定 LINE_CHANNEL_ACCESS_TOKEN 密鑰"

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
    }
    payload = {"messages": [{"type": "text", "text": text_message}]}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            return True, "200 OK（發送成功）"
        else:
            return False, f"HTTP {res.status_code} ({res.text})"
    except Exception as e:
        return False, f"連線例外錯誤: {str(e)}"

def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

def check_training_courses():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    is_manual = (GITHUB_EVENT_NAME in ["workflow_dispatch", "manual"])
    trigger_type = "手動點擊執行" if is_manual else "背景 30 分鐘自動排程"
    
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL
    })

    line_status_str = "未觸發發送"
    system_errors = []

    try:
        # 1. 取得 Session & Auth Token
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
            else:
                system_errors.append("AuthToken API 未傳回有效 Token 欄位")
        else:
            system_errors.append(f"AuthToken API 請求失敗 (HTTP {auth_res.status_code})")

        # 2. 抓取課程資料
        res = session.get(TRAINING_LIST_URL, timeout=10)
        if res.status_code != 200:
            system_errors.append(f"getTrainingList API 請求失敗 (HTTP {res.status_code})")
            json_data = {}
        else:
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

        # 3. 根據結果進行 LINE 廣播並記錄狀態
        if available_courses:
            line_msg = f"🚨【臺灣職安卡】發現可報名課程！\n\n"
            for c in available_courses[:5]:
                line_msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                line_msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                line_msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                line_msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                line_msg += "------------------------------\n"
            line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"

            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ {status_desc}" if success else f"❌ {status_desc}"

        elif is_manual:
            line_msg = "⚙️【臺灣職安卡】監控系統已成功連線！\n目前官網暫無可報名課程，已開始在背景監控。"
            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ {status_desc}" if success else f"❌ {status_desc}"

        # 4. 彙整診斷欄位並傳送至 Discord
        fields = [
            {"name": "⚙️ 觸發模式", "value": trigger_type, "inline": True},
            {"name": "💬 LINE 發送狀態", "value": line_status_str, "inline": True},
            {"name": "📊 官網課程資料", "value": f"總課程數: {len(training_list)} 筆 | 可報名: {len(available_courses)} 筆", "inline": False}
        ]

        if available_courses:
            course_summary = ""
            for c in available_courses[:3]:
                course_summary += f"• **{c.get('trDate')}** | {c.get('organizerName')} (剩 {c.get('remaining')} 人)\n"
            fields.append({"name": "🎯 可報名課程摘要", "value": course_summary, "inline": False})

        if system_errors:
            fields.append({"name": "⚠️ 過程警告/異常", "value": "\n".join(system_errors), "inline": False})
            send_discord_log(
                title="⚠️ 系統運作完成（含警告訊息）",
                description=f"**檢查時間**：{current_time}\n腳本已執行，但過程中捕捉到異常狀態。",
                color=15158332, # 紅/橘色
                fields=fields
            )
        else:
            send_discord_log(
                title="✅ 系統全流程健康檢查報告",
                description=f"**檢查時間**：{current_time}\n官網 API 連線正常、LINE 介面發送驗證完畢。",
                color=3066993, # 綠色
                fields=fields
            )

    except Exception as e:
        error_msg = str(e)
        fields = [
            {"name": "⚙️ 觸發模式", "value": trigger_type, "inline": True},
            {"name": "💬 LINE 發送狀態", "value": line_status_str, "inline": True},
            {"name": "💥 系統崩潰例外", "value": f"`{error_msg}`", "inline": False}
        ]
        send_discord_log(
            title="🚨 系統運作嚴重失敗",
            description=f"**檢查時間**：{current_time}\n腳本在執行過程中中斷崩潰。",
            color=15158332,
            fields=fields
        )

if __name__ == "__main__":
    check_training_courses()
