# 📡 竞品新游雷达

每天自动抓取关注的开发者在 **App Store** 和 **Google Play** 有没有新游上线，结果展示在网页看板上，打开即看。

## 在线看板
👉 https://sven0920.github.io/competitor-tracker/

## 工作原理
- **GitHub Actions** 每天北京时间约 09:00 自动运行 `competitor_tracker.py`
- iOS 走 iTunes API，Android 走 google-play-scraper，和基准库 `competitor_list.json` 比对找出新游
- 新发现写入 `data.json` 并自动 commit，`index.html` 读取后展示（保留最近 120 天）

## 改监控名单
编辑 `targets.csv`，三列：`Developer,Android,iOS`
- Developer：自定义厂商名（用于分组展示）
- Android：Google Play 开发者名（用于搜索匹配）
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
图标 · 品类标签与筛选 · 安卓装机量与 7 天增速（🔥 起量快）· iOS 评分数 · 商店截图预览 · 多条件排序（最近发现/增速/装机量/上架时间/厂商/游戏名）。监控地区含 us/ph/au/ca/gb 主力市场 + tr/br/vn/id/mx 软启动测试市场。

> 说明：Google Play 在云端 IP 上偶尔会限流，个别厂商某天可能漏抓；iTunes 侧稳定。
