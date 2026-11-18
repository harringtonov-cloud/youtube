"use strict";
/* Вкладка «Настройки»: параметры проекта. Хранятся на сервере и переживают перезапуски. */

function settingsFill(cfg) {
  $("#set-repeat").value = cfg.repeat;
  $("#set-idle").value = cfg.live_idle_minutes;
  $("#set-quality").value = cfg.frame_quality;
  $("#set-scenarios").innerHTML = state.scenarios.map((s) => `
    <label class="scn">
      <input type="checkbox" name="set-scn" value="${esc(s.name)}" ${cfg.scenarios.includes(s.name) ? "checked" : ""}>
      <div><b>${esc(s.title)}</b><span>${esc(s.description)}</span></div>
    </label>`).join("");
}

async function settingsLoad() {
  const cfg = await api("/api/settings");
  state.defaults = cfg;
  settingsFill(cfg);
  $("#repeat").value = cfg.repeat;
  renderScenarios();
}

function settingsInit() {
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#set-msg").textContent = "";
    const body = {
      repeat: Number($("#set-repeat").value),
      live_idle_minutes: Number($("#set-idle").value),
      frame_quality: Number($("#set-quality").value),
      scenarios: [...document.querySelectorAll("input[name=set-scn]:checked")].map((b) => b.value),
    };
    try {
      const cfg = await api("/api/settings", { method: "PUT", body });
      state.defaults = cfg;
      settingsFill(cfg);
      $("#repeat").value = cfg.repeat;
      renderScenarios();
      toast("Настройки сохранены");
    } catch (err) {
      $("#set-msg").textContent = err.message;
    }
  });
}
settingsInit();
