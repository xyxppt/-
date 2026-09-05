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

# 離島過濾關鍵字（可自行增減）
EXCLUDED_KEYWORDS = ["澎湖", "連江", "馬祖", "金門"]


def load_and_clean_seen_courses():
    """讀取記憶庫，自動清除過期課程（開課日早於今天）"""
    today_str = datetime.now().strftime("%Y/%m/%d")
    raw_dict = {}

    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 兼容舊版 list 格式
                if isinstance(data, list):
                    for item in data:
                        raw_dict[item] = 0
                elif isinstance(data, dict):
                    raw_dict = data
        except Exception as e:
            print(f"⚠️ 讀取記憶檔失敗: {e}", flush=True)

    # 過濾過期課程
    cleaned = {}
    for key, count in raw_dict.items():
        try:
            course_date = key.split("_")[0]  # key 格式為 "日期_單位"
            if course_date >= today_str:
                cleaned[key] = count
        except Exception:
            # 若 key 格式異常則保留
            cleaned[key] = count

    return cleaned


def save_seen_courses(seen_dict):
    """儲存記憶庫"""
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen_dict, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 寫入記憶檔失敗: {e}", flush=True)


def get_retry_session():
    """建立具備自動重試機制的 HTTP Session"""
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
    """發送 Discord 診斷卡片"""
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
        print(f"⚠️ Discord 傳送失敗: {e}", flush=True)


def broadcast_line_message(text_message):
    """LINE Broadcast 推播"""
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
    """向官網 API 取得課程清單"""
    res = session.get(TRAINING_LIST_URL, timeout=10)
    json_data = res.json() if res.status_code == 200 else {}
    return json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data


def parse_course(item):
    """
    解析單一課程，回傳標準化字典。
    若課程不應被考慮（離島、已額滿、已取消等）則回傳 None。
    """
    # 基本欄位
    raw_date = str(item.get("trDate", "")).replace("-", "/")
    # 處理民國年 (例如 115/09/04 -> 2026/09/04)
    parts = raw_date.split("/")
    if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) < 4:
        raw_date = f"{int(parts[0]) + 1911}/{parts[1]}/{parts[2]}"

    organizer = item.get("organizerName", "未知單位")
    location = item.get("location", "")
    apply_status = str(item.get("applyStatus", "")).strip()
    is_full = str(item.get("isFull", "")).upper()
    is_cancel = str(item.get("isCancel", "")).upper()

    # ---- 過濾條件 ----
    # 1. 離島
    if any(kw in organizer or kw in location for kw in EXCLUDED_KEYWORDS):
        return None

    # 2. 狀態已關閉／額滿
    if is_full == "Y" or is_cancel == "Y":
        return None
    if "額滿" in apply_status or "截止" in apply_status:
        return None

    # 3. 開課日期必須 >= 今天
    today_str = datetime.now().strftime("%Y/%m/%d")
    if raw_date < today_str:
        return None

    # ---- 名額計算 ----
    total = safe_int(item.get("numberOfPeople", item.get("numberOfPeopleSignUp", 0)))
    signed = safe_int(item.get("numberOfPeopleSignUp", item.get("numberOfPeople", 0)))
    # 修正欄位互換（當 total < signed 時）
    if total < signed and signed > 0:
        total, signed = signed, total
    remaining = total - signed

    if remaining <= 0:
        return None

    # ---- 建構標準物件 ----
    return {
        "raw_date": raw_date,
        "organizer": organizer,
        "location": location,
        "total_capacity": total,
        "signed_up": signed,
        "remaining": remaining,
        # 強化唯一識別鍵：日期 + 單位 + 地點（避免同一天同一單位多班次混淆）
        "key": f"{raw_date}_{organizer}_{location}",
        "apply_status": apply_status,
        "is_full": is_full,
        "is_cancel": is_cancel,
        "original_item": item  # 保留原始資料，以備不時之需
    }


def check_training_courses():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    seen_courses = load_and_clean_seen_courses()
    session = get_retry_session()

    line_status_str = "無新課程或名額釋出（LINE 靜音）"

    try:
        # 1. 取得 Auth Token
        session.get(MAIN_PAGE_URL, timeout=10)
        auth_res = session.post(AUTH_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        if auth_res.status_code in [200, 201]:
            token = auth_res.json().get("auth_tn")
            if token:
                if not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                session.headers.update({"Authorization": token})

        # 2. 抓取課程清單
        training_list = fetch_training_list(session)
        if not training_list:
            send_discord_log(
                title="⚠️ 官網回傳空課程清單",
                description=f"**執行時間**：{current_time}\n可能為 API 異常或 Token 失效",
                color=16776960  # 黃色警示
            )
            return

        today_str = datetime.now().strftime("%Y/%m/%d")
        all_available = []          # 本島可報名課程（用於統計）
        candidate_courses = []      # 可能需通知的課程（尚未二次驗證）

        # 3. 解析每一筆課程
        for item in training_list:
            course = parse_course(item)
            if course is None:
                continue

            all_available.append(course)

            # 檢查是否為全新或名額增加
            prev = seen_courses.get(course["key"], 0)
            if prev == 0:
                course["notify_reason"] = "🆕 全新課程釋出"
                candidate_courses.append(course)
            elif course["remaining"] > prev:
                course["notify_reason"] = f"🔄 退選釋出名額 (+{course['remaining'] - prev} 人)"
                candidate_courses.append(course)

        # 4. 雙重延遲驗證（防幽靈）
        real_notify = []
        ghost_courses = []

        if candidate_courses:
            time.sleep(3)
            try:
                recheck_list = fetch_training_list(session)
                if recheck_list:
                    # 建立二次驗證的快速對照表（key -> remaining）
                    recheck_map = {}
                    for r_item in recheck_list:
                        r_course = parse_course(r_item)
                        if r_course is not None:
                            recheck_map[r_course["key"]] = r_course["remaining"]

                    for cand in candidate_courses:
                        ckey = cand["key"]
                        rc_rem = recheck_map.get(ckey, 0)
                        if rc_rem > 0:
                            real_notify.append(cand)
                            seen_courses[ckey] = rc_rem
                        else:
                            ghost_courses.append(cand)
                            seen_courses[ckey] = 0
                else:
                    # 二次抓取失敗，信任第一次結果（避免漏報）
                    real_notify = candidate_courses
                    for cand in candidate_courses:
                        seen_courses[cand["key"]] = cand["remaining"]
            except Exception as e:
                print(f"⚠️ 二次驗證例外: {e}", flush=True)
                real_notify = candidate_courses
                for cand in candidate_courses:
                    seen_courses[cand["key"]] = cand["remaining"]

        # 5. 額外檢查：記憶中有但官網已消失的課程（名額歸零）
        vanished = []
        active_keys = {c["key"] for c in all_available}
        for key, prev_rem in seen_courses.items():
            if prev_rem > 0 and key not in active_keys:
                seen_courses[key] = 0
                vanished.append(key)

        # 6. 發送 LINE 推播（真實課程）
        if real_notify:
            line_msg = "🚨【臺灣職安卡】名額異動通報！\n\n"
            for c in real_notify[:5]:
                line_msg += (
                    f"📌 狀態：{c['notify_reason']}\n"
                    f"📅 日期：{c['raw_date']}\n"
                    f"🏢 單位：{c['organizer']}\n"
                    f"📍 地點：{c['location']}\n"
                    f"🎟️ 剩餘名額：{c['remaining']} 人 (已報名 {c['signed_up']} / {c['total_capacity']})\n"
                    "------------------------------\n"
                )
            line_msg += "\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"

            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ 已推播 {len(real_notify)} 筆異動 ({status_desc})"

        # 儲存記憶
        save_seen_courses(seen_courses)

        # 7. 建構 Discord 診斷卡片
        fields = [
            {"name": "💬 LINE 推播狀態", "value": line_status_str, "inline": False},
            {"name": "📊 官網課程統計 (本島)", 
             "value": f"開放報名中: {len(all_available)} 筆 | 本次推播: {len(real_notify)} 筆", 
             "inline": False}
        ]

        if real_notify:
            summary = "".join(
                f"• **[{c['notify_reason']}] {c['raw_date']}** | {c['organizer']} (剩 {c['remaining']} 人)\n"
                for c in real_notify[:3]
            )
            fields.append({"name": "🎯 通報摘要", "value": summary, "inline": False})

        if ghost_courses:
            ghost_summary = "".join(
                f"• 👻 **{c['raw_date']}** | {c['organizer']} (官網已標記額滿/隱藏)\n"
                for c in ghost_courses
            )
            fields.append({"name": "👻 濾除幽靈課程", "value": ghost_summary, "inline": False})

        if vanished:
            vanished_summary = "".join(f"• 📉 **{v}**\n" for v in vanished[:3])
            fields.append({"name": "📉 已額滿／下架課程", "value": vanished_summary, "inline": False})

        send_discord_log(
            title="🎯 發現真實可報名課程！" if real_notify else "✅ 系統監控正常（無名額異動）",
            description=f"**執行時間**：{current_time}",
            color=3447003 if real_notify else 3066993,
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
