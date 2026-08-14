App.tabs.chart = {
  chart: null,
  series: null,
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
      layout: { background: { color: "#131a22" }, textColor: "#e6edf3" },
      grid: { vertLines: { color: "#223140" }, horzLines: { color: "#223140" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#223140" },
      rightPriceScale: { borderColor: "#223140" },
      autoSize: true,
    });
    this.series = this.chart.addCandlestickSeries({
      upColor: "#22c55e", downColor: "#ef4444",
      borderUpColor: "#22c55e", borderDownColor: "#ef4444",
      wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
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
    this.renderSignalsWindow();

    await App.refreshWinRates();
    this.renderWinRates();
  },

  renderWinRates() {
    const pair = App.state.activePair;
    const pairStats = App.state.winRates[pair];
    document.getElementById("chartPairWinRate").textContent = pairStats ? App.fmtPct(pairStats.win_rate) : "--%";
    document.getElementById("chartPairCounts").textContent = pairStats ? `${pairStats.wins}W / ${pairStats.losses}L` : "0W / 0L";

    const all = Object.values(App.state.winRates);
    let wins = 0, losses = 0;
    for (const r of all) { wins += r.wins; losses += r.losses; }
    const total = wins + losses;
    document.getElementById("chartOverallWinRate").textContent = total ? App.fmtPct(wins / total) : "--%";
    document.getElementById("chartOverallCounts").textContent = `${wins}W / ${losses}L`;
  },

  renderSignalsWindow(highlightId) {
    const list = document.getElementById("signalsWindowList");
    if (!this.signalRows.length) {
      list.innerHTML = `<div style="color:var(--text-dim);font-size:12px;padding:12px 0;text-align:center">No signals yet for this pair</div>`;
      return;
    }
    list.innerHTML = this.signalRows.map((s) => `
      <div class="history-row${s.id === highlightId ? " highlight" : ""}" data-id="${s.id}">
        <div class="left">
          <span class="time">${App.fmtTime(s.entry_ts)}</span>
          <span class="conf">${(s.confidence * 100).toFixed(0)}% · ${s.source || ""}</span>
        </div>
        <div class="right">
          <span class="dir-badge ${s.direction}">${s.direction}</span>
          <span class="result-badge ${s.result}">${s.result}</span>
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
    badge.textContent = `${signal.direction} · ${(signal.confidence * 100).toFixed(0)}%`;
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
