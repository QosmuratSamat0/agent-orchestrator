"""Single self-contained page for the local dashboard (no external scripts, fonts or images)."""

PAGE_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Orchestrator — панель</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1b1f24; --muted: #5f6b7a; --line: #e3e6ea;
  --accent: #2f6fde; --ok: #1f8a4c; --bad: #c7362f; --warn: #b7791f; --idle: #7a8494; --logbg: #0f1419; --logink: #d6dde6;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --panel: #151b23; --ink: #e6edf3; --muted: #8b98a8; --line: #273140;
          --accent: #5a95f5; --ok: #3fb96f; --bad: #f0685f; --warn: #e0a43a; --idle: #8b98a8; --logbg: #090c10; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px; border-bottom: 1px solid var(--line); background: var(--panel); }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
header .sub { color: var(--muted); font-size: 13px; }
#conn { margin-left: auto; font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
#conn i { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); display: inline-block; }
#conn.down i { background: var(--bad); }
main { display: grid; grid-template-columns: minmax(240px, 320px) 1fr; gap: 16px; padding: 16px 20px; max-width: 1400px; margin: 0 auto; }
@media (max-width: 820px) { main { grid-template-columns: 1fr; padding: 12px 16px; } }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
#list { padding: 6px; max-height: calc(100vh - 110px); overflow: auto; }
#list .empty { padding: 16px; color: var(--muted); }
#list .grp { padding: 10px 10px 4px; color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; }
.item { display: block; width: 100%; text-align: left; border: 0; background: none; color: inherit; font: inherit;
        padding: 10px 12px; border-radius: 8px; cursor: pointer; }
.item:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.item.sel { background: color-mix(in srgb, var(--accent) 15%, transparent); }
.item .t { font-weight: 600; display: flex; gap: 8px; align-items: center; overflow: hidden; }
.item .t span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item .m { color: var(--muted); font-size: 12px; margin-top: 2px; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--idle); }
.s-running .dot, .dot.s-running { background: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, transparent); }
.dot.s-completed { background: var(--ok); } .dot.s-failed { background: var(--bad); }
.dot.s-timed_out { background: var(--warn); } .dot.s-cancelled, .dot.s-queued { background: var(--idle); }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600;
         background: color-mix(in srgb, var(--idle) 18%, transparent); color: var(--idle); }
.badge.s-running { background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }
.badge.s-completed { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
.badge.s-failed { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
.badge.s-timed_out { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
#detail { padding: 18px 20px; min-width: 0; }
#detail .placeholder { color: var(--muted); padding: 40px 0; text-align: center; }
.head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.head h2 { margin: 0; font-size: 19px; overflow-wrap: anywhere; }
.head .grow { flex: 1; }
button.cancel { border: 1px solid var(--bad); color: var(--bad); background: none; border-radius: 8px; padding: 6px 12px; font: inherit; cursor: pointer; }
button.cancel:hover { background: color-mix(in srgb, var(--bad) 10%, transparent); }
button.cancel:disabled { opacity: .5; cursor: default; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 10px; margin: 16px 0; }
.kv { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; min-width: 0; }
.kv b { display: block; font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.kv span { overflow-wrap: anywhere; }
.kv .big { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }
h3 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin: 18px 0 8px; }
.note { border-left: 3px solid var(--line); padding: 6px 10px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink); }
.note.err { border-color: var(--bad); }
.events { list-style: none; margin: 0; padding: 0; max-height: 220px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
.events li { display: flex; gap: 10px; padding: 5px 10px; border-bottom: 1px solid var(--line); font-size: 13px; }
.events li:last-child { border-bottom: 0; }
.events time { color: var(--muted); font-variant-numeric: tabular-nums; flex: none; }
.events code { font-weight: 600; }
.logbar { display: flex; align-items: center; gap: 10px; }
.logbar label { margin-left: auto; color: var(--muted); font-size: 12px; display: flex; gap: 6px; align-items: center; }
pre#log { background: var(--logbg); color: var(--logink); border-radius: 8px; padding: 12px; margin: 0; height: 340px; overflow: auto;
          font: 12.5px/1.5 ui-monospace, "Cascadia Mono", Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
details summary { cursor: pointer; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Agent Orchestrator</h1><span class="sub">локальная панель · только этот компьютер</span>
  <span id="conn"><i></i><span id="connText">подключено</span></span>
</header>
<main>
  <nav class="card" id="list" aria-label="Задачи"><div class="empty">Загрузка…</div></nav>
  <section class="card" id="detail"><div class="placeholder">Выберите задачу слева</div></section>
</main>
<script>
"use strict";
const TOKEN = "__DASHBOARD_TOKEN__";
const STATUS_RU = { queued: "в очереди", running: "выполняется", completed: "завершена", failed: "ошибка",
                    timed_out: "время вышло", cancelled: "отменена" };
let tasks = [], selectedId = decodeURIComponent(location.hash.slice(1)) || null, logTaskId = null, logOffset = 0, detail = null, fetchedAt = 0;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function fmtTime(iso) { return iso ? new Date(iso).toLocaleTimeString() : "—"; }
function fmtDur(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.max(0, Math.round(sec));
  return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
}
function liveRemaining(item) {
  if (item.remaining_sec === null || item.remaining_sec === undefined) return null;
  return item.remaining_sec - (Date.now() - fetchedAt) / 1000;
}
async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(response.status);
  return response.json();
}
function setConn(ok) {
  document.getElementById("conn").classList.toggle("down", !ok);
  document.getElementById("connText").textContent = ok ? "подключено" : "нет связи с сервером";
}

function renderList() {
  const list = document.getElementById("list");
  const listScroll = list.scrollTop;
  list.replaceChildren();
  if (!tasks.length) { list.append(el("div", "empty", "Задач пока нет. Запустите: agent-orchestrator run …")); return; }
  const now = Date.now();
  const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
  const groups = [["Последний час", []], ["Сегодня", []], ["Раньше", []]];
  const created = (item) => new Date(item.created_at).getTime() || 0;
  for (const item of tasks.slice().sort((a, b) => created(b) - created(a))) {
    const at = created(item);
    groups[now - at <= 3600 * 1000 ? 0 : at >= midnight.getTime() ? 1 : 2][1].push(item);
  }
  for (const [label, items] of groups) {
    if (!items.length) continue;
    list.append(el("div", "grp", label + " · " + items.length));
    for (const item of items) appendItem(list, item);
  }
  list.scrollTop = listScroll;
}

function appendItem(list, item) {
  const button = el("button", "item" + (item.id === selectedId ? " sel" : ""));
  const title = el("div", "t");
  title.append(el("i", "dot s-" + item.status), el("span", "", item.title || "Без названия"));
  const left = liveRemaining(item);
  const meta = (STATUS_RU[item.status] || item.status) + " · попытка " + item.attempt +
               (left !== null ? " · осталось " + fmtDur(left) : "") + " · " + item.id.slice(0, 8);
  button.append(title, el("div", "m", meta));
  button.addEventListener("click", () => select(item.id));
  list.append(button);
}

function kv(label, value, big) {
  const box = el("div", "kv");
  box.append(el("b", "", label), el("span", big ? "big" : "", value));
  return box;
}

let lastDetailSig = null;
function renderDetail() {
  const pane = document.getElementById("detail");
  if (!detail) { lastDetailSig = null; pane.replaceChildren(el("div", "placeholder", "Выберите задачу слева")); return; }
  // Rebuilding the pane every poll reset the scroll position of the page and of the log; skip it when nothing changed.
  const sig = JSON.stringify(Object.assign({}, detail, { remaining_sec: 0 }));
  if (sig === lastDetailSig && document.getElementById("log")) return;
  lastDetailSig = sig;
  const keepLog = document.getElementById("log");
  const logScroll = keepLog ? keepLog.scrollTop : 0;
  const pageScroll = window.scrollY;
  pane.replaceChildren();
  const head = el("div", "head");
  head.append(el("h2", "", detail.title || "Без названия"),
              el("span", "badge s-" + detail.status, STATUS_RU[detail.status] || detail.status), el("span", "grow"));
  if (!detail.is_terminal) {
    const cancel = el("button", "cancel", "Отменить задачу");
    cancel.addEventListener("click", () => cancelTask(cancel));
    head.append(cancel);
  }
  pane.append(head);

  const grid = el("div", "grid");
  const left = liveRemaining(detail);
  grid.append(kv("Осталось", left === null ? "—" : fmtDur(left), true),
              kv("Попытка", detail.attempt, true),
              kv("Модель", detail.model + (detail.effort ? " · " + detail.effort : "")),
              kv("Вердикт", detail.verdict || "—"),
              kv("Начата", fmtTime(detail.started_at)),
              kv("Завершена", fmtTime(detail.finished_at)),
              kv("ID", detail.id.slice(0, 8)),
              kv("Каталог", detail.cwd));
  pane.append(grid);

  if (detail.error_message) { pane.append(el("h3", "", "Причина"), el("div", "note err", detail.error_message)); }
  if (detail.feedback) { pane.append(el("h3", "", "Замечания верификатора"), el("div", "note", detail.feedback)); }

  const task = el("details");
  task.open = window.__taskOpen === true;
  task.addEventListener("toggle", () => { window.__taskOpen = task.open; });
  task.append(el("summary", "", "Задание и критерии приёмки"),
              el("div", "note", detail.prompt), el("div", "note", detail.acceptance_criteria || "—"));
  pane.append(task);

  pane.append(el("h3", "", "События"));
  const events = el("ul", "events");
  for (const event of detail.events.slice().reverse()) {
    const row = el("li");
    row.append(el("time", "", fmtTime(event.at)), el("code", "", event.type));
    const extra = event.payload && (event.payload.error || event.payload.reason || event.payload.verdict);
    if (extra) row.append(el("span", "", String(extra)));
    events.append(row);
  }
  pane.append(events);

  const bar = el("div", "logbar");
  bar.append(el("h3", "", "Живой журнал Antigravity"));
  const follow = el("label");
  const box = el("input"); box.type = "checkbox"; box.id = "follow"; box.checked = window.__follow !== false;
  box.addEventListener("change", () => { window.__follow = box.checked; });
  follow.append(box, document.createTextNode("следить"));
  bar.append(follow);
  pane.append(bar);
  const log = keepLog || el("pre"); log.id = "log";
  if (!keepLog) {
    log.addEventListener("scroll", () => {
      // Scrolling up pauses following; returning to the bottom resumes it.
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
      window.__follow = atBottom;
      const checkbox = document.getElementById("follow");
      if (checkbox) checkbox.checked = atBottom;
    });
  }
  pane.append(log);
  // A node that was detached loses its scrollTop: put both positions back.
  log.scrollTop = window.__follow !== false ? log.scrollHeight : logScroll;
  window.scrollTo(0, pageScroll);
}

async function refreshLog() {
  if (!selectedId) return;
  const log = document.getElementById("log");
  if (!log) return;
  if (logTaskId !== selectedId) { logTaskId = selectedId; logOffset = 0; log.textContent = ""; }
  const tail = await api("/api/tasks/" + encodeURIComponent(selectedId) + "/log?offset=" + logOffset);
  if (tail.offset < logOffset) log.textContent = "";
  logOffset = tail.offset;
  if (tail.lines.length) {
    log.append(document.createTextNode(tail.lines.join("\n") + "\n"));
    if (window.__follow !== false) log.scrollTop = log.scrollHeight;
  } else if (!log.textContent) {
    log.textContent = "Журнал пока пуст.";
    logOffset = 0;
  }
}

async function refresh() {
  try {
    const data = await api("/api/tasks");
    tasks = data.tasks; fetchedAt = Date.now();
    if (!selectedId && tasks.length) selectedId = (tasks.find(t => !t.is_terminal) || tasks[0]).id;
    detail = selectedId ? await api("/api/tasks/" + encodeURIComponent(selectedId)) : null;
    renderList(); renderDetail(); await refreshLog();
    setConn(true);
  } catch (error) { setConn(false); }
}

function select(id) { selectedId = id; detail = null; lastDetailSig = null; window.__follow = true; history.replaceState(null, "", "#" + encodeURIComponent(id)); refresh(); }

async function cancelTask(button) {
  if (!confirm("Отменить задачу «" + (detail.title || detail.id.slice(0, 8)) + "»?")) return;
  button.disabled = true;
  try {
    const result = await api("/api/tasks/" + encodeURIComponent(detail.id) + "/cancel",
                             { method: "POST", headers: { "X-Dashboard-Token": TOKEN } });
    alert(result.message);
  } catch (error) { alert("Не удалось отменить: " + error.message); }
  refresh();
}

setInterval(() => { renderList(); const box = document.querySelector(".grid .big"); if (box && detail) box.textContent = fmtDur(liveRemaining(detail)) === "—" ? "—" : fmtDur(liveRemaining(detail)); }, 1000);
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""
