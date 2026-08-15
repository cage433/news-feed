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
  fire(ev = {}) { for (const fn of this._handlers) fn({ target: this, ...ev }); }
  closest(sel) {
    const cls = sel.slice(1);
    if (this._classes.has(cls)) return this;
    // A .mark or .headline resolves upward to the .item that owns it.
    if (cls === "item" && this._owner) return this._owner;
    return null;
  }
}

// Build the DOM from the real generated markup.
const items = [...html.matchAll(/<li class="item" data-source="([^"]+)" data-id="([^"]+)"/g)]
  .map(([, source, id]) => {
    const li = new El("li", { source, id }, ["item"]);
    li._mark = new El("button", {}, ["mark"]);
    li._headline = new El("a", {}, ["headline"]);
    li._mark._owner = li; li._headline._owner = li;
    li.querySelector = (sel) => (sel === ".mark" ? li._mark : null);
    return li;
  });

const sourceChips = [...html.matchAll(/<button class="chip src" data-source="([^"]+)"/g)]
  .map(([, source]) => new El("button", { source }, ["chip", "src"]));

const times = [...html.matchAll(/<time datetime="([^"]+)"(\s+data-undated="1")?/g)]
  .map(([, datetime, undated]) => new El("time", undated ? { datetime, undated: "1" } : { datetime }));

const clearBtn = new El("button", {}, ["chip", "link"]);
const unreadBtn = new El("button", {}, ["chip"]);
const markAllBtn = new El("button", {}, ["chip", "link"]);
const shown = new El("span", {});
const unreadCount = new El("span", {});
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
  getElementById: (id) => ({
    list, shown, sources: sourcesNav, clear: clearBtn,
    unread: unreadCount, "unread-only": unreadBtn, "mark-all": markAllBtn,
  }[id]),
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

// ---------- read / unread ----------
const unread = () => Number(unreadCount.textContent);
const fire = (el) => { for (const fn of list._handlers) fn({ target: el }); };

check("everything starts unread", unread(), TOTAL);
check("nothing marked read", items.some((i) => i.classList.contains("is-read")), false);

// opening a story marks it read, but leaves it on the page
const story = items[0];
fire(story._headline);
check("opening marks read", story.classList.contains("is-read"), true);
check("unread count drops", unread(), TOTAL - 1);
check("read story still visible", story.hidden, false);
check("shown count unchanged", Number(shown.textContent), TOTAL);

// the tick toggles both ways without opening
const other = items[1];
fire(other._mark);
check("tick marks read", other.classList.contains("is-read"), true);
check("tick updates its own title", other._mark.title, "Mark as unread");
fire(other._mark);
check("tick marks unread again", other.classList.contains("is-read"), false);
check("unread count restored", unread(), TOTAL - 1);

// unread-only hides read stories
unreadBtn.fire();
check("unread only hides read stories", visible(), TOTAL - 1);
check("unread toggle marked active", unreadBtn.classList.contains("is-active"), true);
check("unread count ignores the toggle", unread(), TOTAL - 1);
unreadBtn.fire();
check("turning it off restores", visible(), TOTAL);

// read state is independent of the source filter
a.click();
check("focus still works with read state", visible(), bySource(a.dataset.source));
markAllBtn.fire();
check("mark all read only affects visible", unread(), 0);
a.click();
check("stories outside the focus stayed unread",
  unread(), TOTAL - bySource(a.dataset.source) - 1);

// persistence and pruning
const readIds = Object.keys(JSON.parse(store["newsfeed-read"]));
check("read state persisted separately", readIds.includes(story.dataset.id), true);
check("read state survives clearing filters",
  (clearBtn.click(), story.classList.contains("is-read")), true);
check("unread-only persisted in filters",
  "unreadOnly" in JSON.parse(store["newsfeed-filters"]), true);
check("read ids are canonical links", readIds.every((id) => id.startsWith("http")), true);

// --- undated rendering
const undated = times.filter((t) => t.dataset.undated);
check("undated times labelled 'first seen'",
  undated.length > 0 && undated.every((t) => t.textContent.startsWith("first seen")), true);
check("dated times not labelled 'first seen'",
  times.filter((t) => !t.dataset.undated).every((t) => !t.textContent.startsWith("first seen")), true);

console.log(failures ? `\n${failures} FAILED` : "\nall checks passed");
Deno.exit(failures ? 1 : 0);
