# 📡 New Game Radar（竞品新游雷达）

每天自动抓取关注的开发者在 **App Store** 和 **Google Play** 有没有新游上线，结果展示在网页看板上，打开即看。

## 在线看板
👉 https://sven0920.github.io/competitor-tracker/

## 工作原理
- **GitHub Actions** 每天北京时间约 10:17 自动运行 `competitor_tracker.py`（避开整点，降低 GitHub 漏跑概率；每日仅一次，避免重复飞书推送）
- iOS 走 iTunes API（`sort=recent&limit=200`），Android 抓开发者自己的商店主页（翻页补全），打不开才退回搜索，和基准库 `competitor_list.json` 比对找出新游。Play 连续失败 8 次会停掉后面的请求，避免把后半段厂商扫空
- 上架超过 180 天的旧作不会当新游推送，只补进基准库
- 飞书只推上架不超过 30 天的（含预售）。更老的、以及解析不出上架日期的，写入看板的「补录」分组，不推送
- 已经在基准库里的游戏会继续合并商店地区（只增不减）。软启动新出现 us / gb / ca / au 时，再单独推一条飞书「地区扩大」，不当作新游
- 新发现写入 `data.json` 并自动 commit，`index.html` 读取后展示（保留最近 120 天）
- 看板里的安卓 7 天增速和内购价、iOS 评分数每天刷新。安卓内购固定向美国商店要价；没有美国上架时标成「非美国商店价」，不和美元混在一起。iOS 内购不从 features 猜测。安卓卡片不再显示包体（几乎都是「因设备而异」）。发现新游时推飞书（需配置 `FEISHU_WEBHOOK`）

## 改监控名单
编辑 `targets.csv`，三列：`Developer,Android,iOS`
- Developer：自定义厂商名（用于分组展示）
- Android：Google Play 开发者名（用于打开该开发者的商店主页；主页抓不到时才按这个名字搜索）
- iOS：App Store 的 artist/开发者 ID

改完 push 即可，下次自动运行生效。也可在仓库 **Actions → Daily Competitor Tracker → Run workflow** 手动立即跑一次。

## 文件
- `index.html` — 网页看板（GitHub Pages）
- `competitor_tracker.py` — 抓取脚本
- `targets.csv` — 监控名单
- `competitor_list.json` — 已知游戏基准库（自动维护）
- `data.json` — 最近发现的新游（自动生成）
- `installs_history.json` — 安卓装机量逐日快照，用于算 7 天增速（自动生成）
- `.github/workflows/tracker.yml` — 每日定时任务

## 看板功能
图标 · 品类 / 厂商 / 游戏名搜索 · 软启动、起量、预售、上架距今天数筛选 · 同名 iOS + Android 并成一张卡 · 安卓装机量与 7 天增速（🔥 起量快只标当天增速前 10，不再用固定 1 万）· iOS 评分数 · 商店截图预览 · 刚上架和补录分组。监控地区含 us/ph/au/ca/gb 主力市场 + tr/br/vn/id/mx 软启动测试市场。

> 说明：Google Play 在云端 IP 上偶尔会限流，个别厂商某天可能漏抓；iTunes 侧稳定。
