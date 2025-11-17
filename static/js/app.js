const LIGHT_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

let refreshTimer = null;
let currentPumpConfig = {};
let currentLightState = false;
let currentLightAuto = true;
let globalSpeedUs = 300;
let currentHeatAuto = true;
let currentHeatEnabled = true;

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
        return data;
    } catch (err) {
        console.error("Action", action, err);
        alert("Erreur: " + err.message);
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
}

function restartRefreshTimer() {
    stopRefreshTimer();
    refreshState();
    refreshTimer = setInterval(refreshState, 1000);
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

function applyPIDWater() {
    const P = parseFloat(document.getElementById("pidwP").value || "0");
    const I = parseFloat(document.getElementById("pidwI").value || "0");
    const D = parseFloat(document.getElementById("pidwD").value || "0");
    apiAction("pid_water", { P, I, D });
}

function applyPIDRes() {
    const P = parseFloat(document.getElementById("pidrP").value || "0");
    const I = parseFloat(document.getElementById("pidrI").value || "0");
    const D = parseFloat(document.getElementById("pidrD").value || "0");
    apiAction("pid_reserve", { P, I, D });
}

function onAutoFanToggle() {
    const auto = document.getElementById("autoFanChk").checked;
    apiAction("auto_fan", { auto });
}

function onFanChange(value) {
    const val = parseInt(value, 10) || 0;
    document.getElementById("fan_val").textContent = val;
    document.getElementById("fanSlider").value = val;
    apiAction("fan_manual", { value: val });
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
    const angle = parseInt(document.getElementById("servoAngle").value || "0", 10);
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

function applyGlobalSpeed() {
    const value = parseInt(document.getElementById("globalSpeedInput").value || "0", 10);
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

    document.getElementById("tw_val").textContent = `${state.tw || "--.-"}°C`;
    document.getElementById("ta_val").textContent = `${state.ta || "--.-"}°C`;
    document.getElementById("tx_val").textContent = `${state.tx || "--.-"}°C`;
    document.getElementById("tymin_val").textContent = `${state.ty_min || "--.-"}°C`;
    document.getElementById("tymax_val").textContent = `${state.ty_max || "--.-"}°C`;
    const tempNames = state.temp_names || {};
    const tname = (k, d) => tempNames[k] || d;
    const mirrorLabel = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val;
    };
    mirrorLabel("tw_label", tname("water", "Eau"));
    mirrorLabel("ta_label", tname("air", "Air"));
    mirrorLabel("tx_label", tname("aux", "Aux"));
    mirrorLabel("tymin_label", tname("ymin", "Y-Min"));
    mirrorLabel("tymax_label", tname("ymax", "Y-Max"));
    mirrorLabel("tymin_label2", tname("ymin", "Y-Min"));
    mirrorLabel("tymax_label2", tname("ymax", "Y-Max"));
    const mirrorVal = (id, val, suffix = "°C") => {
        const el = document.getElementById(id);
        if (el) el.textContent = `${val || "--.-"}${suffix}`;
    };
    mirrorVal("tymin_val2", state.ty_min);
    mirrorVal("tymax_val2", state.ty_max);
    setInputIfIdle("tempName_water", tname("water", "Eau"));
    setInputIfIdle("tempName_air", tname("air", "Air"));
    setInputIfIdle("tempName_aux", tname("aux", "Aux"));
    setInputIfIdle("tempName_ymin", tname("ymin", "Y-Min"));
    setInputIfIdle("tempName_ymax", tname("ymax", "Y-Max"));
    const phV = state.ph_v ?? state.phV ?? null;
    const phRaw = state.ph_raw ?? state.phRaw ?? null;
    document.getElementById("ph_v_val").textContent = phV !== null && phV !== undefined ? `${phV} V` : "--.- V";
    document.getElementById("ph_raw_val").textContent = phRaw !== null && phRaw !== undefined ? phRaw : "----";
    const heatTargets = state.heat_targets || {};
    document.getElementById("tset_water_label").textContent = `${heatTargets.water ?? state.tset_water ?? "--.-"}°C`;
    document.getElementById("tset_res_label").textContent = `${heatTargets.reserve ?? state.tset_res ?? "--.-"}°C`;
    setInputIfIdle("tset_water2", heatTargets.water ?? state.tset_water ?? "");
    setInputIfIdle("tset_res2", heatTargets.reserve ?? state.tset_res ?? "");

    document.getElementById("pidwP").value = state.pidw?.[0] ?? 0;
    document.getElementById("pidwI").value = state.pidw?.[1] ?? 0;
    document.getElementById("pidwD").value = state.pidw?.[2] ?? 0;
    document.getElementById("pidrP").value = state.pidr?.[0] ?? 0;
    document.getElementById("pidrI").value = state.pidr?.[1] ?? 0;
    document.getElementById("pidrD").value = state.pidr?.[2] ?? 0;

    document.getElementById("autoFanChk").checked = !!state.auto_fan;
    document.getElementById("autoFanModeBadge").textContent = state.auto_fan ? "Auto" : "Manuel";
    document.getElementById("auto_thresh").value = state.auto_thresh ?? 28;
    document.getElementById("auto_thresh_label").textContent = state.auto_thresh ?? "--.-";
    document.getElementById("fan_val").textContent = state.fan ?? 0;
    document.getElementById("fanSlider").value = state.fan ?? 0;

    applyLevelBadge("lvl_low", state.lvl_low, false);
    applyLevelBadge("lvl_high", state.lvl_high, true);
    applyLevelBadge("lvl_alert", state.lvl_alert, false);

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

    const gsi = document.getElementById("globalSpeedInput");
    if (gsi) {
        const currentSpeed = state.global_speed ?? state.speed ?? globalSpeedUs;
        gsi.value = currentSpeed;
        globalSpeedUs = currentSpeed;
    }

    document.getElementById("mtrAutoChk").checked = !!state.mtr_auto_off;
    document.getElementById("servoAngle").value = state.servo_angle ?? 10;

    currentLightState = !!state.light_state;
    currentLightAuto = !!state.light_auto;
    updateLightUI(state);

    currentHeatAuto = !!state.heat_auto;
    currentHeatEnabled = !!state.heat_enabled;
    updateHeatUI();
}

function applyLevelBadge(id, val, okWhenOne) {
    const el = document.getElementById(id);
    if (!el) return;
    const text = String(val ?? "?");
    el.textContent = `${el.textContent.split(":")[0]}: ${text}`;
    el.classList.remove("level-pill-ok", "level-pill-bad", "level-pill-unk");
    if (text === "?") {
        el.classList.add("level-pill-unk");
    } else if (text === "1") {
        el.classList.add(okWhenOne ? "level-pill-ok" : "level-pill-bad");
    } else if (text === "0") {
        el.classList.add(okWhenOne ? "level-pill-bad" : "level-pill-ok");
    } else {
        el.classList.add("level-pill-unk");
    }
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
    if (input && !input.matches(":focus")) {
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
        btn.classList.toggle("btn-outline-light", !currentHeatEnabled || currentHeatAuto);
    }
}

function setInputIfIdle(id, value) {
    const input = document.getElementById(id);
    if (input && !input.matches(":focus")) {
        input.value = value ?? "";
    }
}

async function saveTempNames() {
    const payload = {
        water: document.getElementById("tempName_water")?.value || "",
        air: document.getElementById("tempName_air")?.value || "",
        aux: document.getElementById("tempName_aux")?.value || "",
        ymin: document.getElementById("tempName_ymin")?.value || "",
        ymax: document.getElementById("tempName_ymax")?.value || "",
    };
    await apiAction("update_temp_names", payload);
    refreshState();
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
    applyWater: () => applyWater(),
    applyRes: () => applyRes(),
    applyPIDWater: () => applyPIDWater(),
    applyPIDRes: () => applyPIDRes(),
    applyGlobalSpeed: () => applyGlobalSpeed(),
    editPumpName: (el) => enablePumpNameEdit(el.dataset.axis),
    pumpSave: (el) => savePumpConfig(el.dataset.axis),
    saveLightSchedule: (el) => saveLightSchedule(el.dataset.day),
    toggleLight: () => toggleLight(),
    heatPower: () => toggleHeatPower(),
};

const changeHandlers = {
    autoFanToggle: () => onAutoFanToggle(),
    protectToggle: () => onProtectToggle(),
    mtrAutoToggle: () => onMtrAutoChanged(),
    lightAuto: (target) => setLightAuto(target.value === "1"),
    heatMode: (target) => setHeatMode(target.checked),
    saveTempNames: () => saveTempNames(),
};

function initDelegates() {
    document.addEventListener("click", (event) => {
        const target = (event.target instanceof Element) ? event.target.closest("[data-action]") : null;
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

    document.addEventListener("input", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        if (!target) return;
        if (target.dataset.input === "fanSlider") {
            onFanChange(target.value);
        }
    });
}

function init() {
    initDelegates();
    refreshPorts();
    refreshState();
    refreshTimer = setInterval(refreshState, 1000);
}

document.addEventListener("DOMContentLoaded", init);
