import warnings
warnings.filterwarnings("ignore")

import requests
import json
import os
import csv
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
CN_TZ = timezone(timedelta(hours=8))
# ==========================================

def now_cn(fmt):
    return datetime.now(timezone.utc).astimezone(CN_TZ).strftime(fmt)

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
                    if android_id:
                        targets[custom_dev_name]["android"].append(android_id)
                    if ios_id:
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
    except:
        return "未知大小"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
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
    # 裁掉太旧的发现记录
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
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
        except:
            pass
    return {}

def update_velocity(data):
    """对当前窗口内的安卓游戏逐日记录 realInstalls 快照，算出最近 VELOCITY_WINDOW 天的装机增量。"""
    today = now_cn("%Y-%m-%d")
    snaps = _load_snapshots()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_KEEP_DAYS)).strftime("%Y-%m-%d")
    win_start = (datetime.now(timezone.utc) - timedelta(days=VELOCITY_WINDOW)).strftime("%Y-%m-%d")

    for g in data["games"]:
        if g.get("platform") != "Android":
            continue
        app_id = g["app_id"]
        # 取当前 realInstalls（已有则用记录值，否则现拉一次）
        cur = g.get("real_installs", 0) or 0
        if not cur:
            try:
                d = app(app_id, lang="en", country=(g.get("regions") or ["us"])[0])
                cur = d.get("realInstalls", 0) or 0
                g["real_installs"] = cur
                if not g.get("installs"):
                    g["installs"] = d.get("installs", "")
            except:
                cur = 0
        if not cur:
            continue
        # 记录今日快照（按日期去重）
        series = [s for s in snaps.get(app_id, []) if s.get("d", "") >= cutoff]
        series = [s for s in series if s.get("d") != today]
        series.append({"d": today, "v": cur})
        series.sort(key=lambda s: s["d"])
        snaps[app_id] = series
        # 增速 = 最新值 - 窗口起点之前最近一条
        base = None
        for s in series:
            if s["d"] <= win_start:
                base = s["v"]
        if base is None and series:
            base = series[0]["v"]   # 历史不足窗口长度时，用最早一条
        g["velocity"] = max(0, cur - base) if base is not None else 0

    with open(INSTALLS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False)

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
        requests.post(webhook, headers={"Content-Type": "application/json"},
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=10)
        print("✅ 已推送飞书新游卡片")
    except Exception as e:
        print("❌ 飞书推送失败:", e)


def main():
    TARGET_DEVELOPERS = fetch_target_developers()
    if not TARGET_DEVELOPERS:
        return

    known_games = load_history()
    is_first_run = len(known_games) == 0

    scanned_ios_games = {}
    scanned_android_games = {}
    found_records = []

    # 核心：智能判断厂商是“老熟人”还是“新来的”
    dev_status = {c_dev: "new" for c_dev in TARGET_DEVELOPERS.keys()}

    print("\n🚀 开始跨地区抓取竞品数据...")
    for custom_dev, accounts in TARGET_DEVELOPERS.items():
        print(f"  ⏳ 正在检索厂商: {custom_dev}")

        # --- 抓取 iOS ---
        for artist_id in accounts["ios"]:
            for country in TARGET_COUNTRIES:
                try:
                    data = requests.get(f"https://itunes.apple.com/lookup?id={artist_id}&entity=software&country={country}&sort=recent", timeout=10).json().get("results", [])[1:]
                    for game in data:
                        app_id = str(game.get("trackId"))
                        if app_id in known_games:
                            dev_status[custom_dev] = "existing"
                        elif app_id not in scanned_ios_games:
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
                                "iap_info": "支持内购" if game.get("features", []) else "未见明细",
                                "regions": set()
                            }
                        if app_id in scanned_ios_games:
                            scanned_ios_games[app_id]["regions"].add(country)
                except:
                    pass

        # --- 抓取 Android ---
        for dev_name in accounts["android"]:
            for country in TARGET_COUNTRIES:
                try:
                    results = search(dev_name, lang="en", country=country, n_hits=60)
                    if not results:
                        continue
                    for game in results:
                        game_dev = game.get("developer", "")
                        if dev_name.upper() not in game_dev.upper():
                            continue
                        app_id = game.get("appId")
                        if app_id in known_games:
                            dev_status[custom_dev] = "existing"
                        elif app_id not in scanned_android_games:
                            scanned_android_games[app_id] = {
                                "custom_dev": custom_dev,
                                "name": game.get("title", "未知"),
                                "icon": game.get("icon", ""),
                                "url": f"https://play.google.com/store/apps/details?id={app_id}",
                                "regions": set()
                            }
                        if app_id in scanned_android_games:
                            scanned_android_games[app_id]["regions"].add(country)
                except:
                    pass

    print("\n🔍 正在进行基准线分析与数据比对...")

    # 处理 iOS
    for app_id, game_data in scanned_ios_games.items():
        c_dev = game_data.pop("custom_dev")
        game_data["regions"] = list(game_data["regions"])
        if dev_status[c_dev] == "new" or is_first_run:
            print(f"  [建库] 🍎 {game_data['name']} (首次录入厂商 {c_dev}，存量静默保存)")
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
                details = app(app_id, lang='en', country=base_data["regions"][0])
                base_data["name"] = details.get("title", base_data["name"])
                base_data["icon"] = details.get("icon", base_data.get("icon", ""))
                size = details.get("size", "因设备而异")
                iap_info = details.get("inAppProductPrice", "无内购")
                release_date = details.get("released", "未知日期")
                installs = details.get("installs", "")
                real_installs = details.get("realInstalls", 0) or 0
                genre = details.get("genre", "")
                shots = (details.get("screenshots") or [])[:SHOTS_MAX]
            except:
                size, iap_info, release_date, installs = "未知", "未知", "未知日期", ""
                real_installs, genre, shots = 0, "", []
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
