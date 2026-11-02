# New Game Radar — 交接文档（面向 Agent / 接手者）

> 竞品新游雷达：每天自动抓取指定开发者在 **App Store / Google Play** 的新游上线，
> 结果发布到一个静态网页看板。全流程跑在 GitHub Actions + GitHub Pages 上，**无需本地开机**。

---

## 0. 快速定位

| 项目 | 值 |
|---|---|
| 在线看板 | https://sven0920.github.io/competitor-tracker/ |
| 仓库（Public） | `Sven0920/competitor-tracker`（SSH push） |
| 本地工作副本 | `~/Desktop/UA_Work_Tools/03_Competitor_Charts/tracker/` |
| 定时 | 每天 `01:00 UTC`（北京 09:00），GitHub 定时常延迟 10–40 分钟属正常 |
| 当前规模 | 名单 62 行 / 52 家独立厂商；看板保留最近 120 天发现的新游 |

**改动流程**：在本地副本改 → `git push` → Actions 下次运行 / Pages 自动重新部署。

---

## 1. 架构与数据流

```
targets.csv（监控名单）
        │
        ▼
competitor_tracker.py  ── 每天由 Actions 运行
        │  iOS     : iTunes Lookup API（按开发者 artistId 拉全部 App）
        │  Android : google-play-scraper（按开发者名搜索 + developer 字段过滤）
        │  地区    : us / ph / au / ca / gb（主力） + tr / br / vn / id / mx（软启动）
        ▼
  与 competitor_list.json（基准库＝已知游戏）比对
        │
        ├─→ 新游 ──→ 写入 data.json（含 icon / 品类 / 截图 / 装机量 / 评分数）
        ├─→ 安卓装机量快照 ──→ installs_history.json（算 7 天增速）
        └─→ 更新基准库 competitor_list.json
        │
        ▼
Actions 自动 commit 三个数据文件 → Pages 重新部署 → index.html 读 data.json 渲染
```

**核心判定逻辑（务必理解，否则会误判 bug）**：
- 只有**不在基准库**里的 app_id 才算「新游」。
- **新加入名单的厂商会先「静默建库」**：首次扫到它时，把它的存量游戏全部写进基准库但**不报为新游**（否则一加厂商就刷屏几十条）。
  → 所以**新加的厂商当天不会出现在看板**，要等它之后真正上新。这是设计，不是 bug。
- 判定依据是 `dev_status`：某厂商只要有任意一款游戏已在基准库中，就视为「老熟人」，其新游才会上报。

---

## 2. 文件清单

| 文件 | 作用 | 谁维护 |
|---|---|---|
| `competitor_tracker.py` | 抓取主脚本 | 人工 |
| `index.html` | 网页看板（纯前端，读 data.json） | 人工 |
| `targets.csv` | 监控名单 | 人工 |
| `.github/workflows/tracker.yml` | 每日定时任务 | 人工 |
| `requirements.txt` | `requests` / `google-play-scraper` | 人工 |
| `data.json` | 最近 120 天发现的新游（看板数据源） | **脚本自动** |
| `competitor_list.json` | 基准库：所有已知游戏 app_id | **脚本自动** |
| `installs_history.json` | 安卓装机量逐日快照（算增速） | **脚本自动** |

> 三个「脚本自动」的文件不要手工编辑，会被下次运行覆盖或导致误判。

---

## 3. 数据结构

### targets.csv（3 列，无引号）
```
Developer,Android,iOS
显示名,Google Play 开发者名,iOS 开发者数字 ID
```
- **Developer**：看板上的分组显示名。**同一家公司的多个账号写相同显示名即可自动归并**
  （例：LIHUHU 有 3 行 / Mavericks 有 2 行）。
- **Android**：Google Play 的**开发者名字符串**，脚本用它搜索并用 `developer` 字段做**大写子串匹配**。
- **iOS**：**开发者的 artistId（数字）**，不是 App 的 id。

### data.json
```jsonc
{
  "updated_at": "2026-08-20 10:26",     // 北京时间
  "games": [{
    "app_id": "...",        // iOS=trackId / Android=包名
    "developer": "Voodoo",  // 对应 targets.csv 的显示名
    "platform": "iOS" | "Android",
    "name": "...", "icon": "...", "url": "...",
    "genre": "Puzzle",      // iOS 取 genres 里的具体子类（跳过 Games/Entertainment）
    "shots": ["..."],       // 最多 4 张商店截图；iOS 常为空（iTunes 接口不稳定给）
    "regions": ["us","tr"], // 在哪些地区扫到
    "size": "...", "iap_info": "...",
    "release_date": "2026-08-19" | "Aug 19, 2026",  // ⚠️ 两种格式并存，见坑 #5
    "ratings": 115,         // 仅 iOS：评分人数（苹果不公开下载量）
    "installs": "10,000+",  // 仅 Android：公开装机量区间
    "real_installs": 21157, // 仅 Android：更精确的估算整数
    "velocity": 4200,       // 仅 Android：最近 7 天装机增量
    "found_date": "2026-08-20"  // 被雷达发现的日期
  }]
}
```

---

## 4. 常见任务

### 加一个监控厂商
1. **先查准两个标识符**（不要猜）：
   ```bash
   # iOS：确认是「开发者」而非某个 App，拿 artistId
   curl -s "https://itunes.apple.com/lookup?id=<ID>&entity=software&country=us&limit=8" | python3 -m json.tool | head -40
   # 若只知道游戏名，用 search 反查 artistName / artistId
   curl -s "https://itunes.apple.com/search?term=<游戏名>&entity=software&country=us&limit=6"

   # Android：确认开发者名能搜到自家游戏
   python3 -c "
   from google_play_scraper import search
   DEV='<开发者名>'
   m=[h for h in search(DEV,lang='en',country='us',n_hits=40) if DEV.upper() in (h.get('developer','') or '').upper()]
   print(len(m), [h['title'] for h in m[:5]])"
   ```
2. 追加一行到 `targets.csv`（确保文件末尾有换行），`git push`。
3. 该厂商**下次运行先静默建库**，之后上新才会出现在看板。

### 改抓取地区 / 保留天数 / 增速窗口
改 `competitor_tracker.py` 顶部「配置区」：
`TARGET_COUNTRIES`、`KEEP_DAYS=120`、`SNAPSHOT_KEEP_DAYS=35`、`VELOCITY_WINDOW=7`、`SHOTS_MAX=4`。

### 手动立即跑一次
仓库 → Actions → **Daily Competitor Tracker** → Run workflow。

### 飞书推送（当前线上开启）
`.github/workflows/tracker.yml` 里已注入 GitHub Secret `FEISHU_WEBHOOK`，脚本 `send_feishu_new_games()` **只在发现新游时**推。
- 暂停：把 workflow 里的 `env:` / `FEISHU_WEBHOOK` 两行注释掉即可。Secret 不用删。
- ⚠️ **仓库是 Public，webhook 绝不能写进代码**，必须走 Secret。

---

## 5. 已知坑（踩过的，别重复踩）

1. **不能做成纯前端工具**：Google Play 无 CORS，浏览器抓不了；必须服务端（Actions）跑。
2. **Google Play 榜单接口已失效**：`google_play_scraper` 的 `list()` 已被 Google 停用，
   想「发现新厂商」只能扫 **App Store 榜单**（`https://itunes.apple.com/us/rss/topfreeapplications/limit=100/genre=<id>/json`），
   跨平台厂商基本都能覆盖到，再单独解析其 Android 开发者名。
3. **iOS 的「开发者 ID」≠「App ID」**：商店链接里 `developer/id123` 才是要填的；
   `/app/xxx/id456` 是 App。填错会完全匹配不到。
4. **同一家公司在两店常用不同名**：
   如 Pleasure City(GP) = Orange One Limited(iOS)、TREEPLLA(GP) = Neptune Company(iOS)、
   Grand Games A.Ş. 有土耳其后缀。**必须交叉验证**（拿旗舰游戏在另一店查开发者字段）。
   另注意重名干扰（"Grand Games AV"、健身 App "Action Fit srl" 都不是目标）。
5. **`release_date` 两种格式并存**（iOS `2026-08-19` / Android `Aug 19, 2026`）：
   直接 `Date.parse` 会导致 **ISO 按 UTC、美式按本地时区**，同一天差 8 小时 → 同日安卓永远排在 iOS 后。
   `index.html` 里已用 `parseDay()` 统一归一到 **UTC 日**。改排序时别退回 `Date.parse`。
6. **排序比较器绝不能返回 NaN**：曾用 `(x ?? -Infinity) - (y ?? -Infinity)`，两个空值相减得 `NaN`，
   会让 `Array.sort` 整体乱序。现用 `byDate(dir)` 显式判空，无日期一律排末尾。
7. **未来日期不是脏数据**：App Store 预约（pre-order）会返回未来上架日（如 2027-02-01），
   看板已用 `🔜 预售` 标签标出，排序时它们合理地在最前。
8. **GitHub Pages 可能「静默不部署」**：出现过 Actions 每天正常 commit、但线上 data.json 卡在两天前。
   排查：对比 `git show origin/main:data.json` 的 `updated_at` 与线上
   `curl -s https://sven0920.github.io/competitor-tracker/data.json | head -c 200`。
   应急：推一个空提交 `git commit --allow-empty` 触发重新部署。
   根治（尚未做）：改用 GitHub 官方 Pages 部署 Action 显式发布。
9. **别用匿名 GitHub API 排查**：60 次/小时的额度极易耗尽，返回值会变成误导性的空结果
   （曾显示「0 个 workflow」）。优先用 `git log origin/main` 判断 Actions 是否在跑。
10. **Actions 需要写权限**：仓库 Settings → Actions → Workflow permissions 必须是
    **Read and write**，否则抓到数据也 commit 不回来。
11. **Google Play 云端限流**：Actions 的 IP 偶尔被限，个别厂商某天可能漏抓；iTunes 侧稳定。

---

## 6. 自检命令

```bash
cd ~/Desktop/UA_Work_Tools/03_Competitor_Charts/tracker

# 线上 vs 仓库 数据是否一致（查 Pages 是否卡住）
curl -s "https://sven0920.github.io/competitor-tracker/data.json?_=$(date +%s)" \
  | python3 -c "import sys,json;print('线上:',json.load(sys.stdin)['updated_at'])"
git show origin/main:data.json | python3 -c "import sys,json;print('仓库:',json.load(sys.stdin)['updated_at'])"

# Actions 是否在每天自动提交
git log origin/main --author=github-actions -5 --pretty='%ci %s'

# 名单规模 / 某厂商是否已在监控
python3 -c "
import csv;r=[x for x in list(csv.reader(open('targets.csv',encoding='utf-8-sig')))[1:] if x and x[0].strip()]
print('行数',len(r),'厂商',len({x[0].strip() for x in r}))"
grep -in "<关键词>" targets.csv
```
