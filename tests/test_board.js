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

const { mergeGames, hotAppIds, cardMatches } = sandbox;
const HOT_RANK = 10;
const base = { plat: "all", dev: "all", genre: "all", signal: "all", age: "all", q: "", sort: "found_desc" };

const games = [
  { app_id: "ios1", platform: "iOS", developer: "Homa", name: "All in Hole", found_date: "2026-10-01", regions: ["us"], release_date: "2026-10-01", genre: "Puzzle", ratings: 10, url: "https://ios", backfill: false },
  { app_id: "and1", platform: "Android", developer: "Homa", name: "All  in Hole", found_date: "2026-10-02", regions: ["tr"], release_date: "2026-10-02", genre: "Puzzle", installs: "1,000+", velocity: 100, url: "https://and", backfill: false },
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
assert.ok(!cardHtml.includes("因设备而异"));

console.log("board logic ok", cards.length, "cards");
