"use strict";
/* Веб-панель: управление прогонами, логи и результаты в реальном времени (SSE). */

const $ = (s, root = document) => root.querySelector(s);
const STATUS_RU = {
  queued: "в очереди", running: "выполняется", passed: "успешно", failed: "провал",
  error: "ошибка", cancelled: "отменён", retrying: "повтор", pending: "ожидает",
};
const ICON = { passed: "✔", failed: "✘", error: "⚠", running: "●", retrying: "↻", pending: "○", cancelled: "—" };
const MAX_LINES = 2000;

const state = {
  scenarios: [],
  runs: new Map(),          // id -> run
  viewId: null,             // прогон, который показан в панели
  lastLogId: 0,             // последний id лога для viewId
  seenLogs: new Set(),
  level: "all",
  es: null,
  scenarioState: {},        // name -> {status, attempt}
};

/* ---------- утилиты ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour12: false });
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "content-type": "application/json" } : undefined,
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Требуется вход");
  }
  let data = null;
  try { data = await res.json(); } catch { /* пустое тело */ }
  if (!res.ok) throw new Error((data && data.error) || `Ошибка ${res.status}`);
  return data;
}

/* ---------- вход ---------- */
function showLogin() {
  closeEvents();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#login-token").focus();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { token: $("#login-token").value } });
    $("#login-token").value = "";
    await boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});
$("#logout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  showLogin();
});

/* ---------- сценарии и форма ---------- */
function renderScenarios() {
  $("#scenarios").innerHTML = state.scenarios.map((s) => `
    <label class="scn">
      <input type="checkbox" name="scenario" value="${esc(s.name)}" checked>
      <div><b>${esc(s.title)}</b><span>${esc(s.description)}</span></div>
    </label>`).join("");
}
$("#toggle-all").addEventListener("click", () => {
  const boxes = [...document.querySelectorAll("input[name=scenario]")];
  const all = boxes.every((b) => b.checked);
  boxes.forEach((b) => (b.checked = !all));
  $("#toggle-all").textContent = all ? "выбрать все" : "снять все";
});

let parseTimer;
$("#url").addEventListener("input", () => {
  clearTimeout(parseTimer);
  const hint = $("#url-hint");
  const v = $("#url").value.trim();
  hint.className = "hint muted";
  if (!v) { hint.textContent = ""; return; }
  parseTimer = setTimeout(async () => {
    try {
      const r = await api("/api/parse?url=" + encodeURIComponent(v));
      hint.className = "hint ok";
      hint.textContent = `ID видео-метки: ${r.video_id}`;
    } catch (err) {
      hint.className = "hint bad";
      hint.textContent = err.message;
    }
  }, 300);
});

$("#run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#form-error");
  err.textContent = "";
  const scenarios = [...document.querySelectorAll("input[name=scenario]:checked")].map((b) => b.value);
  if (!scenarios.length) { err.textContent = "Выберите хотя бы один сценарий"; return; }
  $("#start").disabled = true;
  try {
    const run = await api("/api/runs", { method: "POST", body: { url: $("#url").value.trim(), scenarios } });
    state.runs.set(run.id, run);
    renderHistory();
    await selectRun(run.id);
    toast("Тест поставлен в очередь");
    $("#live").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e2) {
    err.textContent = e2.message;
  } finally {
    $("#start").disabled = false;
  }
});

/* ---------- отображение прогона ---------- */
async function selectRun(id) {
  state.viewId = id;
  state.lastLogId = 0;
  state.seenLogs.clear();
  state.scenarioState = {};
  $("#console").innerHTML = "";
  const run = await api("/api/runs/" + id);
  state.runs.set(id, run);
  (run.results || []).forEach((r) => (state.scenarioState[r.name] = { status: r.status, attempt: r.attempts }));
  renderLive();
  renderHistory();
  await loadLogs();
  loadShots();
  try {
    const c = await api("/api/sandbox/" + run.video_id);
    renderCounters(c);
  } catch { /* необязательно */ }
}

async function loadLogs() {
  if (!state.viewId) return;
  const id = state.viewId;
  const logs = await api(`/api/runs/${id}/logs?after=${state.lastLogId}`);
  if (id !== state.viewId) return;
  logs.forEach(appendLog);
}

function appendLog(entry) {
  if (entry.run_id !== state.viewId || state.seenLogs.has(entry.id)) return;
  state.seenLogs.add(entry.id);
  state.lastLogId = Math.max(state.lastLogId, entry.id);
  const box = $("#console");
  const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const div = document.createElement("div");
  div.className = `line ${entry.level}`;
  div.dataset.level = entry.level;
  div.innerHTML = `<span class="t">${fmtTime(entry.ts)}</span>` +
    (entry.scenario ? `<span class="sc">[${esc(entry.scenario)}]</span>` : "") +
    `<span class="m">${esc(entry.message)}</span>`;
  div.hidden = state.level !== "all" && entry.level !== state.level;
  box.appendChild(div);
  while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
  if (stick) box.scrollTop = box.scrollHeight;
}

$("#filters").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-level]");
  if (!btn) return;
  state.level = btn.dataset.level;
  document.querySelectorAll("#filters .chip").forEach((c) => c.classList.toggle("active", c === btn));
  document.querySelectorAll("#console .line").forEach((l) => {
    l.hidden = state.level !== "all" && l.dataset.level !== state.level;
  });
});

function renderLive() {
  const run = state.runs.get(state.viewId);
  $("#live-empty").classList.toggle("hidden", !!run);
  $("#live-body").classList.toggle("hidden", !run);
  if (!run) { $("#cancel").classList.add("hidden"); return; }

  const badge = $("#live-status");
  badge.textContent = STATUS_RU[run.status] || run.status;
  badge.className = `badge s-${run.status}`;
  $("#live-id").textContent = "#" + run.id;
  $("#live-video").textContent = "метка " + run.video_id;
  $("#cancel").classList.toggle("hidden", !["queued", "running"].includes(run.status));
  $("#live-error").textContent = run.error || "";

  const names = run.scenarios || [];
  const done = names.filter((n) => ["passed", "failed", "error", "cancelled"].includes((state.scenarioState[n] || {}).status)).length;
  $("#progress-bar").style.width = names.length ? Math.round((done / names.length) * 100) + "%" : "0%";
  const titles = Object.fromEntries(state.scenarios.map((s) => [s.name, s.title]));
  $("#chips").innerHTML = names.map((n) => {
    const st = (state.scenarioState[n] || {}).status || "pending";
    const at = (state.scenarioState[n] || {}).attempt;
    const extra = at > 1 ? ` · попытка ${at}` : "";
    return `<span class="sc-chip s-${st}"><i>${ICON[st] || "○"}</i>${esc(titles[n] || n)}${extra}</span>`;
  }).join("");
}

function renderCounters(c) {
  $("#c-views").textContent = c.views ?? 0;
  $("#c-likes").textContent = c.likes ?? 0;
  $("#c-comments").textContent = c.comments ?? 0;
}

async function loadShots() {
  const id = state.viewId;
  if (!id) return;
  try {
    const names = await api(`/api/runs/${id}/screenshots`);
    if (id !== state.viewId) return;
    $("#shots").innerHTML = names.map((n) =>
      `<a href="/api/runs/${id}/screenshots/${encodeURIComponent(n)}" target="_blank" rel="noopener">` +
      `<img loading="lazy" alt="${esc(n)}" src="/api/runs/${id}/screenshots/${encodeURIComponent(n)}"></a>`).join("");
  } catch { /* скриншотов может не быть */ }
}

$("#cancel").addEventListener("click", async () => {
  if (!state.viewId) return;
  try { await api(`/api/runs/${state.viewId}/cancel`, { method: "POST" }); toast("Отмена запрошена"); }
  catch (e) { toast(e.message); }
});

/* ---------- история ---------- */
function renderHistory() {
  const runs = [...state.runs.values()].sort((a, b) => b.created_at - a.created_at);
  const box = $("#history");
  if (!runs.length) { box.innerHTML = '<div class="h-empty">История пуста</div>'; return; }
  box.innerHTML = runs.map((r) => {
    const sm = r.summary;
    const res = sm ? `${sm.passed}/${sm.total} успешно · ${sm.duration_s} с` : "";
    const active = ["queued", "running"].includes(r.status);
    return `<div class="h-item ${r.id === state.viewId ? "sel" : ""}" data-id="${r.id}">
      <div class="top"><span class="badge s-${r.status}">${STATUS_RU[r.status] || r.status}</span>
        <code>${esc(r.video_id)}</code></div>
      <div class="meta">${fmtDate(r.created_at)} ${res ? "· " + res : ""}</div>
      ${active ? "" : `<button class="btn ghost small del" data-del="${r.id}" type="button" aria-label="Удалить">🗑</button>`}
    </div>`;
  }).join("");
}
$("#history").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    try {
      await api("/api/runs/" + del.dataset.del, { method: "DELETE" });
      state.runs.delete(del.dataset.del);
      if (state.viewId === del.dataset.del) { state.viewId = null; $("#console").innerHTML = ""; renderLive(); }
      renderHistory();
    } catch (err) { toast(err.message); }
    return;
  }
  const item = e.target.closest(".h-item");
  if (item) selectRun(item.dataset.id).catch((err) => toast(err.message));
});
$("#clear-history").addEventListener("click", async () => {
  if (!confirm("Удалить все завершённые прогоны, логи и скриншоты?")) return;
  try {
    await api("/api/runs", { method: "DELETE" });
    await loadRuns();
    if (state.viewId && !state.runs.has(state.viewId)) { state.viewId = null; $("#console").innerHTML = ""; renderLive(); }
  } catch (err) { toast(err.message); }
});
async function loadRuns() {
  const runs = await api("/api/runs?limit=50");
  state.runs = new Map(runs.map((r) => [r.id, r]));
  renderHistory();
}

/* ---------- SSE ---------- */
function setConn(on) {
  const el = $("#conn");
  el.className = "pill " + (on ? "on" : "off");
  el.lastElementChild.textContent = on ? "онлайн" : "нет связи";
}
function closeEvents() {
  if (state.es) { state.es.close(); state.es = null; }
  setConn(false);
}
function openEvents() {
  closeEvents();
  const es = new EventSource("/api/events");
  state.es = es;
  es.onopen = () => {
    setConn(true);
    // после переподключения подтягиваем пропущенное
    loadRuns().catch(() => {});
    loadLogs().catch(() => {});
  };
  es.onerror = () => setConn(false); // EventSource переподключится сам
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    handleEvent(ev);
  };
}
function handleEvent(ev) {
  switch (ev.type) {
    case "log":
      appendLog(ev);
      break;
    case "run_update": {
      state.runs.set(ev.run.id, ev.run);
      renderHistory();
      if (ev.run.id === state.viewId) { renderLive(); if (!["queued", "running"].includes(ev.run.status)) loadShots(); }
      break;
    }
    case "scenario_update":
      if (ev.run_id === state.viewId) {
        state.scenarioState[ev.name] = { status: ev.status, attempt: ev.attempt };
        renderLive();
        if (["passed", "failed", "error"].includes(ev.status)) loadShots();
      }
      break;
    case "sandbox": {
      const run = state.runs.get(state.viewId);
      if (run && run.video_id === ev.vid) renderCounters(ev);
      break;
    }
    default:
  }
}

/* ---------- запуск ---------- */
async function boot() {
  try {
    await api("/api/me");
  } catch {
    showLogin();
    return;
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  if (!state.scenarios.length) {
    state.scenarios = await api("/api/scenarios");
    renderScenarios();
  }
  await loadRuns();
  openEvents();
  // показываем активный прогон или последний
  const runs = [...state.runs.values()].sort((a, b) => b.created_at - a.created_at);
  const active = runs.find((r) => ["queued", "running"].includes(r.status)) || runs[0];
  if (active && !state.viewId) await selectRun(active.id);
}
boot();
