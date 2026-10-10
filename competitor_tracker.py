import warnings
warnings.filterwarnings("ignore")

import requests
import json
import os
import csv
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
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
FRESH_PUSH_DAYS = 30          # 飞书只推上架不超过这么多天的；更老的进看板「补录」
NEW_PUBLISHER_UNKNOWN_THRESHOLD = 8  # 一次扫到这么多未知游戏，才视为「新加厂商建库」
PLAY_REFRESH_MAX_FAILS = 8   # 连续刷新失败这么多次，判定被限流，停止后续拉取
CORE_MARKETS = {"us", "gb", "ca", "au"}  # 软启动进入这些主力市场时再报一次
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


def release_age_days(s, now=None):
    """上架距 now 的天数。解析不了返回 None；预售是负数。"""
    d = parse_release_day(s)
    if d is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - d).days


def is_stale_release(s, max_age=NEW_GAME_MAX_AGE_DAYS):
    age = release_age_days(s)
    if age is None:
        return False
    return age > max_age


def is_backfill_release(s, now=None):
    """没有可解析的上架日，或已经超过新鲜窗口。预售算刚上架。"""
    age = release_age_days(s, now=now)
    if age is None:
        return True
    return age > FRESH_PUSH_DAYS


def preferred_country(regions):
    """详情请求优先用 us，避免内购价格变成土耳其里拉 / 越南盾。"""
    regs = [str(x).lower() for x in (regions or [])]
    for c in TARGET_COUNTRIES:
        if c in regs:
            return c
    return regs[0] if regs else "us"


def note_known_sighting(sightings, app_id, country, platform, name, url, developer):
    """已入库游戏本轮又被扫到：记下国家，供后面合并地区。"""
    slot = sightings.setdefault(app_id, {
        "regions": set(),
        "platform": platform,
        "name": name or "",
        "url": url or "",
        "developer": developer or "",
    })
    if country:
        slot["regions"].add(str(country).lower())
    if name and name not in ("未知",):
        slot["name"] = name
    if url:
        slot["url"] = url
    if developer:
        slot["developer"] = developer
    return slot


def apply_region_sightings(known_games, sightings):
    """把本轮看到的地区并进基准库。只增不减，半次失败不会把已有地区抹掉。

    新出现 us / gb / ca / au 时记成「地区扩大」，不当作新游。
    返回 (expansions, region_updates)。
    """
    expansions = []
    updates = {}
    for app_id, seen in sightings.items():
        entry = known_games.get(app_id)
        if not isinstance(entry, dict):
            continue
        prev = {str(r).lower() for r in (entry.get("regions") or []) if r}
        now = {str(r).lower() for r in (seen.get("regions") or []) if r}
        merged = prev | now
        new_core = sorted((now - prev) & CORE_MARKETS)
        entry["regions"] = sorted(merged)
        if seen.get("name") and entry.get("name") in (None, "", "未知"):
            entry["name"] = seen["name"]
        updates[app_id] = list(entry["regions"])
        if new_core:
            expansions.append({
                "app_id": app_id,
                "developer": seen.get("developer") or "",
                "platform": seen.get("platform") or "",
                "name": seen.get("name") or entry.get("name") or app_id,
                "url": seen.get("url") or "",
                "regions": list(entry["regions"]),
                "new_core": new_core,
            })
    return expansions, updates


def apply_region_updates(games, updates):
    """看板上已有的卡片同步最新地区，软启动标签才会跟着变。"""
    if not updates:
        return
    for g in games:
        regions = updates.get(g.get("app_id"))
        if regions:
            g["regions"] = list(regions)


def with_retry(fn, tries=3, delay=1.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(delay * (i + 1))
    raise last


PLAY_STORE_BASE = "https://play.google.com"
DEV_PAGE_SIZE = 100
DEV_MAX_PAGES = 20
_SCRIPT_RE = re.compile(r"AF_initDataCallback[\s\S]*?</script")
_KEY_RE = re.compile(r"(ds:.*?)'")
_VALUE_RE = re.compile(r"data:([\s\S]*?), sideChannel: {}}\);<\/")
# 翻页请求里 Play 用来挑字段的固定下标，和开发者页「查看更多」同一套
_CLUSTER_FIELDS = [96, 27, 4, 8, 57, 30, 110, 79, 11, 16, 49, 1, 3, 9, 12, 104, 55, 56, 51, 10, 34, 77]


class PlayStopped(Exception):
    """熔断已打开，调用方应停手并保留已有数据。"""


class PlayGuard:
    """连续失败达到 PLAY_REFRESH_MAX_FAILS 后停止后续 Play 请求。"""

    def __init__(self, max_fails=PLAY_REFRESH_MAX_FAILS):
        self.max_fails = max_fails
        self.consecutive = 0
        self.tripped = False
        self.failed = 0

    def allow(self):
        return not self.tripped

    def success(self):
        self.consecutive = 0

    def failure(self):
        self.consecutive += 1
        self.failed += 1
        if self.consecutive >= self.max_fails and not self.tripped:
            self.tripped = True
            print("  [!] Google Play 连续失败，停止后续 Play 请求（沿用已有数据）")

    def call(self, fn):
        if self.tripped:
            raise PlayStopped("Google Play circuit open")
        try:
            result = fn()
        except PlayStopped:
            raise
        except Exception:
            self.failure()
            raise
        self.success()
        return result


def dig(node, path, default=None):
    cur = node
    for p in path:
        if not isinstance(cur, list) or not isinstance(p, int) or p < 0 or p >= len(cur):
            return default
        cur = cur[p]
    return cur


def parse_play_dataset(html):
    dataset = {}
    if not html:
        return dataset
    for match in _SCRIPT_RE.findall(html):
        keys = _KEY_RE.findall(match)
        vals = _VALUE_RE.findall(match)
        if not keys or not vals:
            continue
        try:
            dataset[keys[0]] = json.loads(vals[0])
        except json.JSONDecodeError:
            continue
    return dataset


def _package_name(value):
    if not isinstance(value, str):
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9][A-Za-z0-9_]*)+", value):
        return None
    return value


def parse_catalog_app(node):
    """开发者页首屏卡片和搜索卡片同结构；翻页接口是另一套扁平结构。"""
    app_id = _package_name(dig(node, [0, 0, 0]))
    title = dig(node, [0, 3])
    developer = dig(node, [0, 14])
    icon = dig(node, [0, 1, 3, 2]) or ""
    if app_id and isinstance(title, str):
        return {
            "appId": app_id,
            "title": title,
            "developer": developer if isinstance(developer, str) else "",
            "icon": icon if isinstance(icon, str) else "",
        }
    app_id = _package_name(dig(node, [12, 0]))
    title = dig(node, [2])
    developer = dig(node, [4, 0, 0, 0])
    if app_id and isinstance(title, str):
        return {
            "appId": app_id,
            "title": title,
            "developer": developer if isinstance(developer, str) else "",
            "icon": "",
        }
    return None


def parse_developer_page(html):
    """从开发者商店主页取出本页应用和翻页 token。解析不到返回空列表。"""
    ds = parse_play_dataset(html).get("ds:3")
    if ds is None:
        return [], None
    for apps_path, token_path in (
        ([0, 1, 0, 22, 0], [0, 1, 0, 22, 1, 3, 1]),  # /store/apps/developer?id=名字
        ([0, 1, 0, 21, 0], [0, 1, 0, 21, 1, 3, 1]),  # /store/apps/dev?id=数字
    ):
        raw_apps = dig(ds, apps_path)
        if not isinstance(raw_apps, list) or not raw_apps:
            continue
        apps = [item for item in (parse_catalog_app(node) for node in raw_apps) if item]
        token = dig(ds, token_path)
        if not isinstance(token, str) or len(token) < 20:
            token = None
        return apps, token
    return [], None


def parse_developer_pagination(raw):
    if not raw:
        return [], None
    start = raw.find("\n")
    payload = raw[start + 1:] if raw.startswith(")]}'") else raw
    try:
        outer = json.loads(payload)
        inner_text = None
        for item in outer:
            if isinstance(item, list) and len(item) > 2 and item[1] == "qnKhOb" and isinstance(item[2], str):
                inner_text = item[2]
                break
        if inner_text is None:
            return [], None
        inner = json.loads(inner_text)
    except (json.JSONDecodeError, TypeError, IndexError):
        return [], None
    raw_apps = dig(inner, [0, 0, 0]) or []
    apps = []
    if isinstance(raw_apps, list):
        apps = [item for item in (parse_catalog_app(node) for node in raw_apps) if item]
    token = dig(inner, [0, 0, 7, 1])
    if not isinstance(token, str) or len(token) < 20:
        token = None
    return apps, token


def developer_page_url(dev_name, country, lang="en"):
    return (
        f"{PLAY_STORE_BASE}/store/apps/developer?id={quote(dev_name)}"
        f"&hl={quote(lang)}&gl={quote(country)}"
    )


def developer_pagination_body(token, count=DEV_PAGE_SIZE):
    inner = [[None, [[10, [10, count]], True, None, list(_CLUSTER_FIELDS)], None, token]]
    wrapped = [[["qnKhOb", json.dumps(inner, separators=(",", ":")), None, "generic"]]]
    return "f.req=" + quote(json.dumps(wrapped, separators=(",", ":")))


def developer_pagination_url(country, lang="en"):
    return (
        f"{PLAY_STORE_BASE}/_/PlayStoreUi/data/batchexecute"
        f"?rpcids=qnKhOb&hl={quote(lang)}&gl={quote(country)}"
    )


def _looks_blocked(html):
    markers = ("PlayGatewayError", "unusual traffic", 'id="captcha"')
    return any(marker in (html or "") for marker in markers)


def http_get(url):
    resp = requests.get(url, headers={"User-Agent": "competitor-tracker/1.0"}, timeout=20)
    resp.raise_for_status()
    text = resp.text or ""
    if _looks_blocked(text):
        raise RuntimeError("Google Play blocked the developer page")
    return text


def http_post(url, body):
    resp = requests.post(
        url,
        data=body,
        headers={
            "User-Agent": "competitor-tracker/1.0",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.text or ""
    if "com.google.play.gateway.proto.PlayGatewayError" in text:
        raise RuntimeError("Google Play gateway error")
    return text


def fetch_developer_catalog(dev_name, country, guard, get=None, post=None):
    """抓开发者商店主页并翻页补全。请求失败抛异常；页面正常但没有应用则返回空列表。"""
    getter = get or (lambda url: with_retry(lambda: http_get(url)))
    poster = post or (lambda url, body: with_retry(lambda: http_post(url, body)))
    html = guard.call(lambda: getter(developer_page_url(dev_name, country)))
    apps, token = parse_developer_page(html)
    seen = set()
    pages = 0
    merged = list(apps)
    known_ids = {a["appId"] for a in merged}
    while token and token not in seen and pages < DEV_MAX_PAGES and guard.allow():
        seen.add(token)
        try:
            raw = guard.call(lambda t=token: poster(developer_pagination_url(country), developer_pagination_body(t)))
        except PlayStopped:
            break
        except Exception as e:
            print(f"  [!] 开发者页翻页失败 {dev_name} {country}: {e}")
            break
        more, token = parse_developer_pagination(raw)
        if not more:
            break
        for item in more:
            if item["appId"] not in known_ids:
                merged.append(item)
                known_ids.add(item["appId"])
        pages += 1
    return merged


def search_fallback_apps(dev_name, country, guard, search_fn=None):
    """搜索只做兜底，仍用开发者名子串挡住短名字串台。"""
    def default_search(name, store_country):
        return with_retry(lambda: search(name, lang="en", country=store_country, n_hits=60))

    fn = search_fn or default_search
    results = guard.call(lambda: fn(dev_name, country))
    kept = []
    for game in results or []:
        game_dev = game.get("developer") or ""
        if dev_name.upper() not in game_dev.upper():
            continue
        if not game.get("appId"):
            continue
        kept.append({
            "appId": game.get("appId"),
            "title": game.get("title") or "未知",
            "developer": game_dev,
            "icon": game.get("icon") or "",
        })
    return kept


def android_apps_for_country(dev_name, country, guard, search_fn=None, get=None, post=None):
    """先抓该开发者自己的商店列表；主页没有或打不开时才搜索。"""
    if not guard.allow():
        raise PlayStopped("Google Play circuit open")
    try:
        catalog = fetch_developer_catalog(dev_name, country, guard, get=get, post=post)
    except PlayStopped:
        raise
    except Exception as e:
        print(f"  [!] 开发者页抓取失败 {dev_name} {country}: {e}")
        catalog = []
    if catalog:
        return catalog, "developer"
    if not guard.allow():
        raise PlayStopped("Google Play circuit open")
    print(f"  [i] Android 开发者页无结果，改用搜索兜底: {dev_name} {country}")
    try:
        return search_fallback_apps(dev_name, country, guard, search_fn=search_fn), "search"
    except PlayStopped:
        raise


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

def save_data(found_records, region_updates=None, play_guard=None):
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
    apply_region_updates(data["games"], region_updates)
    # 裁掉太旧的发现记录（日期按北京时间，和 found_date 对齐）
    cutoff = (datetime.now(CN_TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["games"] = [g for g in data["games"] if g.get("found_date", "") >= cutoff]
    data["games"].sort(key=lambda g: g.get("found_date", ""), reverse=True)
    data["updated_at"] = now_cn("%Y-%m-%d %H:%M")
    update_velocity(data, play_guard)   # 刷新安卓装机量并算增速
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

def update_velocity(data, play_guard=None):
    """每天重新拉取安卓 realInstalls，写入快照后计算最近 VELOCITY_WINDOW 天增量。"""
    today = now_cn("%Y-%m-%d")
    snaps = _load_snapshots()
    now = datetime.now(CN_TZ)
    cutoff = (now - timedelta(days=SNAPSHOT_KEEP_DAYS)).strftime("%Y-%m-%d")
    win_start = (now - timedelta(days=VELOCITY_WINDOW)).strftime("%Y-%m-%d")
    guard = play_guard or PlayGuard()

    games = [g for g in data["games"] if g.get("platform") == "Android"]
    games.sort(key=lambda g: g.get("found_date", ""), reverse=True)

    refreshed, failed = 0, 0
    for g in games:
        app_id = g["app_id"]
        cur = g.get("real_installs", 0) or 0
        if guard.allow():
            try:
                country = preferred_country(g.get("regions"))
                d = guard.call(lambda aid=app_id, c=country: with_retry(
                    lambda: app(aid, lang="en", country=c)
                ))
                cur = d.get("realInstalls", 0) or 0
                g["real_installs"] = cur
                if d.get("installs"):
                    g["installs"] = d.get("installs", "")
                refreshed += 1
            except Exception as e:
                failed += 1
                print(f"  [!] 装机量刷新失败 {app_id}: {e}")
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

def _feishu_card(title, body, template="blue"):
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "lark_md", "content": "完整看板 → https://sven0920.github.io/competitor-tracker/"}]},
            ],
        },
    }


def _group_lines(records, line_for):
    groups = {}
    for r in records:
        groups.setdefault(r.get("developer") or "未知厂商", []).append(r)
    lines = []
    for dev, games in groups.items():
        lines.append(f"**🏢 {dev}**")
        for g in games:
            lines.append(line_for(g))
        lines.append("")
    return "\n".join(lines).strip()


def pushable_new_games(found_records):
    """飞书只推刚上架。补录留在看板上。"""
    return [r for r in (found_records or []) if not r.get("backfill")]


def build_new_games_payload(found_records):
    found_records = pushable_new_games(found_records)
    if not found_records:
        return None

    def line_for(g):
        plat = "🍎" if g.get("platform") == "iOS" else "🤖"
        regions = ", ".join(x.upper() for x in (g.get("regions") or []))
        soft = "us" not in [x.lower() for x in (g.get("regions") or [])]
        tag = "🔥 Soft Launch" if soft else "✅ US"
        genre = f" · {g['genre']}" if g.get("genre") else ""
        return f"{plat} [{g['name']}]({g['url']}){genre} · {regions} {tag}"

    body = _group_lines(found_records, line_for)
    title = f"🎯 New Game Radar · 发现 {len(found_records)} 款新游（{now_cn('%m/%d')}）"
    return _feishu_card(title, body, "blue")


def build_region_expansion_payload(records):
    """已入库游戏新进入主力市场。单独一条，不和「发现新游」混在一起。"""
    if not records:
        return None
    shown = records[:40]
    extra = len(records) - len(shown)

    def line_for(g):
        plat = "🍎" if g.get("platform") == "iOS" else "🤖"
        new_core = ", ".join(x.upper() for x in (g.get("new_core") or []))
        regions = ", ".join(x.upper() for x in (g.get("regions") or []))
        return f"{plat} [{g['name']}]({g['url']}) · 新进 {new_core} · 现有 {regions}"

    body = _group_lines(shown, line_for)
    if extra:
        body += f"\n\n另有 {extra} 款，地区已写入基准库。"
    title = f"🌍 New Game Radar · 地区扩大 {len(records)} 款进入主力市场（{now_cn('%m/%d')}）"
    return _feishu_card(title, body, "orange")


def post_feishu(payload, ok_message):
    """webhook 从环境变量 FEISHU_WEBHOOK 读（不写进公开代码）。没有 webhook 就直接返回。"""
    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook or not payload:
        return False
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
            return False
        print(ok_message)
        return True
    except Exception as e:
        print("❌ 飞书推送失败:", e)
        return False


def send_feishu_new_games(found_records):
    """发现新游时推飞书卡片。"""
    post_feishu(build_new_games_payload(found_records), "✅ 已推送飞书新游卡片")


def send_feishu_region_expansions(records):
    post_feishu(build_region_expansion_payload(records), "✅ 已推送飞书地区扩大卡片")


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
    region_sightings = {}
    ios_fail = 0
    android_fail = 0
    play_guard = PlayGuard()

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
                            note_known_sighting(
                                region_sightings, app_id, country, "iOS",
                                game.get("trackName"),
                                game.get("trackViewUrl") or f"https://apps.apple.com/app/id{app_id}",
                                custom_dev,
                            )
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

        # --- 抓取 Android：开发者商店主页，搜索只做兜底 ---
        if not play_guard.allow():
            continue
        for dev_name in accounts["android"]:
            if not play_guard.allow():
                break
            for country in TARGET_COUNTRIES:
                if not play_guard.allow():
                    break
                try:
                    results, _source = android_apps_for_country(dev_name, country, play_guard)
                except PlayStopped:
                    break
                except Exception as e:
                    android_fail += 1
                    print(f"  [!] Android 抓取失败 {custom_dev} {dev_name} {country}: {e}")
                    continue
                for game in results:
                    app_id = game.get("appId")
                    if not app_id:
                        continue
                    if app_id in known_games:
                        known_hits_by_dev.setdefault(custom_dev, set()).add(app_id)
                        note_known_sighting(
                            region_sightings, app_id, country, "Android",
                            game.get("title"),
                            f"https://play.google.com/store/apps/details?id={app_id}",
                            custom_dev,
                        )
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

    print("\n🔍 正在进行基准线分析与数据比对...")
    expansions, region_updates = apply_region_sightings(known_games, region_sightings)
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
                "backfill": is_backfill_release(game_data["release_date"]),
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
            if not play_guard.allow():
                print(f"  [!] Android 详情跳过 {app_id}（Play 已熔断，下轮再试，不写入基准库）")
                continue
            try:
                details = play_guard.call(lambda aid=app_id, c=preferred_country(base_data["regions"]): with_retry(
                    lambda: app(aid, lang="en", country=c)
                ))
                base_data["name"] = details.get("title", base_data["name"])
                base_data["icon"] = details.get("icon", base_data.get("icon", ""))
                size = details.get("size", "因设备而异")
                iap_info = details.get("inAppProductPrice", "无内购")
                release_date = details.get("released", "未知日期")
                installs = details.get("installs", "")
                real_installs = details.get("realInstalls", 0) or 0
                genre = details.get("genre", "")
                shots = (details.get("screenshots") or [])[:SHOTS_MAX]
            except PlayStopped:
                print(f"  [!] Android 详情跳过 {app_id}（Play 已熔断，下轮再试，不写入基准库）")
                continue
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
                    "backfill": is_backfill_release(release_date),
                })
        known_games[app_id] = {"name": base_data["name"], "regions": base_data["regions"]}

    save_history(known_games)
    save_data(found_records, region_updates, play_guard)

    print("\n" + "=" * 60)
    if ios_fail or android_fail:
        print(f"⚠️ 本轮抓取失败：iOS {ios_fail} 次，Android {android_fail} 次（详见上方日志）")
    if expansions:
        print(f"🌍 {len(expansions)} 款已入库游戏进入主力市场（us/gb/ca/au）。")
        send_feishu_region_expansions(expansions)
    fresh_records = pushable_new_games(found_records)
    backfill_records = [r for r in found_records if r.get("backfill")]
    if is_first_run:
        print(f"✅ 首次建库完毕！共记录 {len(known_games)} 款跨区游戏。")
    elif found_records:
        print(f"🚨 本次写入 {len(found_records)} 款（刚上架 {len(fresh_records)}，补录 {len(backfill_records)}）。")
        send_feishu_new_games(fresh_records)
        if backfill_records and not fresh_records:
            print("📥 本轮只有补录，不推飞书。")
    else:
        print("💤 本次监控的厂商均无新游发布。")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
