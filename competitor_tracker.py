import warnings
warnings.filterwarnings("ignore")

import requests
import json
import os
import csv
import re
import time
from datetime import datetime, timezone, timedelta
from google_play_scraper import search, app

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "competitor_list.json")   # 基准库：已知游戏，用于判定“新”
TARGETS_FILE = os.path.join(SCRIPT_DIR, "targets.csv")            # 监控名单
DATA_FILE = os.path.join(SCRIPT_DIR, "data.json")                # 给网页看板读的发现结果
INSTALLS_HISTORY_FILE = os.path.join(SCRIPT_DIR, "installs_history.json")  # 安卓装机量逐日快照，用于算增速

# ================= 配置区 =================
# us/ph/au/ca/gb 主力市场 + tr/br/vn/id/mx 常见软启动测试市场（更早抓到新品）
TARGET_COUNTRIES = ["us", "ph", "au", "ca", "gb", "tr", "br", "vn", "id", "mx"]
KEEP_DAYS = 120        # data.json 里保留最近多少天发现的新游
SNAPSHOT_KEEP_DAYS = 35  # 装机量快照保留天数
VELOCITY_WINDOW = 7    # 增速统计窗口（天）
SHOTS_MAX = 4          # 每款最多存几张截图
ITUNES_LIMIT = 200     # lookup 默认只返回 50 款，大厂会漏
NEW_GAME_MAX_AGE_DAYS = 180  # 上架超过这个天数的，不当作新游推送（补进基准库）
NEW_PUBLISHER_UNKNOWN_THRESHOLD = 8  # 一次扫到这么多未知游戏，才视为「新加厂商建库」
PLAY_REFRESH_MAX_FAILS = 8   # 连续刷新失败这么多次，判定被限流，停止后续拉取
CN_TZ = timezone(timedelta(hours=8))
ITUNES_HEADERS = {"User-Agent": "competitor-tracker/1.0"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}
# ==========================================

def now_cn(fmt):
    return datetime.now(timezone.utc).astimezone(CN_TZ).strftime(fmt)


def parse_release_day(s):
    """把 iOS ISO 日期和 Android 美式日期都解析成 UTC 当天 00:00。解析不了返回 None。"""
    s = str(s or "").strip()
    if not s or s in ("未知日期", "未知"):
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(1).lower()] + 1, int(m.group(2)), tzinfo=timezone.utc)
    return None


def is_stale_release(s, max_age=NEW_GAME_MAX_AGE_DAYS):
    d = parse_release_day(s)
    if d is None:
        return False
    return (datetime.now(timezone.utc) - d).days > max_age


def preferred_country(regions):
    """详情请求优先用 us，避免内购价格变成土耳其里拉 / 越南盾。"""
    regs = [str(x).lower() for x in (regions or [])]
    for c in TARGET_COUNTRIES:
        if c in regs:
            return c
    return regs[0] if regs else "us"


def with_retry(fn, tries=3, delay=1.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(delay * (i + 1))
    raise last


def fetch_target_developers():
    targets = {}
    if not os.path.exists(TARGETS_FILE):
        print(f"  [!] 未找到本地名单: {TARGETS_FILE}")
        return targets
    try:
        with open(TARGETS_FILE, "r", encoding="utf-8-sig") as f:
            csv_data = csv.reader(f)
            next(csv_data, None)
            for row in csv_data:
                if len(row) >= 3:
                    custom_dev_name = row[0].strip()
                    android_id = row[1].strip()
                    ios_id = row[2].strip()
                    if not custom_dev_name:
                        continue
                    if custom_dev_name not in targets:
                        targets[custom_dev_name] = {"android": [], "ios": []}
                    if android_id and android_id not in targets[custom_dev_name]["android"]:
                        targets[custom_dev_name]["android"].append(android_id)
                    if ios_id and ios_id not in targets[custom_dev_name]["ios"]:
                        targets[custom_dev_name]["ios"].append(ios_id)
        print(f"  [√] 名单加载成功！当前共监控 {len(targets)} 个独立厂商主体。")
    except Exception as e:
        print(f"  [x] 读取本地 CSV 失败: {e}")
    return targets

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(history_dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history_dict, f, indent=4, ensure_ascii=False)

def ios_genre(game):
    # iTunes 的 genres 形如 ['Games','Casual','Puzzle']，取后面更具体的子类
    for g in (game.get("genres") or []):
        if g not in ("Games", "Entertainment"):
            return g
    return game.get("primaryGenreName", "")

def bytes_to_mb(bytes_size):
    try:
        return f"{round(int(bytes_size) / (1024 * 1024), 1)} MB"
    except (TypeError, ValueError):
        return "未知大小"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": None, "games": []}

def save_data(found_records):
    """把本次发现的新游合并进 data.json（按 app_id 去重，保留最近 KEEP_DAYS 天）。"""
    today = now_cn("%Y-%m-%d")
    data = load_data()
    existing_ids = {g.get("app_id") for g in data["games"]}
    for rec in found_records:
        if rec["app_id"] not in existing_ids:
            rec["found_date"] = today
            data["games"].append(rec)
            existing_ids.add(rec["app_id"])
    # iTunes lookup 的 features 不是内购字段，历史「支持内购」会误导
    for g in data["games"]:
        if g.get("platform") == "iOS" and g.get("iap_info") in ("支持内购", "未见明细"):
            g["iap_info"] = "未知"
    # 裁掉太旧的发现记录（日期按北京时间，和 found_date 对齐）
    cutoff = (datetime.now(CN_TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["games"] = [g for g in data["games"] if g.get("found_date", "") >= cutoff]
    data["games"].sort(key=lambda g: g.get("found_date", ""), reverse=True)
    data["updated_at"] = now_cn("%Y-%m-%d %H:%M")
    update_velocity(data)   # 刷新安卓装机量并算增速
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _load_snapshots():
    if os.path.exists(INSTALLS_HISTORY_FILE):
        try:
            with open(INSTALLS_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def update_velocity(data):
    """每天重新拉取安卓 realInstalls，写入快照后计算最近 VELOCITY_WINDOW 天增量。"""
    today = now_cn("%Y-%m-%d")
    snaps = _load_snapshots()
    now = datetime.now(CN_TZ)
    cutoff = (now - timedelta(days=SNAPSHOT_KEEP_DAYS)).strftime("%Y-%m-%d")
    win_start = (now - timedelta(days=VELOCITY_WINDOW)).strftime("%Y-%m-%d")

    games = [g for g in data["games"] if g.get("platform") == "Android"]
    games.sort(key=lambda g: g.get("found_date", ""), reverse=True)

    consecutive_fails = 0
    refreshed, failed = 0, 0
    for g in games:
        app_id = g["app_id"]
        cur = g.get("real_installs", 0) or 0
        if consecutive_fails < PLAY_REFRESH_MAX_FAILS:
            try:
                country = preferred_country(g.get("regions"))
                d = with_retry(lambda aid=app_id, c=country: app(aid, lang="en", country=c))
                cur = d.get("realInstalls", 0) or 0
                g["real_installs"] = cur
                if d.get("installs"):
                    g["installs"] = d.get("installs", "")
                consecutive_fails = 0
                refreshed += 1
            except Exception as e:
                consecutive_fails += 1
                failed += 1
                print(f"  [!] 装机量刷新失败 {app_id}: {e}")
                if consecutive_fails >= PLAY_REFRESH_MAX_FAILS:
                    print("  [!] Google Play 连续失败，停止后续装机量刷新（沿用已有数值）")
        if not cur:
            continue
        series = [s for s in snaps.get(app_id, []) if s.get("d", "") >= cutoff]
        series = [s for s in series if s.get("d") != today]
        series.append({"d": today, "v": cur})
        series.sort(key=lambda s: s["d"])
        snaps[app_id] = series
        base = None
        for s in series:
            if s["d"] <= win_start:
                base = s["v"]
        if base is None and series:
            base = series[0]["v"]   # 历史不足窗口长度时，用最早一条
        g["velocity"] = max(0, cur - base) if base is not None else 0

    live_ids = {g["app_id"] for g in games}
    snaps = {k: v for k, v in snaps.items() if k in live_ids}
    with open(INSTALLS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False)
    print(f"  [√] 装机量刷新 {refreshed} 款，失败 {failed} 款")

def send_feishu_new_games(found_records):
    """发现新游时推飞书卡片。webhook 从环境变量 FEISHU_WEBHOOK 读（不写进公开代码）。"""
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook or not found_records:
        return
    groups = {}
    for r in found_records:
        groups.setdefault(r["developer"], []).append(r)
    lines = []
    for dev, games in groups.items():
        lines.append(f"**🏢 {dev}**")
        for g in games:
            plat = "🍎" if g["platform"] == "iOS" else "🤖"
            regions = ", ".join(x.upper() for x in (g.get("regions") or []))
            soft = "us" not in [x.lower() for x in (g.get("regions") or [])]
            tag = "🔥 Soft Launch" if soft else "✅ US"
            genre = f" · {g['genre']}" if g.get("genre") else ""
            lines.append(f"{plat} [{g['name']}]({g['url']}){genre} · {regions} {tag}")
        lines.append("")
    body = "\n".join(lines).strip()
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"🎯 New Game Radar · 发现 {len(found_records)} 款新游（{now_cn('%m/%d')}）"}, "template": "blue"},
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "lark_md", "content": "完整看板 → https://sven0920.github.io/competitor-tracker/"}]},
            ],
        },
    }
    try:
        resp = requests.post(
            webhook,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        resp.raise_for_status()
        try:
            result = resp.json()
        except ValueError:
            result = {}
        code = result.get("code", result.get("StatusCode", result.get("Code", 0)))
        if code not in (0, "0", None):
            print("❌ 飞书推送失败:", result)
        else:
            print("✅ 已推送飞书新游卡片")
    except Exception as e:
        print("❌ 飞书推送失败:", e)


def classify_developers(target_developers, known_hits_by_dev, scanned_ios, scanned_android, is_first_run):
    """按本轮扫描结果判断厂商是新加名单还是老熟人。

    旧逻辑：本轮一个已知游戏都没扫到，就当成新厂商，所有未知游戏静默建库。
    Google Play 限流时会把真正的新游吞掉。现在只有一次扫到大量未知游戏才建库。
    """
    unknown_by_dev = {}
    for app_id, g in scanned_ios.items():
        unknown_by_dev.setdefault(g["custom_dev"], 0)
        unknown_by_dev[g["custom_dev"]] += 1
    for app_id, g in scanned_android.items():
        unknown_by_dev.setdefault(g["custom_dev"], 0)
        unknown_by_dev[g["custom_dev"]] += 1

    status = {}
    for c_dev in target_developers:
        known_hits = len(known_hits_by_dev.get(c_dev, ()))
        unknown = unknown_by_dev.get(c_dev, 0)
        if is_first_run:
            status[c_dev] = "new"
        elif known_hits > 0:
            status[c_dev] = "existing"
        elif unknown >= NEW_PUBLISHER_UNKNOWN_THRESHOLD:
            status[c_dev] = "new"
        else:
            status[c_dev] = "existing"
    return status


def main():
    TARGET_DEVELOPERS = fetch_target_developers()
    if not TARGET_DEVELOPERS:
        return

    known_games = load_history()
    is_first_run = len(known_games) == 0

    scanned_ios_games = {}
    scanned_android_games = {}
    found_records = []
    seen_ios_keys = set()
    known_hits_by_dev = {}
    ios_fail = 0
    android_fail = 0

    print("\n🚀 开始跨地区抓取竞品数据...")
    for custom_dev, accounts in TARGET_DEVELOPERS.items():
        print(f"  ⏳ 正在检索厂商: {custom_dev}")

        # --- 抓取 iOS ---
        for artist_id in accounts["ios"]:
            for country in TARGET_COUNTRIES:
                key = (artist_id, country)
                if key in seen_ios_keys:
                    continue
                seen_ios_keys.add(key)
                try:
                    url = (
                        f"https://itunes.apple.com/lookup?id={artist_id}&entity=software"
                        f"&country={country}&sort=recent&limit={ITUNES_LIMIT}"
                    )
                    data = requests.get(url, headers=ITUNES_HEADERS, timeout=15).json().get("results", [])[1:]
                    for game in data:
                        app_id = str(game.get("trackId"))
                        if app_id in known_games:
                            known_hits_by_dev.setdefault(custom_dev, set()).add(app_id)
                            continue
                        if app_id not in scanned_ios_games:
                            scanned_ios_games[app_id] = {
                                "custom_dev": custom_dev,
                                "name": game.get("trackName", "未知"),
                                "icon": game.get("artworkUrl100", ""),
                                "ratings": game.get("userRatingCount", 0) or 0,
                                "genre": ios_genre(game),
                                "shots": (game.get("screenshotUrls") or [])[:SHOTS_MAX],
                                "url": game.get("trackViewUrl", f"https://apps.apple.com/app/id{app_id}"),
                                "release_date": game.get("releaseDate", "").split("T")[0],
                                "size": bytes_to_mb(game.get("fileSizeBytes", 0)),
                                "iap_info": "未知",
                                "regions": set()
                            }
                        if app_id in scanned_ios_games:
                            scanned_ios_games[app_id]["regions"].add(country)
                except Exception as e:
                    ios_fail += 1
                    print(f"  [!] iOS 抓取失败 {custom_dev} {artist_id} {country}: {e}")

        # --- 抓取 Android ---
        for dev_name in accounts["android"]:
            for country in TARGET_COUNTRIES:
                try:
                    results = with_retry(
                        lambda n=dev_name, c=country: search(n, lang="en", country=c, n_hits=60)
                    )
                    if not results:
                        continue
                    for game in results:
                        game_dev = game.get("developer", "")
                        if dev_name.upper() not in game_dev.upper():
                            continue
                        app_id = game.get("appId")
                        if app_id in known_games:
                            known_hits_by_dev.setdefault(custom_dev, set()).add(app_id)
                            continue
                        if app_id not in scanned_android_games:
                            scanned_android_games[app_id] = {
                                "custom_dev": custom_dev,
                                "name": game.get("title", "未知"),
                                "icon": game.get("icon", ""),
                                "url": f"https://play.google.com/store/apps/details?id={app_id}",
                                "regions": set()
                            }
                        if app_id in scanned_android_games:
                            scanned_android_games[app_id]["regions"].add(country)
                except Exception as e:
                    android_fail += 1
                    print(f"  [!] Android 抓取失败 {custom_dev} {dev_name} {country}: {e}")

    print("\n🔍 正在进行基准线分析与数据比对...")
    dev_status = classify_developers(
        TARGET_DEVELOPERS, known_hits_by_dev, scanned_ios_games, scanned_android_games, is_first_run
    )

    # 处理 iOS
    for app_id, game_data in scanned_ios_games.items():
        c_dev = game_data.pop("custom_dev")
        game_data["regions"] = list(game_data["regions"])
        if dev_status[c_dev] == "new" or is_first_run:
            print(f"  [建库] 🍎 {game_data['name']} (首次录入厂商 {c_dev}，存量静默保存)")
        elif is_stale_release(game_data["release_date"]):
            print(f"  [建库] 🍎 {game_data['name']} (上架超过 {NEW_GAME_MAX_AGE_DAYS} 天，忽略)")
        else:
            found_records.append({
                "app_id": app_id,
                "developer": c_dev,
                "platform": "iOS",
                "name": game_data["name"],
                "icon": game_data.get("icon", ""),
                "ratings": game_data.get("ratings", 0),
                "genre": game_data.get("genre", ""),
                "shots": game_data.get("shots", []),
                "regions": game_data["regions"],
                "size": game_data["size"],
                "iap_info": game_data["iap_info"],
                "release_date": game_data["release_date"],
                "url": game_data["url"],
            })
        known_games[app_id] = {"name": game_data["name"], "regions": game_data["regions"]}

    # 处理 Android
    for app_id, base_data in scanned_android_games.items():
        c_dev = base_data.pop("custom_dev")
        base_data["regions"] = list(base_data["regions"])
        if dev_status[c_dev] == "new" or is_first_run:
            print(f"  [建库] 🤖 {base_data['name']} (首次录入厂商 {c_dev}，极速静默保存)")
        else:
            # 只有对老熟人的新游戏，才去请求详情（耗时操作）
            try:
                details = with_retry(
                    lambda aid=app_id, c=preferred_country(base_data["regions"]): app(aid, lang="en", country=c)
                )
                base_data["name"] = details.get("title", base_data["name"])
                base_data["icon"] = details.get("icon", base_data.get("icon", ""))
                size = details.get("size", "因设备而异")
                iap_info = details.get("inAppProductPrice", "无内购")
                release_date = details.get("released", "未知日期")
                installs = details.get("installs", "")
                real_installs = details.get("realInstalls", 0) or 0
                genre = details.get("genre", "")
                shots = (details.get("screenshots") or [])[:SHOTS_MAX]
            except Exception as e:
                print(f"  [!] Android 详情失败 {app_id}: {e}")
                size, iap_info, release_date, installs = "未知", "未知", "未知日期", ""
                real_installs, genre, shots = 0, "", []
            if is_stale_release(release_date):
                print(f"  [建库] 🤖 {base_data['name']} (上架超过 {NEW_GAME_MAX_AGE_DAYS} 天，忽略)")
            else:
                found_records.append({
                    "app_id": app_id,
                    "developer": c_dev,
                    "platform": "Android",
                    "name": base_data["name"],
                    "icon": base_data.get("icon", ""),
                    "installs": installs,
                    "real_installs": real_installs,
                    "genre": genre,
                    "shots": shots,
                    "regions": base_data["regions"],
                    "size": size,
                    "iap_info": iap_info,
                    "release_date": release_date,
                    "url": base_data["url"],
                })
        known_games[app_id] = {"name": base_data["name"], "regions": base_data["regions"]}

    save_history(known_games)
    save_data(found_records)

    print("\n" + "=" * 60)
    if ios_fail or android_fail:
        print(f"⚠️ 本轮抓取失败：iOS {ios_fail} 次，Android {android_fail} 次（详见上方日志）")
    if is_first_run:
        print(f"✅ 首次建库完毕！共记录 {len(known_games)} 款跨区游戏。")
    elif found_records:
        print(f"🚨 本次发现 {len(found_records)} 款新游，已写入 data.json。")
        send_feishu_new_games(found_records)
    else:
        print("💤 本次监控的厂商均无新游发布。")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
