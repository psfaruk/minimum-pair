/* চার্ট ভিউ — ক্যান্ডেলস্টিক + প্রতি ক্যান্ডেলে সিগন্যাল মার্কার */
App.tabs.chart = {
  chart: null,
  series: null,
  signalSeries: null,
  pendingSignalId: null,
  signalRows: [],
  currentCandleTs: null,
  CANDLE_PERIOD: 60,

  onInit() {
    const select = document.getElementById("pairSelect");
    const groups = [
      { label: "Forex", items: App.state.pairs.forex },
      { label: "OTC", items: App.state.pairs.otc },
    ];
    select.innerHTML = groups.map((g) =>
      `<optgroup label="${g.label}">${g.items.map((p) => `<option value="${p}">${p}</option>`).join("")}</optgroup>`
    ).join("");
    select.addEventListener("change", () => this.loadPair(select.value));
    if (groups[0].items.length) {
      select.value = groups[0].items[0];
      App.state.activePair = select.value;
    }
    setInterval(() => this.tickTimer(), 250);
  },

  tickTimer() {
    const el = document.getElementById("candleTimer");
    if (this.currentCandleTs === null) {
      el.textContent = "--s";
      el.classList.remove("closing");
      return;
    }
    const nowSec = Date.now() / 1000;
    const remaining = Math.max(0, this.CANDLE_PERIOD - (nowSec - this.currentCandleTs));
    el.textContent = `${Math.ceil(remaining)}s`;
    el.classList.toggle("closing", remaining <= 5);
  },

  ensureChart() {
    if (this.chart) return;
    const container = document.getElementById("chartContainer");
    this.chart = LightweightCharts.createChart(container, {
      layout: {
        background: { color: "rgba(0,0,0,0)" },
        textColor: "#8fa1b8",
        fontFamily: "Inter, Hind Siliguri, sans-serif",
      },
      grid: { vertLines: { color: "rgba(148,163,184,0.07)" }, horzLines: { color: "rgba(148,163,184,0.07)" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "rgba(148,163,184,0.18)" },
      rightPriceScale: { borderColor: "rgba(148,163,184,0.18)" },
      autoSize: true,
    });
    this.series = this.chart.addCandlestickSeries({
      upColor: "#00d68f", downColor: "#ff5470",
      borderUpColor: "#00d68f", borderDownColor: "#ff5470",
      wickUpColor: "#00d68f", wickDownColor: "#ff5470",
    });
    // Signal markers are attached directly to the candlestick series
    // via setMarkers() — every candle that produced a signal gets an
    // arrow on the chart. CALL = green up-arrow below the bar,
    // PUT = red down-arrow above it. প্রত্যেক ক্যান্ডেলে সিগন্যাল।
    this.signalMarkers = [];
  },

  onShow() {
    this.ensureChart();
    if (App.state.activePair) this.loadPair(App.state.activePair);
  },

  async loadPair(pair) {
    App.state.activePair = pair;
    this.ensureChart();
    this.hideBadge();
    this.currentCandleTs = null;
    document.getElementById("signalsWindowPair").textContent = pair;

    const [rows, history] = await Promise.all([
      App.api(`/api/candles?pair=${encodeURIComponent(pair)}&limit=200`),
      App.api(`/api/history?pair=${encodeURIComponent(pair)}&limit=50`),
    ]);
    this.series.setData(rows.map((r) => ({ time: r.ts, open: r.open, high: r.high, low: r.low, close: r.close })));

    this.signalRows = history;
    // Seed the chart markers with the history we just loaded. Each
    // signal's `entry_ts` lines up with the open of the candle it
    // was made for, so the marker sits on exactly the right bar.
    this.signalMarkers = this.signalRows
      .map((s) => this._markerFor(s))
      .filter(Boolean);
    this._renderMarkers();
    this.renderSignalsWindow();

    await App.refreshWinRates();
    this.renderWinRates();
  },

  _markerFor(s) {
    if (!s || !s.entry_ts) return null;
    const isCall = s.direction === "CALL";
    return {
      time: s.entry_ts,
      position: isCall ? "belowBar" : "aboveBar",
      color: isCall ? "#00d68f" : "#ff5470",
      shape: isCall ? "arrowUp" : "arrowDown",
      // No text label: every candle fires a signal, so labels pile up
      // into an unreadable wall on a 200-bar chart. The arrow alone
      // marks the candle; the signals window carries the details.
    };
  },

  _renderMarkers() {
    if (!this.series || !this.series.setMarkers) return;
    // lightweight-charts requires markers sorted ascending by time
    // and unique — sort + dedupe just in case the same candle picked
    // up two signals (e.g. a fallback replaced by a confirmed one in
    // the same minute).
    const seen = new Set();
    const sorted = [...this.signalMarkers]
      .filter((m) => {
        if (seen.has(m.time)) return false;
        seen.add(m.time);
        return true;
      })
      .sort((a, b) => a.time - b.time);
    this.series.setMarkers(sorted);
  },

  renderWinRates() {
    const pair = App.state.activePair;
    const stats = App.state.winRates[pair];
    document.getElementById("chartPairWinRate").textContent = stats ? App.fmtPct(stats.win_rate) : "--%";
    document.getElementById("chartPairCounts").textContent = stats ? `${stats.wins}W / ${stats.losses}L` : "0W / 0L";
    document.getElementById("chartPairCallRate").textContent = stats ? App.fmtPct(stats.call.win_rate) : "--%";
    document.getElementById("chartPairCallCounts").textContent = stats ? `${stats.call.wins}W / ${stats.call.losses}L` : "0W / 0L";
    document.getElementById("chartPairPutRate").textContent = stats ? App.fmtPct(stats.put.win_rate) : "--%";
    document.getElementById("chartPairPutCounts").textContent = stats ? `${stats.put.wins}W / ${stats.put.losses}L` : "0W / 0L";
  },

  renderSignalsWindow(highlightId) {
    const list = document.getElementById("signalsWindowList");
    if (!this.signalRows.length) {
      list.innerHTML = `<div class="empty-note">এই পেয়ারে এখনো সিগন্যাল আসেনি</div>`;
      return;
    }
    list.innerHTML = this.signalRows.map((s) => `
      <div class="history-row${s.id === highlightId ? " highlight" : ""}" data-id="${s.id}">
        <div class="left">
          <span class="time">${App.fmtTime(s.entry_ts)} · ${s.source ? s.source.split(",")[0] : ""}</span>
          <span class="conf">${s.confidence != null ? `কনফ ${(s.confidence * 100).toFixed(0)}%` : "কনফ —"} ${s.tier && s.tier !== "confirmed" ? "· fallback" : ""}</span>
        </div>
        <div class="right">
          ${App.dirBadge(s.direction, true)}
          ${App.resultBadge(s.result)}
        </div>
      </div>`).join("");
  },

  onCandle(msg) {
    if (msg.pair !== App.state.activePair || !this.series) return;
    const c = msg.candle;
    this.series.update({ time: c.ts, open: c.open, high: c.high, low: c.low, close: c.close });
    if (!msg.final) this.currentCandleTs = c.ts;
  },

  showBadge(signal) {
    const badge = document.getElementById("signalBadge");
    badge.classList.remove("hidden", "CALL", "PUT");
    badge.classList.add(signal.direction);
    badge.innerHTML = `${signal.direction} ${signal.confidence != null ? `· কনফ ${(signal.confidence * 100).toFixed(0)}%` : ""}`;
    this.pendingSignalId = signal.id;
  },

  hideBadge() {
    document.getElementById("signalBadge").classList.add("hidden");
    this.pendingSignalId = null;
  },

  onSignal(msg) {
    if (msg.pair !== App.state.activePair) return;
    this.showBadge(msg.signal);
    this.signalRows.unshift({ ...msg.signal, source: (msg.signal.sources || []).join(",") });
    this.signalRows = this.signalRows.slice(0, 50);
    // Push the new signal's marker onto the chart and re-render.
    const m = this._markerFor(msg.signal);
    if (m) {
      this.signalMarkers.push(m);
      this._renderMarkers();
    }
    this.renderSignalsWindow(msg.signal.id);
  },

  onGraded(msg) {
    if (msg.pair !== App.state.activePair) return;

    if (msg.signal.id === this.pendingSignalId) {
      const badge = document.getElementById("signalBadge");
      badge.textContent = `${msg.signal.direction} · ${msg.signal.result}`;
      setTimeout(() => this.hideBadge(), 4000);
    }

    const idx = this.signalRows.findIndex((r) => r.id === msg.signal.id);
    if (idx !== -1) {
      this.signalRows[idx] = { ...this.signalRows[idx], result: msg.signal.result, close_price: msg.signal.close_price };
      this.renderSignalsWindow();
    }

    App.refreshWinRates().then(() => this.renderWinRates());
  },
};
