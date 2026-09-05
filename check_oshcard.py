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

# 離島過濾關鍵字，可用環境變數 EXCLUDED_LOCATIONS 覆蓋（逗號分隔），不改程式碼就能調整
EXCLUDED_KEYWORDS = [
    kw.strip()
    for kw in os.getenv("EXCLUDED_LOCATIONS", "澎湖,連江,馬祖,金門").split(",")
    if kw.strip()
]

# 除錯模式："1" 時，連正常推播的課程也會把原始 JSON 貼到 Discord，方便排查誤判
DEBUG_MODE = os.getenv("DEBUG_MODE", "0") == "1"

RAW_FIELD_DUMP_LIMIT = 600  # 避免 Discord embed 內容超過長度限制


# ---------------------------------------------------------------------------
# 記憶庫存取
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 連線與通知
# ---------------------------------------------------------------------------

def get_retry_session():
    """建立具備自動重試功能的 HTTP Session（防範政府網站抽風）"""
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


# ---------------------------------------------------------------------------
# 資料解析工具
# ---------------------------------------------------------------------------

def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def fetch_training_list(session):
    """向官網抓取原始課程清單"""
    res = session.get(TRAINING_LIST_URL, timeout=10)
    try:
        json_data = res.json() if res.status_code == 200 else {}
    except Exception:
        json_data = {}
    training_list = json_data.get("trainingList", []) if isinstance(json_data, dict) else json_data
    return training_list


def dump_raw_item(item):
    """把課程原始欄位轉成精簡字串，方便貼到 Discord 診斷，
    這樣往後若要新增過濾條件，是根據看到的真實欄位，而不是用猜的。"""
    try:
        text = json.dumps(item, ensure_ascii=False)
    except Exception:
        text = str(item)
    if len(text) > RAW_FIELD_DUMP_LIMIT:
        text = text[:RAW_FIELD_DUMP_LIMIT] + "...(截斷)"
    return text


def parse_course(item):
    """
    解析單一課程。numberOfPeople = 總限額，numberOfPeopleSignUp = 已報名人數。
    若已報名人數大於總限額，代表資料矛盾，標記為 anomaly，不直接推播，
    交由 Discord 診斷卡片人工確認，避免用猜測方式（例如自動互換欄位）造成誤判。
    """
    total_capacity = safe_int(item.get("numberOfPeople", 0))
    signed_up = safe_int(item.get("numberOfPeopleSignUp", 0))
    remaining = total_capacity - signed_up

    anomaly = None
    if signed_up > total_capacity:
        anomaly = f"已報名人數({signed_up}) > 總限額({total_capacity})，資料矛盾"

    raw_date = str(item.get("trDate", "")).replace("-", "/")
    organizer = item.get("organizerName", "未知單位")
    location = item.get("location", "")
    course_key = f"{raw_date}_{organizer}"

    return {
        "raw_item": item,
        "total_capacity": total_capacity,
        "signed_up": signed_up,
        "remaining": remaining,
        "raw_date": raw_date,
        "organizer": organizer,
        "location": location,
        "key": course_key,
        "anomaly": anomaly,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def check_training_courses():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    seen_courses = load_and_clean_seen_courses()
    session = get_retry_session()

    line_status_str = "無新課程或名額釋出（LINE 靜音）"
    anomalies = []
    ghost_courses = []
    real_notify_courses = []
    candidate_courses = []
    all_available = []
    current_active_keys = set()
    parse_errors = 0

    try:
        # 1. 驗證 Token
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
        today_str = datetime.now().strftime("%Y/%m/%d")

        if not isinstance(training_list, list):
            send_discord_log(
                title="🚨 API 回傳格式異常",
                description=(
                    f"**執行時間**：{current_time}\n"
                    f"預期收到課程陣列，但實際收到型別：`{type(training_list).__name__}`，"
                    f"請人工檢查官網 API 是否變更。"
                ),
                color=15158332
            )
            return

        for raw_item in training_list:
            try:
                parsed = parse_course(raw_item)
            except Exception:
                parse_errors += 1
                continue

            if parsed["anomaly"]:
                anomalies.append({
                    "key": parsed["key"],
                    "reason": parsed["anomaly"],
                    "raw": dump_raw_item(raw_item)
                })
                continue

            if any(kw in parsed["organizer"] or kw in parsed["location"] for kw in EXCLUDED_KEYWORDS):
                continue

            if parsed["raw_date"] >= today_str and parsed["remaining"] > 0:
                all_available.append(parsed)
                current_active_keys.add(parsed["key"])

                prev_remaining = seen_courses.get(parsed["key"], 0)
                if prev_remaining == 0:
                    parsed["notify_reason"] = "🆕 全新課程釋出"
                    candidate_courses.append(parsed)
                elif parsed["remaining"] > prev_remaining:
                    parsed["notify_reason"] = f"🔄 名額增加 (+{parsed['remaining'] - prev_remaining} 人)"
                    candidate_courses.append(parsed)

        # 追蹤消失/額滿的舊課程
        vanished_courses = []
        for k, prev_rem in list(seen_courses.items()):
            if prev_rem > 0 and k not in current_active_keys:
                seen_courses[k] = 0
                vanished_courses.append(k)

        # 3. 二次延遲雙重驗證（防止瞬間釋出又秒殺/測試資料造成誤報，帶備援機制）
        if candidate_courses:
            time.sleep(3)
            try:
                recheck_list = fetch_training_list(session)
                recheck_map = {}
                if isinstance(recheck_list, list) and recheck_list:
                    for r_raw in recheck_list:
                        try:
                            r_parsed = parse_course(r_raw)
                        except Exception:
                            continue
                        if r_parsed["anomaly"]:
                            continue
                        recheck_map[r_parsed["key"]] = r_parsed["remaining"]

                if recheck_map:
                    for cand in candidate_courses:
                        rc_rem = recheck_map.get(cand["key"], 0)
                        if rc_rem > 0:
                            cand["remaining"] = rc_rem
                            real_notify_courses.append(cand)
                            seen_courses[cand["key"]] = rc_rem
                        else:
                            ghost_courses.append(cand)
                            seen_courses[cand["key"]] = 0
                else:
                    # 第二次抓取為空或失敗，無法判斷真偽，選擇信任第一次結果、避免漏報
                    real_notify_courses = candidate_courses
                    for cand in candidate_courses:
                        seen_courses[cand["key"]] = cand["remaining"]
            except Exception:
                real_notify_courses = candidate_courses
                for cand in candidate_courses:
                    seen_courses[cand["key"]] = cand["remaining"]

        # 4. 推播 LINE
        if real_notify_courses:
            lines = ["🚨【臺灣職安卡】名額異動通報！\n"]
            for c in real_notify_courses[:5]:
                lines.append(
                    f"📌 狀態：{c['notify_reason']}\n"
                    f"📅 日期：{c['raw_date']}\n"
                    f"🏢 單位：{c['organizer']}\n"
                    f"📍 地點：{c['location']}\n"
                    f"🎟️ 剩餘名額：{c['remaining']} 人 (已報名 {c['signed_up']} / {c['total_capacity']})\n"
                    "------------------------------"
                )
            lines.append("\n🔗 報名連結：\nhttps://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist")
            line_msg = "\n".join(lines)

            success, status_desc = broadcast_line_message(line_msg)
            line_status_str = f"✅ 已推播 {len(real_notify_courses)} 筆真實異動至 LINE ({status_desc})"

        save_seen_courses(seen_courses)

        # 5. Discord 診斷報告
        fields = [
            {"name": "💬 LINE 推播狀態", "value": line_status_str, "inline": False},
            {
                "name": "📊 官網課程統計 (本島)",
                "value": (
                    f"開放報名中: {len(all_available)} 筆 | "
                    f"本次推播: {len(real_notify_courses)} 筆 | "
                    f"解析失敗: {parse_errors} 筆"
                ),
                "inline": False
            }
        ]

        if real_notify_courses:
            summary = "\n".join(
                f"• **[{c['notify_reason']}] {c['raw_date']}** | {c['organizer']} (剩 {c['remaining']} 人)"
                for c in real_notify_courses[:3]
            )
            fields.append({"name": "🎯 通報摘要", "value": summary, "inline": False})
            if DEBUG_MODE:
                for c in real_notify_courses[:2]:
                    fields.append({
                        "name": f"🔍 原始資料 - {c['organizer']}",
                        "value": f"```{dump_raw_item(c['raw_item'])}```",
                        "inline": False
                    })

        if ghost_courses:
            ghost_summary = "\n".join(
                f"• 👻 **{c['raw_date']}** | {c['organizer']} (出現後 3 秒內消失)"
                for c in ghost_courses
            )
            fields.append({"name": "👻 攔截到瞬間下架/幽靈釋出", "value": ghost_summary, "inline": False})
            for c in ghost_courses[:2]:
                fields.append({
                    "name": f"🔍 幽靈課程原始資料 - {c['organizer']}",
                    "value": f"```{dump_raw_item(c['raw_item'])}```",
                    "inline": False
                })

        if vanished_courses:
            vanished_summary = "\n".join(f"• 📉 {v}" for v in vanished_courses[:3])
            fields.append({"name": "📉 近期額滿/下架課程", "value": vanished_summary, "inline": False})

        if anomalies:
            anomaly_summary = "\n".join(f"• ⚠️ {a['key']} — {a['reason']}" for a in anomalies[:3])
            fields.append({"name": "⚠️ 資料異常（未推播，需人工確認）", "value": anomaly_summary, "inline": False})
            for a in anomalies[:2]:
                fields.append({
                    "name": f"🔍 異常原始資料 - {a['key']}",
                    "value": f"```{a['raw']}```",
                    "inline": False
                })

        title = "🎯 發現真實可報名課程！" if real_notify_courses else "✅ 系統監控正常（無名額異動）"
        color = 3447003 if real_notify_courses else (16753920 if (ghost_courses or anomalies) else 3066993)

        send_discord_log(
            title=title,
            description=f"**執行時間**：{current_time}",
            color=color,
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
