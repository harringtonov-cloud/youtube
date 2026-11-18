"use strict";
/* Вкладка «Браузер»: экран и управление удалённым Chromium на сервере.
   Экран — реальные кадры браузера (JPEG). Команды уходят на сервер, там Playwright выполняет их.
   Функции $, api, esc, toast, showLogin определены в app.js (вызываются после его загрузки). */

const live = {
  running: false,
  seq: 0,          // номер последнего показанного кадра
  busy: false,     // одна команда за раз
  loop: false,     // идёт ли цикл получения кадров
  blobUrl: null,
  inited: false,
};
const LIVE_CONTROLS = "#live-nav input, #live-nav button, #live-type input, #live-type button, " +
  ".live-nav-btns button, .live-tools button";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function liveMsg(text) {
  $("#live-err").textContent = text || "";
}

/* ---------- состояние ---------- */
function liveRender(s) {
  if (!s) return;
  const running = !!s.running;
  live.running = running;
  // после перезапуска сервера номера кадров начинаются заново
  if (typeof s.frame_seq === "number" && s.frame_seq < live.seq) live.seq = 0;

  const pill = $("#live-pill");
  pill.className = "pill " + (running ? "on" : "off");
  pill.lastElementChild.textContent = running ? (s.loading ? "загрузка…" : "работает") : "выключен";
  $("#live-start").classList.toggle("hidden", running);
  $("#live-stop").classList.toggle("hidden", !running);
  document.querySelectorAll(LIVE_CONTROLS).forEach((el) => (el.disabled = !running));

  const urlInput = $("#live-url");
  if (document.activeElement !== urlInput && s.url) urlInput.value = s.url;
  $("#live-meta").textContent = running
    ? [s.title, s.url].filter(Boolean).join(" — ")
    : (s.note || "Браузер выключен. Он не расходует ресурсы, пока не запущен.");
  $("#live-back").disabled = !running;
  $("#live-fwd").disabled = !running;
  liveMsg(s.error || "");

  const hasFrame = running && !!live.blobUrl;
  $("#live-screen").classList.toggle("hidden", !hasFrame);
  $("#live-placeholder").classList.toggle("hidden", hasFrame);
  if (running) liveFrameLoop();
}

function liveEvent(ev) {
  liveRender(ev);
}

/* ---------- экран ---------- */
function liveShow(blob, seq) {
  const old = live.blobUrl;
  live.blobUrl = URL.createObjectURL(blob);
  live.seq = seq;
  const img = $("#live-screen");
  img.src = live.blobUrl;
  img.classList.remove("hidden");
  $("#live-placeholder").classList.add("hidden");
  if (old) URL.revokeObjectURL(old);
}

async function liveFrameLoop() {
  if (live.loop) return;
  live.loop = true;
  try {
    while (live.running && !document.hidden && !$("#tab-browser").classList.contains("hidden")) {
      try {
        const res = await fetch(`/api/live/frame?after=${live.seq}`, { credentials: "same-origin", cache: "no-store" });
        if (res.status === 401) { showLogin(); break; }
        if (res.status === 200) {
          const seq = Number(res.headers.get("X-Frame-Seq")) || live.seq + 1;
          liveShow(await res.blob(), seq);
        } else {
          await sleep(res.status === 204 ? 250 : 1000);
        }
      } catch {
        await sleep(1000);
      }
    }
  } finally {
    live.loop = false;
  }
}

/* ---------- команды ---------- */
async function liveAct(kind, payload = {}) {
  if (live.busy) { toast("Подождите, предыдущее действие ещё выполняется", 1500); return; }
  live.busy = true;
  try {
    liveRender(await api("/api/live/action", { method: "POST", body: { kind, ...payload } }));
  } catch (e) {
    liveMsg(e.message);
  } finally {
    live.busy = false;
  }
}

async function liveNavigate(url) {
  if (live.busy) return;
  live.busy = true;
  liveMsg("");
  try {
    liveRender(await api("/api/live/navigate", { method: "POST", body: { url } }));
  } catch (e) {
    liveMsg(e.message);
  } finally {
    live.busy = false;
  }
}

function liveInit() {
  if (live.inited) return;
  live.inited = true;

  $("#live-start").addEventListener("click", async () => {
    $("#live-start").disabled = true;
    liveMsg("");
    try {
      live.seq = 0;
      liveRender(await api("/api/live/start", { method: "POST" }));
    } catch (e) {
      liveMsg(e.message);
    } finally {
      $("#live-start").disabled = false;
    }
  });
  $("#live-stop").addEventListener("click", async () => {
    try { liveRender(await api("/api/live/stop", { method: "POST" })); }
    catch (e) { liveMsg(e.message); }
  });

  $("#live-nav").addEventListener("submit", (e) => {
    e.preventDefault();
    liveNavigate($("#live-url").value.trim());
  });
  $("#live-type").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#live-text").value;
    if (!text) return;
    liveAct("type", { text }).then(() => { $("#live-text").value = ""; });
  });

  $("#live-back").addEventListener("click", () => liveAct("back"));
  $("#live-fwd").addEventListener("click", () => liveAct("forward"));
  $("#live-reload").addEventListener("click", () => liveAct("reload"));
  $("#live-up").addEventListener("click", () => liveAct("scroll", { dy: -400 }));
  $("#live-down").addEventListener("click", () => liveAct("scroll", { dy: 400 }));
  $("#live-keyback").addEventListener("click", () => liveAct("key", { key: "Backspace" }));
  $("#live-enter").addEventListener("click", () => liveAct("key", { key: "Enter" }));

  const screen = $("#live-screen");
  screen.addEventListener("click", (e) => {
    const r = screen.getBoundingClientRect();
    if (!r.width || !r.height) return;
    liveAct("click", { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
  });
  screen.addEventListener("wheel", (e) => {
    e.preventDefault();
    liveAct("scroll", { dy: Math.round(e.deltaY) });
  }, { passive: false });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && live.running) liveFrameLoop();
  });

  api("/api/live/status").then(liveRender).catch(() => {});
}

// вызывается из app.js (yt.js → bindTabs) при переключении вкладки
function onTabShow(name) {
  if (name === "browser" && live.running) liveFrameLoop();
}
