window.App = {
  state: {
    pairs: { otc: [], forex: [] },
    winRates: {},        // pair -> ext win-rate row (call/put/confirmed/fallback splits)
    summary: null,       // global summary row
    liveSignals: {},     // pair -> latest signal row
    livePrices: {},      // pair -> last price
    candleTs: {},        // pair -> current running candle start ts
    activePair: null,
    status: { quotex_connected: false, error: null, active_pairs: [] },
    tierFilter: "",      // live view tier filter
    dirFilter: "",       // live view direction filter
    daysWindow: 7,       // stats view window
  },
  tabs: {},
  ws: null,
};

App.fmtPct = (v) => (v === null || v === undefined ? "--%" : (v * 100).toFixed(1) + "%");
App.fmtTime = (ts) =>
  new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
App.fmtPrice = (p) =>
  p === null || p === undefined ? "--" : Number(p).toFixed(Number(p) >= 100 ? 2 : 5);

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
// enough graded history to state a rate yet.
App.fmtConf = (c) => (c === null || c === undefined ? "" : `কনফ ${(c * 100).toFixed(0)}%`);

// Fallback-tier signals exist so every pair always shows something on
// every candle. They pass no quality gate. "noise" is the old name for
// the same concept, kept for old rows.
App.tierBadge = (tier) =>
  tier === "fallback" || tier === "noise"
    ? '<span class="tier-badge fallback" title="কোনো কনফার্মেশন নেই — সেরা-প্রচেষ্টা ফলব্যাক সিগন্যাল">fallback</span>'
    : '<span class="tier-badge confirmed" title="সব কোয়ালিটি গেট পাস করেছে">confirmed</span>';

App.dirBadge = (direction, small) =>
  `<span class="dir-badge ${direction}${small ? " small" : ""}">${
    direction === "CALL" ? '<span class="arrow">▲</span>' : '<span class="arrow">▼</span>'
  }${direction}</span>`;

App.resultBadge = (result) =>
  `<span class="result-badge ${result || "PENDING"}">${result || "PENDING"}</span>`;

// Win-rate colour class — under ~30 graded trades a rate is mostly
// noise, so it reads as undecided rather than good/bad.
App.rateClass = (rate, samples) => {
  if (rate === null || rate === undefined) return "rate-thin";
  if (samples !== undefined && samples < 30) return "rate-mid";
  if (rate >= 0.55) return "rate-good";
  if (rate < 0.45) return "rate-bad";
  return "rate-mid";
};

App.refreshWinRates = async (days) => {
  const d = days === undefined ? 0 : days;
  const [rows, summary] = await Promise.all([
    App.api(`/api/winrate?days=${d}`),
    App.api(`/api/winrate/summary?days=${d}`),
  ]);
  const map = {};
  for (const r of rows) map[r.pair] = r;
  App.state.winRates = map;
  App.state.summary = summary;
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
    if (s.quotex_connected) setConnUI(true, `লাইভ (${s.active_pairs.length} পেয়ার)`);
    else if (s.error) setConnUI(false, "সংযোগ বিচ্ছিন্ন");
    else setConnUI(null, "সংযোগ হচ্ছে…");
    if (App.tabs.settings && App.tabs.settings.onStatus) App.tabs.settings.onStatus(s);
  } catch (e) {
    setConnUI(false, "সার্ভার পাওয়া যাচ্ছে না");
  }
}

function tickClock() {
  const el = document.getElementById("serverClock");
  el.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
  setInterval(tickClock, 1000);
  tickClock();
  const p = await App.api("/api/pairs");
  App.state.pairs = p;
  await App.refreshWinRates();
  // Seed the live board with the latest signal per pair before WS events flow.
  try {
    const live = await App.api("/api/live");
    for (const r of live) App.state.liveSignals[r.pair] = r;
  } catch (e) { /* board starts empty */ }
  connectWS();
  pollStatus();
  setInterval(pollStatus, 8000);
  for (const name in App.tabs) {
    if (App.tabs[name].onInit) App.tabs[name].onInit();
  }
  switchView("live");
}

document.addEventListener("DOMContentLoaded", boot);
