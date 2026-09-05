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

# 離島過濾關鍵字
EXCLUDED_KEYWORDS = ["澎湖", "連江", "馬祖", "金門"]

def load_and_clean_seen_courses():
    """讀取記憶庫並自動清除開課日期已過期的舊紀錄"""
    today_str = datetime.now().strftime("%Y/%m/%d")
    raw_dict = {}

    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        raw_dict[item] = 0
                elif isinstance(data, dict):
                    raw_dict = data
        except Exception as e:
            print(f"讀取記憶檔失敗: {e}", flush=True)

    cleaned_dict = {}
    for key, count in raw_dict.items():
        try:
            course_date = key.split("_")[0]
            if course_date >= today_str:
                cleaned_dict[key] = count
        except Exception:
            cleaned_dict[key] = count

    return cleaned_dict

def save_seen_courses(seen_dict):
    """更新記憶庫檔案"""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入記憶檔失敗: {e}", flush=True)

def get_retry_session():
    """建立具備自動重試功能的 HTTP Session"""
    session = requests.Session()
    session.verify = False
    
    retries = Retry(
        total=3,
        backoff_factor=2,
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

def fetch_training_list(session):
    """向官網抓取原始課程清單"""
    res = session.get(TRAINING_LIST_URL, timeout=10)
    json_data = res.json() if res.status_code == 200 else {}
    return json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data

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

        # 2. 抓取官網課程
        training_list = fetch_training_list(session)
        today_str = datetime.now().strftime("%Y/%m/%d")
        
        all_available = []
        candidate_courses = []
        current_active_keys = set()

        for item in training_list:
            # 相容欄位名稱
            total_capacity = safe_int(item.get("numberOfPeople", item.get("numberOfPeopleSignUp", 0)))
            signed_up = safe_int(item.get("numberOfPeopleSignUp", item.get("numberOfPeople", 0)))
            
            # 若 total < signed_up，修正為邏輯正確方向
            if total_capacity < signed_up and signed_up > 0:
                total_capacity, signed_up = signed_up, total_capacity

            remaining = total_capacity - signed_up

            raw_date = str(item.get("trDate", "")).replace("-", "/")
            organizer = item.get("organizerName", "未知單位")
            location = item.get("location", "")
            
            # 官網狀態欄位過濾
            apply_status = str(item.get("applyStatus", "")).strip()  # 報名狀態
            is_full = str(item.get("isFull", "")).upper()          # 是否額滿 (Y/N)
            is_cancel = str(item.get("isCancel", "")).upper()      # 是否取消 (Y/N)

            # 1. 離島過濾
            if any(kw in organizer or kw in location for kw in EXCLUDED_KEYWORDS):
                continue

            # 2. 官網隱藏狀態過濾：已被標記為已額滿、取消、或非開放狀態則忽略
            if is_full == "Y" or is_cancel == "Y" or "額滿" in apply_status or "截止" in apply_status:
                continue

            course_key = f"{raw_date}_{organizer}"

            if raw_date >= today_str and remaining > 0:
                item["remaining"] = remaining
                item["total_capacity"] = total_capacity
                item["signed_up"] = signed_up
                item["key"] = course_key
                all_available.append(item)
                current_active_keys.add(course_key)

                prev_remaining = seen_courses.get(course_key, 0)
                if prev_remaining == 0:
                    item["notify_reason"] = "🆕 全新課程釋出"
                    candidate_courses.append(item)
                elif remaining > prev_remaining:
                    item["notify_reason"] = f"🔄 有人退選釋出名額 (+{remaining - prev_remaining} 人)"
                    candidate_courses.append(item)

        # 3. 延遲雙重驗證 (防秒殺與幽靈名額)
        real_notify_courses = []
        ghost_courses = []

        if candidate_courses:
            time.sleep(3)
            try:
                recheck_list = fetch_training_list(session)
                if recheck_list:
                    recheck_map = {}
                    for r_item in recheck_list:
                        r_tot = safe_int(r_item.get("numberOfPeople", r_item.get("numberOfPeopleSignUp", 0)))
                        r_sig = safe_int(r_item.get("numberOfPeopleSignUp", r_item.get("numberOfPeople", 0)))
                        if r_tot < r_sig and r_sig > 0:
                            r_tot, r_sig = r_sig, r_tot
                        r_rem = r_tot - r_sig
                        
                        r_date = str(r_item.get("trDate", "")).replace("-", "/")
                        r_org = r_item.get("organizerName", "未知單位")
                        r_full = str(r_item.get("isFull", "")).upper()
                        r_status = str(r_item.get("applyStatus", ""))

                        if r_full != "Y" and "額滿" not in r_status:
                            recheck_map[f"{r_date}_{r_org}"] = r_rem

                    for cand in candidate_courses:
                        ckey = cand["key"]
                        rc_rem = recheck_map.get(ckey, 0)
                        if rc_rem > 0:
                            real_notify_courses.append(cand)
                            seen_courses[ckey] = rc_rem
                        else:
                            ghost_courses.append(cand)
                            seen_courses[ckey] = 0
                else:
                    real_notify_courses = candidate_courses
                    for cand in candidate_courses:
                        seen_courses[cand["key"]] = cand["remaining"]
            except Exception:
                real_notify_courses = candidate_courses
                for cand in candidate_courses:
                    seen_courses[cand["key"]] = cand["remaining"]

        # 4. 推播至 LINE
        if real_notify_courses:
            line_msg = f"🚨【臺灣職安卡】名額異動通報！\n\n"
            for c in real_notify_courses[:5]:
                line_msg += f"📌 狀態：{c['notify_reason']}\n"
                line_msg += f"📅 日期：{c.get('trDate', 'N/A')}\n"
                line_msg += f"🏢 單位：{c.get('organizerName', 'N/A')}\n"
                line_msg += f"📍 地點：{c.get('location', 'N/A')}\n"
                line_msg += f"🎟️ 剩餘名額：{c.get('remaining', 0)} 人 (已報名 {c.get('signed_up', 0)} / {c.get('total_capacity', 0)})\n"
                line_msg += "------------------------------\n"
            line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
            
            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ 已推播 {len(real_notify_courses)} 筆真實異動至 LINE ({status_desc})"

        save_seen_courses(seen_courses)

        # 5. Discord 回報
        fields = [
            {"name": "💬 LINE 推播狀態", "value": line_status_str, "inline": False},
            {"name": "📊 官網課程統計 (本島)", "value": f"開放報名中: {len(all_available)} 筆 | 本次推播: {len(real_notify_courses)} 筆", "inline": False}
        ]

        if real_notify_courses:
            summary = "".join([f"• **[{c['notify_reason']}] {c.get('trDate')}** | {c.get('organizerName')} (剩 {c.get('remaining')} 人)\n" for c in real_notify_courses[:3]])
            fields.append({"name": "🎯 通報摘要", "value": summary, "inline": False})

        if ghost_courses:
            ghost_summary = "".join([f"• 👻 **{c.get('trDate')}** | {c.get('organizerName')} (官網已標記額滿/隱藏)\n" for c in ghost_courses])
            fields.append({"name": "👻 濾除隱藏/幽靈課程", "value": ghost_summary, "inline": False})

        send_discord_log(
            title="🎯 發現真實可報名課程！" if real_notify_courses else "✅ 系統監控正常（無名額異動）",
            description=f"**執行時間**：{current_time}",
            color=3447003 if real_notify_courses else 3066993,
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
