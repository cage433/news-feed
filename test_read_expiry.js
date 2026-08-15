// Seed read-state containing one id that is on the page and one that is not,
// then load the page script and confirm only the stale one is dropped.
const html = await Deno.readTextFile(new URL("./out/index.html", import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ids = [...html.matchAll(/data-id="([^"]+)"/g)].map((m) => m[1]);
const live = ids[0], stale = "https://example.com/gone-from-the-feed";

const DAY = 86400000;
const store = { "newsfeed-read": JSON.stringify({
  [live]: Date.now() - 5 * DAY,          // recent: keep
  [stale]: Date.now() - 5 * DAY,         // absent from page but recent: keep
  "https://example.com/ancient": Date.now() - 300 * DAY,  // expired: drop
}) };
Object.defineProperty(globalThis, "localStorage", { configurable: true, writable: true,
  value: { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } } });

const mk = (cls, ds = {}) => ({ dataset: ds, hidden: false, textContent: "", title: "",
  _h: [], _c: new Set(cls), classList: { toggle: (c, on) => on ? undefined : undefined,
  contains: () => false }, addEventListener(_t, f) { this._h.push(f); },
  querySelector: () => ({ title: "" }), closest: () => null });

const items = ids.map((id) => mk(["item"], { source: "x", id }));
const stub = mk([]);
globalThis.document = {
  getElementById: () => stub,
  querySelectorAll: (s) => (s === "time[datetime]" ? [] : []),
};
document.getElementById = (id) => (id === "list"
  ? Object.assign(stub, { querySelectorAll: () => items }) : mk([]));
new Function(script)();

const after = Object.keys(JSON.parse(store["newsfeed-read"]));
const ok = after.includes(live) && after.includes(stale)
        && !after.includes("https://example.com/ancient");
console.log("on page, recent   ->", after.includes(live) ? "kept" : "DROPPED");
console.log("off page, recent  ->", after.includes(stale) ? "kept" : "DROPPED");
console.log("off page, 300d old->", after.includes("https://example.com/ancient") ? "KEPT" : "dropped");
console.log(ok ? "PASS  expiry by age, not by page membership"
              : "FAIL  pruning did not behave as expected");
Deno.exit(ok ? 0 : 1);
