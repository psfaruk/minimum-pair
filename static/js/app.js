window.App = {
  state: {
    pairs: { otc: [], forex: [] },
    winRates: {},
    latestSignal: null,
    activePair: null,
    status: { quotex_connected: false, error: null, active_pairs: [] },
  },
  tabs: {},
  ws: null,
};

App.fmtPct = (v) => (v === null || v === undefined ? "--%" : (v * 100).toFixed(1) + "%");
App.fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

App.api = async (path) => {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
};

App.apiPost = async (path, body) => {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> ${res.status}`);
  return data;
};

App.allPairNames = () => [...App.state.pairs.forex, ...App.state.pairs.otc];

// A confidence of null means the sources behind this signal don't have
// enough graded history to state a rate yet. Showing "0%" (what the old
// `confidence * 100` did with a null) reads as "certainly wrong", which
// is the opposite of "not known".
App.fmtConf = (c) => (c === null || c === undefined ? "conf —" : `conf ${(c * 100).toFixed(0)}%`);

// Fallback-tier signals exist so every pair always shows something on
// every candle. They pass no quality gate and carry no measured
// confidence, so they must never look like the confirmed ones. "noise"
// is the pre-2026-08-30 name for the same concept, kept for old rows.
App.tierBadge = (tier) =>
  tier === "fallback" || tier === "noise"
    ? '<span class="tier-badge noise" title="No confirmation — best-effort fallback signal">fallback</span>'
    : "";

App.refreshWinRates = async () => {
  const rows = await App.api("/api/winrate");
  const map = {};
  for (const r of rows) map[r.pair] = r;
  App.state.winRates = map;
};

function switchView(name) {
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  document.getElementById(`view-${name}`).classList.add("active");
  document.querySelectorAll(".navbtn").forEach((el) => el.classList.toggle("active", el.dataset.view === name));
  const tab = App.tabs[name];
  if (tab && tab.onShow) tab.onShow();
}

function setupNav() {
  document.querySelectorAll(".navbtn").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
}

function setConnUI(connected, text) {
  const dot = document.getElementById("connDot");
  const label = document.getElementById("connText");
  dot.classList.remove("connected", "error");
  if (connected === true) dot.classList.add("connected");
  else if (connected === false) dot.classList.add("error");
  label.textContent = text;
}

async function pollStatus() {
  try {
    const s = await App.api("/api/status");
    App.state.status = s;
    if (s.quotex_connected) setConnUI(true, `live (${s.active_pairs.length} pairs)`);
    else if (s.error) setConnUI(false, "connection failed");
    else setConnUI(null, "connecting…");
    if (App.tabs.settings && App.tabs.settings.onStatus) App.tabs.settings.onStatus(s);
  } catch (e) {
    setConnUI(false, "server unreachable");
  }
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  App.ws = ws;
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    for (const name in App.tabs) {
      const tab = App.tabs[name];
      if (msg.type === "candle" && tab.onCandle) tab.onCandle(msg);
      if (msg.type === "signal" && tab.onSignal) tab.onSignal(msg);
      if (msg.type === "graded" && tab.onGraded) tab.onGraded(msg);
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2000);
  ws.onerror = () => ws.close();
}

async function boot() {
  setupNav();
  const p = await App.api("/api/pairs");
  App.state.pairs = p;
  await App.refreshWinRates();
  connectWS();
  pollStatus();
  setInterval(pollStatus, 8000);
  for (const name in App.tabs) {
    if (App.tabs[name].onInit) App.tabs[name].onInit();
  }
  switchView("home");
}

document.addEventListener("DOMContentLoaded", boot);
