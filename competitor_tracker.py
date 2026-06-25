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

# ================= 配置区 =================
TARGET_COUNTRIES = ["us", "ph", "au", "ca", "gb"]
KEEP_DAYS = 120   # data.json 里保留最近多少天发现的新游
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
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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
                size = details.get("size", "因设备而异")
                iap_info = details.get("inAppProductPrice", "无内购")
                release_date = details.get("released", "未知日期")
            except:
                size, iap_info, release_date = "未知", "未知", "未知日期"
            found_records.append({
                "app_id": app_id,
                "developer": c_dev,
                "platform": "Android",
                "name": base_data["name"],
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
    else:
        print("💤 本次监控的厂商均无新游发布。")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
