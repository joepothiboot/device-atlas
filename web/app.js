// Device atlas: renders data.json (devices + plans precomputed by `python3 -m atlas site`).
const $ = (s, el = document) => el.querySelector(s);

const ARCH = {
  cache_cpu: { cls: "cache", name: "Hardware caches" },
  simt_gpu: { cls: "gpu", name: "GPU shared memory" },
  scratchpad_dma: { cls: "spm", name: "Scratchpad + DMA" },
};
const NS = "http://www.w3.org/2000/svg";
const DTYPE_ORDER = ["f16", "bf16", "f32", "i8"];

const state = { data: null, devs: [], byId: new Map(), root: null, expanded: new Set(), q: "", route: null, params: new URLSearchParams(), focusId: null, sort: { key: null, dir: 1 } };

/* ---------- small helpers ---------- */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null) continue;
    if (k.startsWith("aria-")) { el.setAttribute(k, String(v)); continue; }
    if (v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
function svg(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function glyph(arch, verified, size = 13) {
  const s = svg("svg", { viewBox: "0 0 14 14", width: size, height: size, class: "glyph a-" + ARCH[arch].cls, "aria-hidden": "true" });
  const cls = verified ? "g-solid" : "g-hollow";
  if (arch === "cache_cpu") s.append(svg("circle", { class: cls, cx: 7, cy: 7, r: 5.2 }));
  else if (arch === "simt_gpu") s.append(svg("polygon", { class: cls, points: "7,1.5 13,12 1,12" }));
  else s.append(svg("rect", { class: cls, x: 1.8, y: 1.8, width: 10.4, height: 10.4, rx: 1.5 }));
  return s;
}
function chevron() {
  const s = svg("svg", { viewBox: "0 0 10 10", width: 10, height: 10, "aria-hidden": "true" });
  s.append(svg("path", { d: "M3 1.5 7 5 3 8.5", fill: "none", stroke: "currentColor", "stroke-width": 1.7, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  return s;
}
const fmtBytes = (n) => {
  for (const [s, d] of [["TB", 2 ** 40], ["GB", 2 ** 30], ["MB", 2 ** 20], ["KB", 1024]]) {
    if (n >= d) { const v = n / d; return (v >= 10 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, "")) + " " + s; }
  }
  return Math.round(n) + " B";
};
const fmtTime = (t) => (t == null ? "n/a" : t < 1e-3 ? (t * 1e6).toFixed(1) + " µs" : (t * 1e3).toFixed(2) + " ms");
const fmtNum = (x) => (x == null ? "n/a" : x >= 100 ? Math.round(x).toString() : x.toFixed(1).replace(/\.0$/, ""));
const dtLabel = (dt) => dt;
const shortRes = (s) => s.replace("software-managed scratchpad", "scratchpad");
const unitOf = (dt) => (dt === "i8" ? "TOP/s" : "TFLOP/s");
function safe(fn, fallback) { try { return fn(); } catch { return fallback; } }

/* ---------- data model ---------- */
function buildTree(devs) {
  const root = { id: "", label: "", children: new Map(), devices: [], level: -1, parent: null };
  for (const d of devs) {
    let node = root;
    d.path.forEach((p, i) => {
      if (!node.children.has(p)) node.children.set(p, { id: d.path.slice(0, i + 1).join("/"), label: p, children: new Map(), devices: [], level: i, parent: node });
      node = node.children.get(p);
    });
    node.devices.push(d);
  }
  return root;
}
const groupIndex = new Map();
function indexGroups(node) { for (const c of node.children.values()) { groupIndex.set(c.id, c); indexGroups(c); } }
function allDevices(node) { return [...node.devices, ...[...node.children.values()].flatMap(allDevices)]; }
function kids(node) {
  const out = [];
  for (const c of node.children.values()) {
    if (c.level === 2 && c.devices.length === 1) out.push({ type: "device", dev: c.devices[0] }); // single-device families need no folder
    else out.push({ type: "group", node: c });
  }
  for (const d of node.devices) out.push({ type: "device", dev: d });
  return out;
}
const matches = (d, q) => !q || [d.name, d.id, d.vendor, ...d.path, ...(d.programming || [])].join(" ").toLowerCase().includes(q);
function preferredDtype(dev) {
  const withPeak = DTYPE_ORDER.find((t) => dev.dtypes.includes(t) && dev.peak_gflops[t]);
  return withPeak || dev.dtypes[0];
}
const planFor = (id, preset, dt) => safe(() => state.data.plans[id][preset][dt], null);

/* ---------- routing ---------- */
function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [pathPart, query = ""] = raw.split("?");
  const parts = pathPart.split("/").filter(Boolean).map((p) => safe(() => decodeURIComponent(p), p));
  state.params = new URLSearchParams(query);
  if (parts[0] === "device" && state.byId.has(parts[1])) return { view: "device", id: parts[1] };
  if (parts[0] === "group" && groupIndex.has(parts[1])) return { view: "group", id: parts[1] };
  return { view: "compare" };
}
function hashFor(route, params) {
  const q = params && [...params].length ? "?" + params.toString() : "";
  if (route.view === "device") return `#/device/${encodeURIComponent(route.id)}${q}`;
  if (route.view === "group") return `#/group/${encodeURIComponent(route.id)}${q}`;
  return `#/compare${q}`;
}
function setParam(key, value) {
  state.params.set(key, value);
  history.replaceState(null, "", hashFor(state.route, state.params));
  renderView();
}

/* ---------- sidebar tree ---------- */
function loadExpanded() {
  const saved = safe(() => JSON.parse(localStorage.getItem("atlas.expanded") || "null"), null);
  if (Array.isArray(saved)) state.expanded = new Set(saved);
  else for (const id of groupIndex.keys()) state.expanded.add(id);
}
const saveExpanded = () => safe(() => localStorage.setItem("atlas.expanded", JSON.stringify([...state.expanded])), null);

function treeItem(entry, level) {
  const q = state.q;
  if (entry.type === "device") {
    const d = entry.dev;
    if (!matches(d, q)) return null;
    const sel = state.route.view === "device" && state.route.id === d.id;
    return h("li", { role: "treeitem", "data-id": d.id, "data-type": "device", "data-arch": ARCH[d.archetype].cls, "aria-level": level + 1, "aria-selected": sel, tabindex: -1 },
      h("div", { class: "row device", style: `--depth:${level}` }, h("span", { class: "chev-pad" }), glyph(d.archetype, d.verified), h("span", { class: "label", text: d.name })));
  }
  const n = entry.node;
  const devs = allDevices(n).filter((d) => matches(d, q));
  if (!devs.length) return null;
  const open = q ? true : state.expanded.has(n.id);
  const sel = state.route.view === "group" && state.route.id === n.id;
  const li = h("li", { role: "treeitem", "data-id": n.id, "data-type": "group", "aria-level": level + 1, "aria-expanded": open, "aria-selected": sel, tabindex: -1 },
    h("div", { class: `row group l${level}`, style: `--depth:${level}` },
      h("button", { class: "chev", type: "button", tabindex: -1, "aria-label": (open ? "Collapse " : "Expand ") + n.label }, chevron()),
      h("span", { class: "label", text: n.label }), h("span", { class: "count", text: devs.length })));
  if (open) li.append(h("ul", { role: "group" }, kids(n).map((k) => treeItem(k, level + 1))));
  return li;
}
function renderTree() {
  const tree = $("#tree");
  const hadFocus = tree.contains(document.activeElement);
  const items = kids(state.root).map((k) => treeItem(k, 0)).filter(Boolean);
  tree.replaceChildren(...items);
  if (!items.length) tree.append(h("li", { class: "empty-tree", role: "none", text: "No device matches. Try a vendor, a chip name, or a tool like Triton." }));
  const all = [...tree.querySelectorAll('[role="treeitem"]')];
  const selId = state.route.view !== "compare" ? state.route.id : null;
  const focusEl = all.find((el) => el.dataset.id === state.focusId) || all.find((el) => el.dataset.id === selId) || all[0];
  if (focusEl) { focusEl.tabIndex = 0; if (hadFocus) focusEl.focus({ preventScroll: true }); }
  const link = $("#compare-link");
  if (state.route.view === "compare") link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
}
function toggle(id, force) {
  const open = force ?? !state.expanded.has(id);
  if (open) state.expanded.add(id); else state.expanded.delete(id);
  saveExpanded();
  state.focusId = id;
  renderTree();
}
function activate(li) {
  const id = li.dataset.id;
  state.focusId = id;
  if (li.dataset.type === "group") { if (!state.expanded.has(id)) { state.expanded.add(id); saveExpanded(); } location.hash = hashFor({ view: "group", id }); }
  else location.hash = hashFor({ view: "device", id });
  closeDrawer();
}
function onTreeClick(e) {
  const li = e.target.closest('[role="treeitem"]');
  if (!li || !e.target.closest(".row")) return;
  if (e.target.closest(".chev")) { toggle(li.dataset.id); return; }
  if (li.dataset.type === "group" && state.route.view === "group" && state.route.id === li.dataset.id) { toggle(li.dataset.id); return; }
  activate(li);
}
function onTreeKey(e) {
  const li = e.target.closest('[role="treeitem"]');
  if (!li) return;
  const items = [...$("#tree").querySelectorAll('[role="treeitem"]')];
  const i = items.indexOf(li);
  const focusAt = (el) => { if (!el) return; items.forEach((x) => (x.tabIndex = -1)); el.tabIndex = 0; el.focus(); state.focusId = el.dataset.id; };
  const isGroup = li.dataset.type === "group";
  const open = li.getAttribute("aria-expanded") === "true";
  const keys = {
    ArrowDown: () => focusAt(items[i + 1]),
    ArrowUp: () => focusAt(items[i - 1]),
    Home: () => focusAt(items[0]),
    End: () => focusAt(items[items.length - 1]),
    ArrowRight: () => { if (isGroup && !open && !state.q) toggle(li.dataset.id, true); else if (isGroup && open) focusAt(items[i + 1]); },
    ArrowLeft: () => { if (isGroup && open && !state.q) toggle(li.dataset.id, false); else focusAt(li.parentElement.closest('[role="treeitem"]')); },
    Enter: () => activate(li),
    " ": () => activate(li),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
}

/* ---------- drawer + theme ---------- */
function openDrawer() { $("#side").classList.add("open"); $("#scrim").hidden = false; $("#menu").setAttribute("aria-expanded", "true"); }
function closeDrawer() { $("#side").classList.remove("open"); $("#scrim").hidden = true; $("#menu").setAttribute("aria-expanded", "false"); }
function cycleTheme() {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  safe(() => localStorage.setItem("atlas.theme", next), null);
}

/* ---------- views ---------- */
function sec(title, ...body) { return h("section", { class: "sec" }, h("h2", { text: title }), h("div", { class: "sec-body" }, ...body)); }
function select(label, id, options, value, onchange) {
  return h("label", { class: "field" }, label,
    h("select", { id, onchange: (e) => onchange(e.target.value) }, options.map(([v, t]) => h("option", { value: v, selected: v === value }, t))));
}
function crumbs(dev) {
  const out = [h("a", { href: "#/compare", text: "All devices" })];
  dev.path.forEach((p, i) => {
    const id = dev.path.slice(0, i + 1).join("/");
    const node = groupIndex.get(id);
    out.push(h("span", { class: "sep", "aria-hidden": "true", text: "/" }));
    if (node && !(node.level === 2 && node.devices.length === 1)) out.push(h("a", { href: hashFor({ view: "group", id }), text: p }));
    else out.push(h("span", { text: p }));
  });
  return h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, out);
}
function presetOptions() { return state.data.presets.map((p) => [p.id, p.label]); }
function currentPreset() { const id = state.params.get("p"); return state.data.presets.find((p) => p.id === id) || state.data.presets[0]; }

/* compare */
function compareView() {
  const preset = currentPreset();
  const allDt = DTYPE_ORDER.filter((t) => state.devs.some((d) => d.dtypes.includes(t)));
  const dt = allDt.includes(state.params.get("t")) ? state.params.get("t") : "f16";
  const rows = [], missing = [];
  for (const d of state.devs) {
    const p = planFor(d.id, preset.id, dt);
    if (!p) { missing.push(d); continue; }
    rows.push({ d, p, waste: 100 * (1 - p.flops / p.flops_padded), pct: p.pct_peak });
  }
  const cols = [
    ["dev", "Device", (r) => r.d.name, false], ["outer", "Outer tile", (r) => r.p.outer, false], ["inner", "Inner unit", (r) => r.p.inner, false],
    ["res", "Tile lives in", (r) => r.p.resident, false], ["waste", "Padding", (r) => r.waste, true], ["ai", "FLOP per byte", (r) => r.p.ai, true],
    ["bound", "Bound by", (r) => r.p.bound, false], ["pct", "Share of peak", (r) => r.pct ?? -1, true],
  ];
  const sort = state.sort;
  if (sort.key) { const c = cols.find((x) => x[0] === sort.key); rows.sort((a, b) => { const x = c[2](a), y = c[2](b); return (x < y ? -1 : x > y ? 1 : 0) * sort.dir; }); }
  const head = h("tr", {}, cols.map(([key, label, , num]) =>
    h("th", { class: num ? "num" : "", scope: "col", "aria-sort": sort.key === key ? (sort.dir === 1 ? "ascending" : "descending") : null },
      h("button", { type: "button", onclick: () => { state.sort = { key, dir: sort.key === key ? -sort.dir : (num ? -1 : 1) }; renderView(); } }, label))));
  const body = rows.map(({ d, p, waste, pct }) =>
    h("tr", {},
      h("td", {}, h("span", { class: "devcell" }, glyph(d.archetype, d.verified), h("a", { href: hashFor({ view: "device", id: d.id }, new URLSearchParams({ p: preset.id, t: dt })), text: d.name }))),
      h("td", {}, h("span", { class: "mono", text: p.outer })), h("td", { text: p.inner }), h("td", { text: shortRes(p.resident) }),
      h("td", { class: "num", text: waste < 0.5 ? "none" : Math.round(waste) + "%" }),
      h("td", { class: "num", text: fmtNum(p.ai) + (p.balance ? " vs " + fmtNum(p.balance) : "") }),
      h("td", { text: p.bound === "n/a" ? "unknown" : p.bound }),
      h("td", { class: "num" }, pct == null ? h("span", { class: "muted", text: "no peak data" }) :
        h("span", { class: "meter", style: `--accent:var(--c-${ARCH[d.archetype].cls});--w:${Math.min(100, pct)}%` }, h("i"), "≤ " + Math.round(pct) + "%"))));
  return h("div", {},
    h("div", { class: "crumbs", "aria-hidden": "true" }, " "),
    h("h1", { class: "title", text: "Same matrix multiply, different machines" }),
    h("p", { class: "lead", text: "Every device below tiles the same problem. The algorithm is identical: tile the output, stream K. What differs is which limit binds first, and that changes the tile, the padding, and what holds you back." }),
    h("div", { style: "height:28px" }),
    h("div", { class: "controls" },
      select("Problem", "preset", presetOptions(), preset.id, (v) => setParam("p", v)),
      select("Data type", "dtype", allDt.map((t) => [t, dtLabel(t)]), dt, (v) => setParam("t", v)),
      h("span", { class: "muted", text: `${rows.length} of ${state.devs.length} devices have a native ${dt} path.` })),
    h("div", { class: "tbl-wrap" }, h("table", {}, h("caption", { class: "sr", text: `Tiling for ${preset.label} in ${dt}` }), h("thead", {}, head), h("tbody", {}, body))),
    missing.length ? h("p", { class: "muted" }, `No native ${dt} path: `, missing.map((d, i) => [i ? ", " : "", h("a", { href: hashFor({ view: "device", id: d.id }), text: d.name })])) : null,
    h("p", { class: "caveat", text: "Estimates are roofline-style upper bounds from a teaching model: they assume the inner loop reaches peak. Device numbers come from public knowledge and are not yet verified. Hollow shapes mark unverified entries." }));
}

/* group */
function groupView(node) {
  const devs = allDevices(node);
  const note = state.data.group_notes[node.label];
  const top = node.level === 0 ? node : null;
  const crumbItems = [h("a", { href: "#/compare", text: "All devices" })];
  let p = node.parent; const chain = [];
  while (p && p.level >= 0) { chain.unshift(p); p = p.parent; }
  chain.forEach((c) => crumbItems.push(h("span", { class: "sep", "aria-hidden": "true", text: "/" }), h("a", { href: hashFor({ view: "group", id: c.id }), text: c.label })));
  const sections = [];
  const children = [...node.children.values()];
  const renderList = (list) => h("div", { class: "cards" }, list.map((d) => deviceRow(d)));
  if (children.length && !(node.level === 1 && false)) {
    for (const c of children) sections.push(h("div", {}, h("h2", { class: "group-title" }, h("a", { href: hashFor({ view: "group", id: c.id }), text: c.label, style: "text-underline-offset:3px" })),
      state.data.group_notes[c.label] ? h("p", { class: "muted", style: "margin:4px 0 0;max-width:68ch", text: state.data.group_notes[c.label] }) : null, renderList(allDevices(c))));
  } else sections.push(renderList(devs));
  return h("div", {},
    h("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, crumbItems),
    h("h1", { class: "title", text: node.label }),
    note ? h("p", { class: "lead", text: note }) : null,
    h("p", { class: "meta" }, h("span", { text: `${devs.length} ${devs.length === 1 ? "device" : "devices"}` })),
    h("div", { style: "height:10px" }), sections);
}
function deviceRow(d) {
  const bits = [`${d.units.count} ${d.units.name}${d.units.count > 1 ? "s" : ""}`];
  if (d.matrix_unit) bits.push(`${d.matrix_unit.m}x${d.matrix_unit.n}x${d.matrix_unit.k} matrix unit`);
  else if (d.vector) bits.push(`${d.vector.bits}-bit vectors`);
  const fast = d.memory.find((m) => ["spm", "smem", "l1"].includes(m.role));
  if (fast) bits.push(`${fmtBytes(fast.kb * 1024)} ${fast.role === "l1" ? "L1" : fast.role === "smem" ? "shared memory" : "scratchpad"}`);
  return h("a", { class: "item", href: hashFor({ view: "device", id: d.id }) },
    h("span", { class: "item-name" }, glyph(d.archetype, d.verified), h("span", { class: "item-name-text" }, h("span", { text: d.name }))),
    h("span", { class: "item-facts", text: bits.join(", ") }));
}

/* device */
function memoryLadder(dev, plan) {
  const lo = 2, hi = 12; // log10 bytes: 100 B .. 1 TB
  const pos = (b) => Math.min(100, Math.max(0, ((Math.log10(Math.max(b, 1)) - lo) / (hi - lo)) * 100));
  const perUnit = "per " + dev.units.name;
  const rows = dev.memory.map((m) => ({ name: m.name, role: m.role, bytes: m.kb * 1024, scope: m.scope === "shared" ? "shared" : perUnit }));
  rows.push({ name: dev.dram.name, role: "dram", bytes: dev.dram.gb ? dev.dram.gb * 1e9 : null, scope: dev.dram.gbps ? `${dev.dram.gbps} GB/s` : "bandwidth not recorded", dram: true });
  const tiles = plan ? plan.tiles_list.filter((t) => t.role) : [];
  const bars = [];
  const lrows = rows.map((r) => {
    const t = tiles.find((x) => x.role === r.role);
    const byteTile = t && t.use != null && !t.unit;
    const cap = r.bytes ? h("div", { class: "lcap", "data-w": pos(r.bytes) }) : null;
    const use = byteTile && r.bytes ? h("div", { class: "luse", "data-w": pos(t.use), title: `${t.level}: ${fmtBytes(t.use)}` }) : null;
    if (cap) bars.push(cap); if (use) bars.push(use);
    let sub = null;
    if (byteTile) sub = `tile uses ${fmtBytes(t.use)} (${Math.round((100 * t.use) / t.cap)}%)`;
    else if (t && t.unit) sub = `tile uses ${t.use} of ${t.cap} ${t.unit}`;
    return h("div", { class: "lrow" },
      h("div", { class: "lname" }, r.name, h("small", { text: r.scope })),
      h("div", { class: "ltrack" }, cap, use),
      h("div", { class: "lval" }, r.dram ? (dev.dram.gb ? dev.dram.gb + " GB" : "unknown") : fmtBytes(r.bytes), sub ? h("small", { text: sub }) : null));
  });
  const ticks = [[10, "1 KB"], [40, "1 MB"], [70, "1 GB"], [100, "1 TB"]];
  requestAnimationFrame(() => requestAnimationFrame(() => bars.forEach((b) => (b.style.width = b.dataset.w + "%"))));
  return h("div", { class: "ladder", "data-arch": dev.archetype, role: "group", "aria-label": "Memory levels, fastest first" },
    lrows,
    h("div", { class: "laxis", "aria-hidden": "true" }, h("span"), h("div", { class: "ticks" }, ticks.map(([p, t]) => h("span", { style: `left:${p}%`, text: t }))), h("span")),
    h("p", { class: "lnote", text: "Log scale, fastest memory first. The wide bar is capacity; the solid bar is the tile's footprint." }));
}
function deviceView(dev) {
  const preset = currentPreset();
  const dt = dev.dtypes.includes(state.params.get("t")) ? state.params.get("t") : preferredDtype(dev);
  const plan = planFor(dev.id, preset.id, dt);
  const a = state.data.archetypes[dev.archetype];
  const spec = [];
  const row = (k, v) => spec.push(h("dt", { text: k }), h("dd", {}, v));
  row("Units", `${dev.units.count} x ${dev.units.name}`);
  if (dev.vector) row("Vector unit", `${dev.vector.bits}-bit, ${dev.vector.regs} registers`);
  if (dev.matrix_unit) row("Matrix unit", [`${dev.matrix_unit.name}: `, h("span", { class: "mono", text: `${dev.matrix_unit.m}x${dev.matrix_unit.n}x${dev.matrix_unit.k}` }), ` (${dev.matrix_unit.dtype.join(", ")})`]);
  row("Data types", dev.dtypes.join(", "));
  const peaks = Object.entries(dev.peak_gflops);
  row("Peak throughput", peaks.length ? peaks.map(([t, g]) => `${fmtNum(g / 1000)} ${unitOf(t)} ${t}`).join(", ") : "not recorded");
  row("DRAM", `${dev.dram.name}${dev.dram.gb ? ", " + dev.dram.gb + " GB" : ""}${dev.dram.gbps ? ", " + dev.dram.gbps + " GB/s" : ""}`);

  let tiling;
  if (!plan) tiling = h("p", { class: "muted", text: `${dev.name} has no native ${dt} path in this model.` });
  else {
    const waste = 100 * (1 - plan.flops / plan.flops_padded);
    tiling = h("div", {},
      h("div", { class: "facts" },
        h("div", { class: "fact" }, h("b", { text: "Outer tile" }), h("span", { class: "big", text: plan.outer.replace(/x/g, " × ") })),
        h("div", { class: "fact" }, h("b", { text: "Inner unit" }), h("span", { class: "mid", text: plan.inner })),
        h("div", { class: "fact" }, h("b", { text: "Tile lives in" }), h("span", { class: "mid", text: plan.resident })),
        waste >= 0.5 ? h("div", { class: "fact" }, h("b", { text: "Padding waste" }), h("span", { class: "mid", text: `${Math.round(waste)}% of the matrix work` })) : null),
      memoryLadder(dev, plan),
      h("div", { style: "height:22px" }),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tile-table" },
        h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "Level" }), h("th", { scope: "col", text: "Tile" }), h("th", { scope: "col", text: "Use of capacity" }))),
        h("tbody", {}, plan.tiles_list.map((t) => {
          let use = h("span", { class: "muted", text: "" });
          if (t.use != null && t.cap) {
            const pct = Math.min(100, (100 * t.use) / t.cap);
            const txt = t.unit ? `${t.use} of ${t.cap} ${t.unit}` : `${fmtBytes(t.use)} of ${fmtBytes(t.cap)}`;
            use = h("span", { class: "meter", style: `--accent:var(--c-${ARCH[dev.archetype].cls});--w:${pct}%` }, h("i"), txt);
          }
          return h("tr", {}, h("td", { text: t.level.trim() }), h("td", {}, h("span", { class: "mono", text: t.shape })), h("td", {}, use));
        })))),
      h("h3", { class: "group-title", text: "Why these numbers" }),
      h("ul", { class: "plain" }, plan.notes.map((n) => h("li", { text: n }))),
      h("h3", { class: "group-title", text: "Estimate" }),
      h("div", { class: "estimate" },
        plan.pct_peak != null
          ? h("p", {}, h("strong", { text: `${plan.bound}-bound` }), `, at most about ${Math.round(plan.pct_peak)}% of peak (${fmtTime(plan.t)}). Intensity ${fmtNum(plan.ai)} FLOP per byte against a machine balance of ${fmtNum(plan.balance)}.`)
          : h("p", {}, `No peak or bandwidth recorded, so there is no bound. Intensity is ${fmtNum(plan.ai)} FLOP per byte.`),
        h("p", { class: "muted", text: `DRAM traffic ${fmtBytes(plan.traffic)}. Compute ${fmtTime(plan.t_comp)}, data movement ${fmtTime(plan.t_dma)}.` }),
        h("p", { class: "caveat", text: "An upper bound from a teaching model: it assumes the inner loop reaches peak, which real kernels rarely do." })));
  }

  const controls = h("div", { class: "controls" },
    select("Problem", "preset", presetOptions(), preset.id, (v) => setParam("p", v)),
    select("Data type", "dtype", dev.dtypes.map((t) => [t, dtLabel(t)]), dt, (v) => setParam("t", v)),
    h("a", { class: "linkish", href: hashFor({ view: "compare" }, new URLSearchParams({ p: preset.id, t: dt })), text: "Compare on every device" }));

  return h("div", {},
    crumbs(dev),
    h("h1", { class: "title", text: dev.name }),
    h("p", { class: "meta" }, h("span", { text: dev.vendor }), h("span", { class: "mono", text: dev.id })),
    h("div", { class: "arch" }, glyph(dev.archetype, dev.verified, 16), h("p", {}, h("strong", { text: a.short + ". " }), `Here, a tile is ${a.tile}.`)),
    dev.verified ? null : h("p", { class: "note" }, glyph(dev.archetype, false), h("span", {}, "These numbers have not been checked against vendor documents yet. Check: ", dev.sources_to_check.join("; "), ".")),
    sec("Compute", h("dl", { class: "spec" }, spec)),
    sec("Memory and tiling", controls, tiling),
    sec("Software", h("ul", { class: "tags" }, dev.programming.map((p) => h("li", { text: p })))),
    sec("Gotchas", h("ul", { class: "plain" }, dev.gotchas.map((g) => h("li", { text: g })))));
}

function renderView() {
  const view = $("#view");
  let el, title;
  const r = state.route;
  if (r.view === "device") { const d = state.byId.get(r.id); el = deviceView(d); title = d.name; }
  else if (r.view === "group") { const g = groupIndex.get(r.id); el = groupView(g); title = g.label; }
  else { el = compareView(); title = "Compare all devices"; }
  view.replaceChildren(el);
  document.title = title + " | Device atlas";
  $("#live").textContent = title;
}
function onRoute() {
  state.route = parseRoute();
  renderTree();
  renderView();
  $("#main").scrollTo({ top: 0 });
  const sel = $('#tree [aria-selected="true"]');
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

/* ---------- boot ---------- */
async function boot() {
  let data;
  try {
    const res = await fetch("data.json");
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (e) {
    $("#view").replaceChildren(h("div", { class: "err" }, h("h1", { class: "title", text: "Could not load the catalog" }),
      h("p", { class: "lead" }, "data.json did not load. If you opened this file directly, serve the folder instead: ", h("code", { text: "python3 -m http.server -d dist" }), ".")));
    return;
  }
  state.data = data; state.devs = data.devices;
  data.devices.forEach((d) => state.byId.set(d.id, d));
  state.root = buildTree(data.devices); indexGroups(state.root); loadExpanded();
  $("#tree").addEventListener("click", onTreeClick);
  $("#tree").addEventListener("keydown", onTreeKey);
  $("#q").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); renderTree(); });
  $("#expand").addEventListener("click", () => { state.expanded = new Set(groupIndex.keys()); saveExpanded(); renderTree(); });
  $("#collapse").addEventListener("click", () => { state.expanded = new Set(); saveExpanded(); renderTree(); });
  $("#menu").addEventListener("click", () => ($("#side").classList.contains("open") ? closeDrawer() : openDrawer()));
  $("#scrim").addEventListener("click", closeDrawer);
  $("#theme").addEventListener("click", cycleTheme);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  window.addEventListener("hashchange", onRoute);
  onRoute();
}
boot();
