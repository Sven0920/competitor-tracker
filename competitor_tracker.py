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
from google_play_scraper.exceptions import NotFoundError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "competitor_list.json")   # Known games, used to decide what is new
TARGETS_FILE = os.path.join(SCRIPT_DIR, "targets.csv")            # Watchlist
DATA_FILE = os.path.join(SCRIPT_DIR, "data.json")                # Discoveries shown on the board
INSTALLS_HISTORY_FILE = os.path.join(SCRIPT_DIR, "installs_history.json")  # Daily Android install snapshots for growth

# ================= Settings =================
# us/ph/au/ca/gb are core markets. tr/br/vn/id/mx are common soft-launch test markets.
TARGET_COUNTRIES = ["us", "ph", "au", "ca", "gb", "tr", "br", "vn", "id", "mx"]
KEEP_DAYS = 120        # How many days of discoveries to keep in data.json
SNAPSHOT_KEEP_DAYS = 35  # How many days of install snapshots to keep
VELOCITY_WINDOW = 7    # Install-growth window, in days
SHOTS_MAX = 4          # Screenshots stored per game
ITUNES_LIMIT = 200     # lookup returns 50 by default, which misses large catalogs
NEW_GAME_MAX_AGE_DAYS = 180  # Older releases are added to the baseline and skipped
FRESH_PUSH_DAYS = 30          # At most this old (including pre-orders) counts as just released; older goes to Backfill
NEW_PUBLISHER_UNKNOWN_THRESHOLD = 8  # This many unknown games in one pass means a newly added developer
PLAY_REFRESH_MAX_FAILS = 8   # Stop further Play requests after this many consecutive failures
CORE_MARKETS = {"us", "gb", "ca", "au"}  # Entering one of these from a soft launch is a region expansion
# Feishu master switch. Messages send only when this is the boolean True.
# The daily job must not pass FEISHU_WEBHOOK. Leave this False so a stored secret cannot fire by accident.
UNKNOWN = "Unknown"
UNKNOWN_DATE = "Unknown date"
UNKNOWN_SIZE = "Unknown size"
NO_IAP = "No IAP"
VARIES_WITH_DEVICE = "Varies with device"
NON_US_PRICE_MARK = "Non-US store price"
UNKNOWN_DEVELOPER = "Unknown developer"
PLACEHOLDER_NAMES = {"未知", UNKNOWN}
BLANK_RELEASE_DATES = {"未知日期", "未知", UNKNOWN_DATE, UNKNOWN}
LEGACY_IAP = {"支持内购", "未见明细"}
_LABEL_MAP = {
    "未知": UNKNOWN,
    "未知日期": UNKNOWN_DATE,
    "未知大小": UNKNOWN_SIZE,
    "因设备而异": VARIES_WITH_DEVICE,
    "无内购": NO_IAP,
    "支持内购": UNKNOWN,
    "未见明细": UNKNOWN,
}
FEISHU_PUSH_ENABLED = False
CN_TZ = timezone(timedelta(hours=8))
ITUNES_HEADERS = {"User-Agent": "competitor-tracker/1.0"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}
# ==========================================

def now_cn(fmt):
    return datetime.now(timezone.utc).astimezone(CN_TZ).strftime(fmt)


def english_label(value):
    """Map stored Chinese labels to English. Store titles and prices pass through."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text in _LABEL_MAP:
        return _LABEL_MAP[text]
    legacy = "非美国商店价"
    if text.startswith(legacy):
        rest = text[len(legacy):].lstrip(" ·")
        return f"{NON_US_PRICE_MARK} · {rest}" if rest else NON_US_PRICE_MARK
    if text.startswith(NON_US_PRICE_MARK):
        return text
    return value


def normalize_game_labels(game):
    for key in ("name", "size", "iap_info", "release_date"):
        if key in game:
            game[key] = english_label(game[key])


def parse_release_day(s):
    """Parse an iOS ISO date or an Android US-style date to 00:00 UTC. None if it cannot be parsed."""
    s = str(s or "").strip()
    if not s or s in BLANK_RELEASE_DATES:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(1).lower()] + 1, int(m.group(2)), tzinfo=timezone.utc)
    return None


def release_age_days(s, now=None):
    """Days since release. None if the date cannot be parsed. Pre-orders are negative."""
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
    """True when the release date is missing or older than the fresh window. Pre-orders count as just released."""
    age = release_age_days(s, now=now)
    if age is None:
        return True
    return age > FRESH_PUSH_DAYS


def preferred_country(regions):
    """Prefer the US store for detail requests so IAP prices are not shown in lira or dong."""
    regs = [str(x).lower() for x in (regions or [])]
    for c in TARGET_COUNTRIES:
        if c in regs:
            return c
    return regs[0] if regs else "us"


def note_known_sighting(sightings, app_id, country, platform, name, url, developer):
    """A known game seen again this run. Record the country for the region merge."""
    slot = sightings.setdefault(app_id, {
        "regions": set(),
        "platform": platform,
        "name": name or "",
        "url": url or "",
        "developer": developer or "",
    })
    if country:
        slot["regions"].add(str(country).lower())
    if name and name not in PLACEHOLDER_NAMES:
        slot["name"] = name
    if url:
        slot["url"] = url
    if developer:
        slot["developer"] = developer
    return slot


def apply_region_sightings(known_games, sightings):
    """Merge regions seen this run into the baseline. Regions are only added, so a partial failure cannot erase them.

    A newly seen us / gb / ca / au is a region expansion, not a new game.
    Returns (expansions, region_updates).
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
        if seen.get("name") and entry.get("name") in (None, "", *PLACEHOLDER_NAMES):
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
    """Copy the latest regions onto board cards so the soft-launch tag stays current."""
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
        except NotFoundError:
            raise
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
# Fixed field indexes Play uses for pagination, the same set as the developer page "See more" request
_CLUSTER_FIELDS = [96, 27, 4, 8, 57, 30, 110, 79, 11, 16, 49, 1, 3, 9, 12, 104, 55, 56, 51, 10, 34, 77]


class PlayStopped(Exception):
    """The circuit is open. Callers should stop and keep the data they already have."""


class PlayGuard:
    """Stop further Play requests after PLAY_REFRESH_MAX_FAILS consecutive failures."""

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
            print("  [!] Google Play failed repeatedly. Stopping further Play requests and keeping existing data.")

    def call(self, fn):
        if self.tripped:
            raise PlayStopped("Google Play circuit open")
        try:
            result = fn()
        except PlayStopped:
            raise
        except NotFoundError:
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
    """First-page developer cards match search cards. Pagination uses a flatter shape."""
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
    """Read this page of apps and the next-page token from a developer store page. Empty list if parsing fails."""
    ds = parse_play_dataset(html).get("ds:3")
    if ds is None:
        return [], None
    for apps_path, token_path in (
        ([0, 1, 0, 22, 0], [0, 1, 0, 22, 1, 3, 1]),  # /store/apps/developer?id=name
        ([0, 1, 0, 21, 0], [0, 1, 0, 21, 1, 3, 1]),  # /store/apps/dev?id=numeric
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
    """Load a developer store page and follow pagination. A failed request raises. A valid empty page returns []."""
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
            print(f"  [!] Developer page pagination failed for {dev_name} {country}: {e}")
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
    """Search is only a fallback. A developer-name substring filter blocks short-name collisions."""
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
            "title": game.get("title") or UNKNOWN,
            "developer": game_dev,
            "icon": game.get("icon") or "",
        })
    return kept


def android_apps_for_country(dev_name, country, guard, search_fn=None, get=None, post=None):
    """Use the developer's own store list first. Search only when that page is empty or unreachable."""
    if not guard.allow():
        raise PlayStopped("Google Play circuit open")
    try:
        catalog = fetch_developer_catalog(dev_name, country, guard, get=get, post=post)
    except PlayStopped:
        raise
    except Exception as e:
        print(f"  [!] Developer page fetch failed for {dev_name} {country}: {e}")
        catalog = []
    if catalog:
        return catalog, "developer"
    if not guard.allow():
        raise PlayStopped("Google Play circuit open")
    print(f"  [i] Android developer page had no results. Falling back to search: {dev_name} {country}")
    try:
        return search_fallback_apps(dev_name, country, guard, search_fn=search_fn), "search"
    except PlayStopped:
        raise


def fetch_target_developers():
    targets = {}
    if not os.path.exists(TARGETS_FILE):
        print(f"  [!] Watchlist not found: {TARGETS_FILE}")
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
        print(f"  [√] Watchlist loaded. Tracking {len(targets)} developers.")
    except Exception as e:
        print(f"  [x] Failed to read the local CSV: {e}")
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
    # iTunes genres look like ['Games','Casual','Puzzle']. Prefer the more specific later label.
    for g in (game.get("genres") or []):
        if g not in ("Games", "Entertainment"):
            return g
    return game.get("primaryGenreName", "")

def bytes_to_mb(bytes_size):
    try:
        return f"{round(int(bytes_size) / (1024 * 1024), 1)} MB"
    except (TypeError, ValueError):
        return UNKNOWN_SIZE

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": None, "games": []}

def save_data(found_records, region_updates=None, play_guard=None):
    """Merge this run's new games into data.json. Deduplicate by app_id and keep the last KEEP_DAYS."""
    today = now_cn("%Y-%m-%d")
    data = load_data()
    existing_ids = {g.get("app_id") for g in data["games"]}
    for rec in found_records:
        if rec["app_id"] not in existing_ids:
            rec["found_date"] = today
            data["games"].append(rec)
            existing_ids.add(rec["app_id"])
    # iTunes lookup "features" is not an IAP field. Older guessed labels are cleared.
    for g in data["games"]:
        normalize_game_labels(g)
        if g.get("platform") == "iOS" and g.get("iap_info") in LEGACY_IAP:
            g["iap_info"] = UNKNOWN
    apply_region_updates(data["games"], region_updates)
    # Drop discoveries older than the window. Dates use China time, matching found_date.
    cutoff = (datetime.now(CN_TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    data["games"] = [g for g in data["games"] if g.get("found_date", "") >= cutoff]
    data["games"].sort(key=lambda g: g.get("found_date", ""), reverse=True)
    data["updated_at"] = now_cn("%Y-%m-%d %H:%M")
    update_velocity(data, play_guard)   # Refresh Android installs and compute growth
    _, rating_fails = refresh_ios_ratings(data["games"])
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return rating_fails

def _load_snapshots():
    if os.path.exists(INSTALLS_HISTORY_FILE):
        try:
            with open(INSTALLS_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def format_iap(price, us_listing):
    """Keep a US store price as returned. If there is no US listing, mark it so a peso price is not read as dollars."""
    text = english_label(str(price or NO_IAP).strip())
    if us_listing:
        return text
    if text.startswith(NON_US_PRICE_MARK):
        return text
    return f"{NON_US_PRICE_MARK} · {text}"


def android_store_details(app_id, regions, guard):
    """Ask the US store first for details and the IAP price. If it is not listed there, use a seen region and mark the price as non-US.

    A failed request returns None so the caller keeps the previous installs and price. A store 404 does not trip the circuit.
    """
    if not guard.allow():
        raise PlayStopped("Google Play circuit open")
    us_details = None
    us_missing = False
    try:
        us_details = guard.call(lambda: with_retry(lambda: app(app_id, lang="en", country="us")))
    except PlayStopped:
        raise
    except NotFoundError:
        us_missing = True
    except Exception:
        us_details = None
    if us_details is not None:
        return us_details, format_iap(us_details.get("inAppProductPrice"), True)

    country = preferred_country(regions)
    local = None
    if guard.allow() and (us_missing or country != "us"):
        try:
            local = guard.call(lambda: with_retry(lambda: app(app_id, lang="en", country=country)))
        except PlayStopped:
            raise
        except NotFoundError:
            local = None
        except Exception:
            local = None
    if local is None:
        return None
    price = local.get("inAppProductPrice")
    if us_missing:
        price = format_iap(price, False)
    return local, price or NO_IAP


def refresh_ios_ratings(games, get=None):
    """Refresh iOS rating counts in batches. Keep the previous value on failure. Do not guess IAP from features."""
    getter = get or (lambda url: requests.get(url, headers=ITUNES_HEADERS, timeout=20))
    ios = [g for g in games if g.get("platform") == "iOS" and g.get("app_id")]
    updated, failed = 0, 0
    for i in range(0, len(ios), 150):
        chunk = ios[i:i + 150]
        ids = ",".join(str(g["app_id"]) for g in chunk)
        url = f"https://itunes.apple.com/lookup?id={ids}&country=us"
        try:
            resp = getter(url)
            payload = resp.json() if hasattr(resp, "json") else resp
            results = payload.get("results", []) if isinstance(payload, dict) else []
        except Exception as e:
            failed += 1
            print(f"  [!] iOS rating refresh failed: {e}")
            continue
        by_id = {str(row.get("trackId")): row for row in results if row.get("trackId") is not None}
        for g in chunk:
            hit = by_id.get(str(g["app_id"]))
            if not hit or "userRatingCount" not in hit:
                continue
            g["ratings"] = hit.get("userRatingCount") or 0
            updated += 1
    print(f"  [√] Refreshed iOS ratings for {updated} games. Failed batches: {failed}.")
    return updated, failed


def update_velocity(data, play_guard=None):
    """Re-fetch Android realInstalls, store a snapshot, then compute growth over VELOCITY_WINDOW days."""
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
                loaded = android_store_details(app_id, g.get("regions"), guard)
            except Exception as e:
                loaded = None
                failed += 1
                print(f"  [!] Install refresh failed for {app_id}: {e}")
            else:
                if loaded is None:
                    failed += 1
                    print(f"  [!] Install refresh failed for {app_id}: no store details available")
                else:
                    d, iap = loaded
                    cur = d.get("realInstalls", 0) or 0
                    g["real_installs"] = cur
                    if d.get("installs"):
                        g["installs"] = d.get("installs", "")
                    if iap:
                        g["iap_info"] = iap
                    refreshed += 1
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
            base = series[0]["v"]   # Shorter history uses the earliest snapshot
        g["velocity"] = max(0, cur - base) if base is not None else 0

    live_ids = {g["app_id"] for g in games}
    snaps = {k: v for k, v in snaps.items() if k in live_ids}
    with open(INSTALLS_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(snaps, f, ensure_ascii=False)
    print(f"  [√] Refreshed installs for {refreshed} games. Failed: {failed}.")

def _feishu_card(title, body, template="blue"):
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "markdown", "content": body},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "lark_md", "content": "Full board → https://sven0920.github.io/competitor-tracker/"}]},
            ],
        },
    }


def _group_lines(records, line_for):
    groups = {}
    for r in records:
        groups.setdefault(r.get("developer") or UNKNOWN_DEVELOPER, []).append(r)
    lines = []
    for dev, games in groups.items():
        lines.append(f"**🏢 {dev}**")
        for g in games:
            lines.append(line_for(g))
        lines.append("")
    return "\n".join(lines).strip()


def pushable_new_games(found_records):
    """Feishu only includes just-released games. Backfill stays on the board."""
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
    title = f"🎯 New Game Radar · Found {len(found_records)} new game(s) ({now_cn('%m/%d')})"
    return _feishu_card(title, body, "blue")


def build_region_expansion_payload(records):
    """Known games that newly entered a core market. Sent on its own, separate from new-game alerts."""
    if not records:
        return None
    shown = records[:40]
    extra = len(records) - len(shown)

    def line_for(g):
        plat = "🍎" if g.get("platform") == "iOS" else "🤖"
        new_core = ", ".join(x.upper() for x in (g.get("new_core") or []))
        regions = ", ".join(x.upper() for x in (g.get("regions") or []))
        return f"{plat} [{g['name']}]({g['url']}) · New in {new_core} · Now in {regions}"

    body = _group_lines(shown, line_for)
    if extra:
        body += f"\n\n{extra} more title(s) had their regions written to the baseline."
    title = f"🌍 New Game Radar · {len(records)} title(s) expanded into core markets ({now_cn('%m/%d')})"
    return _feishu_card(title, body, "orange")


def post_feishu(payload, ok_message):
    """Post to Feishu. When the master switch is off, return without reading the webhook or sending a request."""
    if FEISHU_PUSH_ENABLED is not True:
        return False
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
            print("❌ Feishu post failed:", result)
            return False
        print(ok_message)
        return True
    except Exception as e:
        print("❌ Feishu post failed:", e)
        return False


def send_feishu_new_games(found_records):
    """Post a Feishu card for new games. Nothing is sent while the master switch is off."""
    return post_feishu(build_new_games_payload(found_records), "✅ Posted the Feishu new-game card")


def send_feishu_region_expansions(records):
    return post_feishu(build_region_expansion_payload(records), "✅ Posted the Feishu region-expansion card")


def scrape_is_incomplete(ios_fail, android_fail, play_guard=None):
    """True when the circuit is open or iOS / Android failures hit the consecutive-failure limit. A healthy day stays quiet."""
    play_failed = play_guard.failed if play_guard is not None else 0
    tripped = bool(play_guard and play_guard.tripped)
    high = PLAY_REFRESH_MAX_FAILS
    return tripped or ios_fail >= high or android_fail >= high or play_failed >= high


def developers_missing_known_games(targets, known_hits_by_dev):
    return sorted(name for name in (targets or {}) if not (known_hits_by_dev or {}).get(name))


def build_incomplete_payload(ios_fail, android_fail, tripped, missed_developers):
    lines = [f"iOS failed {ios_fail} time(s). Android failed {android_fail} time(s)."]
    if tripped:
        lines.append("Google Play hit the failure limit and stopped. Developers after that point were not scraped. Data already collected is kept.")
    lines.append("No new-game alert was sent. A stopped scrape does not mean there were no new games today.")
    if missed_developers:
        shown = list(missed_developers[:40])
        extra = len(missed_developers) - len(shown)
        names = ", ".join(shown)
        if extra:
            names += f", plus {extra} more"
        lines.append("")
        lines.append("**Developers with no known game seen this run:**")
        lines.append(names)
    title = f"⚠️ New Game Radar · Scrape incomplete ({now_cn('%m/%d')})"
    return _feishu_card(title, "\n".join(lines), "red")


def send_feishu_incomplete(ios_fail, android_fail, tripped, missed_developers):
    return post_feishu(
        build_incomplete_payload(ios_fail, android_fail, tripped, missed_developers),
        "✅ Posted the Feishu incomplete-scrape alert",
    )


def classify_developers(target_developers, known_hits_by_dev, scanned_ios, scanned_android, is_first_run):
    """Decide whether each developer is newly added or already known, from this run's scan.

    The old rule treated a developer as new whenever no known game was seen, and silently baselined every unknown title.
    Play rate limits then swallowed real launches. A developer is baselined only when one pass finds a large set of unknown games.
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

    print("\n🚀 Scraping competitor releases across regions...")
    for custom_dev, accounts in TARGET_DEVELOPERS.items():
        print(f"  ⏳ Checking developer: {custom_dev}")

        # --- iOS ---
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
                                "name": game.get("trackName", UNKNOWN),
                                "icon": game.get("artworkUrl100", ""),
                                "ratings": game.get("userRatingCount", 0) or 0,
                                "genre": ios_genre(game),
                                "shots": (game.get("screenshotUrls") or [])[:SHOTS_MAX],
                                "url": game.get("trackViewUrl", f"https://apps.apple.com/app/id{app_id}"),
                                "release_date": game.get("releaseDate", "").split("T")[0],
                                "size": bytes_to_mb(game.get("fileSizeBytes", 0)),
                                "iap_info": UNKNOWN,
                                "regions": set()
                            }
                        if app_id in scanned_ios_games:
                            scanned_ios_games[app_id]["regions"].add(country)
                except Exception as e:
                    ios_fail += 1
                    print(f"  [!] iOS scrape failed for {custom_dev} {artist_id} {country}: {e}")

        # --- Android: developer store page, search only as a fallback ---
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
                    print(f"  [!] Android scrape failed for {custom_dev} {dev_name} {country}: {e}")
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
                            "name": game.get("title", UNKNOWN),
                            "icon": game.get("icon", ""),
                            "url": f"https://play.google.com/store/apps/details?id={app_id}",
                            "regions": set()
                        }
                    if app_id in scanned_android_games:
                        scanned_android_games[app_id]["regions"].add(country)

    print("\n🔍 Comparing this scan with the baseline...")
    expansions, region_updates = apply_region_sightings(known_games, region_sightings)
    dev_status = classify_developers(
        TARGET_DEVELOPERS, known_hits_by_dev, scanned_ios_games, scanned_android_games, is_first_run
    )

    # iOS
    for app_id, game_data in scanned_ios_games.items():
        c_dev = game_data.pop("custom_dev")
        game_data["regions"] = list(game_data["regions"])
        if dev_status[c_dev] == "new" or is_first_run:
            print(f"  [baseline] 🍎 {game_data['name']} (first pass for {c_dev}, saved quietly)")
        elif is_stale_release(game_data["release_date"]):
            print(f"  [baseline] 🍎 {game_data['name']} (released more than {NEW_GAME_MAX_AGE_DAYS} days ago, skipped)")
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

    # Android
    for app_id, base_data in scanned_android_games.items():
        c_dev = base_data.pop("custom_dev")
        base_data["regions"] = list(base_data["regions"])
        if dev_status[c_dev] == "new" or is_first_run:
            print(f"  [baseline] 🤖 {base_data['name']} (first pass for {c_dev}, saved quietly)")
        else:
            # Fetch details only for a new game from a developer we already know.
            if not play_guard.allow():
                print(f"  [!] Skipped Android details for {app_id} (Play circuit open; retry next run; not written to the baseline)")
                continue
            try:
                loaded = android_store_details(app_id, base_data["regions"], play_guard)
            except PlayStopped:
                print(f"  [!] Skipped Android details for {app_id} (Play circuit open; retry next run; not written to the baseline)")
                continue
            if loaded is None:
                print(f"  [!] Android details failed for {app_id}")
                size, iap_info, release_date, installs = UNKNOWN_SIZE, UNKNOWN, UNKNOWN_DATE, ""
                real_installs, genre, shots = 0, "", []
            else:
                details, iap_info = loaded
                base_data["name"] = details.get("title", base_data["name"])
                base_data["icon"] = details.get("icon", base_data.get("icon", ""))
                size = english_label(details.get("size", VARIES_WITH_DEVICE))
                release_date = english_label(details.get("released", UNKNOWN_DATE))
                installs = details.get("installs", "")
                real_installs = details.get("realInstalls", 0) or 0
                genre = details.get("genre", "")
                shots = (details.get("screenshots") or [])[:SHOTS_MAX]
            if is_stale_release(release_date):
                print(f"  [baseline] 🤖 {base_data['name']} (released more than {NEW_GAME_MAX_AGE_DAYS} days ago, skipped)")
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
    rating_fails = save_data(found_records, region_updates, play_guard) or 0
    ios_fail += rating_fails

    print("\n" + "=" * 60)
    if ios_fail or android_fail:
        print(f"⚠️ Scrape failures this run: iOS {ios_fail}, Android {android_fail} (see the log above)")
    if expansions:
        print(f"🌍 {len(expansions)} known game(s) entered a core market (us/gb/ca/au).")
        send_feishu_region_expansions(expansions)
    fresh_records = pushable_new_games(found_records)
    backfill_records = [r for r in found_records if r.get("backfill")]
    if is_first_run:
        print(f"✅ Baseline created. Recorded {len(known_games)} games across regions.")
    elif found_records:
        print(f"🚨 Wrote {len(found_records)} game(s) (just released {len(fresh_records)}, backfill {len(backfill_records)}).")
        send_feishu_new_games(fresh_records)
        if backfill_records and not fresh_records:
            print("📥 This run only has backfill, so no Feishu alert is sent.")
    else:
        print("💤 No new releases from the developers on the watchlist.")
    if scrape_is_incomplete(ios_fail, android_fail, play_guard):
        missed = [] if is_first_run else developers_missing_known_games(TARGET_DEVELOPERS, known_hits_by_dev)
        if missed:
            print("⚠️ Scrape incomplete. These developers had no known game this run: " + ", ".join(missed))
        else:
            print("⚠️ Scrape incomplete. No new-game alert does not mean there were no new games today.")
        send_feishu_incomplete(ios_fail, android_fail, play_guard.tripped, missed)
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
