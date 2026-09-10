import os
import json
import time
from datetime import datetime, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


# ============================================================================
# 基本設定
# ============================================================================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAIN_PAGE_URL = "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
AUTH_TOKEN_URL = "https://oshcard.osha.gov.tw/OSC/api/authToken"
TRAINING_LIST_URL = "https://oshcard.osha.gov.tw/OSC/api/public/applyOnline/getTrainingList"

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

SEEN_FILE = "seen_courses.json"

# 離島過濾關鍵字
# 可使用 GitHub Actions Secrets / Variables 的 EXCLUDED_LOCATIONS 覆蓋
# 例如：
# 澎湖,連江,馬祖,金門
EXCLUDED_KEYWORDS = [
    kw.strip()
    for kw in os.getenv(
        "EXCLUDED_LOCATIONS",
        "澎湖,連江,馬祖,金門"
    ).split(",")
    if kw.strip()
]

# DEBUG_MODE=1 時，Discord 會額外顯示部分課程原始 JSON
DEBUG_MODE = os.getenv("DEBUG_MODE", "0") == "1"

# Discord 單一欄位避免過長
RAW_FIELD_DUMP_LIMIT = 900

# API 第一次抓取後，如果發現新課程 / 名額增加，
# 等待幾秒再重新抓取一次確認。
RECHECK_DELAY_SECONDS = 3


# ============================================================================
# 記憶庫
# ============================================================================

def load_and_clean_seen_courses():
    """
    讀取 seen_courses.json。

    舊版本可能是：
        ["2026/09/15_某單位", ...]

    新版本使用：
        {
            "trId": remaining
        }

    為了相容舊檔，舊 list 會自動轉成 remaining=0。

    同時清除已經過期的課程紀錄。
    """

    today_str = datetime.now().strftime("%Y/%m/%d")
    raw_dict = {}

    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        raw_dict[item] = 0

            elif isinstance(data, dict):
                raw_dict = data

        except Exception as e:
            print(f"⚠️ 讀取記憶檔失敗：{e}", flush=True)

    cleaned_dict = {}

    for key, count in raw_dict.items():
        try:
            key_str = str(key)

            # 新版 key 通常是 trId，不一定含日期。
            # 如果 key 本身是日期開頭的舊格式，才做日期清理。
            if len(key_str) >= 10 and key_str[4] == "/" and key_str[7] == "/":
                course_date = key_str[:10]

                if course_date >= today_str:
                    cleaned_dict[key_str] = safe_int(count, 0)
            else:
                # trId 型 key 不從日期判斷過期。
                cleaned_dict[key_str] = safe_int(count, 0)

        except Exception:
            cleaned_dict[str(key)] = safe_int(count, 0)

    return cleaned_dict


def save_seen_courses(seen_dict):
    """儲存課程記憶庫。"""

    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(
                seen_dict,
                f,
                ensure_ascii=False,
                indent=2,
                sort_keys=True
            )

    except Exception as e:
        print(f"⚠️ 寫入記憶檔失敗：{e}", flush=True)


# ============================================================================
# 工具
# ============================================================================

def safe_int(value, default=0):
    """安全轉換整數。"""

    try:
        if value is None:
            return default

        # 有些 API 可能傳 "42.0"
        if isinstance(value, float):
            return int(value)

        value_str = str(value).strip()

        if not value_str:
            return default

        return int(float(value_str))

    except (ValueError, TypeError):
        return default


def normalize_date(value):
    """
    將 API 日期盡可能統一成 YYYY/MM/DD。

    支援例如：
        2026/09/15
        2026-09-15
        2026/09/15 09:00:00
    """

    if value is None:
        return ""

    text = str(value).strip()

    if not text:
        return ""

    text = text.replace("-", "/")

    # 只取日期部分
    if len(text) >= 10:
        candidate = text[:10]

        # 確認格式看起來像 YYYY/MM/DD
        if (
            len(candidate) == 10
            and candidate[4] == "/"
            and candidate[7] == "/"
        ):
            return candidate

    return text


def get_current_time_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_discord_timestamp():
    """
    使用 UTC ISO timestamp 給 Discord Embed。
    """

    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# HTTP Session
# ============================================================================

def get_retry_session():
    """
    建立具有重試功能的 HTTP Session。

    重試：
    - 連線失敗
    - 暫時性網路錯誤
    - 500 / 502 / 503 / 504
    """

    session = requests.Session()

    # 官網目前憑證環境可能需要這個設定。
    session.verify = False

    retries = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=5,
        pool_maxsize=5,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) "
            "Gecko/20100101 Firefox/154.0"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://oshcard.osha.gov.tw",
        "Referer": MAIN_PAGE_URL,
    })

    return session


# ============================================================================
# Discord
# ============================================================================

def send_discord_log(
    title,
    description,
    color=3066993,
    fields=None
):
    """傳送 Discord Embed 診斷訊息。"""

    if not DISCORD_WEBHOOK_URL:
        print("ℹ️ 未設定 DISCORD_WEBHOOK_URL，跳過 Discord 通知。", flush=True)
        return False

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": get_discord_timestamp(),
        "footer": {
            "text": "臺灣職安卡 24H 雲端監控系統"
        }
    }

    if fields:
        # Discord Embed 最多 25 個 fields。
        embed["fields"] = fields[:25]

    try:
        res = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10
        )

        if 200 <= res.status_code < 300:
            return True

        print(
            f"⚠️ Discord HTTP {res.status_code}: {res.text[:500]}",
            flush=True
        )
        return False

    except Exception as e:
        print(f"⚠️ Discord 傳送失敗：{e}", flush=True)
        return False


# ============================================================================
# LINE
# ============================================================================

def broadcast_line_message(text_message):
    """發送 LINE Broadcast。"""

    if not LINE_CHANNEL_ACCESS_TOKEN:
        return False, "未設定 LINE Token"

    url = "https://api.line.me/v2/bot/message/broadcast"

    headers = {
        "Content-Type": "application/json",
        "Authorization": (
            f"Bearer {LINE_CHANNEL_ACCESS_TOKEN.strip()}"
        )
    }

    try:
        res = requests.post(
            url,
            headers=headers,
            json={
                "messages": [
                    {
                        "type": "text",
                        "text": text_message
                    }
                ]
            },
            timeout=10
        )

        if res.status_code == 200:
            return True, "200 OK（發送成功）"

        return False, f"HTTP {res.status_code} ({res.text[:500]})"

    except Exception as e:
        return False, f"連線例外：{str(e)}"


# ============================================================================
# API
# ============================================================================

def fetch_training_list(session):
    """
    抓取職安署課程 API。

    注意：
    API 錯誤不會被當成「空課程清單」。

    這很重要：
        API 壞掉 ≠ 今天沒有課程
    """

    res = session.get(
        TRAINING_LIST_URL,
        timeout=(10, 20)
    )

    if res.status_code != 200:
        raise RuntimeError(
            f"課程 API HTTP {res.status_code}: "
            f"{res.text[:500]}"
        )

    try:
        json_data = res.json()
    except Exception as e:
        raise RuntimeError(
            f"課程 API JSON 解析失敗：{e}"
        )

    if not isinstance(json_data, dict):
        raise RuntimeError(
            f"課程 API 回傳不是 JSON object，而是 "
            f"{type(json_data).__name__}"
        )

    training_list = json_data.get("trainingList", [])

    if not isinstance(training_list, list):
        raise RuntimeError(
            f"trainingList 不是陣列，而是 "
            f"{type(training_list).__name__}"
        )

    return training_list


def obtain_auth_token(session):
    """
    取得職安署 auth token。

    如果取得失敗，不會立刻中斷；
    先讓後面的 API 自己判斷是否真的需要 token。

    回傳：
        token, status_message
    """

    try:
        res = session.post(
            AUTH_TOKEN_URL,
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded"
            },
            timeout=(10, 20)
        )

        if res.status_code not in [200, 201]:
            return None, (
                f"authToken HTTP {res.status_code}"
            )

        data = res.json()

        token = data.get("auth_tn")

        if not token:
            return None, "authToken 回傳成功，但找不到 auth_tn"

        if not str(token).startswith("Bearer "):
            token = f"Bearer {token}"

        return token, "Token 取得成功"

    except Exception as e:
        return None, f"authToken 取得失敗：{e}"


# ============================================================================
# 原始資料診斷
# ============================================================================

def dump_raw_item(item):
    """把課程原始 JSON 壓縮成 Discord 可顯示的字串。"""

    try:
        text = json.dumps(
            item,
            ensure_ascii=False,
            separators=(",", ":")
        )
    except Exception:
        text = str(item)

    if len(text) > RAW_FIELD_DUMP_LIMIT:
        text = (
            text[:RAW_FIELD_DUMP_LIMIT]
            + "...(截斷)"
        )

    return text


# ============================================================================
# 課程解析
# ============================================================================

def parse_course(item):
    """
    解析單一課程。

    【目前已確認的欄位定義】

    numberOfPeopleSignUp
        = 總開放名額

    numberOfPeople
        = 目前已報名人數

    remaining
        = 總開放名額 - 已報名人數

    例如：

        numberOfPeople = 1
        numberOfPeopleSignUp = 100

        → 已報名 1 / 總額 100
        → 剩餘 99


    【唯一識別】

    優先使用 trId。

    如果 trId 缺失，才使用：
        日期 + 單位 + 地點

    這比單純使用：
        日期 + 單位

    安全很多。
    """

    if not isinstance(item, dict):
        raise ValueError("課程資料不是 object")

    # ------------------------------------------------------------------------
    # 名額
    # ------------------------------------------------------------------------

    total_capacity = safe_int(
        item.get("numberOfPeopleSignUp"),
        0
    )

    signed_up = safe_int(
        item.get("numberOfPeople"),
        0
    )

    remaining = max(
        0,
        total_capacity - signed_up
    )

    is_overbooked = (
        total_capacity > 0
        and signed_up > total_capacity
    )

    # ------------------------------------------------------------------------
    # 報名狀態
    # ------------------------------------------------------------------------

    registration_open = str(
        item.get("onlineRegistrationOpen", "")
    ).strip().upper()

    registration_cancelled = str(
        item.get("onlineRegistrationCancel", "")
    ).strip().upper()

    # ------------------------------------------------------------------------
    # 基本資料
    # ------------------------------------------------------------------------

    raw_date = normalize_date(
        item.get("trDate", "")
    )

    organizer = str(
        item.get("organizerName", "未知單位")
    ).strip()

    location = str(
        item.get("location", "")
    ).strip()

    tr_id = str(
        item.get("trId", "")
    ).strip()

    # ------------------------------------------------------------------------
    # 唯一 Key
    # ------------------------------------------------------------------------

    if tr_id:
        course_key = tr_id
        key_source = "trId"
    else:
        course_key = (
            f"{raw_date}|{organizer}|{location}"
        )
        key_source = "fallback"

    return {
        "raw_item": item,

        "tr_id": tr_id,

        "total_capacity": total_capacity,
        "signed_up": signed_up,
        "remaining": remaining,

        "is_overbooked": is_overbooked,

        "registration_open": registration_open,
        "registration_cancelled": registration_cancelled,

        "raw_date": raw_date,
        "organizer": organizer,
        "location": location,

        "key": course_key,
        "key_source": key_source,
    }


def is_excluded_location(parsed):
    """
    是否屬於離島。

    同時檢查：
        organizer
        location
    """

    organizer = parsed["organizer"]
    location = parsed["location"]

    return any(
        kw in organizer or kw in location
        for kw in EXCLUDED_KEYWORDS
    )


def is_registration_available(parsed):
    """
    判斷線上報名是否仍可接受。

    規則：

    onlineRegistrationCancel == Y
        → 一律不可報名

    onlineRegistrationOpen 有值且不是 Y
        → 不可報名

    欄位缺值
        → 暫時採寬鬆策略，允許繼續判斷
    """

    if parsed["registration_cancelled"] == "Y":
        return False

    if (
        parsed["registration_open"]
        and parsed["registration_open"] != "Y"
    ):
        return False

    return True


def is_course_date_active(parsed, today_str):
    """
    判斷課程日期是否今天或未來。

    日期格式異常時，採保守方式：
    不直接當作過期。
    """

    course_date = parsed["raw_date"]

    if not course_date:
        return False

    return course_date >= today_str


def is_open_and_available(parsed, today_str):
    """
    最終判斷：
    這堂課現在是否屬於「本島、日期有效、線上報名、仍有名額」。
    """

    if is_excluded_location(parsed):
        return False

    if not is_registration_available(parsed):
        return False

    if not is_course_date_active(parsed, today_str):
        return False

    if parsed["remaining"] <= 0:
        return False

    return True


# ============================================================================
# 診斷表
# ============================================================================

def diagnostic_line(parsed, category):
    """
    建立一行診斷資料。
    """

    raw = parsed["raw_item"]

    return (
        f"{parsed['tr_id']} | "
        f"{parsed['raw_date']} | "
        f"{parsed['organizer']} | "
        f"{parsed['location']} | "
        f"{raw.get('numberOfPeople')} | "
        f"{raw.get('numberOfPeopleSignUp')} | "
        f"{parsed['signed_up']}/{parsed['total_capacity']} | "
        f"{parsed['remaining']} | "
        f"{category}"
    )


# ============================================================================
# 主流程
# ============================================================================

def check_training_courses():

    current_time = get_current_time_string()

    print(
        f"===== 臺灣職安卡監控開始：{current_time} =====",
        flush=True
    )

    # ------------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------------

    seen_courses = load_and_clean_seen_courses()

    session = get_retry_session()

    line_status_str = (
        "無新課程或名額釋出（LINE 靜音）"
    )

    ghost_courses = []
    real_notify_courses = []
    candidate_courses = []

    all_available = []

    current_active_keys = set()

    parse_errors = 0
    overbooked_courses = []
    registration_closed_count = 0
    expired_or_full_count = 0
    excluded_count = 0

    auth_warning = None

    # ------------------------------------------------------------------------
    # API 主流程
    # ------------------------------------------------------------------------

    try:

        # ====================================================================
        # 1. 先進入官網
        # ====================================================================

        try:
            main_res = session.get(
                MAIN_PAGE_URL,
                timeout=(10, 20)
            )

            print(
                f"官網首頁 HTTP：{main_res.status_code}",
                flush=True
            )

        except Exception as e:
            raise RuntimeError(
                f"無法連線職安署官網：{e}"
            )

        # ====================================================================
        # 2. 取得 Token
        # ====================================================================

        token, auth_status = obtain_auth_token(session)

        print(
            f"Auth Token：{auth_status}",
            flush=True
        )

        if token:
            session.headers.update({
                "Authorization": token
            })
        else:
            auth_warning = auth_status

        # ====================================================================
        # 3. 抓取課程
        # ====================================================================

        training_list = fetch_training_list(session)

        print(
            f"API 共取得 {len(training_list)} 筆課程",
            flush=True
        )

        today_str = datetime.now().strftime("%Y/%m/%d")

        # ====================================================================
        # 4. 診斷標題
        # ====================================================================

        diagnostic_lines = [
            "trId | 日期 | 單位 | 地點 | "
            "原始numberOfPeople | "
            "原始numberOfPeopleSignUp | "
            "已報名/總額 | 剩餘 | 分類"
        ]

        # ====================================================================
        # 5. 逐筆分析
        # ====================================================================

        for raw_item in training_list:

            try:
                parsed = parse_course(raw_item)

            except Exception as e:
                parse_errors += 1

                print(
                    f"⚠️ 課程解析失敗：{e}",
                    flush=True
                )

                continue

            category = "開放報名中"

            # ----------------------------------------------------------------
            # A. 離島過濾
            # ----------------------------------------------------------------

            if is_excluded_location(parsed):

                excluded_count += 1

                category = "離島已過濾"

                diagnostic_lines.append(
                    diagnostic_line(
                        parsed,
                        category
                    )
                )

                continue

            # ----------------------------------------------------------------
            # B. 報名狀態
            # ----------------------------------------------------------------

            if not is_registration_available(parsed):

                registration_closed_count += 1

                category = "報名未開放/已取消"

                diagnostic_lines.append(
                    diagnostic_line(
                        parsed,
                        category
                    )
                )

                continue

            # ----------------------------------------------------------------
            # C. 額滿 / 候補
            # ----------------------------------------------------------------

            if parsed["is_overbooked"]:

                overbooked_courses.append(parsed)

                category = "額滿/候補"

                diagnostic_lines.append(
                    diagnostic_line(
                        parsed,
                        category
                    )
                )

                continue

            # ----------------------------------------------------------------
            # D. 日期 + 名額
            # ----------------------------------------------------------------

            if is_open_and_available(
                parsed,
                today_str
            ):

                category = "開放報名中"

                all_available.append(parsed)

                current_active_keys.add(
                    parsed["key"]
                )

                previous_remaining = safe_int(
                    seen_courses.get(
                        parsed["key"],
                        0
                    ),
                    0
                )

                # ------------------------------------------------------------
                # 新課程
                # ------------------------------------------------------------

                if previous_remaining == 0:

                    parsed["notify_reason"] = (
                        "🆕 全新課程釋出"
                    )

                    candidate_courses.append(parsed)

                # ------------------------------------------------------------
                # 名額增加
                # ------------------------------------------------------------

                elif parsed["remaining"] > previous_remaining:

                    increase = (
                        parsed["remaining"]
                        - previous_remaining
                    )

                    parsed["notify_reason"] = (
                        f"🔄 名額增加 (+{increase} 人)"
                    )

                    candidate_courses.append(parsed)

            else:

                expired_or_full_count += 1

                category = "過期或無名額"

            diagnostic_lines.append(
                diagnostic_line(
                    parsed,
                    category
                )
            )

        # ====================================================================
        # 6. GitHub Actions 完整診斷
        # ====================================================================

        print(
            "===== 課程診斷明細 =====",
            flush=True
        )

        for line in diagnostic_lines:
            print(
                line,
                flush=True
            )

        print(
            "===== 診斷明細結束 =====",
            flush=True
        )

        # ====================================================================
        # 7. 找出原本有名額、現在消失的課程
        # ====================================================================

        vanished_courses = []

        for key, previous_remaining in list(
            seen_courses.items()
        ):

            previous_remaining = safe_int(
                previous_remaining,
                0
            )

            if (
                previous_remaining > 0
                and key not in current_active_keys
            ):

                seen_courses[key] = 0

                vanished_courses.append(key)

        # ====================================================================
        # 8. 二次驗證
        # ====================================================================

        if candidate_courses:

            print(
                f"🔎 發現 {len(candidate_courses)} 筆候選異動，"
                f"{RECHECK_DELAY_SECONDS} 秒後進行二次驗證...",
                flush=True
            )

            time.sleep(RECHECK_DELAY_SECONDS)

            try:

                recheck_list = fetch_training_list(
                    session
                )

                print(
                    f"🔎 二次驗證 API 共取得 "
                    f"{len(recheck_list)} 筆課程",
                    flush=True
                )

                recheck_map = {}

                if isinstance(
                    recheck_list,
                    list
                ):

                    for r_raw in recheck_list:

                        try:
                            r_parsed = parse_course(
                                r_raw
                            )

                        except Exception:
                            continue

                        # ----------------------------------------------------
                        # 二次驗證時重新檢查所有重要條件
                        # ----------------------------------------------------

                        if is_excluded_location(
                            r_parsed
                        ):
                            continue

                        if not is_registration_available(
                            r_parsed
                        ):
                            continue

                        if not is_course_date_active(
                            r_parsed,
                            today_str
                        ):
                            continue

                        if r_parsed["remaining"] <= 0:
                            continue

                        recheck_map[
                            r_parsed["key"]
                        ] = r_parsed

                # ------------------------------------------------------------
                # 判斷每個候選課程
                # ------------------------------------------------------------

                for candidate in candidate_courses:

                    verified = recheck_map.get(
                        candidate["key"]
                    )

                    if verified:

                        # ----------------------------------------------------
                        # 重要：
                        # 二次驗證後，完整同步最新資料
                        # ----------------------------------------------------

                        candidate["remaining"] = (
                            verified["remaining"]
                        )

                        candidate["signed_up"] = (
                            verified["signed_up"]
                        )

                        candidate["total_capacity"] = (
                            verified["total_capacity"]
                        )

                        candidate["raw_item"] = (
                            verified["raw_item"]
                        )

                        candidate["location"] = (
                            verified["location"]
                        )

                        candidate["organizer"] = (
                            verified["organizer"]
                        )

                        candidate["raw_date"] = (
                            verified["raw_date"]
                        )

                        candidate["tr_id"] = (
                            verified["tr_id"]
                        )

                        real_notify_courses.append(
                            candidate
                        )

                        seen_courses[
                            candidate["key"]
                        ] = candidate["remaining"]

                    else:

                        # 3 秒後已不存在 / 沒名額
                        ghost_courses.append(
                            candidate
                        )

                        seen_courses[
                            candidate["key"]
                        ] = 0

                # ------------------------------------------------------------
                # 二次驗證成功，但 API 完全空掉
                # ------------------------------------------------------------

                if (
                    not recheck_map
                    and recheck_list
                ):
                    print(
                        "⚠️ 二次驗證沒有找到任何有效候選課程，"
                        "候選課程將視為瞬間消失/失效。",
                        flush=True
                    )

            except Exception as e:

                # ------------------------------------------------------------
                # 第二次 API 失敗
                #
                # 不因為第二次 API 暫時故障而漏掉真正課程。
                # 因此採用第一次結果。
                # ------------------------------------------------------------

                print(
                    f"⚠️ 二次驗證失敗：{e}",
                    flush=True
                )

                print(
                    "ℹ️ 將信任第一次成功抓取結果，避免漏報。",
                    flush=True
                )

                real_notify_courses = (
                    candidate_courses
                )

                for candidate in candidate_courses:

                    seen_courses[
                        candidate["key"]
                    ] = candidate["remaining"]

        # ====================================================================
        # 9. LINE 推播
        # ====================================================================

        if real_notify_courses:

            lines = [
                "🚨【臺灣職安卡】名額異動通報！\n"
            ]

            # 最多推播前 5 筆
            for course in real_notify_courses[:5]:

                lines.append(
                    f"📌 狀態："
                    f"{course['notify_reason']}\n"
                    f"🆔 課程編號："
                    f"{course['tr_id'] or '未知'}\n"
                    f"📅 日期："
                    f"{course['raw_date']}\n"
                    f"🏢 單位："
                    f"{course['organizer']}\n"
                    f"📍 地點："
                    f"{course['location']}\n"
                    f"🎟️ 剩餘名額："
                    f"{course['remaining']} 人 "
                    f"(已報名 "
                    f"{course['signed_up']} / "
                    f"{course['total_capacity']})\n"
                    "------------------------------"
                )

            if len(real_notify_courses) > 5:
                lines.append(
                    f"\n另外還有 "
                    f"{len(real_notify_courses) - 5} 筆異動，"
                    f"請查看 Discord。"
                )

            lines.append(
                "\n🔗 報名連結：\n"
                "https://oshcard.osha.gov.tw/oscVue/OnlineApply/applylist"
            )

            line_msg = "\n".join(lines)

            success, status_desc = (
                broadcast_line_message(
                    line_msg
                )
            )

            if success:

                line_status_str = (
                    f"✅ 已推播 "
                    f"{len(real_notify_courses)} "
                    f"筆真實異動至 LINE "
                    f"({status_desc})"
                )

            else:

                line_status_str = (
                    f"❌ LINE 推播失敗："
                    f"{status_desc}"
                )

        # ====================================================================
        # 10. 儲存記憶
        # ====================================================================

        save_seen_courses(
            seen_courses
        )

        # ====================================================================
        # 11. Discord 報告
        # ====================================================================

        fields = []

        # --------------------------------------------------------------------
        # LINE
        # --------------------------------------------------------------------

        fields.append({
            "name": "💬 LINE 推播狀態",
            "value": line_status_str,
            "inline": False
        })

        # --------------------------------------------------------------------
        # 統計
        # --------------------------------------------------------------------

        fields.append({
            "name": "📊 官網課程統計（本島）",
            "value": (
                f"開放報名中："
                f"{len(all_available)} 筆\n"
                f"本次推播："
                f"{len(real_notify_courses)} 筆\n"
                f"解析失敗："
                f"{parse_errors} 筆\n"
                f"額滿/候補："
                f"{len(overbooked_courses)} 筆\n"
                f"報名未開放/已取消："
                f"{registration_closed_count} 筆\n"
                f"過期或無名額："
                f"{expired_or_full_count} 筆\n"
                f"離島已過濾："
                f"{excluded_count} 筆"
            ),
            "inline": False
        })

        # --------------------------------------------------------------------
        # Auth 狀態
        # --------------------------------------------------------------------

        if auth_warning:

            fields.append({
                "name": "⚠️ Auth Token",
                "value": auth_warning,
                "inline": False
            })

        # --------------------------------------------------------------------
        # 真實通知摘要
        # --------------------------------------------------------------------

        if real_notify_courses:

            summary = "\n".join(
                f"• `{c['tr_id'] or c['key']}` "
                f"**[{c['notify_reason']}]** "
                f"{c['raw_date']} | "
                f"{c['organizer']} "
                f"(剩 {c['remaining']} 人)"
                for c in real_notify_courses[:5]
            )

            fields.append({
                "name": "🎯 真實異動摘要",
                "value": summary,
                "inline": False
            })

            # DEBUG_MODE
            if DEBUG_MODE:

                for course in real_notify_courses[:2]:

                    fields.append({
                        "name": (
                            f"🔍 原始資料 - "
                            f"{course['organizer']}"
                        ),
                        "value": (
                            f"```json\n"
                            f"{dump_raw_item(course['raw_item'])}"
                            f"\n```"
                        ),
                        "inline": False
                    })

        # --------------------------------------------------------------------
        # 幽靈課程
        # --------------------------------------------------------------------

        if ghost_courses:

            ghost_summary = "\n".join(
                f"• 👻 `{c['tr_id'] or c['key']}` "
                f"{c['raw_date']} | "
                f"{c['organizer']} "
                f"(第一次剩 "
                f"{c['remaining']} 人，"
                f"二次驗證消失/失效)"
                for c in ghost_courses[:8]
            )

            fields.append({
                "name": "👻 瞬間下架 / 幽靈釋出",
                "value": ghost_summary,
                "inline": False
            })

            for course in ghost_courses[:2]:

                fields.append({
                    "name": (
                        f"🔍 幽靈課程原始資料 - "
                        f"{course['organizer']}"
                    ),
                    "value": (
                        f"```json\n"
                        f"{dump_raw_item(course['raw_item'])}"
                        f"\n```"
                    ),
                    "inline": False
                })

        # --------------------------------------------------------------------
        # 消失課程
        # --------------------------------------------------------------------

        if vanished_courses:

            vanished_summary = "\n".join(
                f"• 📉 `{v}`"
                for v in vanished_courses[:8]
            )

            fields.append({
                "name": "📉 近期額滿 / 下架 / 消失課程",
                "value": vanished_summary,
                "inline": False
            })

        # --------------------------------------------------------------------
        # 額滿 / 候補
        # --------------------------------------------------------------------

        if overbooked_courses:

            overbooked_summary = "\n".join(
                f"• `{c['tr_id'] or c['key']}` "
                f"{c['raw_date']} | "
                f"{c['organizer']} "
                f"(已報名 "
                f"{c['signed_up']} / "
                f"總額 "
                f"{c['total_capacity']})"
                for c in overbooked_courses[:8]
            )

            fields.append({
                "name": "🪑 額滿 / 候補課程",
                "value": overbooked_summary,
                "inline": False
            })

            # 額滿課程永遠附原始資料，
            # 因為這是最值得人工核對的地方。
            for course in overbooked_courses[:2]:

                fields.append({
                    "name": (
                        f"🔍 額滿課程原始資料 - "
                        f"{course['organizer']}"
                    ),
                    "value": (
                        f"```json\n"
                        f"{dump_raw_item(course['raw_item'])}"
                        f"\n```"
                    ),
                    "inline": False
                })

        # ====================================================================
        # 12. Discord 標題
        # ====================================================================

        if real_notify_courses:

            title = "🎯 發現真實可報名課程！"
            color = 3447003

        elif ghost_courses:

            title = "👻 發現瞬間釋出 / 下架課程"
            color = 16753920

        else:

            title = "✅ 系統監控正常（無名額異動）"
            color = 3066993

        # ====================================================================
        # 13. 發送 Discord
        # ====================================================================

        send_discord_log(
            title=title,
            description=(
                f"**執行時間**：{current_time}\n"
                f"**API 課程總數**："
                f"{len(training_list)} 筆"
            ),
            color=color,
            fields=fields
        )

        print(
            "===== 監控完成 =====",
            flush=True
        )

    # =========================================================================
    # 最外層錯誤
    # =========================================================================

    except Exception as e:

        error_text = str(e)

        print(
            f"🚨 系統錯誤：{error_text}",
            flush=True
        )

        send_discord_log(
            title="🚨 系統連線或解析失敗",
            description=(
                f"**執行時間**：{current_time}\n"
                f"**錯誤資訊**：`{error_text}`\n\n"
                "這次執行未將 API 錯誤當成「沒有課程」，"
                "避免誤判。"
            ),
            color=15158332
        )


# ============================================================================
# 程式入口
# ============================================================================

if __name__ == "__main__":
    check_training_courses()
