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
    this._handlers = {};   // keyed by event type: the page listens for several
    this.classList = {
      toggle: (c, on) => on ? this._classes.add(c) : this._classes.delete(c),
      contains: (c) => this._classes.has(c),
    };
  }
  get dateTime() { return this.dataset.datetime; }
  addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
  click(shiftKey = false) {
    // Bubble to the nav that owns this chip.
    if (sourcesNav._children.includes(this)) sourcesNav.dispatch("click", { target: this, shiftKey });
  }
  dispatch(type, ev = {}) {
    for (const fn of this._handlers[type] || []) fn({ target: this, ...ev });
  }
  fire(ev = {}) { this.dispatch("click", ev); }
  closest(sel) {
    // Handles compound selectors like ".chip.src", which the page uses.
    const classes = sel.split(".").filter(Boolean);
    if (classes.every((c) => this._classes.has(c))) return this;
    // A .mark or .headline resolves upward to the .item that owns it.
    if (sel === ".item" && this._owner) return this._owner;
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

// One story already read in an earlier session. It must behave differently
// from one marked read during this session: only the latter stays on screen
// under "unread only".
const PRESEEDED = items[5];
store["newsfeed-read"] = JSON.stringify({ [PRESEEDED.dataset.id]: Date.now() });

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
const fire = (el) => list.dispatch("click", { target: el });

check("preseeded story loads as read", PRESEEDED.classList.contains("is-read"), true);
check("its box loads already ticked", PRESEEDED._mark.textContent, "✓");
check("unread boxes start empty", items[0]._mark.textContent, "");
check("unread count excludes it", unread(), TOTAL - 1);
check("read story still shown when not filtering", PRESEEDED.hidden, false);

// opening a story marks it read, but leaves it on the page
const story = items[0];
fire(story._headline);
check("opening marks read", story.classList.contains("is-read"), true);
check("unread count drops", unread(), TOTAL - 2);
check("read story still visible", story.hidden, false);
check("shown count unchanged", Number(shown.textContent), TOTAL);

// the tick toggles both ways without opening
const other = items[1];
fire(other._mark);
check("tick marks read", other.classList.contains("is-read"), true);
check("tick updates its own title", other._mark.title, "Mark as unread");
check("box shows a tick when read", other._mark.textContent, "✓");
fire(other._mark);
check("tick marks unread again", other.classList.contains("is-read"), false);
check("box is empty when unread", other._mark.textContent, "");
check("unread count restored", unread(), TOTAL - 2);

// unread-only hides stories read earlier, but keeps this session's in place
unreadBtn.fire();
check("unread toggle marked active", unreadBtn.classList.contains("is-active"), true);
check("story read this session stays visible", story.hidden, false);
check("story read earlier is hidden", PRESEEDED.hidden, true);
check("only the earlier read story is hidden", visible(), TOTAL - 1);
check("unread count ignores the toggle", unread(), TOTAL - 2);

// un-marking something read this session keeps it visible too
fire(story._mark);
check("un-marking restores unread count", unread(), TOTAL - 1);
check("still visible after un-marking", story.hidden, false);
fire(story._mark);

// mark all read empties the view, unlike a single mark
markAllBtn.fire();
check("mark all read clears the visible list", visible(), 0);
check("mark all read zeroes the unread count", unread(), 0);
check("mark-all button hides when nothing is unread", markAllBtn.hidden, true);
unreadBtn.fire();
check("everything reappears with the toggle off", visible(), TOTAL);

// read state is independent of the source filter
for (const i of items) fire(i._mark);              // un-read everything
check("reset to all unread", unread(), TOTAL);
a.click();
check("focus still works with read state", visible(), bySource(a.dataset.source));
markAllBtn.fire();
check("mark all read only affects visible", unread(), 0);
a.click();
check("stories outside the focus stayed unread",
  unread(), TOTAL - bySource(a.dataset.source));

// persistence and pruning. The reset loop above left `story` unread, so mark
// something that is definitely read: one of the focused source's own items.
const inFocus = items.find((i) => i.dataset.source === a.dataset.source);
const readIds = Object.keys(JSON.parse(store["newsfeed-read"]));
check("read state persisted separately", readIds.includes(inFocus.dataset.id), true);
check("read state survives clearing filters",
  (clearBtn.click(), inFocus.classList.contains("is-read")), true);
check("unread-only persisted in filters",
  "unreadOnly" in JSON.parse(store["newsfeed-filters"]), true);
check("read ids are canonical links", readIds.every((id) => id.startsWith("http")), true);

// ---------- long press (the mobile equivalent of shift-click) ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const HOLD = 600;   // comfortably past the page's 450ms threshold

// A touch held on a chip excludes that source, without any shift key.
// The jitter matters: a real finger never holds perfectly still, and
// cancelling on any movement at all is what broke this on a phone.
clearBtn.click();
sourcesNav.dispatch("pointerdown", { target: a, pointerType: "touch", clientX: 100, clientY: 200 });
for (const [dx, dy] of [[1, 0], [2, 1], [1, 2], [3, 1]]) {
  await sleep(40);
  sourcesNav.dispatch("pointermove",
    { target: a, pointerType: "touch", clientX: 100 + dx, clientY: 200 + dy });
}
check("a jittering finger keeps the press alive", a.classList.contains("is-pressing"), true);
await sleep(HOLD);
check("long press excludes", visible(), TOTAL - bySource(a.dataset.source));
check("the pressing state is cleared once it fires", a.classList.contains("is-pressing"), false);
check("long-pressed chip marked excluded", a.classList.contains("is-excluded"), true);

// The browser fires a click after the press; it must not undo the exclusion.
a.click();
check("the click after a long press is swallowed",
  visible(), TOTAL - bySource(a.dataset.source));

// Holding it again re-includes, matching shift-click's behaviour.
sourcesNav.dispatch("pointerdown", { target: a, pointerType: "touch" });
await sleep(HOLD);
check("long pressing again re-includes", visible(), TOTAL);
a.click();

// A short tap still focuses.
clearBtn.click();
sourcesNav.dispatch("pointerdown", { target: b, pointerType: "touch" });
await sleep(80);
sourcesNav.dispatch("pointerup", { target: b, pointerType: "touch" });
await sleep(HOLD);
check("a short tap does not exclude", b.classList.contains("is-excluded"), false);
b.click();
check("a short tap focuses", visible(), bySource(b.dataset.source));
b.click();

// Scrolling away mid-press must cancel it, not silently exclude.
sourcesNav.dispatch("pointerdown", { target: a, pointerType: "touch" });
await sleep(80);
sourcesNav.dispatch("pointercancel", { target: a, pointerType: "touch" });
await sleep(HOLD);
check("a cancelled press excludes nothing", visible(), TOTAL);

// A deliberate drag past the tolerance cancels too.
sourcesNav.dispatch("pointerdown", { target: a, pointerType: "touch", clientX: 100, clientY: 200 });
await sleep(60);
sourcesNav.dispatch("pointermove", { target: a, pointerType: "touch", clientX: 100, clientY: 260 });
await sleep(HOLD);
check("dragging away cancels the press", visible(), TOTAL);
check("and clears the pressing state", a.classList.contains("is-pressing"), false);

// A browser reporting no pointerType should still be treated as touch.
sourcesNav.dispatch("pointerdown", { target: a, clientX: 100, clientY: 200 });
await sleep(HOLD);
check("an unknown pointerType is treated as touch",
  visible(), TOTAL - bySource(a.dataset.source));
a.click();
sourcesNav.dispatch("pointerdown", { target: a, clientX: 100, clientY: 200 });
await sleep(HOLD);
check("and toggles back", visible(), TOTAL);
a.click();

// A slow mouse click is a click, not an exclusion.
sourcesNav.dispatch("pointerdown", { target: a, pointerType: "mouse" });
await sleep(HOLD);
check("holding a mouse button does not exclude", a.classList.contains("is-excluded"), false);
a.click();
check("it still focuses", visible(), bySource(a.dataset.source));
clearBtn.click();

// --- undated rendering
const undated = times.filter((t) => t.dataset.undated);
check("undated times labelled 'first seen'",
  undated.length > 0 && undated.every((t) => t.textContent.startsWith("first seen")), true);
check("dated times not labelled 'first seen'",
  times.filter((t) => !t.dataset.undated).every((t) => !t.textContent.startsWith("first seen")), true);

console.log(failures ? `\n${failures} FAILED` : "\nall checks passed");
Deno.exit(failures ? 1 : 0);
