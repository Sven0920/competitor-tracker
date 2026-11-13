# 📡 New Game Radar

Every day this checks whether watched developers shipped a new game on the **App Store** or **Google Play**, then shows the result on a web board.

## Live board
👉 https://sven0920.github.io/competitor-tracker/

## How it works
- **GitHub Actions** runs `competitor_tracker.py` once a day at about 10:17 China time (02:17 UTC, off the hour so GitHub is less likely to skip it). Feishu alerts are off. The board still updates.
- iOS uses the iTunes API (`sort=recent&limit=200`). Android opens each developer's own store page and follows pagination, and falls back to search only when that page cannot be opened. Results are compared with the baseline in `competitor_list.json`. After 8 Play failures in a row, later Play requests stop so the rest of the list is not wiped.
- A title released more than 180 days ago is added to the baseline and is not treated as a new game.
- A title released within 30 days, including pre-orders, is **Just released**. Older titles, and titles with no parseable release date, go in the **Backfill** group. Neither group is sent to Feishu.
- Games already in the baseline keep gaining store regions. Regions are only added. A soft launch that newly appears in us / gb / ca / au is a **region expansion**, not a new game, and is not sent to Feishu.
- New finds are written to `data.json` and committed. `index.html` reads that file and keeps the last 120 days.
- The board refreshes Android 7-day install growth, Android IAP price, and iOS rating counts every day. Android IAP is priced from the US store. If there is no US listing, the price is marked **Non-US store price** so it is not mixed with US dollars. iOS IAP is not guessed from features. Android cards omit package size, which is almost always "varies by device". New games, region expansions, and incomplete scrapes are not sent to Feishu. `FEISHU_WEBHOOK` stays in the repository secrets and is not read by the daily job.

## Change the watchlist
Edit `targets.csv`. Three columns: `Developer,Android,iOS`
- Developer: the name used to group cards
- Android: the Google Play developer name, used to open that developer's store page. Search uses this name only when the page cannot be scraped
- iOS: the App Store artist / developer ID

Push the change. It takes effect on the next run. You can also run it now from **Actions → Daily Competitor Tracker → Run workflow**.

## Files
- `index.html` — web board (GitHub Pages)
- `competitor_tracker.py` — scraper
- `targets.csv` — watchlist
- `competitor_list.json` — baseline of known games (maintained automatically)
- `data.json` — recently found games (generated automatically)
- `installs_history.json` — daily Android install snapshots used for 7-day growth (generated automatically)
- `.github/workflows/tracker.yml` — daily scheduled job

## Board
Icons, search by genre / developer / game name, and filters for soft launch, rising fast, pre-order, and days since release. Matching iOS and Android titles from the same developer collapse into one card. Android installs and 7-day growth are shown. **Rising fast** marks only that day's top 10 by 7-day growth, not a fixed 10,000-install cutoff. iOS rating counts and store screenshots are included, with Just released and Backfill groups. Watched regions are the core markets us/ph/au/ca/gb plus the soft-launch test markets tr/br/vn/id/mx.

> Google Play sometimes rate-limits cloud IPs, so one developer can be missed on a given day. iTunes is steadier. When the circuit opens or failures hit 8, the log records "Scrape incomplete" and names developers whose known games were not seen. That notice is not sent to Feishu. The script still exits successfully so a partial run does not throw away data already collected.
