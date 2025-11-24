const LIGHT_DAY_KEYS = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];
const DEFAULT_FEEDER_PUMP_STOP_DURATION = 5;

let refreshTimer = null;
let currentPumpConfig = {};
let currentLightState = false;
let currentLightAuto = true;
let globalSpeedUs = 300;
let currentHeatAuto = true;
let currentHeatEnabled = true;
let currentFanOn = false;
let refreshIntervalMs = 5000;
let loaderTimer = null;
let nextRefreshAt = 0;
let inputsInitialized = false;
let currentFeederAuto = true;
let currentFeederSchedule = [];
let feederInitialized = false;
let feederDirty = false;
let lastFeederScheduleJson = "[]";
let toastContainer = null;
let lastAnalysisSummary = null;
const ANALYSIS_PERIOD_LABELS = {
  last_3_days: "0 à -3 jours",
  last_week: "-3 à -7 jours",
  last_month: "-7 jours à -1 mois",
  last_year: "-1 mois à -1 an",
};

async function apiAction(action, params = {}) {
  try {
    const res = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || res.statusText);
    }
    showToast(`Action ${action} OK`, "success");
    return data;
  } catch (err) {
    console.error("Action", action, err);
    showToast(`Erreur ${action}: ${err.message}`, "danger");
    throw err;
  }
}

async function refreshPorts() {
  try {
    const res = await fetch("/api/ports");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const ports = await res.json();
    const select = document.getElementById("portSelect");
    select.innerHTML = "";
    ports.forEach((port) => {
      const opt = document.createElement("option");
      opt.value = port.device;
      opt.textContent = `${port.device} — ${port.description}`;
      select.appendChild(opt);
    });
  } catch (err) {
    console.error("refreshPorts", err);
  }
}

async function connect() {
  const select = document.getElementById("portSelect");
  if (!select.value) {
    alert("Sélectionnez un port série.");
    return;
  }
  await apiAction("connect", { port: select.value });
  restartRefreshTimer();
}

async function disconnect() {
  await apiAction("disconnect");
  stopRefreshTimer();
  refreshState();
}

function stopRefreshTimer() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (loaderTimer) {
    clearInterval(loaderTimer);
    loaderTimer = null;
  }
}

function restartRefreshTimer() {
  stopRefreshTimer();
  refreshState();
  nextRefreshAt = Date.now() + refreshIntervalMs;
  resetRefreshLoader();
  refreshTimer = setInterval(() => {
    refreshState();
    nextRefreshAt = Date.now() + refreshIntervalMs;
  }, refreshIntervalMs);
}

function applyWater() {
  const value = parseFloat(document.getElementById("tset_water2").value || "0");
  if (!isFinite(value)) return;
  apiAction("set_water", { t: value });
}

function applyRes() {
  const value = parseFloat(document.getElementById("tset_res2").value || "0");
  if (!isFinite(value)) return;
  apiAction("set_reserve", { t: value });
}

function submitWaterQuality() {
  const fieldMap = [
    { id: "water_no3", key: "no3" },
    { id: "water_no2", key: "no2" },
    { id: "water_gh", key: "gh" },
    { id: "water_kh", key: "kh" },
    { id: "water_cl2", key: "cl2" },
    { id: "water_po4", key: "po4" },
  ];
  const params = {};
  fieldMap.forEach(({ id, key }) => {
    const input = document.getElementById(id);
    if (!input) return;
    const strValue = (input.value || "").trim();
    if (!strValue) return;
    const numericValue = parseFloat(strValue);
    if (!isFinite(numericValue)) return;
    params[key] = numericValue;
  });
  if (Object.keys(params).length === 0) {
    showToast("Aucune donnée fournie pour la qualité d'eau.", "warning");
    return;
  }
  apiAction("submit_water_quality", params);
}

async function saveOpenAiKey(apiKey) {
  const res = await fetch("/api/openai-key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
  if (!res.ok) {
    let errorMsg = `HTTP ${res.status}`;
    try {
      const errData = await res.json();
      errorMsg = errData.error || errorMsg;
    } catch (err) {
      console.error("OpenAI key save parse error:", err);
    }
    throw new Error(errorMsg);
  }
}

async function promptAndSaveOpenAiKey() {
  const userKey = window.prompt(
    "Entrez votre clé API OpenAI (elle sera stockée en local pour les prochaines analyses) :"
  );
  if (!userKey || !userKey.trim()) {
    showToast("Clé API OpenAI non fournie.", "warning");
    return false;
  }
  try {
    await saveOpenAiKey(userKey.trim());
    showToast("Clé API OpenAI enregistrée.", "success");
    return true;
  } catch (err) {
    showToast(`Erreur lors de l'enregistrement de la clé : ${err.message}`, "danger");
    return false;
  }
}

async function prepareAiAnalysis() {
  const resultDiv = document.getElementById("aiAnalysisResult");
  const prepareBtn = document.getElementById("prepareAiBtn");
  const launchBtn = document.getElementById("launchAiBtn");
  const promptDetails = document.getElementById("aiPromptDetails");
  const summaryDetails = document.getElementById("aiSummaryDetails");
  const summaryPreview = document.getElementById("aiSummaryPreview");
  if (prepareBtn) prepareBtn.disabled = true;
  if (launchBtn) launchBtn.disabled = true;
  if (resultDiv) {
    resultDiv.innerHTML = "Récupération des données InfluxDB en cours...";
  }
  if (promptDetails) promptDetails.classList.add("d-none");
  if (summaryDetails) summaryDetails.classList.add("d-none");
  if (summaryPreview) summaryPreview.textContent = "";
  lastAnalysisSummary = null;
  try {
    const res = await fetch("/analysis/run?periods=3d,week,month,year");
    if (!res.ok) {
      let errorMsg = `HTTP ${res.status}`;
      try {
        const errData = await res.json();
        errorMsg = errData.error || errorMsg;
      } catch (err) {
        console.error("Analysis data parse error:", err);
      }
      throw new Error(errorMsg);
    }
    const data = await res.json();
    lastAnalysisSummary = data.summary;
    const periods = (lastAnalysisSummary && lastAnalysisSummary.periods) || {};
    const sortedPeriods = Object.entries(periods).sort((a, b) => {
      const ta = a[1] && a[1].earliest_time ? new Date(a[1].earliest_time).getTime() : Number.NEGATIVE_INFINITY;
      const tb = b[1] && b[1].earliest_time ? new Date(b[1].earliest_time).getTime() : Number.NEGATIVE_INFINITY;
      return tb - ta; // dates les plus récentes en haut, les plus anciennes en bas
    });
    const earliestLines = sortedPeriods
      .map(([key, value]) => {
        if (!value || !value.earliest_time) {
          return `${ANALYSIS_PERIOD_LABELS[key] || key}: aucune donnée`;
        }
        const date = new Date(value.earliest_time);
        return `${ANALYSIS_PERIOD_LABELS[key] || key}: ${date.toLocaleString()}`;
      })
      .join("<br>");
    if (resultDiv) {
      resultDiv.innerHTML = `
        <div>Données préparées pour ${
          sortedPeriods.map(([key]) => ANALYSIS_PERIOD_LABELS[key] || key).join(", ") ||
          "les périodes demandées"
        }.</div>
        <div class="mt-1"><strong>Ancienneté des séries (plus ancienne en bas):</strong><br>${earliestLines}</div>
        <div class="mt-1">Vous pouvez maintenant interroger l'IA.</div>
      `;
    }
    if (summaryPreview) {
      summaryPreview.textContent = JSON.stringify(lastAnalysisSummary, null, 2);
    }
    if (summaryDetails) summaryDetails.classList.remove("d-none");
    if (launchBtn) launchBtn.disabled = false;
    showToast("Historique récupéré avec succès.", "success");
  } catch (err) {
    console.error("Prepare AI analysis error:", err);
    lastAnalysisSummary = null;
    if (resultDiv) {
      resultDiv.innerHTML = `<div class="alert alert-danger">Impossible de récupérer les données : ${err.message}</div>`;
    }
    if (summaryDetails) summaryDetails.classList.add("d-none");
    showToast(`Erreur préparation analyse : ${err.message}`, "danger");
  } finally {
    if (prepareBtn) prepareBtn.disabled = false;
  }
}

async function getAiAnalysis() {
  const resultDiv = document.getElementById("aiAnalysisResult");
  const spinner = document.getElementById("aiAnalysisSpinner");
  const btn = document.querySelector('[data-action="get_ai_analysis"]');
  const promptDetails = document.getElementById("aiPromptDetails");
  const promptContent = document.getElementById("aiPromptContent");
  const summaryDetails = document.getElementById("aiSummaryDetails");
  if (!lastAnalysisSummary) {
    showToast("Préparez d'abord les données avant d'interroger l'IA.", "warning");
    return;
  }
  const contextInput = document.getElementById("aiContextInput");
  const userContext = contextInput ? contextInput.value : "";
  const clientTime = new Date().toISOString();
  if (spinner) spinner.classList.remove("d-none");
  if (btn) btn.disabled = true;
  if (resultDiv) {
    resultDiv.innerHTML = "Analyse en cours, veuillez patienter...";
  }
  if (promptDetails) promptDetails.classList.add("d-none");
  if (promptContent) promptContent.textContent = "";
  try {
    const res = await fetch("/analysis/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        summary: lastAnalysisSummary,
        context: userContext,
        client_time: clientTime,
      }),
    });
    if (!res.ok) {
      let errorMsg = `HTTP ${res.status}`;
      let errorCode;
      try {
        const errData = await res.json();
        errorMsg = errData.error || errorMsg;
        errorCode = errData.error_code || errData.code;
      } catch (err) {
        console.error("AI Analysis parse error:", err);
      }
      const error = new Error(errorMsg);
      if (errorCode) {
        error.code = errorCode;
      }
      throw error;
    }
    const data = await res.json();
    const content = data.analysis || "Aucune analyse disponible.";
    if (resultDiv) {
      resultDiv.innerHTML = `<pre style="white-space: pre-wrap; word-wrap: break-word;">${content}</pre>`;
    }
    if (promptDetails && data.prompt) {
      if (promptContent) {
        promptContent.textContent = data.prompt;
      }
      promptDetails.classList.remove("d-none");
    }
  } catch (err) {
    if (err && err.code === "OPENAI_API_KEY_MISSING") {
      const saved = await promptAndSaveOpenAiKey();
      if (saved) {
        return await getAiAnalysis();
      }
      if (resultDiv) {
        resultDiv.innerHTML =
          '<div class="alert alert-warning">Clé API OpenAI requise pour lancer l’analyse.</div>';
      }
      if (promptDetails) promptDetails.classList.add("d-none");
      if (summaryDetails) summaryDetails.classList.remove("d-none");
      return;
    }
    console.error("AI Analysis Error:", err);
    if (resultDiv) {
      resultDiv.innerHTML = `<div class="alert alert-danger">Erreur lors de l'analyse : ${err.message}</div>`;
    }
    if (promptDetails) promptDetails.classList.add("d-none");
  } finally {
    if (spinner) spinner.classList.add("d-none");
    if (btn) btn.disabled = false;
  }
}

function applyPIDWater() {}

function applyPIDRes() {}

function onAutoFanToggle() {
  const auto = document.getElementById("autoFanChk").checked;
  apiAction("auto_fan", { auto });
}

function applyAutocool() {
  const th = parseFloat(document.getElementById("auto_thresh").value || "28");
  apiAction("set_autocool", { thresh: th });
}

function onProtectToggle() {
  const enable = document.getElementById("protectChk").checked;
  apiAction("protect", { enable });
}

function applyServo() {
  const angle = parseInt(
    document.getElementById("servoAngle").value || "0",
    10
  );
  apiAction("servo", { angle });
}

function dispenseMacro() {
  apiAction("dispense", {});
}

function onMtrAutoChanged() {
  const enable = document.getElementById("mtrAutoChk").checked;
  apiAction("mtr_auto_off", { enable });
}

async function pumpGo(axis) {
  const cfg = currentPumpConfig[axis];
  if (!cfg) {
    alert("Configuration pompe manquante.");
    return;
  }
  const volume = parseFloat(cfg.volume_ml || 0);
  if (!(volume > 0)) {
    alert("Volume invalide.");
    return;
  }
  const STEPS_PER_ML = 5000;
  const steps = Math.max(1, Math.round(volume * STEPS_PER_ML));
  const backwards = cfg.direction < 0;
  await apiAction("set_steps_speed", { steps, speed: globalSpeedUs });
  await apiAction("pump", { axis, backwards });
}

async function savePumpSchedule(axis) {
  const input = document.getElementById(`pumpSchedule_${axis}`);
  const time = input ? input.value || null : null;
  await apiAction("set_peristaltic_schedule", { axis, time });
  refreshState();
}

async function runPumpSchedule(axis) {
  await apiAction("peristaltic_cycle", { axis, reason: "manual_cycle" });
  refreshState();
}

function applyGlobalSpeed() {
  const value = parseInt(
    document.getElementById("globalSpeedInput").value || "0",
    10
  );
  if (!(value > 0)) {
    alert("µs/step invalide");
    return;
  }
  globalSpeedUs = value;
  apiAction("set_global_speed", { speed: value });
}

function enablePumpNameEdit(axis) {
  const el = document.getElementById(`pumpName_${axis}`);
  if (!el) return;
  el.removeAttribute("disabled");
  el.dataset.editing = "1";
  el.focus();
  el.select();
}

async function savePumpConfig(axis) {
  const nameEl = document.getElementById(`pumpName_${axis}`);
  const volumeEl = document.getElementById(`pumpVolume_${axis}`);
  const dirEl = document.getElementById(`pumpDir_${axis}`);
  const name = nameEl ? nameEl.value.trim() : undefined;
  const volume_ml = parseFloat(volumeEl?.value || "0");
  const direction = parseInt(dirEl?.value || "1", 10);
  if (!(volume_ml > 0)) {
    alert("Volume invalide");
    return;
  }
  await apiAction("update_pump_config", { axis, name, volume_ml, direction });
  if (nameEl) {
    nameEl.setAttribute("disabled", "disabled");
    nameEl.dataset.editing = "0";
  }
  refreshState();
}

async function saveLightSchedule(day) {
  if (!day) return;
  const payload = { day };
  const onInput = document.getElementById(`light_${day}_on`);
  const offInput = document.getElementById(`light_${day}_off`);
  if (onInput) payload.on = onInput.value || null;
  if (offInput) payload.off = offInput.value || null;
  await apiAction("update_light_schedule", payload);
  refreshState();
}

async function toggleLight(forceState) {
  const params = {};
  if (typeof forceState === "boolean") {
    params.state = forceState;
  } else {
    params.state = !currentLightState;
  }
  await apiAction("light_toggle", params);
  refreshState();
}

async function setLightAuto(enable) {
  await apiAction("light_auto", { enable });
  refreshState();
}

async function setHeatMode(auto) {
  await apiAction("heat_mode", { auto });
  refreshState();
}

async function toggleHeatPower() {
  const enable = !currentHeatEnabled;
  await apiAction("heat_power", { enable });
  refreshState();
}

async function refreshState() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) {
      throw new Error("HTTP " + res.status);
    }
    const s = await res.json();
    applyStateToUI(s);
  } catch (err) {
    console.error("refreshState", err);
  }
  nextRefreshAt = Date.now() + refreshIntervalMs;
  resetRefreshLoader();
}

function applyStateToUI(state) {
  const badge = document.getElementById("statusBadge");
  badge.textContent = state.status || "";
  badge.classList.toggle("bg-success", !!state.connected);
  badge.classList.toggle("bg-danger", !state.connected);

  const errBox = document.getElementById("megaError");
  if (state.mega_error && (state.mega_error.message || state.mega_error.code)) {
    const raw = state.mega_error.raw ? ` (${state.mega_error.raw})` : "";
    const code = state.mega_error.code ? `[${state.mega_error.code}] ` : "";
    errBox.textContent = code + (state.mega_error.message || "") + raw;
    errBox.classList.remove("d-none");
  } else {
    errBox.textContent = "";
    errBox.classList.add("d-none");
  }

  document.getElementById("temp1_val").textContent = `${state.temp_1 || "--.-"}°C`;
  document.getElementById("temp2_val").textContent = `${state.temp_2 || "--.-"}°C`;
  document.getElementById("temp3_val").textContent = `${state.temp_3 || "--.-"}°C`;
  document.getElementById("temp4_val").textContent = `${state.temp_4 || "--.-"}°C`;
  const phV = state.ph_v ?? state.phV ?? null;
  const phRaw = state.ph_raw ?? state.phRaw ?? null;
  document.getElementById("ph_v_val").textContent =
    phV !== null && phV !== undefined ? `${phV} V` : "--.- V";
  document.getElementById("ph_raw_val").textContent =
    phRaw !== null && phRaw !== undefined ? phRaw : "----";
  const phVal = state.ph ?? null;
  document.getElementById("ph_val").textContent =
    phVal !== null && phVal !== undefined ? phVal : "--.-";
  const tempNames = state.temp_names || {};
  const tname = (k, d) => tempNames[k] || d;
  const mirrorLabel = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  mirrorLabel("temp1_label", tname("temp_1", "Temp 1"));
  mirrorLabel("temp2_label", tname("temp_2", "Temp 2"));
  mirrorLabel("temp3_label", tname("temp_3", "Temp 3"));
  mirrorLabel("temp4_label", tname("temp_4", "Temp 4"));
  mirrorLabel("temp2_label2", tname("temp_2", "Temp 2"));
  mirrorLabel("temp4_label2", tname("temp_4", "Temp 4"));
  const mirrorVal = (id, val, suffix = "°C") => {
    const el = document.getElementById(id);
    if (el) el.textContent = `${val || "--.-"}${suffix}`;
  };
  mirrorVal("temp2_val2", state.temp_2);
  mirrorVal("temp4_val2", state.temp_4);
  const pumpLabel = document.getElementById("pumpStateLabel");
  const pumpBtn = document.getElementById("pumpToggleBtn");
  if (pumpLabel)
    pumpLabel.textContent = state.pump_state ? "Pompe OFF" : "Pompe On";
  if (pumpBtn) {
    // Relais OFF (pump_state=false) -> label "Pompe On", bouton "Arrêter"
    pumpBtn.textContent = state.pump_state ? "Démarrer" : "Arrêter";
    pumpBtn.classList.toggle("btn-danger", !state.pump_state);
    pumpBtn.classList.toggle("btn-outline-light", state.pump_state);
  }
  if (!inputsInitialized) {
    setInputValue("tempName_temp1", tname("temp_1", "Temp 1"));
    setInputValue("tempName_temp2", tname("temp_2", "Temp 2"));
    setInputValue("tempName_temp3", tname("temp_3", "Temp 3"));
    setInputValue("tempName_temp4", tname("temp_4", "Temp 4"));
    setInputValue("heatHyst", state.heat_hyst ?? 0.3);
    setInputValue("refreshInterval", (refreshIntervalMs / 1000).toString());
  }
  const heatTargets = state.heat_targets || {};
  document.getElementById("tset_water_label").textContent = `${
    heatTargets.temp_1 ?? state.tset_water ?? "--.-"
  }°C`;
  document.getElementById("tset_res_label").textContent = `${
    heatTargets.temp_2 ?? state.tset_res ?? "--.-"
  }°C`;
  if (!inputsInitialized) {
    setInputIfIdle("tset_water2", heatTargets.temp_1 ?? state.tset_water ?? "");
    setInputIfIdle("tset_res2", heatTargets.temp_2 ?? state.tset_res ?? "");
  }

  const lightLuxEl = document.getElementById("lightLuxValue");
  if (lightLuxEl) {
    const lux = state.light_lux;
    if (typeof lux === "number" && isFinite(lux)) {
      lightLuxEl.textContent = `${lux.toFixed(1)} lx`;
    } else {
      lightLuxEl.textContent = "-- lx";
    }
  }


  document.getElementById("autoFanChk").checked = !!state.auto_fan;
  document.getElementById("autoFanModeBadge").textContent = state.auto_fan
    ? "Auto"
    : "Manuel";
  document.getElementById("auto_thresh").value = state.auto_thresh ?? 28;
  document.getElementById("auto_thresh_label").textContent =
    state.auto_thresh ?? "--.-";
  currentFanOn = !!state.fan_on;
  const fanBtn = document.getElementById("fanToggleBtn");
  if (fanBtn) {
    fanBtn.textContent = currentFanOn ? "Arrêter" : "Allumer";
    fanBtn.classList.toggle("btn-danger", currentFanOn);
    fanBtn.classList.toggle("btn-outline-light", !currentFanOn);
    fanBtn.disabled = !!state.auto_fan;
  }

  updateHighLevelBadge(state.lvl_high);

  document.getElementById("protectChk").checked = !!state.protect;
  const protectBadge = document.getElementById("protectBadge");
  protectBadge.textContent = state.protect ? "PROTECT ON" : "PROTECT OFF";
  protectBadge.classList.toggle("badge-protect-on", !!state.protect);
  protectBadge.classList.toggle("badge-protect-off", !state.protect);

  currentPumpConfig = state.pump_config || {};
  bindPumpInfo("X", currentPumpConfig.X);
  bindPumpInfo("Y", currentPumpConfig.Y);
  bindPumpInfo("Z", currentPumpConfig.Z);
  bindPumpInfo("E", currentPumpConfig.E);
  const peristalticSchedule = state.peristaltic_schedule || {};
  ["X", "Y", "Z", "E"].forEach((axis) => {
    const input = document.getElementById(`pumpSchedule_${axis}`);
    if (!input) return;
    const entry = peristalticSchedule[axis] || {};
    const value = entry.time || "";
    if (!input.matches(":focus")) {
      input.value = value || "";
    }
  });
  const peristalticAutoToggle = document.getElementById("peristalticAutoToggle");
  if (peristalticAutoToggle) {
    peristalticAutoToggle.checked = !!state.peristaltic_auto;
  }

  const gsi = document.getElementById("globalSpeedInput");
  if (gsi) {
    const currentSpeed = state.global_speed ?? state.speed ?? globalSpeedUs;
    gsi.value = currentSpeed;
    globalSpeedUs = currentSpeed;
  }

  document.getElementById("mtrAutoChk").checked = !!state.mtr_auto_off;
  if (!inputsInitialized) {
    document.getElementById("servoAngle").value = state.servo_angle ?? 10;
  }

  currentLightState = !!state.light_state;
  currentLightAuto = !!state.light_auto;
  updateLightUI(state);

  currentHeatAuto = !!state.heat_auto;
  currentHeatEnabled = !!state.heat_enabled;
  updateHeatUI();

  // Feeder
  const incomingSchedule = Array.isArray(state.feeder_schedule)
    ? state.feeder_schedule.map((entry) => {
        const method = (entry?.method || "GET").toString().toUpperCase();
        const stopPump = !!entry?.stop_pump;
        const rawDuration = parseInt(
          entry?.pump_stop_duration_min ??
            (stopPump ? DEFAULT_FEEDER_PUMP_STOP_DURATION : 0),
          10
        );
        const duration =
          Number.isFinite(rawDuration) && rawDuration >= 0
            ? rawDuration
            : stopPump
            ? DEFAULT_FEEDER_PUMP_STOP_DURATION
            : 0;
        return {
          time: entry?.time || "",
          url: entry?.url || "",
          method: method === "POST" ? "POST" : "GET",
          stop_pump: stopPump,
          pump_stop_duration_min: duration,
        };
      })
    : [];
  const incomingJson = JSON.stringify(incomingSchedule);
  currentFeederAuto = !!state.feeder_auto;
  // Ne réécrit les inputs que si jamais initialisé ou si pas en édition locale
  if (!feederInitialized || (!feederDirty && incomingJson !== lastFeederScheduleJson)) {
    currentFeederSchedule = incomingSchedule;
    lastFeederScheduleJson = incomingJson;
    renderFeederSchedule();
    feederInitialized = true;
    feederDirty = false;
  }
  inputsInitialized = true;
}

function updateHighLevelBadge(val) {
  const el = document.getElementById("lvl_high");
  if (!el) return;
  let cls = "level-pill-unk";
  let textValue = "?";
  if (val === "1" || val === 1 || val === true) {
    cls = "level-pill-ok";
    textValue = "OK";
  } else if (val === "0" || val === 0 || val === false) {
    cls = "level-pill-bad";
    textValue = "BAS";
  }
  el.textContent = `Niveau haut: ${textValue}`;
  el.classList.remove("level-pill-ok", "level-pill-bad", "level-pill-unk");
  el.classList.add(cls);
}

function bindPumpInfo(axis, cfg) {
  const label = document.getElementById(`pumpLabel_${axis}`);
  const info = document.getElementById(`pumpInfo_${axis}`);
  const nameInput = document.getElementById(`pumpName_${axis}`);
  const volInput = document.getElementById(`pumpVolume_${axis}`);
  const dirSelect = document.getElementById(`pumpDir_${axis}`);
  if (!cfg) return;
  if (label) label.textContent = cfg.name || `Pompe ${axis}`;
  if (info) {
    const dirLabel = cfg.direction >= 0 ? "avant (+)" : "arrière (-)";
    info.textContent = `${cfg.volume_ml ?? "-"} mL — sens ${dirLabel}`;
  }
  if (nameInput && nameInput.dataset.editing !== "1") {
    nameInput.value = cfg.name || "";
  }
  if (volInput && !volInput.matches(":focus")) {
    volInput.value = cfg.volume_ml ?? "";
  }
  if (dirSelect && !dirSelect.matches(":focus")) {
    dirSelect.value = cfg.direction ?? 1;
  }
}

function updateLightUI(state) {
  const stateLabel = document.getElementById("lightStateLabel");
  const toggleBtn = document.getElementById("lightToggleBtn");
  if (stateLabel) {
    stateLabel.textContent = currentLightState ? "Allumée" : "Éteinte";
  }
  if (toggleBtn) {
    toggleBtn.textContent = currentLightState ? "Éteindre" : "Allumer";
    toggleBtn.classList.toggle("btn-danger", currentLightState);
    toggleBtn.classList.toggle("btn-outline-light", !currentLightState);
  }
  const slider = document.getElementById("lightAutoSlider");
  const label = document.getElementById("lightAutoLabel");
  if (slider) slider.value = currentLightAuto ? "1" : "0";
  if (label) label.textContent = currentLightAuto ? "Automatique" : "Manuel";

  const schedule = state.light_schedule || {};
  LIGHT_DAY_KEYS.forEach((day) => {
    const entry = schedule[day] || {};
    setInputValue(`light_${day}_on`, entry.on);
    setInputValue(`light_${day}_off`, entry.off);
  });
}

function setInputValue(id, value) {
  const input = document.getElementById(id);
  if (inputsInitialized) return;
  if (input) {
    input.value = value ?? "";
  }
}

function updateHeatUI() {
  const modeSwitch = document.getElementById("heatModeSwitch");
  if (modeSwitch) {
    modeSwitch.checked = currentHeatAuto;
  }
  const status = document.getElementById("heatStatusLabel");
  if (status) {
    if (currentHeatAuto) {
      status.textContent = "Mode automatique";
    } else if (currentHeatEnabled) {
      status.textContent = "Manuel — Chauffage ON";
    } else {
      status.textContent = "Manuel — Chauffage OFF";
    }
  }
  const btn = document.getElementById("heatPowerBtn");
  if (btn) {
    btn.textContent = currentHeatEnabled ? "Éteindre" : "Allumer";
    btn.disabled = currentHeatAuto;
    btn.classList.toggle("btn-danger", currentHeatEnabled && !currentHeatAuto);
    btn.classList.toggle(
      "btn-outline-light",
      !currentHeatEnabled || currentHeatAuto
    );
  }
}

function setInputIfIdle(id, value) {
  const input = document.getElementById(id);
  if (inputsInitialized) return;
  if (input && !input.matches(":focus")) {
    input.value = value ?? "";
  }
}

function setInputValue(id, value) {
  const input = document.getElementById(id);
  if (inputsInitialized) return;
  if (input) {
    input.value = value ?? "";
  }
}

function updateRefreshLoader() {
  const bar = document.getElementById("refreshProgress");
  if (!bar || !nextRefreshAt || !refreshIntervalMs) return;
  const now = Date.now();
  const remaining = Math.max(0, nextRefreshAt - now);
  const pct = Math.min(100, Math.max(0, 100 - (remaining / refreshIntervalMs) * 100));
  bar.style.width = `${pct.toFixed(1)}%`;
}

function resetRefreshLoader() {
  nextRefreshAt = Date.now() + refreshIntervalMs;
  updateRefreshLoader();
  if (loaderTimer) {
    clearInterval(loaderTimer);
  }
  loaderTimer = setInterval(updateRefreshLoader, 200);
}

async function saveTempNames() {
  const payload = {
    temp_1: document.getElementById("tempName_temp1")?.value || "",
    temp_2: document.getElementById("tempName_temp2")?.value || "",
    temp_3: document.getElementById("tempName_temp3")?.value || "",
    temp_4: document.getElementById("tempName_temp4")?.value || "",
  };
  await apiAction("update_temp_names", payload);
  refreshState();
}

async function togglePump(forceState) {
  const params = {};
  if (typeof forceState === "boolean") {
    params.state = forceState;
  }
  await apiAction("toggle_pump", params);
  refreshState();
}

async function toggleFanManual(forceState) {
  const params = {};
  if (typeof forceState === "boolean") {
    params.value = forceState ? 1 : 0;
  } else {
    params.value = currentFanOn ? 0 : 1;
  }
  await apiAction("fan_manual", params);
  refreshState();
}

async function applyHeatHyst() {
  const val = parseFloat(document.getElementById("heatHyst")?.value || "");
  if (!isFinite(val) || val < 0) {
    alert("Hystérésis invalide");
    return;
  }
  await apiAction("set_heat_hyst", { value: val });
  refreshState();
}

function applyRefreshInterval() {
  const val = parseFloat(
    document.getElementById("refreshInterval")?.value || ""
  );
  if (!isFinite(val) || val < 0.5) {
    alert("Intervalle invalide (min 0.5s)");
    return;
  }
  refreshIntervalMs = val * 1000;
  restartRefreshTimer();
}

function renderFeederSchedule() {
  const body = document.getElementById("feederTableBody");
  if (!body) return;
  body.innerHTML = "";
  const autoToggle = document.getElementById("feederAutoToggle");
  if (autoToggle) autoToggle.checked = !!currentFeederAuto;
  const rows = currentFeederSchedule.length ? currentFeederSchedule : [];
  if (rows.length === 0) {
    addFeederRow();
  } else {
    rows.forEach((row, idx) => {
      const stopPump = !!row?.stop_pump;
      const duration =
        typeof row?.pump_stop_duration_min === "number"
          ? row.pump_stop_duration_min
          : parseInt(row?.pump_stop_duration_min || "0", 10);
      addFeederRow(
        row.time || "",
        row.url || "",
        row.method || "GET",
        stopPump,
        Number.isFinite(duration) ? duration : stopPump ? DEFAULT_FEEDER_PUMP_STOP_DURATION : 0,
        idx
      );
    });
  }
}

function addFeederRow(
  timeVal = "",
  urlVal = "",
  methodVal = "GET",
  stopPumpVal = false,
  durationVal = stopPumpVal ? DEFAULT_FEEDER_PUMP_STOP_DURATION : 0,
  idx = null
) {
  const body = document.getElementById("feederTableBody");
  if (!body) return;
  const tr = document.createElement("tr");
  const methodClean = (methodVal || "GET").toString().toUpperCase();
  const stopCheckedAttr = stopPumpVal ? "checked" : "";
  const durationSafe =
    Number.isFinite(durationVal) && durationVal >= 0
      ? durationVal
      : stopPumpVal
      ? DEFAULT_FEEDER_PUMP_STOP_DURATION
      : 0;
  const durationDisabledAttr = stopPumpVal ? "" : "disabled";
  tr.innerHTML = `
    <td><input type="time" class="form-control form-control-sm feeder-time" value="${timeVal}"></td>
    <td>
      <select class="form-select form-select-sm feeder-method">
        <option value="GET"${methodClean === "GET" ? " selected" : ""}>GET</option>
        <option value="POST"${methodClean === "POST" ? " selected" : ""}>POST</option>
      </select>
    </td>
    <td><input type="text" class="form-control form-control-sm feeder-url" placeholder="http://..." value="${urlVal}"></td>
    <td>
      <div class="form-check form-switch form-switch-sm mb-1">
        <input class="form-check-input feeder-stop-pump" type="checkbox" role="switch" ${stopCheckedAttr}>
        <label class="form-check-label small">Arrêt pompe</label>
      </div>
      <div class="input-group input-group-sm">
        <span class="input-group-text">Durée</span>
        <input type="number" min="0" step="1" class="form-control form-control-sm feeder-stop-duration" value="${durationSafe}" ${durationDisabledAttr}>
        <span class="input-group-text">min</span>
      </div>
    </td>
    <td class="text-end">
      <div class="btn-group btn-group-sm" role="group">
        <button class="btn btn-outline-primary feeder-run">Lancer</button>
        <button class="btn btn-outline-danger feeder-del">Supprimer</button>
      </div>
    </td>
  `;
  tr.querySelector(".feeder-del").addEventListener("click", (e) => {
    e.preventDefault();
    tr.remove();
    feederDirty = true;
  });
  tr.querySelector(".feeder-run").addEventListener("click", async (e) => {
    e.preventDefault();
    const url = tr.querySelector(".feeder-url")?.value || "";
    const method = (
      tr.querySelector(".feeder-method")?.value || "GET"
    ).toString().toUpperCase();
    if (!url) {
      alert("URL manquante");
      return;
    }
    const stopPump = !!tr.querySelector(".feeder-stop-pump")?.checked;
    let duration = parseInt(
      tr.querySelector(".feeder-stop-duration")?.value ?? "0",
      10
    );
    if (!Number.isFinite(duration) || duration < 0) {
      duration = stopPump ? DEFAULT_FEEDER_PUMP_STOP_DURATION : 0;
    }
    if (!stopPump) duration = 0;
    await apiAction("trigger_feeder_url", {
      url,
      method,
      stop_pump: stopPump,
      pump_stop_duration_min: duration,
    });
  });
  const stopCheckbox = tr.querySelector(".feeder-stop-pump");
  const durationInput = tr.querySelector(".feeder-stop-duration");
  if (stopCheckbox && durationInput) {
    const syncDurationState = () => {
      const enabled = stopCheckbox.checked;
      durationInput.disabled = !enabled;
      if (!enabled) {
        durationInput.classList.add("bg-body-tertiary");
      } else {
        durationInput.classList.remove("bg-body-tertiary");
        if (!durationInput.value || Number(durationInput.value) <= 0) {
          durationInput.value = DEFAULT_FEEDER_PUMP_STOP_DURATION;
        }
      }
    };
    stopCheckbox.addEventListener("change", () => {
      syncDurationState();
      feederDirty = true;
    });
    durationInput.addEventListener("input", () => {
      const parsed = parseInt(durationInput.value || "0", 10);
      if (!Number.isFinite(parsed) || parsed < 0) {
        durationInput.value = "";
      }
    });
    syncDurationState();
  }
  body.appendChild(tr);
}

function collectFeederEntries() {
  const body = document.getElementById("feederTableBody");
  if (!body) return [];
  const rows = Array.from(body.querySelectorAll("tr"));
  return rows
    .map((tr) => {
      const time = tr.querySelector(".feeder-time")?.value || "";
      const url = tr.querySelector(".feeder-url")?.value || "";
      const method = (tr.querySelector(".feeder-method")?.value || "GET")
        .toString()
        .toUpperCase();
      const stopPump = !!tr.querySelector(".feeder-stop-pump")?.checked;
      let duration = parseInt(
        tr.querySelector(".feeder-stop-duration")?.value ?? "0",
        10
      );
      if (!Number.isFinite(duration) || duration < 0) {
        duration = stopPump ? DEFAULT_FEEDER_PUMP_STOP_DURATION : 0;
      }
      if (!stopPump) {
        duration = 0;
      }
      return {
        time,
        url,
        method,
        stop_pump: stopPump,
        pump_stop_duration_min: duration,
      };
    })
    .filter((e) => e.time && e.url);
}

async function saveFeederSchedule() {
  const entries = collectFeederEntries();
  await apiAction("set_feeder_schedule", { entries });
  currentFeederSchedule = entries;
  lastFeederScheduleJson = JSON.stringify(entries);
  renderFeederSchedule();
}

const clickHandlers = {
  refreshPorts: () => refreshPorts(),
  connect: () => connect(),
  disconnect: () => disconnect(),
  readTemps: () => apiAction("read_temps"),
  readLevels: () => apiAction("read_levels"),
  applyAutocool: () => applyAutocool(),
  applyServo: () => applyServo(),
  dispenseMacro: () => dispenseMacro(),
  motorRaw: (el) => {
    const cmd = el.dataset.cmd;
    if (cmd) apiAction("raw", { cmd });
  },
  emergencyStop: () => apiAction("emergency_stop"),
  pumpGo: (el) => pumpGo(el.dataset.axis),
  savePumpSchedule: (el) => savePumpSchedule(el.dataset.axis),
  runPumpSchedule: (el) => runPumpSchedule(el.dataset.axis),
  applyWater: () => applyWater(),
  applyRes: () => applyRes(),
  submitWaterQuality: () => submitWaterQuality(),
  applyGlobalSpeed: () => applyGlobalSpeed(),
  editPumpName: (el) => enablePumpNameEdit(el.dataset.axis),
  pumpSave: (el) => savePumpConfig(el.dataset.axis),
  saveLightSchedule: (el) => saveLightSchedule(el.dataset.day),
  toggleLight: () => toggleLight(),
  heatPower: () => toggleHeatPower(),
  fanToggle: () => toggleFanManual(),
  togglePump: () => togglePump(),
  saveTempNames: () => saveTempNames(),
  applyRefreshInterval: () => applyRefreshInterval(),
  applyHeatHyst: () => applyHeatHyst(),
  addFeederRow: () => addFeederRow(),
  saveFeederSchedule: () => saveFeederSchedule(),
  prepareAiAnalysis: () => prepareAiAnalysis(),
  get_ai_analysis: () => getAiAnalysis(),
};

const changeHandlers = {
  autoFanToggle: () => onAutoFanToggle(),
  protectToggle: () => onProtectToggle(),
  mtrAutoToggle: () => onMtrAutoChanged(),
  lightAuto: (target) => setLightAuto(target.value === "1"),
  heatMode: (target) => setHeatMode(target.checked),
};

function initDelegates() {
  document.addEventListener("click", (event) => {
    const target =
      event.target instanceof Element
        ? event.target.closest("[data-action]")
        : null;
    if (!target) return;
    const handler = clickHandlers[target.dataset.action];
    if (!handler) return;
    event.preventDefault();
    handler(target);
  });

  document.addEventListener("change", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const action = target.dataset.change;
    if (!action) return;
    const handler = changeHandlers[action];
    if (!handler) return;
    handler(target);
  });

  const addFeederBtn = document.getElementById("addFeederRowBtn");
  if (addFeederBtn) {
    addFeederBtn.addEventListener("click", (e) => {
      e.preventDefault();
      addFeederRow();
      feederDirty = true;
    });
  }
  const saveFeederBtn = document.getElementById("saveFeederBtn");
  if (saveFeederBtn) {
    saveFeederBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      await saveFeederSchedule();
      feederDirty = false;
    });
  }
  const feederAutoToggle = document.getElementById("feederAutoToggle");
  if (feederAutoToggle) {
    feederAutoToggle.addEventListener("change", async (e) => {
      await apiAction("set_feeder_auto", { enable: e.target.checked });
      currentFeederAuto = e.target.checked;
      feederDirty = true;
    });
  }

  const peristalticAutoToggle = document.getElementById("peristalticAutoToggle");
  if (peristalticAutoToggle) {
    peristalticAutoToggle.addEventListener("change", async (e) => {
      await apiAction("set_peristaltic_auto", { enable: e.target.checked });
    });
  }

  const feederBody = document.getElementById("feederTableBody");
  if (feederBody) {
    feederBody.addEventListener("input", () => {
      feederDirty = true;
    });
    feederBody.addEventListener("change", () => {
      feederDirty = true;
    });
  }

  toastContainer = document.getElementById("toastContainer");
}

function init() {
  initDelegates();
  refreshPorts();
  refreshState();
  nextRefreshAt = Date.now() + refreshIntervalMs;
  resetRefreshLoader();
  refreshTimer = setInterval(() => {
    refreshState();
    nextRefreshAt = Date.now() + refreshIntervalMs;
  }, refreshIntervalMs);
}

document.addEventListener("DOMContentLoaded", init);

function showToast(message, type = "info", delay = 3000) {
  if (!toastContainer) return;
  const toastEl = document.createElement("div");
  toastEl.className = "toast align-items-center text-bg-" + type + " border-0";
  toastEl.setAttribute("role", "alert");
  toastEl.setAttribute("aria-live", "assertive");
  toastEl.setAttribute("aria-atomic", "true");
  toastEl.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${message}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
    </div>
  `;
  toastContainer.appendChild(toastEl);
  const toast = new bootstrap.Toast(toastEl, { delay, autohide: true });
  toast.show();
  toastEl.addEventListener("hidden.bs.toast", () => {
    toastEl.remove();
  });
}
