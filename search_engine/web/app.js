// Preview UI for Report Intelligence. Plain JavaScript, no build step.
// Server-provided *_html fields are already HTML-escaped (only <mark> is added); everything else
// is inserted with textContent, so report text can never inject markup.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const PAGE = 25;
let state = { route: "search", q: "", type: "", from: "", to: "", equipment: "", partial: false, offset: 0 };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function td(content, cls) {
  const c = el("td", cls);
  if (content instanceof Node) c.appendChild(content); else c.textContent = content ?? "";
  return c;
}
function htmlCell(html, cls) { const c = el("td", cls); c.innerHTML = html || ""; return c; }
async function api(path, params = {}, options = {}) {
  const u = new URL(path, location.origin);
  Object.entries(params).forEach(([k, v]) => { if (v !== "" && v != null && v !== false) u.searchParams.set(k, v); });
  const r = await fetch(u, options);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (_) {} throw new Error(m); }
  return r.json();
}
const fmtDate = d => d || "";
const fmtNum = v => (v === null || v === undefined) ? "" : String(v);

// ---------------------------------------------------------------- theme
function applyTheme(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("theme", t); } catch (_) {} }
function initTheme() {
  let t = null; try { t = localStorage.getItem("theme"); } catch (_) {}
  applyTheme(t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  $("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

// ---------------------------------------------------------------- summary cards
async function loadCards() {
  try {
    const s = await api("/api/stats");
    const box = $("#cards"); box.replaceChildren();
    const add = (label, value, opts = {}) => {
      const c = el("div", "card" + (opts.sel ? " sel" : "") + (opts.type !== undefined ? " clickable" : ""));
      c.appendChild(el("div", "label", label));
      c.appendChild(el("div", "num" + (opts.warn ? " warn" : ""), String(value)));
      if (opts.type !== undefined) c.onclick = () => { state.type = opts.type; $("#type").value = opts.type; state.offset = 0; run(); };
      box.appendChild(c);
    };
    add("Total reports", s.reports.toLocaleString(), { sel: state.type === "", type: "" });
    s.by_type.forEach(t => add(t.name + " reports", t.reports.toLocaleString(), { sel: state.type === t.name, type: t.name }));
    add("Failed files", s.failed_files.toLocaleString(), { warn: s.failed_files > 0 });
    $("#coverage").textContent = s.first_report_date ? `${s.reports.toLocaleString()} reports, ${s.lines.toLocaleString()} lines · ${s.first_report_date} to ${s.last_report_date}` : "No reports imported yet";
    const sel = $("#type");
    if (sel.options.length <= 1) s.by_type.forEach(t => sel.add(new Option(t.name, t.name)));
  } catch (e) { $("#status").textContent = "Cannot reach the database: " + e.message; $("#status").className = "status error"; }
}

// ---------------------------------------------------------------- routing and state
function readHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const p = new URLSearchParams(query);
  state = { route: path === "reports" ? "reports" : "search", q: p.get("q") || "", type: p.get("type") || "", from: p.get("from") || "",
            to: p.get("to") || "", equipment: p.get("equipment") || "", partial: p.get("partial") === "1", offset: parseInt(p.get("offset") || "0", 10) || 0 };
}
function writeHash() {
  const p = new URLSearchParams();
  [["q", state.q], ["type", state.type], ["from", state.from], ["to", state.to], ["equipment", state.equipment]].forEach(([k, v]) => v && p.set(k, v));
  if (state.partial) p.set("partial", "1");
  if (state.offset) p.set("offset", state.offset);
  history.replaceState(null, "", `#/${state.route}${p.toString() ? "?" + p : ""}`);
}
function syncForm() {
  $("#q").value = state.q; $("#type").value = state.type; $("#from").value = state.from; $("#to").value = state.to;
  $("#equipment").value = state.equipment; $("#partial").checked = state.partial;
  const isSearch = state.route === "search";
  $("#qField").hidden = !isSearch; $("#equipment").hidden = !isSearch; $("#partialWrap").hidden = !isSearch;
  $("#pageTitle").textContent = isSearch ? "REPORT SEARCH" : "REPORTS";
  document.querySelectorAll("#menu a").forEach(a => a.classList.toggle("active", a.dataset.route === state.route));
}
function readForm() {
  state.q = $("#q").value.trim(); state.type = $("#type").value; state.from = $("#from").value; state.to = $("#to").value;
  state.equipment = $("#equipment").value.trim(); state.partial = $("#partial").checked;
}

// ---------------------------------------------------------------- results
function setStatus(text, error) { const s = $("#status"); s.textContent = text || ""; s.className = "status" + (error ? " error" : ""); }
function setHead(cols) { const h = $("#table thead"); h.replaceChildren(); const r = el("tr"); cols.forEach(c => r.appendChild(el("th", "", c))); h.appendChild(r); }

async function run() {
  writeHash(); syncForm(); loadCards();
  setStatus("Loading…");
  try { state.route === "search" ? await runSearch() : await runReports(); }
  catch (e) { setStatus(e.message, true); $("#table tbody").replaceChildren(); $("#pageInfo").textContent = ""; }
}
function pager(count, hasMore) {
  $("#prev").disabled = state.offset <= 0; $("#next").disabled = !hasMore;
  $("#pageInfo").textContent = count ? `Showing ${state.offset + 1}–${state.offset + count}` : "";
}
function emptyRow(cols, text) { const tr = el("tr"); const c = el("td", "empty", text); c.colSpan = cols; tr.appendChild(c); return tr; }

async function runSearch() {
  const cols = ["Date", "Type", "Shift", "Section", "Item", "Remarks", "Value"];
  setHead(cols);
  const page = await api("/api/search", { q: state.q, type: state.type, from: state.from, to: state.to, equipment: state.equipment,
                                          mode: state.partial ? "partial" : "fulltext", limit: PAGE, offset: state.offset });
  const body = $("#table tbody"); body.replaceChildren();
  if (!state.q && !state.type && !state.from && !state.to && !state.equipment) { setStatus("Type a keyword, or pick a type or date range."); }
  else setStatus(page.hits.length ? "" : "No matches. Try “Partial / typos”, fewer words, or a wider date range.");
  page.hits.forEach(h => {
    const tr = el("tr", "clickable");
    tr.append(td(fmtDate(h.report_date)), td(pill(h.report_type)), td(h.shift), htmlCell(h.section_html), htmlCell(h.label_html), htmlCell(h.text_html),
              td(h.numeric_value != null ? `${fmtNum(h.numeric_value)} ${h.unit || ""}`.trim() : "", "num"));
    tr.onclick = () => openReport(h.report_id, h.line_id);
    body.appendChild(tr);
  });
  if (!page.hits.length && body.children.length === 0) body.appendChild(emptyRow(cols.length, ""));
  pager(page.hits.length, page.has_more);
}

async function runReports() {
  const cols = ["Doc Number", "Report Date", "Type", "Shift Interval", "Shift Supervisor", ""];
  setHead(cols);
  const page = await api("/api/reports", { type: state.type, from: state.from, to: state.to, limit: PAGE, offset: state.offset });
  const body = $("#table tbody"); body.replaceChildren();
  setStatus(page.reports.length ? "" : "No reports found for this selection.");
  page.reports.forEach(r => {
    const tr = el("tr", "clickable");
    const docNo = r.header?.doc_no || r.file_name?.replace(/\.[^.]+$/, "") || "";
    tr.append(td(el("span", "link", docNo)), td(fmtDate(r.report_date)), td(pill(r.report_type)), td(r.shift), td(r.header?.supervisor || ""), td(""));
    const remove = el("button", "btn danger", "Delete");
    remove.type = "button";
    remove.onclick = async e => {
      e.stopPropagation();
      if (!confirm(`Delete report ${docNo || r.report_date}? This removes it from the database; the source Excel file is kept.`)) return;
      remove.disabled = true;
      try { await api(`/api/reports/${r.id}`, {}, { method: "DELETE" }); await run(); }
      catch (error) { setStatus("Could not delete report: " + error.message, true); remove.disabled = false; }
    };
    tr.lastElementChild.appendChild(remove);
    tr.onclick = () => openReport(r.id);
    body.appendChild(tr);
  });
  pager(page.reports.length, page.has_more);
}
function pill(t) { return el("span", "pill", t); }

// ---------------------------------------------------------------- one report, every line in its own row
async function openReport(id, hitLineId) {
  $("#overlay").hidden = false; $("#drawer").hidden = false;
  $("#dTitle").textContent = "Loading…"; $("#dSub").textContent = ""; $("#dHeader").replaceChildren(); $("#dTable tbody").replaceChildren();
  try {
    const r = await api(`/api/reports/${id}`);
    $("#dTitle").textContent = `${r.report_type} report · ${r.report_date}${r.shift ? " · " + r.shift : ""}`;
    $("#dSub").textContent = `${r.lines.length} lines · source: ${r.source.file_name} (${r.source.origin})`;
    const kv = $("#dHeader");
    Object.entries(r.header).forEach(([k, v]) => { if (v == null || v === "") return; const d = el("div"); d.append(el("b", "", k.replace(/_/g, " ")), document.createTextNode(String(v))); kv.appendChild(d); });
    r.warnings.forEach(w => { const d = el("div"); d.append(el("b", "", "warning"), document.createTextNode(w)); kv.appendChild(d); });
    kv.hidden = kv.children.length === 0;
    const body = $("#dTable tbody");
    let hitRow = null;
    const sectionOrder = ["Oil Treatment", "Gas Treatment", "Utility Systems", "GTG & Power Plant", "Storage Tanks Levels", "Wells and MFs"];
    const normalizeSection = value => {
      const s = (value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
      if (s.includes("oil treatment")) return "Oil Treatment";
      if (s.includes("gas treatment")) return "Gas Treatment";
      if (s.includes("utility system")) return "Utility Systems";
      if (s.includes("gtg") || s.includes("power plant")) return "GTG & Power Plant";
      if (s.includes("storage tank")) return "Storage Tanks Levels";
      if (s.includes("well") || s.includes("mf")) return "Wells and MFs";
      return value || "Other";
    };
    const lines = r.lines.filter(l => normalizeSection(l.section) !== "Crew").sort((a, b) => {
      const ai = sectionOrder.indexOf(normalizeSection(a.section)); const bi = sectionOrder.indexOf(normalizeSection(b.section));
      return (ai < 0 ? sectionOrder.length : ai) - (bi < 0 ? sectionOrder.length : bi) || a.line_no - b.line_no;
    });
    lines.forEach(l => {
      const tr = el("tr", l.id === hitLineId ? "hit" : "");
      tr.append(td(normalizeSection(l.section)), td(l.label), td(l.text), td(fmtNum(l.numeric_value), "num"), td(l.unit));
      if (l.id === hitLineId) hitRow = tr;
      body.appendChild(tr);
    });
    if (hitRow) hitRow.scrollIntoView({ block: "center" });
  } catch (e) { $("#dTitle").textContent = "Cannot open report"; $("#dSub").textContent = e.message; }
}
function closeReport() { $("#overlay").hidden = true; $("#drawer").hidden = true; }

// ---------------------------------------------------------------- wiring
function init() {
  initTheme();
  $("#go").onclick = () => { readForm(); state.offset = 0; run(); };
  $("#clear").onclick = () => { state = { route: state.route, q: "", type: "", from: "", to: "", equipment: "", partial: false, offset: 0 }; run(); };
  $("#prev").onclick = () => { state.offset = Math.max(0, state.offset - PAGE); run(); };
  $("#next").onclick = () => { state.offset += PAGE; run(); };
  $("#filters").addEventListener("keydown", e => { if (e.key === "Enter") $("#go").click(); });
  $("#type").onchange = () => $("#go").click();
  $("#overlay").onclick = closeReport; $("#dClose").onclick = closeReport;
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeReport();
    if (e.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
  });
  window.addEventListener("hashchange", () => { readHash(); run(); });
  if (!location.hash) location.hash = "#/search";
  readHash(); run();
}
init();
