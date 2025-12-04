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
const LAST_ACTIVE_TAB_KEY = "reef_active_tab";
const LAST_CAMERA_SUBTAB_KEY = "reef_camera_subtab";
const PH_CAL_REFERENCES = ["4.01", "6.86", "9.18"];

let refreshTimer = null;
let currentPumpConfig = {};
let currentLightState = false;
let currentLightAuto = true;
let globalSpeedUs = 400;
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
let popinResolver = null;
let popinIsConfirm = false;
let cameraSettings = null;
const GALLERY_DEFAULT_PER_PAGE = 30;
const galleryState = {
  photos: { page: 1, perPage: GALLERY_DEFAULT_PER_PAGE, totalPages: 1, sort: "desc" },
  videos: { page: 1, perPage: GALLERY_DEFAULT_PER_PAGE, totalPages: 1, sort: "desc" },
};
let mediaViewerEl = null;
let mediaViewerContentEl = null;
let cameraLiveImageEl = null;
let cameraLiveOverlayEl = null;
let logbookEntriesCache = [];
let logbookEmptyText = "";
const LIVESTOCK_CATEGORY_LABELS = { animal: "Animal", plant: "Vegetal" };
const LIVESTOCK_COLUMN_COUNT = 6;
const LIVESTOCK_PARAM_FIELDS = [
  { key: "ph", label: "pH", step: "0.1" },
  { key: "kh", label: "KH", step: "0.5" },
  { key: "gh", label: "GH", step: "0.5" },
  { key: "temperature", label: "Température (°C)", step: "0.5" },
];
const livestockCatalogState = { animals: [], plants: [] };
let livestockEditContext = null;
const livestockPreviewUrls = new Map();
let esp32CamConfig = { url: "" };
let esp32CamSettings = null;
let esp32CamPreviewUrl = null;
let esp32CamCapturePending = false;
const DEFAULT_PHOTO_LABEL_CATEGORIES = ["Plante", "Produit", "Poisson"];
const photoLabelsState = {
  categories: [...DEFAULT_PHOTO_LABEL_CATEGORIES],
  labels: {},
  loaded: false,
  loadingPromise: null,
};
const waterTargetsState = {
  metrics: [],
  metricMap: new Map(),
  dom: {},
  loading: false,
  error: null,
  fishCount: 0,
};
const ANALYSIS_PERIOD_LABELS = {
  last_3_days: "0 à -3 jours",
  last_week: "-3 à -7 jours",
  last_month: "-7 jours à -1 mois",
  last_year: "-1 mois à -1 an",
};
const AI_IMAGE_SELECTION_LIMIT = 5;
let aiConfigState = null;
let aiSummaryState = null;
let aiInsightsState = [];
let aiSelectedPhotos = new Set();
let aiPhotoPool = [];
let aiWorkerStatusState = null;
let aiWorkerStatusTimer = null;


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
    showPopin("Sélectionnez un port série.", "warning");
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
  apiAction("submit_water_quality", params)
    .then(() => loadWaterTargets(true))
    .catch(() => {});
}


function waterTargetsContainer() {
  return document.getElementById("waterTargetsContent");
}

function setWaterTargetsMessage(message, tone = "secondary") {
  const container = waterTargetsContainer();
  if (!container) return;
  container.innerHTML = `<div class="text-${tone} small">${message}</div>`;
}

function formatWaterUnit(metric) {
  if (!metric || !metric.unit) return "";
  if (metric.unit_prefix === "deg") {
    return `°${metric.unit}`;
  }
  return metric.unit;
}

function formatWaterValue(value, metric) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "--";
  }
  const decimals = typeof metric.decimals === "number" ? metric.decimals : 1;
  const formatted = Number(value).toFixed(decimals);
  const unit = formatWaterUnit(metric);
  return unit ? `${formatted} ${unit}` : formatted;
}

function waterMetricPercent(metric, value) {
  if (value === null || value === undefined) return null;
  const min = Number(metric.scale_min);
  const max = Number(metric.scale_max);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return null;
  const percent = ((Number(value) - min) / (max - min)) * 100;
  return Math.max(0, Math.min(100, percent));
}

async function loadWaterTargets(force = false) {
  if (waterTargetsState.loading && !force) {
    return;
  }
  const container = waterTargetsContainer();
  if (!container) return;
  if (!waterTargetsState.metrics.length || force) {
    setWaterTargetsMessage(
      force ? "Actualisation des paramètres en cours..." : "Chargement des paramètres...",
      "secondary"
    );
  }
  waterTargetsState.loading = true;
  try {
    const res = await fetch("/api/water/targets");
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const metrics = Array.isArray(data.metrics) ? data.metrics : [];
    waterTargetsState.metrics = metrics;
    waterTargetsState.metricMap = new Map(metrics.map((m) => [m.key, m]));
    waterTargetsState.fishCount = data.fish_count || 0;
    waterTargetsState.error = null;
    renderWaterTargets();
  } catch (err) {
    console.error("loadWaterTargets", err);
    waterTargetsState.error = err.message;
    setWaterTargetsMessage(`Impossible de charger les cibles: ${err.message}`, "danger");
  } finally {
    waterTargetsState.loading = false;
  }
}

function renderWaterTargets() {
  const container = waterTargetsContainer();
  if (!container) return;
  container.innerHTML = "";
  waterTargetsState.dom = {};
  const metrics = waterTargetsState.metrics || [];
  if (!metrics.length) {
    const message =
      waterTargetsState.fishCount > 0
        ? "Aucun intervalle exploitable pour le moment."
        : "Ajoutez des poissons actifs pour calculer les cibles.";
    setWaterTargetsMessage(message, "secondary");
    return;
  }
  metrics.forEach((metric) => {
    const row = buildWaterTargetRow(metric);
    container.appendChild(row);
    updateWaterTargetCurrentView(metric);
  });
}

function buildWaterTargetRow(metric) {
  const row = document.createElement("div");
  row.className = "water-target-row";

  const label = document.createElement("div");
  label.className = "water-target-label";
  label.textContent = metric.label || metric.key;
  row.appendChild(label);

  const content = document.createElement("div");
  content.className = "water-target-body";

  const bar = document.createElement("div");
  bar.className = `water-target-bar water-target-bar-${metric.key}`;

  if (metric.comfort_min !== null && metric.comfort_min !== undefined && metric.comfort_max !== null && metric.comfort_max !== undefined) {
    const minPercent = waterMetricPercent(metric, metric.comfort_min);
    const maxPercent = waterMetricPercent(metric, metric.comfort_max);
    if (minPercent !== null && maxPercent !== null) {
      const comfort = document.createElement("div");
      comfort.className = "water-target-comfort";
      const left = Math.min(minPercent, maxPercent);
      const width = Math.abs(maxPercent - minPercent);
      comfort.style.left = `${left}%`;
      comfort.style.width = `${Math.max(0.5, width)}%`;
      bar.appendChild(comfort);
    }
  }

  const minMarker = appendWaterMarker(
    bar,
    "water-target-marker marker-extreme",
    waterMetricPercent(metric, metric.min),
    metric.min !== null && metric.min !== undefined ? `Minimum global: ${formatWaterValue(metric.min, metric)}` : ""
  );
  const maxMarker = appendWaterMarker(
    bar,
    "water-target-marker marker-extreme",
    waterMetricPercent(metric, metric.max),
    metric.max !== null && metric.max !== undefined ? `Maximum global: ${formatWaterValue(metric.max, metric)}` : ""
  );
  const comfortMinMarker = appendWaterMarker(
    bar,
    "water-target-marker marker-comfort",
    waterMetricPercent(metric, metric.comfort_min),
    metric.comfort_min !== null && metric.comfort_min !== undefined
      ? `Zone confort (min): ${formatWaterValue(metric.comfort_min, metric)}`
      : ""
  );
  const comfortMaxMarker = appendWaterMarker(
    bar,
    "water-target-marker marker-comfort",
    waterMetricPercent(metric, metric.comfort_max),
    metric.comfort_max !== null && metric.comfort_max !== undefined
      ? `Zone confort (max): ${formatWaterValue(metric.comfort_max, metric)}`
      : ""
  );
  const currentMarker = appendWaterMarker(
    bar,
    "water-target-marker marker-current",
    waterMetricPercent(metric, metric.current),
    metric.current !== null && metric.current !== undefined ? `Valeur actuelle: ${formatWaterValue(metric.current, metric)}` : ""
  );

  content.appendChild(bar);

  const legend = document.createElement("div");
  legend.className = "water-target-legend";
  const legendItems = [
    { label: "Min", value: metric.min },
    { label: "Confort", value: metric.comfort_min !== null && metric.comfort_max !== null ? `${formatWaterValue(metric.comfort_min, metric)} – ${formatWaterValue(metric.comfort_max, metric)}` : "--" },
    { label: "Max", value: metric.max },
  ];
  legendItems.forEach((item) => {
    const block = document.createElement("div");
    block.className = "water-target-legend-item";
    const spanLabel = document.createElement("span");
    spanLabel.className = "legend-label";
    spanLabel.textContent = `${item.label}: `;
    const spanValue = document.createElement("span");
    spanValue.className = "legend-value";
    spanValue.textContent =
      typeof item.value === "string" ? item.value : formatWaterValue(item.value, metric);
    block.appendChild(spanLabel);
    block.appendChild(spanValue);
    legend.appendChild(block);
  });
  const currentBlock = document.createElement("div");
  currentBlock.className = "water-target-legend-item";
  const currentLabel = document.createElement("span");
  currentLabel.className = "legend-label";
  currentLabel.textContent = "Actuel: ";
  const currentValueEl = document.createElement("span");
  currentValueEl.className = "legend-value";
  currentValueEl.textContent = formatWaterValue(metric.current, metric);
  currentBlock.appendChild(currentLabel);
  currentBlock.appendChild(currentValueEl);
  legend.appendChild(currentBlock);

  content.appendChild(legend);
  row.appendChild(content);

  waterTargetsState.dom[metric.key] = {
    currentMarker,
    currentValue: currentValueEl,
    minMarker,
    maxMarker,
    comfortMinMarker,
    comfortMaxMarker,
  };
  return row;
}

function appendWaterMarker(bar, className, percent, title) {
  const marker = document.createElement("div");
  marker.className = className;
  if (percent === null || percent === undefined) {
    marker.classList.add("d-none");
  } else {
    marker.style.left = `${percent}%`;
  }
  if (title) {
    marker.title = title;
  }
  bar.appendChild(marker);
  return marker;
}

function updateWaterTargetCurrentView(metric) {
  const dom = waterTargetsState.dom[metric.key];
  if (!dom) return;
  const percent = waterMetricPercent(metric, metric.current);
  if (percent === null || percent === undefined) {
    dom.currentMarker.classList.add("d-none");
  } else {
    dom.currentMarker.classList.remove("d-none");
    dom.currentMarker.style.left = `${percent}%`;
    dom.currentMarker.title = `Valeur actuelle: ${formatWaterValue(metric.current, metric)}`;
  }
  if (dom.currentValue) {
    dom.currentValue.textContent = formatWaterValue(metric.current, metric);
  }
}

function setWaterTargetCurrentValue(key, value) {
  if (value === null || value === undefined) return;
  if (!Number.isFinite(Number(value))) return;
  const metric = waterTargetsState.metricMap.get(key);
  if (!metric) return;
  metric.current = Number(value);
  updateWaterTargetCurrentView(metric);
}

function readTemperatureFromState(state) {
  const keys = ["temp_2", "temp_1", "temp_3", "temp_4"];
  for (const key of keys) {
    const val = Number(state[key]);
    if (Number.isFinite(val)) {
      return val;
    }
  }
  return null;
}

function updateWaterTargetsCurrent(state) {
  if (!waterTargetsState.metricMap.size) return;
  if (state && state.ph !== undefined) {
    const phValue = Number(state.ph);
    if (Number.isFinite(phValue)) {
      setWaterTargetCurrentValue("ph", phValue);
    }
  }
  const tempValue = state ? readTemperatureFromState(state) : null;
  if (tempValue !== null) {
    setWaterTargetCurrentValue("temperature", tempValue);
  }
}

async function handlePhCalibration(el) {
  const ref = el?.dataset?.ref;
  if (!ref) return;
  const confirmed = await showPopin(
    `Plongez la sonde dans la solution pH ${ref}, attendez 30 secondes puis confirmez.`,
    "info",
    { confirmable: true, confirmText: `Calibrer ${ref}`, cancelText: "Annuler" }
  );
  if (!confirmed) return;
  await apiAction("ph_calibrate", { reference: ref });
  refreshState();
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
    showToast(
      `Erreur lors de l'enregistrement de la clé : ${err.message}`,
      "danger"
    );
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
      const ta =
        a[1] && a[1].earliest_time
          ? new Date(a[1].earliest_time).getTime()
          : Number.NEGATIVE_INFINITY;
      const tb =
        b[1] && b[1].earliest_time
          ? new Date(b[1].earliest_time).getTime()
          : Number.NEGATIVE_INFINITY;
      return tb - ta; // dates les plus récentes en haut, les plus anciennes en bas
    });
    const earliestLines = sortedPeriods
      .map(([key, value]) => {
        if (!value || !value.earliest_time) {
          return `${ANALYSIS_PERIOD_LABELS[key] || key}: aucune donnée`;
        }
        const date = new Date(value.earliest_time);
        return `${
          ANALYSIS_PERIOD_LABELS[key] || key
        }: ${date.toLocaleString()}`;
      })
      .join("<br>");
    if (resultDiv) {
      resultDiv.innerHTML = `
        <div>Données préparées pour ${
          sortedPeriods
            .map(([key]) => ANALYSIS_PERIOD_LABELS[key] || key)
            .join(", ") || "les périodes demandées"
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
    showToast(
      "Préparez d'abord les données avant d'interroger l'IA.",
      "warning"
    );
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

async function loadAiConfig() {
  try {
    const res = await fetch("/api/ai/config");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Reponse invalide");
    }
    applyAiConfigToForm(data.config || {});
  } catch (err) {
    console.error("loadAiConfig", err);
    showToast(`Configuration IA: ${err.message}`, "danger");
  }
}

function applyAiConfigToForm(config = {}) {
  aiConfigState = config;
  const modeSelect = document.getElementById("aiModeSelect");
  if (modeSelect) {
    modeSelect.value = (config.ai_mode || "cloud").toLowerCase();
  }
  const localBase = document.getElementById("localAiBaseUrl");
  if (localBase && !localBase.matches(":focus")) {
    localBase.value = config.local_ai_base_url || "";
  }
  const localModel = document.getElementById("localAiModel");
  if (localModel && !localModel.matches(":focus")) {
    localModel.value = config.local_ai_model || "";
  }
  const localKey = document.getElementById("localAiApiKey");
  if (localKey && !localKey.matches(":focus")) {
    localKey.value = "";
  }
  const localClear = document.getElementById("localAiClearKey");
  if (localClear) {
    localClear.checked = false;
    localClear.disabled = !config.local_ai_has_key;
  }
  const cloudBase = document.getElementById("cloudAiBaseUrl");
  if (cloudBase && !cloudBase.matches(":focus")) {
    cloudBase.value = config.cloud_ai_base_url || "";
  }
  const cloudModel = document.getElementById("cloudAiModel");
  if (cloudModel && !cloudModel.matches(":focus")) {
    cloudModel.value = config.cloud_ai_model || "";
  }
  const cloudKey = document.getElementById("cloudAiApiKey");
  if (cloudKey && !cloudKey.matches(":focus")) {
    cloudKey.value = "";
  }
  const cloudClear = document.getElementById("cloudAiClearKey");
  if (cloudClear) {
    cloudClear.checked = false;
    cloudClear.disabled = !config.cloud_ai_has_key;
  }
  updateAiStatusBadge();
  const status = document.getElementById("aiConfigStatus");
  if (status) {
    status.textContent = "Configuration IA chargee.";
  }
}

function updateAiStatusBadge() {
  const badge = document.getElementById("aiStatusBadge");
  if (!badge) return;
  const modeSelect = document.getElementById("aiModeSelect");
  const mode = modeSelect ? modeSelect.value : "cloud";
  const hasCloudKey =
    aiConfigState && (aiConfigState.cloud_ai_has_key || aiConfigState.cloud_ai_api_key);
  let text = mode === "local" ? "Mode local selectionne" : "Mode cloud selectionne";
  badge.classList.remove("bg-success", "bg-info", "bg-danger", "bg-secondary-subtle");
  if (mode === "cloud" && !hasCloudKey) {
    text += " - cle manquante";
    badge.classList.add("bg-danger");
  } else {
    badge.classList.add(mode === "local" ? "bg-info" : "bg-success");
  }
  badge.textContent = text;
}

function onAiModeChanged() {
  updateAiStatusBadge();
}

async function saveAiConfig() {
  const mode = document.getElementById("aiModeSelect")?.value || "cloud";
  const payload = {
    ai_mode: mode,
    local_ai_base_url: document.getElementById("localAiBaseUrl")?.value?.trim() || "",
    local_ai_model: document.getElementById("localAiModel")?.value?.trim() || "",
    cloud_ai_base_url: document.getElementById("cloudAiBaseUrl")?.value?.trim() || "",
    cloud_ai_model: document.getElementById("cloudAiModel")?.value?.trim() || "",
  };
  const localKeyInput = document.getElementById("localAiApiKey");
  const localKeyClear = document.getElementById("localAiClearKey");
  const localKeyValue = localKeyInput?.value?.trim() || "";
  if (localKeyValue) {
    payload.local_ai_api_key = localKeyValue;
  } else if (localKeyClear?.checked) {
    payload.local_ai_api_key = "";
  } else {
    payload.local_ai_api_key = null;
  }
  const cloudKeyInput = document.getElementById("cloudAiApiKey");
  const cloudKeyClear = document.getElementById("cloudAiClearKey");
  const cloudKeyValue = cloudKeyInput?.value?.trim() || "";
  if (cloudKeyValue) {
    payload.cloud_ai_api_key = cloudKeyValue;
  } else if (cloudKeyClear?.checked) {
    payload.cloud_ai_api_key = "";
  } else {
    payload.cloud_ai_api_key = null;
  }
  const status = document.getElementById("aiConfigStatus");
  if (status) {
    status.textContent = "Enregistrement de la configuration IA...";
  }
  try {
    const res = await fetch("/api/ai/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    applyAiConfigToForm(data.config || {});
    if (localKeyInput) localKeyInput.value = "";
    if (cloudKeyInput) cloudKeyInput.value = "";
    showToast("Configuration IA enregistree.", "success");
    if (status) {
      status.textContent = "Configuration IA mise a jour.";
    }
  } catch (err) {
    console.error("saveAiConfig", err);
    if (status) {
      status.textContent = `Erreur configuration IA: ${err.message}`;
    }
    showToast(`Config IA: ${err.message}`, "danger");
  }
}

async function testAiConnection() {
  const status = document.getElementById("aiConfigStatus");
  if (status) {
    status.textContent = "Test de connexion IA en cours...";
  }
  const mode = document.getElementById("aiModeSelect")?.value || undefined;
  try {
    const res = await fetch("/api/ai/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    if (status) {
      status.textContent = `IA connectee (${data.mode_used || mode}) - ${data.latency_ms} ms`;
    }
    showToast("Test IA reussi.", "success");
  } catch (err) {
    console.error("testAiConnection", err);
    if (status) {
      status.textContent = `Test IA echoue: ${err.message}`;
    }
    showToast(`IA indisponible: ${err.message}`, "danger");
  }
}

async function loadAiPhotoOptions() {
  const status = document.getElementById("aiVisionStatus");
  if (status) {
    status.textContent = "Chargement des photos...";
  }
  try {
    const res = await fetch("/gallery/media?type=photos&sort=desc&per_page=20");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Reponse galerie invalide");
    }
    aiPhotoPool = Array.isArray(data.items) ? data.items : [];
    aiSelectedPhotos.forEach((filename) => {
      if (!aiPhotoPool.some((item) => item.filename === filename)) {
        aiSelectedPhotos.delete(filename);
      }
    });
    renderAiPhotoPicker(aiPhotoPool);
    if (status) {
      status.textContent = "";
    }
  } catch (err) {
    console.error("loadAiPhotoOptions", err);
    if (status) status.textContent = `Erreur galerie: ${err.message}`;
    showToast(`Photos indisponibles: ${err.message}`, "danger");
    renderAiPhotoPicker([]);
  }
}

function renderAiPhotoPicker(photos = []) {
  const container = document.getElementById("aiPhotoPicker");
  if (!container) return;
  if (!photos.length) {
    container.innerHTML =
      '<div class="col"><div class="small text-secondary">Aucune photo disponible.</div></div>';
    updateAiVisionCountBadge();
    return;
  }
  container.innerHTML = "";
  photos.forEach((photo) => {
    const col = document.createElement("div");
    col.className = "col";
    const selected = aiSelectedPhotos.has(photo.filename);
    col.innerHTML = `
      <div class="ai-photo-option card border ${
        selected ? "border-info shadow" : "border-secondary-subtle"
      } overflow-hidden" role="button" data-photo-name="${photo.filename}">
        <img src="${photo.thumbnail_url || photo.url}" class="card-img-top" alt="${photo.filename}">
        <div class="card-body py-1 px-2">
          <div class="small text-truncate">${photo.filename}</div>
        </div>
      </div>`;
    container.appendChild(col);
  });
  updateAiVisionCountBadge();
}

function toggleAiPhotoSelection(filename) {
  if (!filename) return;
  if (aiSelectedPhotos.has(filename)) {
    aiSelectedPhotos.delete(filename);
  } else {
    if (aiSelectedPhotos.size >= AI_IMAGE_SELECTION_LIMIT) {
      showToast(`Maximum ${AI_IMAGE_SELECTION_LIMIT} images.`, "warning");
      return;
    }
    aiSelectedPhotos.add(filename);
  }
  renderAiPhotoPicker(aiPhotoPool);
}

function updateAiVisionCountBadge() {
  const badge = document.getElementById("aiVisionImageCount");
  if (!badge) return;
  badge.textContent = `${aiSelectedPhotos.size}/${AI_IMAGE_SELECTION_LIMIT} image(s)`;
}

async function sendAiVisionRequest() {
  const prompt = document.getElementById("aiVisionPrompt")?.value?.trim() || "";
  const images = Array.from(aiSelectedPhotos);
  if (!prompt && images.length === 0) {
    showToast("Indiquez un prompt ou selectionnez des photos.", "warning");
    return;
  }
  const status = document.getElementById("aiVisionStatus");
  const btn = document.querySelector('[data-action="aiSendVisionPrompt"]');
  if (status) status.textContent = "Analyse IA en cours...";
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/ai/analyze_with_images", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, image_filenames: images }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const resultEl = document.getElementById("aiVisionResult");
    if (resultEl) {
      resultEl.classList.remove("d-none");
      resultEl.innerHTML = `<div class="fw-semibold mb-1">Mode ${
        data.mode_used || "inconnu"
      }</div><pre class="mb-0" style="white-space: pre-wrap;">${escapeHtml(
        data.analysis || ""
      )}</pre>`;
    }
    showToast("Analyse IA terminee.", "success");
    if (status) status.textContent = "";
  } catch (err) {
    console.error("sendAiVisionRequest", err);
    if (status) status.textContent = `Erreur IA: ${err.message}`;
    showToast(`Analyse IA impossible: ${err.message}`, "danger");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadAiSummary(initial = false) {
  const select = document.getElementById("aiSummaryPeriodSelect");
  const period = select ? select.value : "last_3_days";
  const label = document.getElementById("aiSummaryPeriodLabel");
  if (label) {
    const readable = ANALYSIS_PERIOD_LABELS[period] || period;
    label.textContent = `Periode: ${readable}`;
  }
  const content = document.getElementById("aiSummaryContent");
  if (content && !initial) {
    content.textContent = "Chargement du resume IA...";
  }
  try {
    const res = await fetch(`/api/ai/summary?period=${encodeURIComponent(period)}`);
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Resume IA indisponible");
    }
    aiSummaryState = data.summary;
    renderAiSummary(aiSummaryState);
  } catch (err) {
    console.error("loadAiSummary", err);
    if (content) {
      content.innerHTML = `<div class="alert alert-danger mb-0">Erreur resume IA: ${escapeHtml(
        err.message
      )}</div>`;
    }
  }
}

function renderAiSummary(summary) {
  const container = document.getElementById("aiSummaryContent");
  if (!container) return;
  if (!summary) {
    container.textContent = "Aucun resume disponible.";
    return;
  }
  const parts = [];
  if (summary.telemetry_summary) {
    parts.push(
      `<div class="mb-2">${escapeHtml(summary.telemetry_summary)}</div>`
    );
  }
  const events = Array.isArray(summary.events) ? summary.events.slice(-5) : [];
  if (events.length) {
    const items = events
      .map((event) => {
        const when = formatLogbookDate(event.time) || "";
        return `<li>${escapeHtml(when)} - ${escapeHtml(
          event.device_type || ""
        )} ${escapeHtml(event.value ?? event.field ?? "")}</li>`;
      })
      .join("");
    parts.push(`<div class="mt-2"><strong>Evenements recents</strong><ul class="mb-0">${items}</ul></div>`);
  }
  const images = Array.isArray(summary.images) ? summary.images : [];
  if (images.length) {
    const thumbs = images
      .map(
        (img) =>
          `<button class="btn btn-sm btn-outline-light me-2 mb-2" data-ai-image-url="${img.url}" type="button">` +
          `<img src="${img.thumbnail_url || img.url}" alt="${escapeHtml(
            img.filename || "photo"
          )}" style="max-width:80px; max-height:60px;"></button>`
      )
      .join("");
    parts.push(`<div class="mt-2"><strong>Photos recentes</strong><div>${thumbs}</div></div>`);
  }
  container.innerHTML = parts.join("") || "Aucune donnee resume disponible.";
}

async function loadAiInsights() {
  const list = document.getElementById("aiInsightsList");
  if (list) list.textContent = "Chargement...";
  try {
    const res = await fetch("/api/ai/insights");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Reponse invalide");
    }
    aiInsightsState = Array.isArray(data.insights) ? data.insights : [];
    renderAiInsights(aiInsightsState);
  } catch (err) {
    console.error("loadAiInsights", err);
    if (list) {
      list.innerHTML = `<div class="alert alert-danger mb-0">Erreur insights IA: ${escapeHtml(
        err.message
      )}</div>`;
    }
  }
}

function renderAiInsights(insights) {
  const list = document.getElementById("aiInsightsList");
  if (!list) return;
  if (!insights || insights.length === 0) {
    list.innerHTML = '<div class="text-secondary">Aucun insight enregistre.</div>';
    return;
  }
  const items = insights.slice(0, 10).map((insight) => {
    const risk =
      `<span class="badge ${insight.risk_level === "danger" ? "bg-danger" : "bg-info"}">${escapeHtml(
        insight.risk_level || "info"
      )}</span>`;
    return `
      <div class="border border-secondary-subtle rounded p-2 mb-2 bg-body-secondary bg-opacity-25">
        <div class="d-flex justify-content-between align-items-center small">
          <div class="fw-semibold">${escapeHtml(insight.source || "inconnu")}</div>
          <div class="text-secondary">${escapeHtml(insight.mode || "")}</div>
        </div>
        <div class="small text-secondary">${escapeHtml(
          formatLogbookDate(insight.created_at) || ""
        )} ${risk}</div>
        <div class="mt-1">${escapeHtml(insight.text || "")}</div>
      </div>`;
  });
  list.innerHTML = items.join("");
}

function renderAiWorkerStatus(status) {
  const badge = document.getElementById("aiWorkerStatusBadge");
  const details = document.getElementById("aiWorkerStatusDetails");
  const startBtn = document.querySelector("[data-action='aiWorkerStart']");
  const stopBtn = document.querySelector("[data-action='aiWorkerStop']");
  const running = status && status.running;
  if (badge) {
    badge.textContent = running ? "En cours" : "A l'arret";
    badge.className = running ? "badge bg-success" : "badge bg-secondary-subtle text-dark";
  }
  if (details) {
    if (!status) {
      details.textContent = "Etat du worker indisponible.";
    } else {
      const started = status.started_at ? formatLogbookDate(status.started_at) : null;
      const stopped = status.stopped_at ? formatLogbookDate(status.stopped_at) : null;
      const lines = [];
      if (running) {
        lines.push(`PID ${status.pid || "?"}, demarrage ${started || "inconnu"}.`);
      } else {
        lines.push("Worker inactif.");
        if (stopped) {
          lines.push(`Arrete ${stopped} (code ${status.last_exit_code ?? "?"}).`);
        }
      }
      if (status.log_path) {
        lines.push(`Journal: ${status.log_path}`);
      }
      details.textContent = lines.join(" ");
    }
  }
  if (startBtn) {
    startBtn.disabled = !!running;
  }
  if (stopBtn) {
    stopBtn.disabled = !running;
  }
}

async function loadAiWorkerStatus(showLoading = false) {
  const details = document.getElementById("aiWorkerStatusDetails");
  if (details && showLoading) {
    details.textContent = "Chargement...";
  }
  try {
    const res = await fetch("/api/ai/worker/status");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Reponse invalide");
    }
    aiWorkerStatusState = data.status || null;
    renderAiWorkerStatus(aiWorkerStatusState);
  } catch (err) {
    console.error("loadAiWorkerStatus", err);
    if (details) {
      details.textContent = `Erreur etat worker: ${err.message}`;
    }
  }
}

async function startAiWorker() {
  const btn = document.querySelector("[data-action='aiWorkerStart']");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/ai/worker/start", { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Worker IA demarre.", "success");
  } catch (err) {
    console.error("startAiWorker", err);
    showToast(`Demarrage worker impossible: ${err.message}`, "danger");
  } finally {
    await loadAiWorkerStatus(true);
  }
}

async function stopAiWorker() {
  const btn = document.querySelector("[data-action='aiWorkerStop']");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/ai/worker/stop", { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Worker IA arrete.", "info");
  } catch (err) {
    console.error("stopAiWorker", err);
    showToast(`Arret worker impossible: ${err.message}`, "danger");
  } finally {
    await loadAiWorkerStatus(true);
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
    showPopin("Configuration pompe manquante.", "warning");
    return;
  }
  const volume = parseFloat(cfg.volume_ml || 0);
  if (!(volume > 0)) {
    showPopin("Volume invalide.", "warning");
    return;
  }
  const STEPS_PER_ML = 5000;
  const steps = Math.max(1, Math.round(volume * STEPS_PER_ML));
  const backwards = cfg.direction < 0;
  await apiAction("set_steps_speed", { steps, speed: globalSpeedUs });
  await apiAction("pump", { axis, backwards });
  refreshState();
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
    showPopin("µs/step invalide", "warning");
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
    showPopin("Volume invalide", "warning");
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
  const statusText =
    state.status || (state.connected ? "Connecté" : "Déconnecté");
  if (badge) {
    badge.textContent = state.connected
      ? `🟢 ${statusText}`
      : `🔴 ${statusText}`;
    badge.classList.toggle("bg-success", !!state.connected);
    badge.classList.toggle("bg-danger", !state.connected);
  }

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

  document.getElementById("temp1_val").textContent = `${
    state.temp_1 || "--.-"
  }°C`;
  document.getElementById("temp2_val").textContent = `${
    state.temp_2 || "--.-"
  }°C`;
  document.getElementById("temp3_val").textContent = `${
    state.temp_3 || "--.-"
  }°C`;
  document.getElementById("temp4_val").textContent = `${
    state.temp_4 || "--.-"
  }°C`;
  const phV = state.ph_v ?? state.phV ?? null;
  const phRaw = state.ph_raw ?? state.phRaw ?? null;
  document.getElementById("ph_v_val").textContent =
    phV !== null && phV !== undefined ? `${phV} V` : "--.- V";
  document.getElementById("ph_raw_val").textContent =
    phRaw !== null && phRaw !== undefined ? phRaw : "----";
  const phVal = state.ph ?? null;
  document.getElementById("ph_val").textContent =
    phVal !== null && phVal !== undefined ? phVal : "--.-";
  updatePhCalibrationUi(state.ph_calibration, state.connected);
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
  updateWaterTargetsCurrent(state);
  const pumpLabel = document.getElementById("pumpStateLabel");
  const pumpBtn = document.getElementById("pumpToggleBtn");
  const pumpIcon = document.getElementById("pumpIcon");
  if (pumpLabel) {
    pumpLabel.textContent = state.pump_state
      ? "Pompe au repos 💤"
      : "Pompe en marche 💦";
  }
  if (pumpBtn) {
    // Relais OFF (pump_state=false) -> label "Pompe On", bouton "Arrêter"
    pumpBtn.textContent = state.pump_state ? "Démarrer" : "Arrêter";
    pumpBtn.classList.toggle("btn-danger", !state.pump_state);
    pumpBtn.classList.toggle("btn-outline-light", state.pump_state);
  }
  if (pumpIcon) {
    pumpIcon.classList.toggle("is-active", !state.pump_state);
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
  const peristalticState = state.peristaltic_state || {};
  ["X", "Y", "Z", "E"].forEach((axis) => {
    const card = document.querySelector(`.peristaltic-card[data-axis="${axis}"]`);
    const chip = document.getElementById(`peristalticStatus_${axis}`);
    const running = !!peristalticState?.[axis];
    if (card) {
      card.classList.toggle("is-active", running);
    }
    if (chip) {
      chip.textContent = running ? "Cycle en cours ⚙️" : "Repos 🌙";
      chip.classList.toggle("chip-active", running);
    }
  });
  const peristalticAutoToggle = document.getElementById(
    "peristalticAutoToggle"
  );
  if (peristalticAutoToggle) {
    peristalticAutoToggle.checked = !!state.peristaltic_auto;
  }
  renderPeristalticHistory(state.peristaltic_history || {});

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
  if (
    !feederInitialized ||
    (!feederDirty && incomingJson !== lastFeederScheduleJson)
  ) {
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
    stateLabel.textContent = currentLightState ? "Allumée ✨" : "Éteinte 🌙";
  }
  if (toggleBtn) {
    toggleBtn.textContent = currentLightState ? "Éteindre" : "Allumer";
    toggleBtn.classList.toggle("btn-danger", currentLightState);
    toggleBtn.classList.toggle("btn-outline-light", !currentLightState);
  }
  const lampIcon = document.getElementById("lampIcon");
  if (lampIcon) {
    lampIcon.classList.toggle("lamp-on", currentLightState);
    lampIcon.classList.toggle("lamp-off", !currentLightState);
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
  const pct = Math.min(
    100,
    Math.max(0, 100 - (remaining / refreshIntervalMs) * 100)
  );
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

function setupTabPersistence() {
  const tabButtons = document.querySelectorAll('[data-bs-toggle="tab"]');
  tabButtons.forEach((btn) => {
    btn.addEventListener("shown.bs.tab", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.getAttribute("data-bs-target")
          : null;
      if (target) {
        try {
          localStorage.setItem(LAST_ACTIVE_TAB_KEY, target);
        } catch (err) {
          console.warn("Unable to persist active tab", err);
        }
      }
    });
  });
  let stored = null;
  try {
    stored = localStorage.getItem(LAST_ACTIVE_TAB_KEY);
  } catch (err) {
    stored = null;
  }
  if (stored) {
    const trigger = document.querySelector(
      `[data-bs-toggle="tab"][data-bs-target="${stored}"]`
    );
    if (trigger) {
      const tabInstance = bootstrap.Tab.getOrCreateInstance(trigger);
      tabInstance.show();
    }
  }
}

function setupCameraSubnavPersistence() {
  const cameraButtons = document.querySelectorAll(
    '#tab-camera [data-bs-toggle="tab"][data-bs-target^="#camera-pane-"]'
  );
  if (!cameraButtons.length) {
    return;
  }
  cameraButtons.forEach((btn) => {
    btn.addEventListener("shown.bs.tab", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.getAttribute("data-bs-target")
          : null;
      if (target) {
        try {
          localStorage.setItem(LAST_CAMERA_SUBTAB_KEY, target);
        } catch (err) {
          console.warn("Unable to persist camera subtab", err);
        }
      }
    });
  });
  let stored = null;
  try {
    stored = localStorage.getItem(LAST_CAMERA_SUBTAB_KEY);
  } catch (err) {
    stored = null;
  }
  if (stored) {
    const trigger = document.querySelector(
      `#tab-camera [data-bs-toggle="tab"][data-bs-target="${stored}"]`
    );
    if (trigger) {
      const tabInstance = bootstrap.Tab.getOrCreateInstance(trigger);
      tabInstance.show();
    }
  }
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

function renderPeristalticHistory(historyMap = {}) {
  const tbody = document.getElementById("peristalticHistoryBody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const axes = ["X", "Y", "Z", "E"];
  let rowCount = 0;
  axes.forEach((axis) => {
    const entries = Array.isArray(historyMap?.[axis])
      ? historyMap[axis]
      : [];
    const recent = entries.slice(-7).reverse();
    recent.forEach((entry) => {
      const tr = document.createElement("tr");
      const axisCell = document.createElement("td");
      axisCell.textContent = `Pompe ${axis}`;
      axisCell.classList.add("text-nowrap");
      tr.appendChild(axisCell);
      const iso =
        typeof entry?.timestamp === "string" ? entry.timestamp : "";
      let dateText =
        typeof entry?.date === "string" && entry.date ? entry.date : "";
      if (!dateText && iso.includes("T")) {
        dateText = iso.split("T", 1)[0];
      }
      let timeText =
        typeof entry?.label === "string" && entry.label ? entry.label : "";
      if (!timeText && iso.includes("T")) {
        timeText = iso.split("T")[1].slice(0, 5);
      }
      const dateCell = document.createElement("td");
      dateCell.textContent = dateText || "--";
      if (iso) {
        dateCell.title = iso;
      }
      tr.appendChild(dateCell);
      const timeCell = document.createElement("td");
      timeCell.textContent = timeText || "--:--";
      if (iso) {
        timeCell.title = iso;
      }
      tr.appendChild(timeCell);
      tbody.appendChild(tr);
      rowCount += 1;
    });
  });
  if (rowCount === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "text-center text-secondary small";
    td.textContent = "Aucune action peristaltique";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

async function applyHeatHyst() {
  const val = parseFloat(document.getElementById("heatHyst")?.value || "");
  if (!isFinite(val) || val < 0) {
    showPopin("Hystérésis invalide", "warning");
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
    showPopin("Intervalle invalide (min 0.5s)", "warning");
    return;
  }
  refreshIntervalMs = val * 1000;
  restartRefreshTimer();
}

async function restartReefService() {
  const confirmed = await showPopin(
    "Redémarrer le service Reef va interrompre temporairement l'IHM. Continuer ?",
    "warning",
    { confirmable: true, confirmText: "Redémarrer", cancelText: "Annuler" }
  );
  if (!confirmed) {
    return;
  }
  await apiAction("restart_service");
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
        Number.isFinite(duration)
          ? duration
          : stopPump
          ? DEFAULT_FEEDER_PUMP_STOP_DURATION
          : 0,
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
        <option value="GET"${
          methodClean === "GET" ? " selected" : ""
        }>GET</option>
        <option value="POST"${
          methodClean === "POST" ? " selected" : ""
        }>POST</option>
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
    const method = (tr.querySelector(".feeder-method")?.value || "GET")
      .toString()
      .toUpperCase();
    if (!url) {
      showPopin("URL manquante", "warning");
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
  reloadWaterTargets: () => loadWaterTargets(true),
  phCalibrate: (el) => handlePhCalibration(el),
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
  restartService: () => restartReefService(),
  applyHeatHyst: () => applyHeatHyst(),
  addFeederRow: () => addFeederRow(),
  saveFeederSchedule: () => saveFeederSchedule(),
  prepareAiAnalysis: () => prepareAiAnalysis(),
  get_ai_analysis: () => getAiAnalysis(),
  cameraSaveSettings: () => saveCameraSettings(),
  cameraCapturePhoto: () => captureCameraPhoto(),
  cameraCaptureVideo: () => captureCameraVideo(),
  cameraChangeDevice: () => changeCameraDevice(),
  esp32camSaveConfig: () => saveEsp32CamConfig(),
  esp32camRefreshSettings: () => refreshEsp32CamSettings(),
  esp32camApplySettings: () => applyEsp32CamSettings(),
  esp32camCapture: () => captureEsp32CamPhoto(),
  logbookSubmit: () => submitLogbookEntry(),
  logbookRefresh: () => loadLogbookEntries(),
  logbookReset: () => resetLogbookForm(),
  livestockAdd: (el) => startLivestockCreate(el.dataset.category),
  livestockEdit: (el) => startLivestockEdit(el.dataset.category, el.dataset.entryId),
  livestockCancelEdit: () => cancelLivestockEdit(),
  livestockSave: (el) => saveLivestockEntry(el),
  livestockRefresh: () => loadLivestockCatalog(),
  livestockDelete: (el) => deleteLivestockEntry(el),
  livestockFetchComfort: (el) => fetchLivestockComfort(el),
  photoCategoryAdd: () => showPhotoCategoryInput(),
  photoCategoryCancel: () => hidePhotoCategoryInput(),
  photoCategorySave: (el) => submitPhotoCategory(el),
  aiConfigSave: () => saveAiConfig(),
  aiTestConnection: () => testAiConnection(),
  aiGalleryRefresh: () => loadAiPhotoOptions(),
  aiSendVisionPrompt: () => sendAiVisionRequest(),
  aiSummaryRefresh: () => loadAiSummary(false),
  aiInsightsRefresh: () => loadAiInsights(),
  aiWorkerRefresh: () => loadAiWorkerStatus(true),
  aiWorkerStart: () => startAiWorker(),
  aiWorkerStop: () => stopAiWorker(),
};

const changeHandlers = {
  autoFanToggle: () => onAutoFanToggle(),
  protectToggle: () => onProtectToggle(),
  mtrAutoToggle: () => onMtrAutoChanged(),
  lightAuto: (target) => setLightAuto(target.value === "1"),
  heatMode: (target) => setHeatMode(target.checked),
  aiModeChanged: () => onAiModeChanged(),
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

  const peristalticAutoToggle = document.getElementById(
    "peristalticAutoToggle"
  );
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

async function loadCameraSettings() {
  try {
    const res = await fetch("/camera/settings");
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (!data.ok) {
      throw new Error(data.error || "Reponse invalide");
    }
    cameraSettings = data.settings || {};
    applyCameraSettingsToUI(cameraSettings);
  } catch (err) {
    console.error("loadCameraSettings", err);
    showToast(`Impossible de charger la caméra: ${err.message}`, "danger");
  }
}

function applyCameraSettingsToUI(settings = {}) {
  const hflip = document.getElementById("cameraHFlip");
  if (hflip) hflip.checked = !!settings.hflip;
  const vflip = document.getElementById("cameraVFlip");
  if (vflip) vflip.checked = !!settings.vflip;
  const autoTime = document.getElementById("cameraAutoTime");
  if (autoTime && !autoTime.matches(":focus")) {
    autoTime.value = settings.auto_capture_time || "";
  }
  setCameraSliderValue(
    "cameraBrightness",
    typeof settings.brightness === "number" ? settings.brightness : 0
  );
  setCameraSliderValue(
    "cameraContrast",
    typeof settings.contrast === "number" ? settings.contrast : 1
  );
  setCameraSliderValue(
    "cameraSaturation",
    typeof settings.saturation === "number" ? settings.saturation : 1
  );
  const rotationSelect = document.getElementById("cameraRotation");
  if (rotationSelect && !rotationSelect.matches(":focus")) {
    const rotation = typeof settings.rotation === "number" ? settings.rotation : 0;
    rotationSelect.value = String(rotation);
  }
  const dirInput = document.getElementById("cameraSaveDirectory");
  if (dirInput && !dirInput.matches(":focus")) {
    dirInput.value = settings.save_directory || "";
  }
  populateCameraDeviceSelect(settings);
  const badge = document.getElementById("cameraStatusBadge");
  const available = !!settings.camera_available;
  if (badge) {
    badge.textContent = available ? "Caméra prête" : "Caméra indisponible";
    badge.classList.toggle("bg-success", available);
    badge.classList.toggle("bg-secondary-subtle", !available);
  }
  handleCameraFeedState(available);
}

function populateCameraDeviceSelect(settings = {}) {
  const select = document.getElementById("cameraDeviceSelect");
  if (!select) return;
  const cameras = Array.isArray(settings.cameras) ? settings.cameras : [];
  select.innerHTML = "";
  if (!cameras.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Aucune caméra détectée";
    select.appendChild(opt);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  cameras.forEach((cam) => {
    const opt = document.createElement("option");
    opt.value = cam.id;
    opt.textContent = cam.name || cam.model || cam.id;
    if (cam.selected) {
      opt.selected = true;
    }
    select.appendChild(opt);
  });
  if (!select.value && cameras[0]) {
    select.value = cameras[0].id;
  }
}

async function saveCameraSettings() {
  const payload = {
    hflip: !!document.getElementById("cameraHFlip")?.checked,
    vflip: !!document.getElementById("cameraVFlip")?.checked,
    auto_capture_time:
      document.getElementById("cameraAutoTime")?.value?.trim() || "",
    save_directory: document.getElementById("cameraSaveDirectory")?.value || "",
    brightness: parseFloat(
      document.getElementById("cameraBrightness")?.value || "0"
    ),
    contrast: parseFloat(
      document.getElementById("cameraContrast")?.value || "1"
    ),
    saturation: parseFloat(
      document.getElementById("cameraSaturation")?.value || "1"
    ),
    rotation: parseInt(
      document.getElementById("cameraRotation")?.value || "0",
      10
    ),
  };
  if (!isFinite(payload.brightness)) payload.brightness = 0;
  if (!isFinite(payload.contrast)) payload.contrast = 1;
  if (!isFinite(payload.saturation)) payload.saturation = 1;
  try {
    const res = await fetch("/camera/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    cameraSettings = data.settings || payload;
    applyCameraSettingsToUI(cameraSettings);
    showToast("Configuration caméra enregistrée.", "success");
  } catch (err) {
    console.error("saveCameraSettings", err);
    showToast(`Erreur configuration caméra: ${err.message}`, "danger");
  }
}

function setCameraSliderValue(id, value) {
  const input = document.getElementById(id);
  if (input && !input.matches(":active")) {
    input.value = String(value);
  }
  updateCameraSliderDisplay(id);
}

function updateCameraSliderDisplay(id) {
  const input = document.getElementById(id);
  const label = document.getElementById(`${id}Value`);
  if (!input || !label) {
    return;
  }
  const numericValue = parseFloat(input.value);
  label.textContent = Number.isFinite(numericValue)
    ? numericValue.toFixed(2)
    : "--";
}

function initCameraModule() {
  cameraLiveImageEl = document.getElementById("cameraLiveFeed");
  cameraLiveOverlayEl = document.getElementById("cameraFeedUnavailable");
  setupCameraSubnavPersistence();
  ["cameraBrightness", "cameraContrast", "cameraSaturation"].forEach((id) => {
    const input = document.getElementById(id);
    if (input) {
      input.addEventListener("input", () => updateCameraSliderDisplay(id));
      updateCameraSliderDisplay(id);
    }
  });
  ["esp32CamBrightness", "esp32CamContrast", "esp32CamSaturation"].forEach(
    (id) => {
      const input = document.getElementById(id);
      if (input) {
        input.addEventListener("input", () => updateEsp32SliderDisplay(id));
        updateEsp32SliderDisplay(id);
      }
    }
  );
  if (cameraLiveImageEl) {
    cameraLiveImageEl.addEventListener("error", () => handleCameraFeedState(false));
    cameraLiveImageEl.addEventListener("load", () =>
      handleCameraFeedState(true)
    );
  }
  initMediaViewer();
  loadCameraSettings();
  loadEsp32CamConfig();
  initPhotoCategoryInput();
  ["photos", "videos"].forEach((mediaType) => {
    const sortSelect = document.querySelector(
      `[data-gallery-sort="${mediaType}"]`
    );
    if (sortSelect) {
      sortSelect.addEventListener("change", () => {
        loadGallery(mediaType, 1, sortSelect.value || "desc");
      });
    }
    const selectAll = document.querySelector(
      `[data-gallery-select-all="${mediaType}"]`
    );
    if (selectAll) {
      selectAll.addEventListener("change", (event) => {
        toggleGallerySelectAll(mediaType, event.target.checked);
      });
    }
    const deleteBtn = document.querySelector(
      `[data-gallery-delete="${mediaType}"]`
    );
    if (deleteBtn) {
      deleteBtn.addEventListener("click", (event) => {
        event.preventDefault();
        deleteGallerySelection(mediaType);
      });
    }
    const prevBtn = document.querySelector(
      `[data-gallery-prev="${mediaType}"]`
    );
    if (prevBtn) {
      prevBtn.addEventListener("click", (event) => {
        event.preventDefault();
        const state = galleryState[mediaType];
        if (state.page > 1) {
          loadGallery(mediaType, state.page - 1);
        }
      });
    }
    const nextBtn = document.querySelector(
      `[data-gallery-next="${mediaType}"]`
    );
    if (nextBtn) {
      nextBtn.addEventListener("click", (event) => {
        event.preventDefault();
        const state = galleryState[mediaType];
        if (state.page < state.totalPages) {
          loadGallery(mediaType, state.page + 1);
        }
      });
    }
    const grid = document.getElementById(`galleryGrid_${mediaType}`);
    if (grid) {
      grid.addEventListener("click", (event) =>
        handleGalleryGridClick(mediaType, event)
      );
    }
    loadGallery(mediaType);
  });
}

function handleCameraFeedState(isAvailable) {
  if (cameraLiveOverlayEl) {
    cameraLiveOverlayEl.classList.toggle("d-none", !!isAvailable);
  }
}

async function captureCameraPhoto() {
  try {
    const res = await fetch("/camera/capture_photo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Photo enregistrée.", "success");
    await loadGallery("photos");
  } catch (err) {
    console.error("captureCameraPhoto", err);
    showToast(`Capture photo impossible: ${err.message}`, "danger");
  }
}

async function captureCameraVideo() {
  const durationInput = document.getElementById("cameraVideoDuration");
  let duration = parseInt(durationInput?.value || "0", 10);
  if (!Number.isFinite(duration) || duration <= 0) {
    duration = 10;
  }
  try {
    const res = await fetch("/camera/capture_video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration_seconds: duration }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast(`Vidéo enregistrée (${duration}s).`, "success");
    await loadGallery("videos");
  } catch (err) {
    console.error("captureCameraVideo", err);
    showToast(`Capture vidéo impossible: ${err.message}`, "danger");
  }
}

async function changeCameraDevice() {
  const select = document.getElementById("cameraDeviceSelect");
  if (!select) return;
  const cameraId = select.value;
  if (!cameraId) {
    showToast("Aucune caméra sélectionnée.", "warning");
    return;
  }
  try {
    const res = await fetch("/camera/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ camera_id: cameraId }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    cameraSettings = data.settings || cameraSettings;
    applyCameraSettingsToUI(cameraSettings);
    showToast("Caméra changée.", "success");
  } catch (err) {
    console.error("changeCameraDevice", err);
    showToast(`Impossible de changer de caméra: ${err.message}`, "danger");
  }
}

// --- ESP32-CAM support ---
function setEsp32Status(message, tone = "secondary") {
  const badge = document.getElementById("esp32CamStatusBadge");
  if (!badge) return;
  const tones = ["bg-success", "bg-danger", "bg-warning", "bg-info", "bg-secondary-subtle"];
  tones.forEach((cls) => badge.classList.remove(cls));
  const toneClass =
    {
      success: "bg-success",
      danger: "bg-danger",
      warning: "bg-warning",
      info: "bg-info",
      secondary: "bg-secondary-subtle",
    }[tone] || "bg-secondary-subtle";
  badge.classList.add(toneClass);
  badge.textContent = message;
}

function ensureEsp32Configured(showAlert = true) {
  if (esp32CamConfig.url) {
    return true;
  }
  if (showAlert) {
    showToast("Configurez l'URL de l'ESP32-CAM avant d'utiliser ces actions.", "warning");
  }
  setEsp32Status("URL requise", "warning");
  return false;
}

function updateEsp32SliderDisplay(id) {
  const input = document.getElementById(id);
  const valueEl = document.getElementById(`${id}Value`);
  if (input && valueEl) {
    valueEl.textContent = input.value;
  }
}

function setEsp32SliderValue(id, value) {
  const input = document.getElementById(id);
  if (!input) return;
  const numericValue =
    typeof value === "number" ? value : parseInt(value, 10);
  const safeValue = Number.isFinite(numericValue) ? numericValue : 0;
  input.value = String(safeValue);
  updateEsp32SliderDisplay(id);
}

function updateEsp32Preview(blob) {
  const img = document.getElementById("esp32CamPreview");
  if (!img) return;
  if (esp32CamPreviewUrl) {
    URL.revokeObjectURL(esp32CamPreviewUrl);
    esp32CamPreviewUrl = null;
  }
  if (!blob) {
    img.src = "";
    img.classList.add("d-none");
    img.removeAttribute("data-loaded");
    return;
  }
  const objectUrl = URL.createObjectURL(blob);
  esp32CamPreviewUrl = objectUrl;
  img.classList.add("d-none");
  img.onload = () => {
    img.classList.remove("d-none");
    img.setAttribute("data-loaded", "1");
  };
  img.onerror = () => {
    img.classList.add("d-none");
    img.removeAttribute("data-loaded");
  };
  img.src = objectUrl;
}

async function loadEsp32CamConfig() {
  const input = document.getElementById("esp32CamUrlInput");
  if (!input) return;
  try {
    const res = await fetch("/esp32cam/config");
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    esp32CamConfig.url = data.url || "";
    input.value = esp32CamConfig.url;
    if (esp32CamConfig.url) {
      setEsp32Status("Configuration OK", "info");
      await refreshEsp32CamSettings({ silent: true });
    } else {
      setEsp32Status("URL requise", "warning");
    }
  } catch (err) {
    console.error("loadEsp32CamConfig", err);
    setEsp32Status("Erreur config", "danger");
    showToast(`ESP32-CAM: ${err.message}`, "danger");
  }
}

async function saveEsp32CamConfig() {
  const input = document.getElementById("esp32CamUrlInput");
  if (!input) return;
  const url = (input.value || "").trim();
  if (!url) {
    showToast("Entrez l'URL de l'ESP32-CAM.", "warning");
    setEsp32Status("URL requise", "warning");
    return;
  }
  try {
    const res = await fetch("/esp32cam/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    esp32CamConfig.url = data.url || "";
    showToast("URL ESP32-CAM enregistrée.", "success");
    await refreshEsp32CamSettings({ silent: false });
  } catch (err) {
    console.error("saveEsp32CamConfig", err);
    showToast(`Impossible d'enregistrer l'ESP32-CAM: ${err.message}`, "danger");
    setEsp32Status("Erreur enregistrement", "danger");
  }
}

async function refreshEsp32CamSettings(options = {}) {
  const { silent = false } = options;
  if (!ensureEsp32Configured(!silent)) {
    return;
  }
  setEsp32Status("Connexion...", "info");
  try {
    const res = await fetch("/esp32cam/settings");
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    esp32CamSettings = data.settings || data;
    applyEsp32SettingsToUI(esp32CamSettings);
    setEsp32Status("Connecté", "success");
    if (!silent) {
      showToast("Réglages ESP32-CAM chargés.", "success");
    }
  } catch (err) {
    console.error("refreshEsp32CamSettings", err);
    setEsp32Status("Injoignable", "danger");
    if (!silent) {
      showToast(`ESP32-CAM indisponible: ${err.message}`, "danger");
    }
  }
}

function applyEsp32SettingsToUI(settings = {}) {
  setEsp32SliderValue(
    "esp32CamBrightness",
    typeof settings.brightness === "number" ? settings.brightness : 0
  );
  setEsp32SliderValue(
    "esp32CamContrast",
    typeof settings.contrast === "number" ? settings.contrast : 0
  );
  setEsp32SliderValue(
    "esp32CamSaturation",
    typeof settings.saturation === "number" ? settings.saturation : 0
  );
  const frameSelect = document.getElementById("esp32CamFramesize");
  if (frameSelect && !frameSelect.matches(":focus")) {
    const value = settings.framesize || settings.frame_size || "SVGA";
    frameSelect.value = value;
  }
}

function collectEsp32SettingsPayload() {
  const payload = {};
  ["Brightness", "Contrast", "Saturation"].forEach((key) => {
    const input = document.getElementById(`esp32Cam${key}`);
    if (input) {
      const parsed = parseInt(input.value, 10);
      payload[key.toLowerCase()] = Number.isFinite(parsed) ? parsed : 0;
    }
  });
  const frameSelect = document.getElementById("esp32CamFramesize");
  if (frameSelect) {
    payload.framesize = frameSelect.value;
  }
  return payload;
}

async function applyEsp32CamSettings() {
  if (!ensureEsp32Configured(true)) return;
  const payload = collectEsp32SettingsPayload();
  setEsp32Status("Application...", "info");
  try {
    const res = await fetch("/esp32cam/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    esp32CamSettings = data.settings || data;
    applyEsp32SettingsToUI(esp32CamSettings);
    setEsp32Status("Réglages appliqués", "success");
    showToast("Réglages ESP32-CAM sauvegardés.", "success");
  } catch (err) {
    console.error("applyEsp32CamSettings", err);
    setEsp32Status("Erreur réglages", "danger");
    showToast(`Impossible d'appliquer les réglages ESP32-CAM: ${err.message}`, "danger");
  }
}

async function captureEsp32CamPhoto() {
  if (esp32CamCapturePending) {
    return;
  }
  if (!ensureEsp32Configured(true)) return;
  const captureBtn = document.querySelector('[data-action="esp32camCapture"]');
  if (captureBtn) {
    captureBtn.disabled = true;
  }
  esp32CamCapturePending = true;
  setEsp32Status("Capture en cours...", "info");
  try {
    const res = await fetch(`/esp32cam/capture?ts=${Date.now()}`, {
      cache: "no-store",
    });
    const contentType = res.headers.get("Content-Type") || "";
    if (!res.ok) {
      let errMsg = `HTTP ${res.status}`;
      if (contentType.includes("application/json")) {
        try {
          const errData = await res.json();
          errMsg = errData.error || errMsg;
        } catch (parseErr) {
          console.error("captureEsp32CamPhoto parse", parseErr);
        }
      }
      throw new Error(errMsg);
    }
    if (contentType.includes("application/json")) {
      const errData = await res.json();
      throw new Error(errData.error || "Reponse inattendue de l'ESP32-CAM.");
    }
    const blob = await res.blob();
    updateEsp32Preview(blob);
    setEsp32Status("Capture OK", "success");
    showToast("Photo ESP32-CAM reçue.", "success");
  } catch (err) {
    console.error("captureEsp32CamPhoto", err);
    updateEsp32Preview(null);
    setEsp32Status("Capture impossible", "danger");
    showToast(`Capture ESP32-CAM impossible: ${err.message}`, "danger");
  } finally {
    esp32CamCapturePending = false;
    if (captureBtn) {
      captureBtn.disabled = false;
    }
  }
}

async function loadPhotoLabelData(options = {}) {
  const { force = false, silent = false } = options;
  if (!force && photoLabelsState.loaded && !photoLabelsState.loadingPromise) {
    return photoLabelsState;
  }
  if (photoLabelsState.loadingPromise) {
    try {
      await photoLabelsState.loadingPromise;
      if (!force) {
        return photoLabelsState;
      }
    } catch (_err) {
      if (!force) {
        throw _err;
      }
    }
  }
  const loader = (async () => {
    try {
      const res = await fetch("/gallery/labels");
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      const categories = Array.isArray(data.categories) ? data.categories.slice() : [];
      photoLabelsState.categories =
        categories.length > 0 ? categories : [...DEFAULT_PHOTO_LABEL_CATEGORIES];
      const labels = data.labels && typeof data.labels === "object" ? data.labels : {};
      photoLabelsState.labels = labels;
      photoLabelsState.loaded = true;
      renderPhotoCategoryBadges();
      return photoLabelsState;
    } catch (err) {
      photoLabelsState.loaded = false;
      if (!silent) {
        showToast(
          `Impossible de charger les catégories photos: ${err.message}`,
          "danger"
        );
      }
      throw err;
    } finally {
      photoLabelsState.loadingPromise = null;
    }
  })();
  photoLabelsState.loadingPromise = loader;
  return loader;
}

function renderPhotoCategoryBadges() {
  const container = document.getElementById("photoCategoryList");
  if (!container) return;
  container.innerHTML = "";
  const categories = photoLabelsState.categories || [];
  if (!categories.length) {
    const info = document.createElement("span");
    info.className = "text-secondary small";
    info.textContent = "Aucune catégorie disponible.";
    container.appendChild(info);
    return;
  }
  categories.forEach((category) => {
    const pill = document.createElement("span");
    pill.className = "photo-category-pill";
    pill.textContent = category;
    container.appendChild(pill);
  });
}

function showPhotoCategoryInput() {
  const group = document.getElementById("photoCategoryInputGroup");
  const btn = document.getElementById("addPhotoCategoryBtn");
  if (group) {
    group.classList.remove("d-none");
  }
  if (btn) {
    btn.classList.add("d-none");
  }
  const input = document.getElementById("photoCategoryInput");
  if (input) {
    input.value = "";
    input.focus();
  }
}

function hidePhotoCategoryInput() {
  const group = document.getElementById("photoCategoryInputGroup");
  const btn = document.getElementById("addPhotoCategoryBtn");
  if (group) {
    group.classList.add("d-none");
  }
  if (btn) {
    btn.classList.remove("d-none");
  }
  const input = document.getElementById("photoCategoryInput");
  if (input) {
    input.value = "";
  }
}

async function submitPhotoCategory(triggerEl) {
  const input = document.getElementById("photoCategoryInput");
  if (!input) return;
  const value = (input.value || "").trim();
  if (!value) {
    showToast("Entrez un nom de catégorie.", "warning");
    input.focus();
    return;
  }
  if (triggerEl) {
    triggerEl.disabled = true;
  }
  try {
    const res = await fetch("/gallery/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: value }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const categories = Array.isArray(data.categories) ? data.categories.slice() : [];
    photoLabelsState.categories =
      categories.length > 0 ? categories : [...DEFAULT_PHOTO_LABEL_CATEGORIES];
    photoLabelsState.loaded = true;
    renderPhotoCategoryBadges();
    hidePhotoCategoryInput();
    showToast("Catégorie ajoutée.", "success");
    await loadGallery("photos");
  } catch (err) {
    console.error("submitPhotoCategory", err);
    showToast(`Impossible d'ajouter la catégorie: ${err.message}`, "danger");
  } finally {
    if (triggerEl) {
      triggerEl.disabled = false;
    }
  }
}

function initPhotoCategoryInput() {
  const input = document.getElementById("photoCategoryInput");
  if (input) {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitPhotoCategory();
      } else if (event.key === "Escape") {
        event.preventDefault();
        hidePhotoCategoryInput();
      }
    });
  }
  renderPhotoCategoryBadges();
}

function setPhotoLabelsFor(filename, labels) {
  if (!filename) return;
  if (Array.isArray(labels) && labels.length) {
    photoLabelsState.labels[filename] = labels.slice();
  } else {
    delete photoLabelsState.labels[filename];
  }
}

function clearPhotoLabelsForFilenames(filenames = []) {
  if (!Array.isArray(filenames)) return;
  filenames.forEach((name) => {
    if (name && photoLabelsState.labels[name]) {
      delete photoLabelsState.labels[name];
    }
  });
}

function populatePhotoLabelButtons(container, filename) {
  if (!container) return;
  container.innerHTML = "";
  const categories = photoLabelsState.categories || [];
  if (!categories.length) {
    const info = document.createElement("small");
    info.className = "text-secondary";
    info.textContent = "Ajoutez une catégorie pour commencer.";
    container.appendChild(info);
    return;
  }
  const assigned = new Set(photoLabelsState.labels[filename] || []);
  categories.forEach((category) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "photo-label-btn" + (assigned.has(category) ? " active" : "");
    btn.dataset.category = category;
    btn.dataset.filename = filename;
    btn.textContent = category;
    container.appendChild(btn);
  });
}

function updatePhotoLabelButtons(container, filename) {
  if (!container) return;
  const assigned = new Set(photoLabelsState.labels[filename] || []);
  container.querySelectorAll(".photo-label-btn").forEach((btn) => {
    const category = btn.dataset.category;
    btn.classList.toggle("active", !!(category && assigned.has(category)));
  });
}

function buildPhotoLabelSection(wrapper, filename) {
  const section = document.createElement("div");
  section.className = "photo-label-section";
  const title = document.createElement("div");
  title.className =
    "text-secondary text-uppercase small mb-1 photo-label-section-title";
  title.textContent = "Étiquettes";
  section.appendChild(title);
  const picker = document.createElement("div");
  picker.className = "photo-label-picker";
  picker.dataset.filename = filename;
  section.appendChild(picker);
  populatePhotoLabelButtons(picker, filename);
  wrapper.appendChild(section);
}

function handlePhotoLabelButtonClick(buttonEl) {
  const picker = buttonEl.closest(".photo-label-picker");
  if (!picker || picker.dataset.saving === "1") {
    return;
  }
  const filename = buttonEl.dataset.filename;
  const category = buttonEl.dataset.category;
  if (!filename || !category) {
    return;
  }
  togglePhotoLabel(filename, category, picker);
}

async function togglePhotoLabel(filename, category, picker) {
  const current = new Set(photoLabelsState.labels[filename] || []);
  if (current.has(category)) {
    current.delete(category);
  } else {
    current.add(category);
  }
  const nextLabels = Array.from(current);
  picker.dataset.saving = "1";
  picker.querySelectorAll(".photo-label-btn").forEach((btn) => {
    btn.classList.add("saving");
  });
  try {
    const res = await fetch("/gallery/labels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, labels: nextLabels }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const confirmed = Array.isArray(data.labels) ? data.labels : [];
    setPhotoLabelsFor(filename, confirmed);
  } catch (err) {
    console.error("togglePhotoLabel", err);
    showToast(`Impossible d'actualiser les étiquettes: ${err.message}`, "danger");
  } finally {
    picker.dataset.saving = "0";
    picker.querySelectorAll(".photo-label-btn").forEach((btn) => {
      btn.classList.remove("saving");
    });
    updatePhotoLabelButtons(picker, filename);
  }
}

async function loadGallery(mediaType, pageOverride, sortOverride) {
  const state = galleryState[mediaType];
  if (!state) return;
  if (typeof pageOverride === "number") {
    state.page = Math.max(1, pageOverride);
  }
  if (typeof sortOverride === "string") {
    state.sort = sortOverride;
  }
  const grid = document.getElementById(`galleryGrid_${mediaType}`);
  if (!grid) return;
  grid.innerHTML =
    '<div class="text-center text-secondary py-3">Chargement...</div>';
  try {
    if (mediaType === "photos") {
      try {
        await loadPhotoLabelData({ silent: true });
      } catch (err) {
        console.error("loadPhotoLabelData", err);
      }
    }
    const params = new URLSearchParams({
      type: mediaType,
      page: String(state.page),
      sort: state.sort,
      per_page: String(state.perPage),
    });
    const res = await fetch(`/gallery/media?${params.toString()}`);
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    state.totalPages = Math.max(1, data.total_pages || 1);
    state.page = Math.min(state.page, state.totalPages);
    renderGallery(mediaType, data.items || []);
    updateGalleryPagination(mediaType);
  } catch (err) {
    console.error("loadGallery", err);
    grid.innerHTML = `<div class="alert alert-danger">Impossible de charger la galerie: ${err.message}</div>`;
  }
}

function renderGallery(mediaType, items) {
  const grid = document.getElementById(`galleryGrid_${mediaType}`);
  const selectAll = document.querySelector(
    `[data-gallery-select-all="${mediaType}"]`
  );
  if (!grid) return;
  grid.innerHTML = "";
  if (selectAll) {
    selectAll.checked = false;
  }
  if (!items.length) {
    grid.innerHTML =
      '<div class="text-center text-secondary py-4">Aucun média pour le moment.</div>';
    return;
  }
  items.forEach((item, index) => {
    const inputId = `${mediaType}_media_${index}_${Date.now()}`;
    const wrapper = document.createElement("div");
    wrapper.className = `camera-gallery-item ${
      mediaType === "videos" ? "is-video" : ""
    }`;
    const thumbUrl = item.thumbnail_url || item.url;
    if (mediaType === "photos" && Array.isArray(item.labels)) {
      setPhotoLabelsFor(item.filename, item.labels);
    }
    wrapper.innerHTML = `
      <div class="camera-gallery-thumb" data-media-url="${item.url}" data-media-type="${mediaType}">
        <img src="${thumbUrl}" alt="${item.filename}">
        ${
          mediaType === "videos"
            ? '<span class="gallery-badge">Vidéo</span>'
            : ""
        }
      </div>
      <div class="camera-gallery-meta">
        <div class="form-check">
          <input class="form-check-input gallery-select" type="checkbox" id="${inputId}" data-filename="${
      item.filename
    }">
          <label class="form-check-label small text-truncate" for="${inputId}">${
      item.filename
    }</label>
        </div>
      </div>
    `;
    grid.appendChild(wrapper);
    if (mediaType === "photos") {
      buildPhotoLabelSection(wrapper, item.filename);
    }
  });
}

function updateGalleryPagination(mediaType) {
  const state = galleryState[mediaType];
  if (!state) return;
  const indicator = document.getElementById(
    `galleryPageIndicator_${mediaType}`
  );
  if (indicator) {
    indicator.textContent = `Page ${state.page} sur ${state.totalPages}`;
  }
  const prevBtn = document.querySelector(
    `[data-gallery-prev="${mediaType}"]`
  );
  if (prevBtn) {
    prevBtn.disabled = state.page <= 1;
  }
  const nextBtn = document.querySelector(
    `[data-gallery-next="${mediaType}"]`
  );
  if (nextBtn) {
    nextBtn.disabled = state.page >= state.totalPages;
  }
}

function toggleGallerySelectAll(mediaType, checked) {
  const grid = document.getElementById(`galleryGrid_${mediaType}`);
  if (!grid) return;
  grid.querySelectorAll(".gallery-select").forEach((input) => {
    input.checked = checked;
  });
}

function handleGalleryGridClick(mediaType, event) {
  if (mediaType === "photos") {
    const labelBtn = event.target.closest(".photo-label-btn");
    if (labelBtn) {
      event.preventDefault();
      handlePhotoLabelButtonClick(labelBtn);
      return;
    }
  }
  const checkbox = event.target.closest(".gallery-select");
  if (checkbox) {
    return;
  }
  const thumb = event.target.closest(".camera-gallery-thumb");
  if (!thumb) return;
  const url = thumb.dataset.mediaUrl;
  if (!url) return;
  openMediaViewer(url, thumb.dataset.mediaType || mediaType);
}

async function deleteGallerySelection(mediaType) {
  const grid = document.getElementById(`galleryGrid_${mediaType}`);
  if (!grid) return;
  const checked = Array.from(
    grid.querySelectorAll(".gallery-select:checked")
  );
  if (!checked.length) {
    showToast("Sélectionnez au moins un média.", "warning");
    return;
  }
  const filenames = checked
    .map((input) => input.dataset.filename)
    .filter(Boolean);
  if (
    !window.confirm(
      `Êtes-vous sûr de vouloir supprimer ces ${filenames.length} élément(s) ?`
    )
  ) {
    return;
  }
  try {
    const res = await fetch("/gallery/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filenames }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Médias supprimés.", "success");
    if (mediaType === "photos") {
      clearPhotoLabelsForFilenames(filenames);
    }
    await loadGallery(mediaType);
  } catch (err) {
    console.error("deleteGallerySelection", err);
    showToast(`Suppression impossible: ${err.message}`, "danger");
  }
}

function setLogbookStatus(message = "", tone = "secondary") {
  const statusEl = document.getElementById("logbookFormStatus");
  if (!statusEl) return;
  const classes = ["text-secondary", "text-success", "text-danger"];
  classes.forEach((cls) => statusEl.classList.remove(cls));
  let className = "text-secondary";
  if (tone === "success") {
    className = "text-success";
  } else if (tone === "danger") {
    className = "text-danger";
  }
  statusEl.classList.add(className);
  statusEl.textContent = message || "";
}

function resetLogbookForm(clearStatus = true) {
  const textArea = document.getElementById("logbookText");
  if (textArea) {
    textArea.value = "";
  }
  const photosInput = document.getElementById("logbookPhotos");
  if (photosInput) {
    photosInput.value = "";
    updateLogbookSelectedFiles(photosInput);
  }
  if (clearStatus) {
    setLogbookStatus("");
  }
}

function updateLogbookSelectedFiles(inputEl) {
  const input = inputEl || document.getElementById("logbookPhotos");
  const container = document.getElementById("logbookSelectedFiles");
  if (!input || !container) return;
  container.innerHTML = "";
  const files = Array.from(input.files || []);
  if (!files.length) {
    container.textContent = "";
    return;
  }
  files.forEach((file) => {
    const pill = document.createElement("span");
    pill.className = "file-pill";
    pill.textContent = file.name;
    container.appendChild(pill);
  });
}

async function submitLogbookEntry() {
  const textArea = document.getElementById("logbookText");
  const photosInput = document.getElementById("logbookPhotos");
  if (!textArea || !photosInput) {
    showToast("Formulaire journal indisponible.", "danger");
    return;
  }
  const textValue = (textArea.value || "").trim();
  const files = photosInput.files || [];
  if (!textValue && files.length === 0) {
    setLogbookStatus("Ajoutez du texte ou au moins une photo.", "danger");
    return;
  }
  const submitBtn = document.querySelector('[data-action="logbookSubmit"]');
  if (submitBtn) {
    submitBtn.disabled = true;
  }
  setLogbookStatus("Enregistrement en cours...", "secondary");
  const formData = new FormData();
  formData.append("text", textValue);
  Array.from(files).forEach((file) => formData.append("photos", file));
  try {
    const res = await fetch("/logbook/entries", {
      method: "POST",
      body: formData,
    });
    let data = {};
    try {
      data = await res.json();
    } catch (err) {
      console.error("submitLogbookEntry parse", err);
    }
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    setLogbookStatus("Entree ajoutee.", "success");
    showToast("Journal mis a jour.", "success");
    resetLogbookForm(false);
    await loadLogbookEntries(true);
  } catch (err) {
    console.error("submitLogbookEntry", err);
    setLogbookStatus(`Erreur: ${err.message}`, "danger");
    showToast(`Journal indisponible: ${err.message}`, "danger");
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
    }
  }
}

async function loadLogbookEntries(showErrors = true) {
  const list = document.getElementById("logbookEntriesList");
  const loader = document.getElementById("logbookEntriesLoader");
  const empty = document.getElementById("logbookEntriesEmpty");
  if (!list) return;
  if (loader) {
    loader.classList.remove("d-none");
  }
  if (empty) {
    empty.classList.add("d-none");
  }
  try {
    const res = await fetch("/logbook/entries");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    logbookEntriesCache = Array.isArray(data.entries) ? data.entries : [];
    renderLogbookEntries(logbookEntriesCache);
  } catch (err) {
    console.error("loadLogbookEntries", err);
    if (showErrors) {
      showToast(`Journal indisponible: ${err.message}`, "danger");
    }
    if (empty) {
      empty.textContent = `Impossible de charger le journal: ${err.message}`;
      empty.classList.remove("d-none");
    }
  } finally {
    if (loader) {
      loader.classList.add("d-none");
    }
  }
}

function renderLogbookEntries(entries) {
  const list = document.getElementById("logbookEntriesList");
  const empty = document.getElementById("logbookEntriesEmpty");
  if (!list) return;
  list.innerHTML = "";
  if (!entries || entries.length === 0) {
    if (empty) {
      empty.textContent = logbookEmptyText || empty.textContent;
      empty.classList.remove("d-none");
    }
    return;
  }
  if (empty) {
    empty.textContent = logbookEmptyText || empty.textContent;
    empty.classList.add("d-none");
  }
  entries.forEach((entry) => {
    const wrapper = document.createElement("div");
    wrapper.className = "logbook-entry";
    const dateEl = document.createElement("div");
    dateEl.className = "logbook-entry-date";
    dateEl.textContent = formatLogbookDate(entry.created_at);
    wrapper.appendChild(dateEl);
    if (entry.text) {
      const textEl = document.createElement("p");
      textEl.className = "logbook-entry-text";
      textEl.textContent = entry.text;
      wrapper.appendChild(textEl);
    }
    if (Array.isArray(entry.photos) && entry.photos.length > 0) {
      const photosWrapper = document.createElement("div");
      photosWrapper.className = "logbook-entry-photos";
      entry.photos.forEach((photo) => {
        if (!photo || !photo.url) return;
        const photoEl = document.createElement("div");
        photoEl.className = "logbook-entry-photo";
        photoEl.dataset.url = photo.url;
        const img = document.createElement("img");
        img.src = photo.thumbnail_url || photo.url;
        img.alt = photo.filename || "Photo";
        photoEl.appendChild(img);
        photosWrapper.appendChild(photoEl);
      });
      wrapper.appendChild(photosWrapper);
    }
    list.appendChild(wrapper);
  });
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatPhCalibrationTimestamp(ts) {
  if (ts === undefined || ts === null) return "Jamais calibre";
  try {
    const date = new Date(Number(ts) * 1000);
    if (Number.isNaN(date.getTime())) {
      return "Date inconnue";
    }
    return date.toLocaleString();
  } catch (err) {
    return "Date inconnue";
  }
}

function updatePhCalibrationUi(calibration, isConnected) {
  const coeffs =
    calibration && calibration.coefficients ? calibration.coefficients : {};
  const slopeVal = Number(coeffs.slope);
  const offsetVal = Number(coeffs.offset);
  const slopeText = Number.isFinite(slopeVal) ? slopeVal.toFixed(3) : "--";
  const offsetText = Number.isFinite(offsetVal) ? offsetVal.toFixed(3) : "--";
  const summaryEl = document.getElementById("phCalSummary");
  if (summaryEl) {
    summaryEl.textContent = `Pente: ${slopeText} pH/V | Offset: ${offsetText}`;
  }
  const points =
    calibration && calibration.points ? calibration.points : undefined;
  PH_CAL_REFERENCES.forEach((ref) => {
    const infoId = `phCalRef${ref.replace(".", "")}Info`;
    const infoEl = document.getElementById(infoId);
    const btn = document.querySelector(
      `[data-action="phCalibrate"][data-ref="${ref}"]`
    );
    if (btn) {
      btn.disabled = !isConnected;
    }
    if (!infoEl) return;
    const meta = points ? points[ref] : null;
    if (meta && meta.voltage !== undefined && meta.voltage !== null) {
      const voltageVal = Number(meta.voltage);
      const voltageText = Number.isFinite(voltageVal)
        ? voltageVal.toFixed(3)
        : meta.voltage;
      const tsText = meta.updated_at
        ? formatPhCalibrationTimestamp(meta.updated_at)
        : "Date inconnue";
      infoEl.textContent = `V=${voltageText} V (${tsText})`;
    } else {
      infoEl.textContent = "Pas encore calibre";
    }
  });
}

function formatLogbookDate(value) {
  if (!value) return "Date inconnue";
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString();
  } catch (err) {
    return value;
  }
}

function initLogbookModule() {
  const photosInput = document.getElementById("logbookPhotos");
  if (photosInput) {
    photosInput.addEventListener("change", () =>
      updateLogbookSelectedFiles(photosInput)
    );
    updateLogbookSelectedFiles(photosInput);
  }
  const empty = document.getElementById("logbookEntriesEmpty");
  if (empty && !logbookEmptyText) {
    logbookEmptyText = empty.textContent || "";
  }
  const list = document.getElementById("logbookEntriesList");
  if (list) {
    list.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) return;
      const photo = target.closest(".logbook-entry-photo");
      if (!photo) return;
      const url = photo.dataset.url;
      if (url) {
        openMediaViewer(url, "photos");
      }
    });
  }
  loadLogbookEntries(false);
}

function formatLivestockDate(value) {
  if (!value) return "—";
  try {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleDateString();
    }
  } catch (err) {
    // ignore parse errors
  }
  if (typeof value === "string" && value.length >= 10) {
    return value.slice(0, 10);
  }
  return value;
}

function toLivestockDateInput(value) {
  if (!value) return "";
  try {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) {
      return date.toISOString().slice(0, 10);
    }
  } catch (err) {
    // ignore parse errors
  }
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) {
    return value.slice(0, 10);
  }
  return "";
}

function getLivestockArray(category) {
  return category === "plant"
    ? livestockCatalogState.plants
    : livestockCatalogState.animals;
}

function getLivestockBody(category) {
  const id = category === "plant" ? "livestockPlantBody" : "livestockAnimalBody";
  return document.getElementById(id);
}

function getLivestockDomKey(category) {
  return category === "plant" ? "Plant" : "Animal";
}

function getLivestockRowKey(category, entryId) {
  return entryId ? `${category}-${entryId}` : `draft-${category}`;
}

function toLivestockNumber(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function formatLivestockNumber(value) {
  const num = toLivestockNumber(value);
  if (num === null) return "";
  return Number.isInteger(num) ? `${num}` : num.toFixed(1);
}

function formatLivestockRangeLabel(label, minValue, maxValue) {
  const min = toLivestockNumber(minValue);
  const max = toLivestockNumber(maxValue);
  if (min === null && max === null) return "";
  const minText = min === null ? null : formatLivestockNumber(min);
  const maxText = max === null ? null : formatLivestockNumber(max);
  if (minText && maxText) {
    return `${label} ${minText}-${maxText}`;
  }
  if (minText) {
    return `${label} ≥${minText}`;
  }
  if (maxText) {
    return `${label} ≤${maxText}`;
  }
  return "";
}

function buildLivestockParamSummary(entry) {
  if (!entry || entry.category !== "animal") return "";
  const parts = [];
  LIVESTOCK_PARAM_FIELDS.forEach(({ key, label }) => {
    const summary = formatLivestockRangeLabel(label, entry[`${key}_min`], entry[`${key}_max`]);
    if (summary) {
      parts.push(summary);
    }
  });
  if (entry.resistance) {
    parts.push(`Resistance ${entry.resistance}`);
  }
  return parts.join(" | ");
}

function setLivestockRangeInputValue(input, value) {
  if (!input) return;
  const num = toLivestockNumber(value);
  if (num === null) {
    input.value = "";
    return;
  }
  input.value = `${num}`;
}

function applyComfortRangesToInputs(row, ranges) {
  if (!row || !ranges) return;
  LIVESTOCK_PARAM_FIELDS.forEach(({ key }) => {
    setLivestockRangeInputValue(
      row.querySelector(`.livestock-input-${key}-min`),
      ranges[`${key}_min`]
    );
    setLivestockRangeInputValue(
      row.querySelector(`.livestock-input-${key}-max`),
      ranges[`${key}_max`]
    );
  });
  const resInput = row.querySelector(".livestock-input-resistance");
  if (resInput) {
    const resistanceValue =
      typeof ranges.resistance === "string" ? ranges.resistance.trim() : "";
    resInput.value = resistanceValue;
  }
}

function setLivestockStatus(message = "", tone = "secondary") {
  const el = document.getElementById("livestockGlobalStatus");
  if (!el) return;
  ["text-secondary", "text-success", "text-danger"].forEach((cls) =>
    el.classList.remove(cls)
  );
  let className = "text-secondary";
  if (tone === "success") {
    className = "text-success";
  } else if (tone === "danger") {
    className = "text-danger";
  }
  el.classList.add(className);
  el.textContent = message || "";
}

function setLivestockLoading(category, message) {
  const body = getLivestockBody(category);
  if (!body) return;
  body.innerHTML = "";
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = LIVESTOCK_COLUMN_COUNT;
  td.className = "text-center text-secondary small py-3";
  td.textContent = message;
  tr.appendChild(td);
  body.appendChild(tr);
}

function buildLivestockEmptyRow(category) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = LIVESTOCK_COLUMN_COUNT;
  td.className = "text-center text-secondary small py-3";
  td.textContent =
    category === "plant"
      ? "Aucune plante enregistree."
      : "Aucun animal enregistre.";
  tr.appendChild(td);
  return tr;
}

function buildLivestockViewRow(entry, category) {
  const tr = document.createElement("tr");
  tr.dataset.livestockRow = "view";
  tr.dataset.entryId = entry.id || "";
  tr.dataset.category = category;
  tr.dataset.rowKey = getLivestockRowKey(category, entry.id || "");
  tr.title = "Double-cliquez pour ouvrir la fiche detaillee";

  const thumbTd = document.createElement("td");
  const thumb = document.createElement("div");
  thumb.className = "livestock-thumb";
  if (entry.photo && entry.photo.thumbnail_url) {
    const img = document.createElement("img");
    img.src = entry.photo.thumbnail_url;
    img.alt = entry.photo.filename || entry.name || "Photo";
    thumb.appendChild(img);
  } else {
    thumb.textContent = "Photo";
  }
  thumbTd.appendChild(thumb);
  tr.appendChild(thumbTd);

  const speciesTd = document.createElement("td");
  const nameEl = document.createElement("div");
  nameEl.className = "livestock-species";
  nameEl.textContent = entry.name || "Sans nom";
  speciesTd.appendChild(nameEl);
  const statusEl = document.createElement("div");
  statusEl.className = "livestock-subtext";
  const badge = document.createElement("span");
  badge.className = `livestock-status-badge ${
    entry.removed_at ? "livestock-status-archived" : "livestock-status-active"
  }`;
  badge.textContent = entry.removed_at ? "Sorti" : "Actif";
  statusEl.appendChild(badge);
  speciesTd.appendChild(statusEl);
  if (category === "animal") {
    const summaryText = buildLivestockParamSummary(entry);
    if (summaryText) {
      const paramsEl = document.createElement("div");
      paramsEl.className = "livestock-param-summary";
      paramsEl.textContent = summaryText;
      speciesTd.appendChild(paramsEl);
    }
  }
  tr.appendChild(speciesTd);

  const introTd = document.createElement("td");
  introTd.textContent = formatLivestockDate(entry.introduced_at);
  tr.appendChild(introTd);

  const countTd = document.createElement("td");
  const countValue = Number.isFinite(Number(entry.count))
    ? Number(entry.count)
    : entry.count || 0;
  countTd.textContent = countValue;
  tr.appendChild(countTd);

  const removedTd = document.createElement("td");
  removedTd.textContent = formatLivestockDate(entry.removed_at);
  tr.appendChild(removedTd);

  const actionsTd = document.createElement("td");
  actionsTd.className = "text-end";
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "btn btn-outline-light btn-sm";
  editBtn.dataset.action = "livestockEdit";
  editBtn.dataset.entryId = entry.id;
  editBtn.dataset.category = category;
  editBtn.innerHTML = '<i class="bi bi-pencil-square"></i>';
  actionsTd.appendChild(editBtn);
  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "btn btn-outline-danger btn-sm ms-2";
  deleteBtn.dataset.action = "livestockDelete";
  deleteBtn.dataset.entryId = entry.id;
  deleteBtn.dataset.category = category;
  deleteBtn.innerHTML = '<i class="bi bi-trash"></i>';
  actionsTd.appendChild(deleteBtn);
  tr.appendChild(actionsTd);

  return tr;
}

function buildAnimalParamEditor(entry) {
  const wrapper = document.createElement("div");
  wrapper.className = "livestock-animal-editor mt-2";
  const header = document.createElement("div");
  header.className = "livestock-animal-editor-header";
  const title = document.createElement("span");
  title.className = "fw-semibold";
  title.textContent = "Parametres eau";
  const aiBtn = document.createElement("button");
  aiBtn.type = "button";
  aiBtn.className = "btn btn-outline-light btn-sm ms-auto";
  aiBtn.dataset.action = "livestockFetchComfort";
  aiBtn.innerHTML = '<i class="bi bi-stars me-1"></i>Auto (IA)';
  header.appendChild(title);
  header.appendChild(aiBtn);
  wrapper.appendChild(header);
  const grid = document.createElement("div");
  grid.className = "livestock-animal-editor-grid";
  LIVESTOCK_PARAM_FIELDS.forEach(({ key, label, step }) => {
    const row = document.createElement("div");
    row.className = "livestock-param-row";
    const labelEl = document.createElement("span");
    labelEl.className = "livestock-param-label";
    labelEl.textContent = label;
    row.appendChild(labelEl);
    const group = document.createElement("div");
    group.className = "input-group input-group-sm livestock-param-inputs";
    const minInput = document.createElement("input");
    minInput.type = "number";
    minInput.step = step;
    minInput.className = `form-control livestock-input-${key}-min`;
    minInput.placeholder = "Min";
    setLivestockRangeInputValue(minInput, entry ? entry[`${key}_min`] : "");
    group.appendChild(minInput);
    const sep = document.createElement("span");
    sep.className = "input-group-text";
    sep.textContent = "à";
    group.appendChild(sep);
    const maxInput = document.createElement("input");
    maxInput.type = "number";
    maxInput.step = step;
    maxInput.className = `form-control livestock-input-${key}-max`;
    maxInput.placeholder = "Max";
    setLivestockRangeInputValue(maxInput, entry ? entry[`${key}_max`] : "");
    group.appendChild(maxInput);
    row.appendChild(group);
    grid.appendChild(row);
  });
  wrapper.appendChild(grid);
  const resistanceGroup = document.createElement("div");
  resistanceGroup.className = "mt-2";
  const resistanceLabel = document.createElement("label");
  resistanceLabel.className = "form-label form-label-sm mb-1";
  resistanceLabel.textContent = "Resistance (fragile, robuste, etc.)";
  resistanceGroup.appendChild(resistanceLabel);
  const resistanceInput = document.createElement("input");
  resistanceInput.type = "text";
  resistanceInput.className = "form-control form-control-sm livestock-input-resistance";
  resistanceInput.placeholder = "Ex: Faible, Moyenne, Elevee";
  resistanceInput.value = entry && entry.resistance ? entry.resistance : "";
  resistanceGroup.appendChild(resistanceInput);
  wrapper.appendChild(resistanceGroup);
  const note = document.createElement("div");
  note.className = "text-secondary small mt-1";
  note.textContent =
    "Precisez les plages min/max pour l'eau ou utilisez l'assistant IA.";
  wrapper.appendChild(note);
  return wrapper;
}

function buildLivestockEditRow(entry, category) {
  const tr = document.createElement("tr");
  tr.className = "livestock-edit-row";
  tr.dataset.livestockRow = "edit";
  tr.dataset.category = category;
  tr.dataset.editing = "true";
  tr.dataset.entryId = entry && entry.id ? entry.id : "";
  tr.dataset.rowKey = getLivestockRowKey(category, entry && entry.id ? entry.id : "");
  const currentPhoto =
    entry && entry.photo && entry.photo.thumbnail_url ? entry.photo.thumbnail_url : "";
  if (currentPhoto) {
    tr.dataset.photoUrl = currentPhoto;
  }

  const thumbTd = document.createElement("td");
  const thumb = document.createElement("div");
  thumb.className = "livestock-thumb";
  if (currentPhoto) {
    const img = document.createElement("img");
    img.src = currentPhoto;
    img.alt = entry.photo.filename || "Photo";
    thumb.appendChild(img);
  } else {
    thumb.textContent = "Photo";
  }
  thumbTd.appendChild(thumb);
  const photoInput = document.createElement("input");
  photoInput.type = "file";
  photoInput.accept = "image/*";
  photoInput.className =
    "form-control form-control-sm livestock-photo-input mt-1 livestock-input-photo";
  thumbTd.appendChild(photoInput);
  tr.appendChild(thumbTd);

  const speciesTd = document.createElement("td");
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "form-control form-control-sm livestock-input-name";
  nameInput.placeholder = "Nom de l'espece";
  nameInput.value = entry ? entry.name || "" : "";
  speciesTd.appendChild(nameInput);
  const hint = document.createElement("div");
  hint.className = "livestock-row-status mt-1";
  hint.textContent = "Cliquez sur la disquette pour enregistrer.";
  speciesTd.appendChild(hint);
  if (category === "animal") {
    speciesTd.appendChild(buildAnimalParamEditor(entry || {}));
  }
  tr.appendChild(speciesTd);

  const introTd = document.createElement("td");
  const introInput = document.createElement("input");
  introInput.type = "date";
  introInput.className = "form-control form-control-sm livestock-input-intro";
  introInput.value = toLivestockDateInput(entry ? entry.introduced_at : "");
  introTd.appendChild(introInput);
  tr.appendChild(introTd);

  const countTd = document.createElement("td");
  const countInput = document.createElement("input");
  countInput.type = "number";
  countInput.min = "0";
  countInput.step = "1";
  countInput.className = "form-control form-control-sm livestock-input-count";
  const defaultCount =
    entry && Number.isFinite(Number(entry.count)) ? Number(entry.count) : 1;
  countInput.value = defaultCount;
  countTd.appendChild(countInput);
  tr.appendChild(countTd);

  const removedTd = document.createElement("td");
  const removedInput = document.createElement("input");
  removedInput.type = "date";
  removedInput.className = "form-control form-control-sm livestock-input-removed";
  removedInput.value = toLivestockDateInput(entry ? entry.removed_at : "");
  removedTd.appendChild(removedInput);
  tr.appendChild(removedTd);

  const actionsTd = document.createElement("td");
  actionsTd.className = "text-end";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn btn-sm btn-aqua me-2";
  saveBtn.dataset.action = "livestockSave";
  saveBtn.innerHTML = '<i class="bi bi-save"></i>';
  actionsTd.appendChild(saveBtn);
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "btn btn-sm btn-outline-light";
  cancelBtn.dataset.action = "livestockCancelEdit";
  cancelBtn.innerHTML = '<i class="bi bi-x-circle"></i>';
  actionsTd.appendChild(cancelBtn);
  tr.appendChild(actionsTd);

  return tr;
}

function renderLivestockZone(category, entries) {
  const body = getLivestockBody(category);
  if (!body) return;
  body.innerHTML = "";
  let rows = 0;
  if (livestockEditContext && livestockEditContext.category === category && livestockEditContext.isNew) {
    body.appendChild(buildLivestockEditRow(null, category));
    rows += 1;
  }
  entries.forEach((entry) => {
    const isEditing =
      livestockEditContext &&
      livestockEditContext.category === category &&
      !livestockEditContext.isNew &&
      livestockEditContext.id === entry.id;
    const row = isEditing
      ? buildLivestockEditRow(entry, category)
      : buildLivestockViewRow(entry, category);
    body.appendChild(row);
    rows += 1;
  });
  if (!rows) {
    body.appendChild(buildLivestockEmptyRow(category));
  }
}

function updateLivestockCounters() {
  ["animal", "plant"].forEach((category) => {
    const entries = getLivestockArray(category);
    let active = 0;
    entries.forEach((entry) => {
      if (!entry.removed_at) {
        active += 1;
      }
    });
    const archived = Math.max(0, entries.length - active);
    const domKey = getLivestockDomKey(category);
    const activeEl = document.getElementById(`livestock${domKey}ActiveBadge`);
    if (activeEl) {
      activeEl.textContent = `Actifs: ${active}`;
    }
    const inactiveEl = document.getElementById(`livestock${domKey}InactiveBadge`);
    if (inactiveEl) {
      inactiveEl.textContent = `Sortis: ${archived}`;
    }
  });
}

function renderLivestockTables() {
  renderLivestockZone("animal", livestockCatalogState.animals || []);
  renderLivestockZone("plant", livestockCatalogState.plants || []);
  updateLivestockCounters();
}

function findLivestockEntry(category, entryId) {
  if (!entryId) return null;
  return getLivestockArray(category).find((entry) => entry.id === entryId) || null;
}

function cleanupLivestockPreviews(rowKey) {
  if (typeof rowKey === "string" && rowKey) {
    const existing = livestockPreviewUrls.get(rowKey);
    if (existing) {
      URL.revokeObjectURL(existing);
      livestockPreviewUrls.delete(rowKey);
    }
    return;
  }
  livestockPreviewUrls.forEach((url) => URL.revokeObjectURL(url));
  livestockPreviewUrls.clear();
}

function handleLivestockPhotoPreview(input) {
  const row = input.closest("[data-livestock-row]");
  if (!row) return;
  const key = row.dataset.rowKey || row.dataset.entryId || `draft-${row.dataset.category || ""}`;
  cleanupLivestockPreviews(key);
  const file = input.files && input.files[0];
  const thumb = row.querySelector(".livestock-thumb");
  if (!thumb) return;
  if (file) {
    const url = URL.createObjectURL(file);
    livestockPreviewUrls.set(key, url);
    thumb.innerHTML = "";
    const img = document.createElement("img");
    img.src = url;
    img.alt = file.name;
    thumb.appendChild(img);
  } else {
    const photoUrl = row.dataset.photoUrl;
    thumb.innerHTML = "";
    if (photoUrl) {
      const img = document.createElement("img");
      img.src = photoUrl;
      img.alt = "Photo";
      thumb.appendChild(img);
    } else {
      thumb.textContent = "Photo";
    }
  }
}

function startLivestockCreate(category) {
  if (!category) return;
  if (livestockEditContext) {
    if (livestockEditContext.category === category && livestockEditContext.isNew) {
      return;
    }
    showToast("Terminez l'edition en cours avant d'ajouter une nouvelle fiche.", "warning");
    return;
  }
  livestockEditContext = { category, id: null, isNew: true };
  renderLivestockTables();
  const label = LIVESTOCK_CATEGORY_LABELS[category] || category;
  setLivestockStatus(`Edition ${label} en cours.`, "secondary");
}

function startLivestockEdit(category, entryId) {
  if (!category || !entryId) return;
  if (livestockEditContext) {
    if (!livestockEditContext.isNew && livestockEditContext.id === entryId) {
      return;
    }
    showToast("Terminez l'edition en cours avant de modifier une autre fiche.", "warning");
    return;
  }
  const entry = findLivestockEntry(category, entryId);
  if (!entry) {
    showToast("Entree introuvable.", "danger");
    return;
  }
  livestockEditContext = { category, id: entryId, isNew: false };
  renderLivestockTables();
  setLivestockStatus(`Edition de ${entry.name || "cette fiche"}.`, "secondary");
}

function cancelLivestockEdit() {
  if (!livestockEditContext) return;
  cleanupLivestockPreviews();
  livestockEditContext = null;
  renderLivestockTables();
  setLivestockStatus("Edition annulee.", "secondary");
}

async function saveLivestockEntry(trigger) {
  if (!livestockEditContext) {
    showToast("Aucune edition en cours.", "warning");
    return;
  }
  const row = trigger.closest("[data-livestock-row]");
  if (!row) return;
  const category = livestockEditContext.category;
  const nameInput = row.querySelector(".livestock-input-name");
  const introInput = row.querySelector(".livestock-input-intro");
  const removedInput = row.querySelector(".livestock-input-removed");
  const countInput = row.querySelector(".livestock-input-count");
  const photoInput = row.querySelector(".livestock-input-photo");
  const paramInputs = {};
  LIVESTOCK_PARAM_FIELDS.forEach(({ key }) => {
    paramInputs[key] = {
      min: row.querySelector(`.livestock-input-${key}-min`),
      max: row.querySelector(`.livestock-input-${key}-max`),
    };
  });
  const resistanceInput = row.querySelector(".livestock-input-resistance");
  const name = (nameInput?.value || "").trim();
  if (!name) {
    showToast("Ajoutez un nom d'espece.", "danger");
    nameInput?.focus();
    return;
  }
  const introducedValue = introInput?.value || "";
  const removedValue = removedInput?.value || "";
  const countValue = countInput?.value || "0";

  const formData = new FormData();
  formData.append("name", name);
  formData.append("introduced_at", introducedValue);
  formData.append("removed_at", removedValue);
  formData.append("count", countValue);
  formData.append("category", category);
  LIVESTOCK_PARAM_FIELDS.forEach(({ key }) => {
    const minInput = paramInputs[key]?.min;
    const maxInput = paramInputs[key]?.max;
    if (minInput) {
      formData.append(`${key}_min`, minInput.value || "");
    }
    if (maxInput) {
      formData.append(`${key}_max`, maxInput.value || "");
    }
  });
  if (resistanceInput) {
    formData.append("resistance", resistanceInput.value.trim());
  }
  if (photoInput && photoInput.files && photoInput.files[0]) {
    formData.append("photo", photoInput.files[0]);
  }

  const endpoint = livestockEditContext.isNew
    ? "/logbook/catalog"
    : `/logbook/catalog/${livestockEditContext.id}`;
  const method = livestockEditContext.isNew ? "POST" : "PUT";
  const cancelBtn = row.querySelector('[data-action="livestockCancelEdit"]');

  trigger.disabled = true;
  if (cancelBtn) cancelBtn.disabled = true;
  setLivestockStatus("Enregistrement en cours...", "secondary");
  try {
    const res = await fetch(endpoint, { method, body: formData });
    let data = {};
    try {
      data = await res.json();
    } catch (err) {
      // ignore parse error
    }
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Fiche enregistree.", "success");
    setLivestockStatus("Fiche enregistree.", "success");
    livestockEditContext = null;
    cleanupLivestockPreviews();
    await loadLivestockCatalog(false, false);
  } catch (err) {
    console.error("saveLivestockEntry", err);
    showToast(`Sauvegarde impossible: ${err.message}`, "danger");
    setLivestockStatus(`Sauvegarde impossible: ${err.message}`, "danger");
  } finally {
    trigger.disabled = false;
    if (cancelBtn) cancelBtn.disabled = false;
  }
}

async function deleteLivestockEntry(trigger) {
  const entryId = trigger.dataset.entryId;
  const category = trigger.dataset.category;
  if (!entryId || !category) return;
  if (livestockEditContext) {
    showToast("Terminez l'edition avant de supprimer une fiche.", "warning");
    return;
  }
  const entry = findLivestockEntry(category, entryId);
  const label = entry?.name || "cette fiche";
  const confirmed = await showPopin(
    `Supprimer la fiche ${label} ?`,
    "danger",
    { confirmable: true, confirmText: "Supprimer", cancelText: "Annuler" }
  );
  if (!confirmed) return;
  trigger.disabled = true;
  setLivestockStatus("Suppression de la fiche...", "secondary");
  try {
    const res = await fetch(`/logbook/catalog/${entryId}`, { method: "DELETE" });
    let data = {};
    try {
      data = await res.json();
    } catch (err) {
      // ignore parse error
    }
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    showToast("Fiche supprimee.", "success");
    setLivestockStatus("Fiche supprimee.", "success");
    await loadLivestockCatalog(false, false);
  } catch (err) {
    console.error("deleteLivestockEntry", err);
    showToast(`Suppression impossible: ${err.message}`, "danger");
    setLivestockStatus(`Suppression impossible: ${err.message}`, "danger");
  } finally {
    trigger.disabled = false;
  }
}

async function fetchLivestockComfort(trigger) {
  const row = trigger.closest("[data-livestock-row]");
  if (!row) return;
  const nameInput = row.querySelector(".livestock-input-name");
  const speciesName = (nameInput?.value || "").trim();
  if (!speciesName) {
    showToast("Ajoutez un nom d'espece avant d'utiliser l'IA.", "warning");
    nameInput?.focus();
    return;
  }
  const prevLabel = trigger.innerHTML;
  trigger.disabled = true;
  trigger.innerHTML =
    '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>IA...';
  setLivestockStatus("Recherche des parametres via IA...", "secondary");
  try {
    const res = await fetch("/logbook/catalog/comfort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: speciesName, category: "animal" }),
    });
    let data = {};
    try {
      data = await res.json();
    } catch (err) {
      // ignore parsing error here, fallback to HTTP status
    }
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    applyComfortRangesToInputs(row, data.ranges || {});
    showToast("Parametres IA importes.", "success");
    setLivestockStatus("Parametres IA importes.", "success");
  } catch (err) {
    console.error("fetchLivestockComfort", err);
    showToast(`IA indisponible: ${err.message}`, "danger");
    setLivestockStatus(`IA indisponible: ${err.message}`, "danger");
  } finally {
    trigger.disabled = false;
    trigger.innerHTML = prevLabel;
  }
}

async function loadLivestockCatalog(showErrors = true, resetStatus = true) {
  const zone = document.getElementById("livestockCatalogZone");
  if (!zone) return;
  if (resetStatus) {
    setLivestockStatus("Chargement du catalogue...", "secondary");
  }
  if (!livestockEditContext) {
    ["animal", "plant"].forEach((category) =>
      setLivestockLoading(category, "Chargement du catalogue...")
    );
  }
  try {
    const res = await fetch("/logbook/catalog");
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    livestockCatalogState.animals = Array.isArray(data.animals) ? data.animals : [];
    livestockCatalogState.plants = Array.isArray(data.plants) ? data.plants : [];
    renderLivestockTables();
    loadWaterTargets(true);
    if (resetStatus) {
      setLivestockStatus("", "secondary");
    }
  } catch (err) {
    console.error("loadLivestockCatalog", err);
    if (showErrors) {
      showToast(`Catalogue vivant indisponible: ${err.message}`, "danger");
    }
    setLivestockStatus(`Catalogue indisponible: ${err.message}`, "danger");
    ["animal", "plant"].forEach((category) => {
      const body = getLivestockBody(category);
      if (body && !body.children.length) {
        setLivestockLoading(category, `Erreur: ${err.message}`);
      }
    });
  }
}

function openLivestockDetail(entry) {
  if (!mediaViewerEl || !mediaViewerContentEl) return;
  mediaViewerContentEl.innerHTML = "";
  const wrapper = document.createElement("div");
  wrapper.className = "livestock-detail";
  if (entry.photo && entry.photo.url) {
    const img = document.createElement("img");
    img.src = entry.photo.url;
    img.alt = entry.photo.filename || entry.name || "Photo";
    wrapper.appendChild(img);
  }
  const info = document.createElement("div");
  info.className = "livestock-detail-info";
  const details = [
    ["Categorie", LIVESTOCK_CATEGORY_LABELS[entry.category] || entry.category || "-"],
    ["Espece", entry.name || "-"],
    ["Introduit", formatLivestockDate(entry.introduced_at)],
    ["Nombre", entry.count ?? "-"],
    ["Sortie", formatLivestockDate(entry.removed_at)],
  ];
  if (entry.category === "animal") {
    LIVESTOCK_PARAM_FIELDS.forEach(({ key, label }) => {
      const text = formatLivestockRangeLabel(label, entry[`${key}_min`], entry[`${key}_max`]);
      details.push([label, text || "Non renseigne"]);
    });
    if (entry.resistance) {
      details.push(["Resistance", entry.resistance]);
    }
  }
  details.forEach(([label, value]) => {
    const group = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = label;
    const span = document.createElement("span");
    span.textContent = value === undefined || value === null || value === "" ? "—" : value;
    group.appendChild(title);
    group.appendChild(document.createElement("br"));
    group.appendChild(span);
    info.appendChild(group);
  });
  wrapper.appendChild(info);
  mediaViewerContentEl.appendChild(wrapper);
  mediaViewerEl.classList.remove("d-none");
  mediaViewerEl.classList.add("show");
}

function initLivestockModule() {
  const zone = document.getElementById("livestockCatalogZone");
  if (!zone) return;
  zone.addEventListener("dblclick", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const row = target.closest("[data-livestock-row]");
    if (!row || row.dataset.editing === "true") return;
    const category = row.dataset.category;
    const entryId = row.dataset.entryId;
    if (!category || !entryId) return;
    const entry = findLivestockEntry(category, entryId);
    if (entry) {
      openLivestockDetail(entry);
    }
  });
  zone.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.classList.contains("livestock-input-photo")) {
      handleLivestockPhotoPreview(target);
    }
  });
  loadLivestockCatalog(false);
}


function initMediaViewer() {
  mediaViewerEl = document.getElementById("mediaViewer");
  mediaViewerContentEl = document.getElementById("mediaViewerContent");
  const closeBtn = document.getElementById("mediaViewerClose");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => hideMediaViewer());
  }
  if (mediaViewerEl) {
    mediaViewerEl.addEventListener("click", (event) => {
      if (
        event.target === mediaViewerEl ||
        event.target.classList.contains("media-viewer-backdrop")
      ) {
        hideMediaViewer();
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideMediaViewer();
    }
  });
}

function openMediaViewer(url, mediaType) {
  if (!mediaViewerEl || !mediaViewerContentEl) return;
  mediaViewerContentEl.innerHTML = "";
  if (mediaType === "videos") {
    const video = document.createElement("video");
    video.src = url;
    video.controls = true;
    video.autoplay = true;
    video.className = "w-100";
    mediaViewerContentEl.appendChild(video);
  } else {
    const img = document.createElement("img");
    img.src = url;
    img.alt = "Media";
    mediaViewerContentEl.appendChild(img);
  }
  mediaViewerEl.classList.remove("d-none");
  mediaViewerEl.classList.add("show");
}

function hideMediaViewer() {
  if (!mediaViewerEl || !mediaViewerContentEl) return;
  mediaViewerEl.classList.add("d-none");
  mediaViewerEl.classList.remove("show");
  mediaViewerContentEl.innerHTML = "";
}

function initAiModule() {
  const picker = document.getElementById("aiPhotoPicker");
  if (picker) {
    picker.addEventListener("click", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.closest(".ai-photo-option")
          : null;
      if (!target) return;
      const filename = target.getAttribute("data-photo-name");
      toggleAiPhotoSelection(filename);
    });
  }
  const summaryContainer = document.getElementById("aiSummaryContent");
  if (summaryContainer) {
    summaryContainer.addEventListener("click", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.closest("[data-ai-image-url]")
          : null;
      if (!target) return;
      const url = target.getAttribute("data-ai-image-url");
      if (url) {
        openMediaViewer(url, "photos");
      }
    });
  }
  loadAiConfig();
  loadAiSummary(true);
  loadAiInsights();
  loadAiPhotoOptions();
  loadAiWorkerStatus(true);
  if (!aiWorkerStatusTimer) {
    aiWorkerStatusTimer = setInterval(() => loadAiWorkerStatus(false), 30000);
  }
}

function init() {
  initDelegates();
  setupTabPersistence();
  initPopin();
  initCameraModule();
  initLogbookModule();
  initLivestockModule();
  initAiModule();
  loadWaterTargets();
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

window.addEventListener("beforeunload", () => {
  if (esp32CamPreviewUrl) {
    URL.revokeObjectURL(esp32CamPreviewUrl);
    esp32CamPreviewUrl = null;
  }
});

function initPopin() {
  const popin = document.getElementById("reefPopin");
  const confirmBtn = document.getElementById("reefPopinConfirm");
  const cancelBtn = document.getElementById("reefPopinCancel");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", () => hidePopin(true));
  }
  if (cancelBtn) {
    cancelBtn.addEventListener("click", () => hidePopin(popinIsConfirm ? false : true));
  }
  if (popin) {
    popin.addEventListener("click", (event) => {
      if (event.target === popin) {
        hidePopin(popinIsConfirm ? false : true);
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hidePopin(popinIsConfirm ? false : true);
    }
  });
}

function showPopin(message, type = "info", options = {}) {
  const { confirmable = false, confirmText = "OK", cancelText = "Annuler" } =
    options || {};
  const popin = document.getElementById("reefPopin");
  const icon = document.getElementById("reefPopinIcon");
  const msg = document.getElementById("reefPopinMessage");
  const confirmBtn = document.getElementById("reefPopinConfirm");
  const cancelBtn = document.getElementById("reefPopinCancel");
  if (!popin || !icon || !msg || !confirmBtn || !cancelBtn) {
    console.warn("Popin unavailable", message);
    return Promise.resolve(confirmable ? false : true);
  }
  if (!popin.classList.contains("d-none")) {
    hidePopin(popinIsConfirm ? false : true);
  }
  const iconMap = {
    danger: "⛔",
    warning: "⚠️",
    success: "✅",
    info: "💬",
  };
  icon.textContent = iconMap[type] || iconMap.info;
  msg.textContent = message;
  confirmBtn.textContent = confirmText;
  cancelBtn.textContent = cancelText;
  cancelBtn.classList.toggle("d-none", !confirmable);
  popinIsConfirm = confirmable;
  popin.classList.remove("d-none");
  popin.classList.add("show");
  return new Promise((resolve) => {
    popinResolver = resolve;
  });
}

function hidePopin(result) {
  const popin = document.getElementById("reefPopin");
  if (!popin) return;
  popin.classList.add("d-none");
  popin.classList.remove("show");
  if (typeof popinResolver === "function") {
    const finalValue =
      typeof result === "undefined" ? (!popinIsConfirm || !popinResolver ? true : false) : result;
    popinResolver(finalValue);
  }
  popinResolver = null;
  popinIsConfirm = false;
}

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
