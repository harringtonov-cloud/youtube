"use strict";
/* Вкладка «YouTube API»: реальные данные и операции через официальный YouTube Data API.
   Функции $, api, esc, toast, fmtTime, fmtDate определены в app.js (вызываются уже после его загрузки). */

const yt = {
  status: null,
  video: null,        // текущее видео (ответ /api/yt/video)
  order: "relevance",
  nextPage: null,
  bound: false,
  devicePoll: null,
  timers: {},
};

const fmtNum = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("ru-RU"));
function fmtDur(s) {
  if (s === null || s === undefined) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}
function debounce(key, fn, ms = 400) {
  clearTimeout(yt.timers[key]);
  yt.timers[key] = setTimeout(fn, ms);
}
function ytMsg(id, text, ok = false) {
  const el = $(id);
  el.textContent = text || "";
  el.style.color = ok ? "var(--ok)" : "";
}

/* ---------- инициализация ---------- */
function ytInit() {
  if (!yt.bound) {
    yt.bound = true;
    bindTabs();
    $("#yt-form").addEventListener("submit", onLookup);
    $("#yt-like").addEventListener("click", onLike);
    $("#yt-watch").addEventListener("click", onWatch);
    $("#yt-comment-form").addEventListener("submit", onComment);
    $("#yt-edit-form").addEventListener("submit", onEdit);
    $("#yt-refresh").addEventListener("click", onRefreshWatch);
    $("#yt-more").addEventListener("click", () => loadComments(false));
    $("#yt-export-copy").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText($("#yt-export-text").value); toast("Скопировано"); }
      catch { $("#yt-export-text").select(); toast("Выделено — скопируйте вручную"); }
    });
    $("#yt-comments").addEventListener("click", onCommentClick);
    $("#yt-watchlist").addEventListener("click", onWatchClick);
    document.querySelectorAll("[data-order]").forEach((b) => b.addEventListener("click", () => {
      yt.order = b.dataset.order;
      document.querySelectorAll("[data-order]").forEach((x) => x.classList.toggle("active", x === b));
      loadComments(true);
    }));
    const q = new URLSearchParams(location.search);
    if (q.get("yt") === "connected") toast("Вход через Google выполнен");
    if (q.get("yt_error")) ytMsg("#yt-auth-error", "Вход не выполнен: " + q.get("yt_error"));
    if (q.has("yt") || q.has("yt_error")) history.replaceState(null, "", location.pathname + "#youtube");
  }
  ytRefreshAll();
}

function ytRefreshAll() {
  loadStatus();
  loadWatchlist();
  loadActions();
}

function bindTabs() {
  const names = [...document.querySelectorAll(".tab")].map((t) => t.dataset.tab);
  const show = (name) => {
    if (!names.includes(name)) name = "youtube";
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    names.forEach((n) => $("#tab-" + n).classList.toggle("hidden", n !== name));
    history.replaceState(null, "", "#" + name);
    if (typeof onTabShow === "function") onTabShow(name);
  };
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => show(t.dataset.tab)));
  show(location.hash.slice(1));
}

/* ---------- подключение ---------- */
async function loadStatus() {
  try {
    yt.status = await api("/api/yt/status");
    renderStatus();
  } catch (e) {
    $("#yt-status").textContent = e.message;
  }
}

function renderStatus() {
  const st = yt.status;
  if (!st) return;
  let html;
  if (st.connected) {
    const ch = st.channel ? esc(st.channel.title) : "канал не найден (у аккаунта нет канала YouTube)";
    html = `✅ Вход выполнен: <b>${ch}</b>
      <div class="chips" style="margin-top:8px">
        <span class="sc-chip s-${st.can_rate ? "passed" : "pending"}">лайки ${st.can_rate ? "доступны" : "нет прав"}</span>
        <span class="sc-chip s-${st.can_comment ? "passed" : "pending"}">комментарии ${st.can_comment ? "доступны" : "нет прав"}</span>
      </div>`;
    if (!st.can_comment) {
      html += `<div class="hint muted">Комментарии требуют право youtube.force-ssl: его даёт только веб-вход (нужен HTTPS-адрес возврата, см. README).</div>`;
    }
  } else if (st.api_key) {
    html = "🔎 Режим чтения (API-ключ): публичные данные доступны. Для лайков и комментариев войдите через Google.";
  } else {
    html = "⚙️ Не настроено. Задайте в .env YOUTUBE_API_KEY (чтение) и/или GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET (вход).";
  }
  $("#yt-status").innerHTML = html;

  const btns = [];
  if (st.connected) {
    btns.push(`<button class="btn ghost small" data-act="export" type="button">Токен для хостинга</button>`);
    btns.push(`<button class="btn ghost small" data-act="logout" type="button">Выйти и отозвать доступ</button>`);
  } else if (st.oauth_client) {
    if (st.device.status === "pending") {
      btns.push(`<button class="btn ghost small" data-act="device-cancel" type="button">Отменить вход</button>`);
    } else {
      btns.push(`<button class="btn primary small" data-act="device" type="button">Войти по коду (без домена)</button>`);
    }
    if (st.web_login_available) btns.push(`<button class="btn small" data-act="web" type="button">Войти через Google (веб)</button>`);
  }
  $("#yt-auth-btns").innerHTML = btns.join("");
  $("#yt-auth-btns").onclick = onAuthClick;

  const dev = $("#yt-device");
  if (!st.connected && st.device.status === "pending") {
    dev.classList.remove("hidden");
    dev.innerHTML = `<div>1. Откройте <a href="${esc(st.device.verification_url)}" target="_blank" rel="noopener">${esc(st.device.verification_url)}</a></div>
      <div>2. Введите код:</div><div class="code">${esc(st.device.user_code)}</div>
      <div class="muted">Ожидаю подтверждения… код действует около 30 минут.</div>`;
    startDevicePoll();
  } else if (!st.connected && ["denied", "expired", "error"].includes(st.device.status)) {
    dev.classList.remove("hidden");
    dev.innerHTML = `<div class="error">${esc(st.device.message || "Вход не завершён")}</div>`;
    stopDevicePoll();
  } else {
    dev.classList.add("hidden");
    stopDevicePoll();
  }

  const q = st.quota;
  const pct = Math.min(100, Math.round((q.used_estimate / q.limit) * 100));
  $("#yt-quota-text").textContent = `${fmtNum(q.used_estimate)} / ${fmtNum(q.limit)}`;
  $("#yt-quota-bar").style.width = pct + "%";
  $("#yt-quota-bar").title = "Оценка по журналу этого приложения. Точный остаток виден в Google Cloud Console. Обнуление: " + fmtDate(q.resets_at);
  $("#yt-writes-text").textContent = `${st.writes.used} / ${st.writes.limit}`;
  updateActionsUi();
}

function startDevicePoll() {
  if (yt.devicePoll) return;
  yt.devicePoll = setInterval(loadStatus, 3000);
}
function stopDevicePoll() {
  if (yt.devicePoll) { clearInterval(yt.devicePoll); yt.devicePoll = null; }
}

async function onAuthClick(e) {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  ytMsg("#yt-auth-error", "");
  try {
    if (btn.dataset.act === "device") {
      yt.status = await api("/api/yt/auth/device/start", { method: "POST" });
      renderStatus();
    } else if (btn.dataset.act === "device-cancel") {
      yt.status = await api("/api/yt/auth/device/cancel", { method: "POST" });
      renderStatus();
    } else if (btn.dataset.act === "web") {
      const r = await api("/api/yt/auth/web/start");
      location.href = r.url;
    } else if (btn.dataset.act === "export") {
      if (!confirm("Показать секретный токен доступа? Он нужен только чтобы вставить его в переменные окружения хостинга.")) return;
      const r = await api("/api/yt/auth/export", { method: "POST" });
      $("#yt-export-text").value = Object.entries(r).map(([k, v]) => `${k}=${v}`).join("\n");
      $("#yt-export").classList.remove("hidden");
    } else if (btn.dataset.act === "logout") {
      if (!confirm("Выйти и отозвать доступ приложения к вашему аккаунту Google?")) return;
      yt.status = await api("/api/yt/auth/logout", { method: "POST" });
      $("#yt-export").classList.add("hidden");
      $("#yt-export-text").value = "";
      renderStatus();
      toast("Вы вышли, доступ отозван");
    }
  } catch (err) {
    ytMsg("#yt-auth-error", err.message);
  }
}

/* ---------- видео ---------- */
async function onLookup(e) {
  e.preventDefault();
  ytMsg("#yt-video-error", "");
  const btn = e.submitter;
  if (btn) btn.disabled = true;
  try {
    yt.video = await api("/api/yt/video?url=" + encodeURIComponent($("#yt-url").value.trim()));
    renderVideo();
    $("#yt-actions-card").classList.remove("hidden");
    $("#yt-comments-card").classList.remove("hidden");
    ytMsg("#yt-op-msg", "");
    updateActionsUi();
    loadComments(true);
  } catch (err) {
    ytMsg("#yt-video-error", err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderVideo() {
  const v = yt.video;
  const note = (val, why) => (val === null ? `<small class="muted">${why}</small>` : "");
  const rating = v.my_rating === "like" ? "👍 вы поставили лайк" : v.my_rating === "none" ? "без вашей оценки"
    : (v.my_rating_error ? "оценка недоступна" : "");
  $("#yt-video").classList.remove("hidden");
  $("#yt-video").innerHTML = `
    ${v.thumbnail ? `<img class="thumb" alt="" src="${esc(v.thumbnail)}" referrerpolicy="no-referrer">` : ""}
    <div class="v-title">${esc(v.title)}</div>
    <div class="muted">${esc(v.channel_title)} · ${v.published_at ? esc(new Date(v.published_at).toLocaleDateString("ru-RU")) : ""} · ${fmtDur(v.duration_s)} · ${esc(v.privacy || "")}</div>
    <div class="counters">
      <div><b>${fmtNum(v.views)}</b><span>просмотры</span></div>
      <div><b>${fmtNum(v.likes)}</b><span>лайки ${note(v.likes, "скрыты автором")}</span></div>
      <div><b>${fmtNum(v.comments)}</b><span>комментарии ${note(v.comments, "отключены/скрыты")}</span></div>
    </div>
    <div class="muted small-note">Данные получены из YouTube Data API. ${rating ? esc(rating) + ". " : ""}Дизлайки API не отдаёт с 2021 года.</div>`;
}

function updateActionsUi() {
  const st = yt.status, v = yt.video;
  if (!st || !v) return;
  const likeBtn = $("#yt-like");
  const liked = v.my_rating === "like";
  likeBtn.textContent = liked ? "👎 Снять лайк" : "👍 Поставить лайк";
  likeBtn.disabled = !st.can_rate;
  likeBtn.title = st.can_rate ? "" : "Войдите через Google";
  const cBtn = $("#yt-comment-btn");
  cBtn.disabled = !st.can_comment;
  cBtn.title = st.can_comment ? "" : "Нужен веб-вход с правом youtube.force-ssl";
  const own = st.connected && st.channel && st.channel.id === v.channel_id && st.can_update;
  $("#yt-edit-form").classList.toggle("hidden", !own);
  if (own && $("#yt-edit-title").dataset.vid !== v.id) {
    $("#yt-edit-title").value = v.title || "";
    $("#yt-edit-desc").value = v.description || "";
    $("#yt-edit-title").dataset.vid = v.id;
  }
}

async function onLike() {
  const v = yt.video;
  const want = v.my_rating === "like" ? "none" : "like";
  if (!confirm(want === "like" ? "Поставить лайк этому видео от имени вашего аккаунта?" : "Снять ваш лайк?")) return;
  ytMsg("#yt-op-msg", "");
  $("#yt-like").disabled = true;
  try {
    const r = await api("/api/yt/rate", { method: "POST", body: { video: v.id, rating: want } });
    v.my_rating = r.rating;
    toast(r.changed ? (want === "like" ? "Лайк поставлен" : "Лайк снят") : "Без изменений: оценка уже такая");
    renderVideo();
  } catch (err) {
    ytMsg("#yt-op-msg", err.message);
  } finally {
    updateActionsUi();
  }
}

async function onComment(e) {
  e.preventDefault();
  const text = $("#yt-comment").value.trim();
  if (!text) { ytMsg("#yt-op-msg", "Введите текст комментария"); return; }
  if (!confirm("Опубликовать этот комментарий от имени вашего аккаунта? Его увидят все.")) return;
  ytMsg("#yt-op-msg", "");
  try {
    await api("/api/yt/comment", { method: "POST", body: { video: yt.video.id, text } });
    $("#yt-comment").value = "";
    toast("Комментарий опубликован");
    yt.order = "time";
    document.querySelectorAll("[data-order]").forEach((x) => x.classList.toggle("active", x.dataset.order === "time"));
    loadComments(true);
  } catch (err) {
    ytMsg("#yt-op-msg", err.message);
  }
}

async function onEdit(e) {
  e.preventDefault();
  if (!confirm("Изменить название и описание вашего видео на YouTube?")) return;
  ytMsg("#yt-op-msg", "");
  try {
    const r = await api("/api/yt/video/update", {
      method: "POST",
      body: { video: yt.video.id, title: $("#yt-edit-title").value, description: $("#yt-edit-desc").value },
    });
    yt.video.title = r.title;
    yt.video.description = r.description;
    renderVideo();
    ytMsg("#yt-op-msg", "Сохранено", true);
  } catch (err) {
    ytMsg("#yt-op-msg", err.message);
  }
}

async function onWatch() {
  try {
    await api("/api/yt/watch", { method: "POST", body: { video: yt.video.id } });
    toast("Видео добавлено в отслеживание");
    loadWatchlist();
  } catch (err) {
    ytMsg("#yt-op-msg", err.message);
  }
}

/* ---------- комментарии ---------- */
async function loadComments(reset) {
  const box = $("#yt-comments");
  if (reset) { yt.nextPage = null; box.innerHTML = '<div class="muted">Загрузка…</div>'; }
  const qs = new URLSearchParams({ video: yt.video.id, order: yt.order });
  if (!reset && yt.nextPage) qs.set("page", yt.nextPage);
  try {
    const r = await api("/api/yt/comments?" + qs);
    if (reset) box.innerHTML = "";
    if (reset && !r.items.length) box.innerHTML = '<div class="muted">Комментариев пока нет.</div>';
    const myId = yt.status && yt.status.channel ? yt.status.channel.id : null;
    box.insertAdjacentHTML("beforeend", r.items.map((c) => commentHtml(c, myId)).join(""));
    yt.nextPage = r.next_page;
    $("#yt-more").classList.toggle("hidden", !r.next_page);
  } catch (err) {
    box.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    $("#yt-more").classList.add("hidden");
  }
}

function commentHtml(c, myId) {
  const mine = myId && c.author_channel_id === myId;
  const replies = (c.replies || []).map((r) => `<div class="reply"><b>${esc(r.author)}</b> ${esc(r.text)}</div>`).join("");
  return `<div class="cmt" data-id="${esc(c.id)}">
    <div class="cmt-head"><b>${esc(c.author)}</b><span class="muted">${c.published_at ? esc(new Date(c.published_at).toLocaleDateString("ru-RU")) : ""} · 👍 ${fmtNum(c.likes)}</span></div>
    <div class="cmt-text">${esc(c.text)}</div>
    ${replies}
    <div class="cmt-actions">
      ${yt.status && yt.status.can_comment ? '<button class="link" data-do="reply" type="button">Ответить</button>' : ""}
      ${mine && yt.status.can_comment ? '<button class="link" data-do="delete" type="button">Удалить</button>' : ""}
    </div></div>`;
}

async function onCommentClick(e) {
  const btn = e.target.closest("[data-do]");
  if (!btn) return;
  const id = btn.closest(".cmt").dataset.id;
  try {
    if (btn.dataset.do === "reply") {
      const text = prompt("Ваш ответ:");
      if (!text || !text.trim()) return;
      if (!confirm("Опубликовать ответ от имени вашего аккаунта?")) return;
      await api("/api/yt/reply", { method: "POST", body: { video: yt.video.id, parent: id, text } });
      toast("Ответ опубликован");
      loadComments(true);
    } else if (btn.dataset.do === "delete") {
      if (!confirm("Удалить этот комментарий?")) return;
      await api(`/api/yt/comment/${encodeURIComponent(id)}?video=${yt.video.id}`, { method: "DELETE" });
      toast("Комментарий удалён");
      loadComments(true);
    }
  } catch (err) {
    ytMsg("#yt-op-msg", err.message);
  }
}

/* ---------- отслеживание ---------- */
async function loadWatchlist() {
  try {
    renderWatchlist(await api("/api/yt/watch"));
  } catch (e) {
    $("#yt-watchlist").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

function delta(latest, prev, key) {
  if (!latest || !prev || latest[key] === null || prev[key] === null) return "";
  const d = latest[key] - prev[key];
  return d === 0 ? "" : ` <small class="${d > 0 ? "up" : "down"}">${d > 0 ? "+" : ""}${fmtNum(d)}</small>`;
}

function renderWatchlist(list) {
  const box = $("#yt-watchlist");
  if (!list.length) { box.innerHTML = '<div class="h-empty">Список пуст. Найдите видео и нажмите «Отслеживать статистику».</div>'; return; }
  box.innerHTML = list.map((w) => {
    const l = w.latest, p = w.previous;
    return `<div class="w-item" data-id="${esc(w.video_id)}">
      <div class="w-main" data-do="chart">
        <div class="w-title">${esc(w.title || w.video_id)}</div>
        <div class="muted small-note">${esc(w.channel_title || "")} · ${l ? "обновлено " + fmtDate(l.ts) : "нет данных"}</div>
        <div class="w-stats">
          <span>👁 <b>${fmtNum(l && l.views)}</b>${delta(l, p, "views")}</span>
          <span>👍 <b>${fmtNum(l && l.likes)}</b>${delta(l, p, "likes")}</span>
          <span>💬 <b>${fmtNum(l && l.comments)}</b>${delta(l, p, "comments")}</span>
        </div>
      </div>
      <button class="btn ghost small" data-do="remove" type="button" aria-label="Убрать">🗑</button>
      <div class="w-chart hidden"></div>
    </div>`;
  }).join("");
}

async function onWatchClick(e) {
  const item = e.target.closest(".w-item");
  if (!item) return;
  const id = item.dataset.id;
  const act = e.target.closest("[data-do]");
  if (!act) return;
  try {
    if (act.dataset.do === "remove") {
      if (!confirm("Убрать видео из отслеживания и удалить историю его счётчиков?")) return;
      await api("/api/yt/watch/" + id, { method: "DELETE" });
      loadWatchlist();
    } else if (act.dataset.do === "chart") {
      const chart = item.querySelector(".w-chart");
      if (!chart.classList.contains("hidden")) { chart.classList.add("hidden"); return; }
      const snaps = await api("/api/yt/watch/" + id);
      chart.innerHTML = sparkline(snaps);
      chart.classList.remove("hidden");
    }
  } catch (err) {
    toast(err.message);
  }
}

function sparkline(snaps) {
  const pts = snaps.filter((s) => s.views !== null);
  if (pts.length < 2) return '<div class="muted small-note">Нужно минимум два замера, чтобы построить график. Они появятся по расписанию или после «Обновить сейчас».</div>';
  const W = 600, H = 120, pad = 6;
  const xs = pts.map((p) => p.ts), ys = pts.map((p) => p.views);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = (x) => pad + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * pad);
  const sy = (y) => H - pad - ((y - y0) / (y1 - y0 || 1)) * (H - 2 * pad);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.ts).toFixed(1)},${sy(p.views).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="spark" role="img" aria-label="График просмотров">
      <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
    <div class="row-between muted small-note"><span>${fmtDate(x0)} · ${fmtNum(y0)}</span><span>${fmtDate(x1)} · ${fmtNum(y1)}</span></div>`;
}

async function onRefreshWatch() {
  const btn = $("#yt-refresh");
  btn.disabled = true;
  try {
    const r = await api("/api/yt/watch/refresh", { method: "POST" });
    toast(r.polled ? `Обновлено видео: ${r.polled}` : "Нет видео для обновления");
    if (r.missing && r.missing.length) toast(`Недоступны: ${r.missing.join(", ")}`, 5000);
    loadWatchlist();
  } catch (err) {
    toast(err.message, 5000);
  } finally {
    btn.disabled = false;
  }
}

/* ---------- журнал обращений к API ---------- */
async function loadActions() {
  try {
    renderActions(await api("/api/yt/actions?limit=50"));
  } catch (e) {
    $("#yt-log").innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

const KIND_RU = { read: "чтение", write: "запись", auth: "вход" };
function actionRow(a) {
  return `<tr class="${a.ok ? "" : "bad"}"><td>${fmtTime(a.ts)}</td><td><span class="badge k-${esc(a.kind)}">${KIND_RU[a.kind] || esc(a.kind)}</span></td>
    <td><code>${esc(a.name)}</code></td><td>${a.ok ? "✔" : "✘"} ${a.http_status || ""}</td><td>${a.units}</td><td class="detail">${esc(a.detail || "")}</td></tr>`;
}
function renderActions(list) {
  $("#yt-log").innerHTML = list.length
    ? `<table class="tbl"><thead><tr><th>Время</th><th>Тип</th><th>Метод</th><th>Итог</th><th>Квота</th><th>Подробности</th></tr></thead><tbody id="yt-log-body">${list.map(actionRow).join("")}</tbody></table>`
    : '<div class="h-empty">Обращений к API пока не было.</div>';
}

/* ---------- события SSE ---------- */
function ytEvent(ev) {
  if (ev.type === "yt_action") {
    const body = $("#yt-log-body");
    if (body) {
      body.insertAdjacentHTML("afterbegin", actionRow(ev.action));
      while (body.rows.length > 100) body.deleteRow(-1);
    } else {
      loadActions();
    }
    debounce("status", loadStatus);
  } else if (ev.type === "yt_auth") {
    loadStatus();
  } else if (ev.type === "yt_snapshot") {
    debounce("watch", loadWatchlist);
  }
}
