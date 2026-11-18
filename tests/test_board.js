const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const html = fs.readFileSync("index.html", "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
const el = () => ({
  textContent: "",
  innerHTML: "",
  value: "all",
  dataset: {},
  children: [],
  classList: { remove() {}, add() {}, toggle() {} },
  addEventListener() {},
  appendChild() {},
});
const sandbox = {
  document: { getElementById: el },
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ games: [] }) }),
  Date,
  Set,
  Map,
  console,
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

const { mergeGames, hotAppIds, cardMatches, applySupergenres, gameCard, loadTitleAliases } = sandbox;
const HOT_RANK = 10;
const base = { plat: "all", dev: "all", genre: "all", signal: "all", age: "all", q: "", sort: "found_desc" };

const games = [
  { app_id: "ios1", platform: "iOS", developer: "Homa", name: "All in Hole", found_date: "2026-10-01", regions: ["us"], release_date: "2026-10-01", genre: "Puzzle", ratings: 10, url: "https://ios", backfill: false },
  { app_id: "and1", platform: "Android", developer: "Homa", name: "All  in Hole", found_date: "2026-10-02", regions: ["tr"], release_date: "2026-10-02", genre: "Puzzle", installs: "1,000+", velocity: 100, url: "https://and", size: "Varies with device", backfill: false },
  { app_id: "other", platform: "Android", developer: "Peak", name: "All in Hole", found_date: "2026-10-03", regions: ["us"], release_date: "2026-09-01", velocity: 50, backfill: true },
  { app_id: "pre", platform: "iOS", developer: "Peak", name: "Soon", found_date: "2026-10-09", regions: ["gb"], release_date: "2026-12-01", backfill: false },
];
for (let i = 0; i < 12; i++) {
  games.push({
    app_id: "hot" + i,
    platform: "Android",
    developer: "Voodoo",
    name: "Game " + i,
    found_date: "2026-10-08",
    regions: ["us"],
    release_date: "2026-10-08",
    velocity: 1000 + i * 1000,
    backfill: false,
  });
}

sandbox.HOT_IDS = hotAppIds(games);
const cards = mergeGames(games);
const merged = cards.find(c => c.developer === "Homa" && c.name.startsWith("All"));
assert.strictEqual(merged.sides.length, 2, "same developer and normalized name merge");
assert.ok(cards.filter(c => c.name === "All in Hole" || c.name.startsWith("All")).length >= 2, "different developers stay apart");
assert.strictEqual(merged.soft, true, "android side without us keeps the soft-launch flag");
assert.strictEqual(cards.find(c => c.sides.some(s => s.app_id === "other")).soft, false);

const hotCards = cards.filter(c => c.hot);
assert.strictEqual(hotCards.length, HOT_RANK);
assert.ok(hotCards.every(c => c.velocity >= 3000), "top 10 are the fastest, not everyone over 10000");
assert.ok(!cards.some(c => c.sides.some(s => s.app_id === "and1") && c.hot));

const now = Date.UTC(2026, 9, 10);
const soft = cards.filter(c => cardMatches(c, { ...base, signal: "soft" }, now));
assert.ok(soft.some(c => c.developer === "Homa"));
assert.ok(!soft.some(c => c.sides.some(s => s.app_id === "other")));

const pre = cards.filter(c => cardMatches(c, { ...base, signal: "pre" }, now));
assert.strictEqual(pre.length, 1);
assert.strictEqual(pre[0].name, "Soon");

const week = cards.filter(c => cardMatches(c, { ...base, age: "7" }, now));
assert.ok(week.some(c => c.name === "Soon"), "preorder counts as within the window");
assert.ok(!week.some(c => c.sides.some(s => s.app_id === "other")));

const byDev = cards.filter(c => cardMatches(c, { ...base, q: "peak" }, now));
assert.ok(byDev.length >= 2 && byDev.every(c => c.developer === "Peak"));

const cardHtml = sandbox.gameCard(merged);
const sizeMarks = cardHtml.split("📦").length - 1;
assert.strictEqual(sizeMarks, 1, "only the iOS side keeps a package-size line");
assert.ok(cardHtml.includes("📥"));
assert.ok(!cardHtml.includes("Varies with device"));
assert.ok(!cardHtml.includes("Live in US"), "a US listing does not repeat a live badge");
assert.strictEqual((cardHtml.match(/Soft launch/g) || []).length, 1, "only the non-US side is marked soft launch");

const bothUs = gameCard({
  name: "Both Live",
  developer: "Homa",
  sides: [
    { platform: "iOS", regions: ["us", "gb"], url: "https://ios", ratings: 1, release_date: "2026-10-01" },
    { platform: "Android", regions: ["us"], url: "https://and", installs: "1,000+", velocity: 0, release_date: "2026-10-01" },
  ],
  genres: [],
  supergenres: [],
  hot: false,
  preorder: false,
  backfill: false,
  found_date: "2026-10-01",
});
assert.ok(bothUs.includes("🍎 iOS") && bothUs.includes("🤖 Android"));
assert.ok(!bothUs.includes("Live in US"));
assert.ok(!bothUs.includes("Soft launch"));

const catalog = {
  genres: [{
    name: "3D Arrow",
    titles: ["Cube Jam 3D!", "Amaze Clear 3D: Arrow Cube", "Amaze Clear 3D"],
    app_ids: ["com.oakever.amaze3d"],
  }],
};
const tagged = applySupergenres([
  { app_id: "1", platform: "iOS", developer: "Grand Games", name: "Cube Jam 3D!", genre: "Casual" },
  { app_id: "com.oakever.amaze3d", platform: "Android", developer: "Learnings", name: "Amaze Clear 3D: Arrow Cube", genre: "Puzzle" },
  { app_id: "2", platform: "iOS", developer: "Learnings", name: "Amaze Clear 3D", genre: "Casual" },
  { app_id: "3", platform: "Android", developer: "Other", name: "Arrow Jam!", genre: "Puzzle" },
], catalog);
assert.strictEqual(JSON.stringify(tagged.map(g => g.supergenres)), JSON.stringify([["3D Arrow"], ["3D Arrow"], ["3D Arrow"], []]));
const arrowCards = mergeGames(tagged);
const cube = arrowCards.find(c => c.name.startsWith("Cube Jam"));
assert.ok(gameCard(cube).includes("3D Arrow"));
assert.strictEqual(arrowCards.filter(c => cardMatches(c, { ...base, super: "3D Arrow" }, now)).length, 3);
assert.ok(!arrowCards.some(c => c.name === "Arrow Jam!" && cardMatches(c, { ...base, super: "3D Arrow" }, now)));

loadTitleAliases({
  aliases: [{
    developer: "Learnings",
    name: "Amaze Clear 3D: Arrow Cube",
    titles: ["Amaze Clear 3D", "Amaze Clear 3D: Arrow Cube"],
  }],
});
const mergedAmaze = mergeGames(tagged).filter(c => c.developer === "Learnings");
assert.strictEqual(mergedAmaze.length, 1, "iOS and Android Amaze Clear titles share one card");
assert.strictEqual(mergedAmaze[0].sides.length, 2);
assert.strictEqual(mergedAmaze[0].name, "Amaze Clear 3D: Arrow Cube");
const amazeHtml = gameCard(mergedAmaze[0]);
assert.ok(amazeHtml.includes("App Store") && amazeHtml.includes("Google Play"));
assert.ok(cardMatches(mergedAmaze[0], { ...base, q: "amaze clear 3d" }, now));
loadTitleAliases({ aliases: [] });

assert.strictEqual(sandbox.formatRelease("Nov 16, 2026"), "2026-11-16");
assert.strictEqual(sandbox.formatRelease("2026-11-16"), "2026-11-16");
assert.strictEqual(sandbox.formatRelease("Unknown date"), "Unknown date");
assert.strictEqual(sandbox.iapLabel({ platform: "iOS", iap_info: "Unknown" }), "Not listed by the App Store");
assert.strictEqual(sandbox.iapLabel({ platform: "Android", iap_info: "Non-US store price · $1.99" }), "Non-US store price · $1.99");
assert.ok(sandbox.runStatusText(null).includes("next daily update"));
const incomplete = sandbox.runStatusText({
  finished_at: "2026-11-18 10:17",
  complete: false,
  ios_failures: 8,
  android_failures: 1,
  play_circuit_open: true,
  missed_developers: ["Peak"],
  baselined_titles: 0,
});
assert.ok(incomplete.includes("Run incomplete"));
assert.ok(incomplete.includes("Peak"));
assert.ok(incomplete.includes("held until a complete run"));
assert.ok(sandbox.runStatusText({ finished_at: "2026-11-18 10:17", complete: true, baselined_titles: 4 }).includes("Quietly filed 4"));

sandbox.GAMES = games;
sandbox.filter = { ...base, dev: "Peak" };
const csv = sandbox.csvForCards(sandbox.visibleCards());
assert.ok(csv.startsWith("Developer,Game,Platform,Regions,Release date,Install range,7-day installs,IAP,Link"));
assert.ok(csv.includes("Peak"));
assert.ok(!csv.includes("Homa"));
const peakCard = sandbox.gameCard(cards.find(c => c.developer === "Peak" && c.sides.some(s => s.app_id === "other")));
assert.ok(peakCard.includes("Play range"));

console.log("board logic ok", cards.length, "cards");
