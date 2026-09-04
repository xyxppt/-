import os
import json
import time
from datetime import datetime
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAIN_PAGE_URL = "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/OSC/api/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/OSC/api/public/applyOnline/getTrainingList"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SEEN_FILE = "seen_courses.json"

# 優化 1：離島地區過濾關鍵字
EXCLUDED_KEYWORDS = ["澎湖", "連江", "馬祖", "金門"]

def load_and_clean_seen_courses():
    """優化 3：讀取記憶庫並自動清除開課日期已過期的舊紀錄"""
    today_str = datetime.now().strftime("%Y/%m/%d")
    raw_dict = {}

    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 兼容舊版 list 格式 (轉為 dict)
                if isinstance(data, list):
                    for item in data:
                        raw_dict[item] = 0
                elif isinstance(data, dict):
                    raw_dict = data
        except Exception as e:
            print(f"讀取記憶檔失敗: {e}", flush=True)

    # 過濾過期資料 (key 格式為 'YYYY/MM/DD_單位')
    cleaned_dict = {}
    for key, count in raw_dict.items():
        try:
            course_date = key.split("_")[0]
            if course_date >= today_str:
                cleaned_dict[key] = count
        except Exception:
            cleaned_dict[key] = count # 解析失敗則保留

    return cleaned_dict

def save_seen_courses(seen_dict):
    """更新記憶庫檔案"""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入記憶檔失敗: {e}", flush=True)

def get_retry_session():
    """優化 5：建立具備自動重試功能的 HTTP Session (防範政府網站抽風)"""
    session = requests.Session()
    session.verify = False
    
    retries = Retry(
        total=3,                # 最多重試 3 次
        backoff_factor=2,       # 間隔 2s, 4s, 8s 重試
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL
    })
    return session

def send_discord_log(title, description, color=3066993, fields=None):
    """傳送系統診斷卡片至 Discord"""
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
    """發送 LINE Broadcast 通報"""
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
    seen_courses = load_and_clean_seen_courses()
    session = get_retry_session()
    
    line_status_str = "無新課程或名額釋出（LINE 靜音）"

    try:
        # 1. 取得身份驗證 Token
        session.get(MAIN_PAGE_URL, timeout=10)
        auth_res = session.post(AUTH_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        if auth_res.status_code in [200, 201]:
            token = auth_res.json().get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})

        # 2. 抓取官網全網課程
        res = session.get(TRAINING_LIST_URL, timeout=10)
        json_data = res.json() if res.status_code == 200 else {}
        training_list = json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data

        today_str = datetime.now().strftime("%Y/%m/%d")
        all_available = []
        notify_courses = []

        for item in training_list:
            total_capacity = safe_int(item.get("numberOfPeopleSignUp", 0))
            signed_up = safe_int(item.get("numberOfPeople", 0))
            remaining = total_capacity - signed_up
            raw_date = str(item.get("trDate", "")).replace("-", "/")
            organizer = item.get("organizerName", "未知單位")
            location = item.get("location", "")

            # 優化 1：過濾離島（連江、澎湖、馬祖、金門）
            if any(kw in organizer or kw in location for kw in EXCLUDED_KEYWORDS):
                continue

            course_key = f"{raw_date}_{organizer}"

            if raw_date >= today_str and remaining > 0:
                item["remaining"] = remaining
                item["total_capacity"] = total_capacity
                item["signed_up"] = signed_up
                item["key"] = course_key
                all_available.append(item)

                # 優化 2：動態名額遞補追蹤（新課程 或 舊課程名額變多）
                prev_remaining = seen_courses.get(course_key)
                if prev_remaining is None:
                    item["notify_reason"] = "🆕 全新課程釋出"
                    notify_courses.append(item)
                elif remaining > prev_remaining:
                    item["notify_reason"] = f"🔄 有人退選釋出名額 (+{remaining - prev_remaining} 人)"
                    notify_courses.append(item)

                # 更新記憶檔案裡的名額紀錄
                seen_courses[course_key] = remaining

        # 3. 觸發 LINE 通知
        if notify_courses:
            line_msg = f"🚨【臺灣職安卡】名額異動通報！\n\n"
            for c in notify_courses[:5]:
                line_msg += f"📌 狀態：{c['notify_reason']}\n"
                line_msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                line_msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                line_msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                line_msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                line_msg += "------------------------------\n"
                
            line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
            
            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ 已推播 {len(notify_courses)} 筆異動至 LINE ({status_desc})"

        # 寫入更新與清理後的記憶檔
        save_seen_courses(seen_courses)

        # 4. 回傳 Discord 診斷
        fields = [
            {"name": "💬 LINE 推播狀態", "value": line_status_str, "inline": False},
            {"name": "📊 官網課程統計 (本島)", "value": f"開放報名中: {len(all_available)} 筆 | 本次異動通報: {len(notify_courses)} 筆", "inline": False}
        ]

        if notify_courses:
            summary = "".join([f"• **[{c['notify_reason']}] {c.get('trDate')}** | {c.get('organizerName')} (剩 {c.get('remaining')} 人)\n" for c in notify_courses[:3]])
            fields.append({"name": "🎯 通報摘要", "value": summary, "inline": False})

        send_discord_log(
            title="🎯 發現新課程或退選釋出名額！" if notify_courses else "✅ 系統監控正常（無名額異動）",
            description=f"**執行時間**：{current_time}",
            color=3447003 if notify_courses else 3066993,
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
