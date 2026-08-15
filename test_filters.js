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
    for (const nav of [sectionsNav, sourcesNav]) {
      if (nav._children.includes(this)) for (const fn of nav._handlers) fn(ev);
    }
  }
  closest(sel) {
    if (sel === ".chip") return this._classes.has("chip") ? this : null;
    return null;
  }
}

// Build the DOM from the real generated markup.
const items = [...html.matchAll(/<li class="item" data-section="([^"]+)" data-source="([^"]+)"/g)]
  .map(([, section, source]) => new El("li", { section, source }, ["item"]));

const sectionChips = [...html.matchAll(/<button class="chip(?: is-active)?" data-filter="([^"]+)"/g)]
  .map(([, filter]) => new El("button", { filter }, ["chip"]));

const sourceChips = [...html.matchAll(/<button class="chip src is-active" data-source="([^"]+)" data-section="([^"]+)"/g)]
  .map(([, source, section]) => new El("button", { source, section }, ["chip", "src"]));

const times = [...html.matchAll(/<time datetime="([^"]+)"(\s+data-undated="1")?/g)]
  .map(([, datetime, undated]) => new El("time", undated ? { datetime, undated: "1" } : { datetime }));

const toggleAll = new El("button", {}, ["chip", "link"]);
const shown = new El("span", {});
const sectionsNav = new El("nav", {}); sectionsNav._children = sectionChips;
const sourcesNav = new El("nav", {}); sourcesNav._children = [...sourceChips, toggleAll];
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
  getElementById: (id) => ({ list, "toggle-all": toggleAll, shown, sections: sectionsNav, sources: sourcesNav }[id]),
  querySelectorAll: (sel) => {
    if (sel === "time[datetime]") return times;
    if (sel === "#sections .chip") return sectionChips;
    if (sel === ".chip.src") return sourceChips;
    throw new Error("unstubbed selector: " + sel);
  },
};
list.querySelectorAll = () => items;

// Execute the real page script.
new Function(script)();

const visible = () => items.filter((i) => !i.hidden).length;
const bySection = (s) => items.filter((i) => i.dataset.section === s).length;
const bySource = (s) => items.filter((i) => i.dataset.source === s).length;

const TOTAL = items.length;
console.log(`loaded ${TOTAL} items, ${sectionChips.length} section chips, ${sourceChips.length} source chips\n`);

check("initial: everything visible", visible(), TOTAL);
check("initial: count element", Number(shown.textContent), TOTAL);

// --- muting a single source
const noisiest = sourceChips[0];
noisiest.click();
check("mute one source hides exactly its items", visible(), TOTAL - bySource(noisiest.dataset.source));
check("muted chip loses is-active", noisiest.classList.contains("is-active"), false);
noisiest.click();
check("unmute restores", visible(), TOTAL);

// --- shift-click isolates
const target = sourceChips[3];
target.click(true);
check("shift-click isolates one source", visible(), bySource(target.dataset.source));
check("isolated chip stays active", target.classList.contains("is-active"), true);

// --- none / all
toggleAll.click();
check("'none' hides everything", visible(), 0);
toggleAll.click();
check("'all' restores everything", visible(), TOTAL);

// --- section filter combines with source mute
const sec = sectionChips.find((c) => c.dataset.filter !== "all");
sec.click();
const secName = sec.dataset.filter;
check("section filter narrows to that section", visible(), bySection(secName));
check("irrelevant source chips hidden",
  sourceChips.filter((c) => !c.hidden).every((c) => c.dataset.section === secName), true);

const inSection = sourceChips.find((c) => !c.hidden);
inSection.click();
check("mute inside a section subtracts only that source",
  visible(), bySection(secName) - bySource(inSection.dataset.source));

sectionChips.find((c) => c.dataset.filter === "all").click();
check("back to All keeps the source muted", visible(), TOTAL - bySource(inSection.dataset.source));

// --- persistence
const saved = JSON.parse(store["newsfeed-filters"]);
check("state persisted to localStorage", saved.muted.includes(inSection.dataset.source), true);
check("section persisted", saved.section, "all");

// --- undated rendering
const undated = times.filter((t) => t.dataset.undated);
check("undated times labelled 'first seen'",
  undated.length > 0 && undated.every((t) => t.textContent.startsWith("first seen")), true);
check("dated times not labelled 'first seen'",
  times.filter((t) => !t.dataset.undated).every((t) => !t.textContent.startsWith("first seen")), true);

console.log(failures ? `\n${failures} FAILED` : "\nall checks passed");
Deno.exit(failures ? 1 : 0);
