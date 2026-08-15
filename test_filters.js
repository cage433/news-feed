// Runs the real <script> extracted from out/index.html against a minimal DOM
// stub, then simulates clicks to verify the section/source filter logic.
const html = await Deno.readTextFile(new URL("./out/index.html", import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

let failures = 0;
const check = (name, got, want) => {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) failures++;
  console.log(`${ok ? "pass" : "FAIL"}  ${name}${ok ? "" : `  got ${JSON.stringify(got)} want ${JSON.stringify(want)}`}`);
};

class El {
  constructor(tag, attrs = {}, classes = []) {
    this.tag = tag; this.dataset = attrs; this._classes = new Set(classes);
    this.hidden = false; this.textContent = ""; this.title = "";
    this._handlers = [];
    this.classList = {
      toggle: (c, on) => on ? this._classes.add(c) : this._classes.delete(c),
      contains: (c) => this._classes.has(c),
    };
  }
  get dateTime() { return this.dataset.datetime; }
  addEventListener(_type, fn) { this._handlers.push(fn); }
  click(shiftKey = false) {
    const ev = { target: this, shiftKey };
    // Bubble to the nav that owns this chip.
    if (sourcesNav._children.includes(this)) for (const fn of sourcesNav._handlers) fn(ev);
  }
  closest(sel) {
    if (sel === ".chip") return this._classes.has("chip") ? this : null;
    return null;
  }
}

// Build the DOM from the real generated markup.
const items = [...html.matchAll(/<li class="item" data-source="([^"]+)"/g)]
  .map(([, source]) => new El("li", { source }, ["item"]));

const sourceChips = [...html.matchAll(/<button class="chip src" data-source="([^"]+)"/g)]
  .map(([, source]) => new El("button", { source }, ["chip", "src"]));

const times = [...html.matchAll(/<time datetime="([^"]+)"(\s+data-undated="1")?/g)]
  .map(([, datetime, undated]) => new El("time", undated ? { datetime, undated: "1" } : { datetime }));

const clearBtn = new El("button", {}, ["chip", "link"]);
const shown = new El("span", {});
const sourcesNav = new El("nav", {}); sourcesNav._children = [...sourceChips, clearBtn];
const list = new El("ul", {});

const store = {};
// Deno ships a real localStorage; defineProperty to make sure ours wins.
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  writable: true,
  value: {
    getItem: (k) => store[k] ?? null,
    setItem: (k, v) => { store[k] = v; },
  },
});
globalThis.document = {
  getElementById: (id) => ({ list, clear: clearBtn, shown, sources: sourcesNav }[id]),
  querySelectorAll: (sel) => {
    if (sel === "time[datetime]") return times;
    if (sel === ".chip.src") return sourceChips;
    throw new Error("unstubbed selector: " + sel);
  },
};
list.querySelectorAll = () => items;

// Execute the real page script.
new Function(script)();

const visible = () => items.filter((i) => !i.hidden).length;
const bySource = (s) => items.filter((i) => i.dataset.source === s).length;

const TOTAL = items.length;
console.log(`loaded ${TOTAL} items, ${sourceChips.length} source chips\n`);

check("initial: everything visible", visible(), TOTAL);
check("initial: count element", Number(shown.textContent), TOTAL);
check("initial: 'all' button hidden", clearBtn.hidden, true);

// --- click focuses, clicking again reverts
const a = sourceChips[0];
a.click();
check("click shows only that source", visible(), bySource(a.dataset.source));
check("focused chip marked", a.classList.contains("is-focus"), true);
check("others dimmed, not struck through",
  sourceChips.slice(1).every((c) => c.classList.contains("is-dimmed")
    && !c.classList.contains("is-excluded")), true);
check("'all' button appears once filtered", clearBtn.hidden, false);
a.click();
check("clicking again removes focus", visible(), TOTAL);
check("focus class cleared", a.classList.contains("is-focus"), false);

// --- moving focus between sources
const b = sourceChips[1];
a.click(); b.click();
check("focusing another source replaces the first", visible(), bySource(b.dataset.source));
check("previous focus released", a.classList.contains("is-focus"), false);
b.click();
check("cleared back to everything", visible(), TOTAL);

// --- shift-click excludes, shift-clicking again reverts
a.click(true);
check("shift-click hides that source", visible(), TOTAL - bySource(a.dataset.source));
check("excluded chip marked", a.classList.contains("is-excluded"), true);
b.click(true);
check("exclusions accumulate", visible(), TOTAL - bySource(a.dataset.source) - bySource(b.dataset.source));
a.click(true);
check("shift-clicking again re-includes", visible(), TOTAL - bySource(b.dataset.source));
b.click(true);
check("all exclusions reverted", visible(), TOTAL);

// --- focus wins over exclusion, and can't strand an empty page
a.click(true);
a.click();
check("focusing an excluded source shows it", visible(), bySource(a.dataset.source));
a.click(true);
check("shift-click on the focused source drops focus and excludes it",
  visible(), TOTAL - bySource(a.dataset.source));
a.click(true);

// --- 'all' clears every filter
a.click(true);
b.click(true);
check("'all' visible while filtered", clearBtn.hidden, false);
clearBtn.click();
check("'all' clears every filter", visible(), TOTAL);
check("'all' hides itself again", clearBtn.hidden, true);

// --- persistence
a.click();
const saved = JSON.parse(store["newsfeed-filters"]);
check("focus persisted", saved.focus, a.dataset.source);
a.click(true);
check("exclusion persisted",
  JSON.parse(store["newsfeed-filters"]).excluded, [a.dataset.source]);
clearBtn.click();

// --- undated rendering
const undated = times.filter((t) => t.dataset.undated);
check("undated times labelled 'first seen'",
  undated.length > 0 && undated.every((t) => t.textContent.startsWith("first seen")), true);
check("dated times not labelled 'first seen'",
  times.filter((t) => !t.dataset.undated).every((t) => !t.textContent.startsWith("first seen")), true);

console.log(failures ? `\n${failures} FAILED` : "\nall checks passed");
Deno.exit(failures ? 1 : 0);
