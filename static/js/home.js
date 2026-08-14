App.tabs.home = {
  onInit() {
    this.renderWinRates();
  },

  onShow() {
    App.refreshWinRates().then(() => this.renderWinRates());
  },

  renderWinRates() {
    const rows = Object.values(App.state.winRates);
    let wins = 0, losses = 0, pending = 0;
    for (const r of rows) { wins += r.wins; losses += r.losses; pending += r.pending || 0; }
    const total = wins + losses;
    document.getElementById("overallWinRate").textContent = total ? App.fmtPct(wins / total) : "--%";
    document.getElementById("overallCounts").textContent =
      `${wins}W / ${losses}L · ${total + pending} signals total${pending ? ` (${pending} pending)` : ""}`;

    const list = document.getElementById("pairWinRateList");
    const names = App.allPairNames();
    if (!names.length) { list.innerHTML = ""; return; }
    // most-active pairs first, so "which pair fired the most" is scannable at a glance
    const sorted = [...names].sort((a, b) => {
      const ra = App.state.winRates[a], rb = App.state.winRates[b];
      const ta = ra ? ra.wins + ra.losses + (ra.pending || 0) : 0;
      const tb = rb ? rb.wins + rb.losses + (rb.pending || 0) : 0;
      return tb - ta;
    });
    list.innerHTML = sorted.map((name) => {
      const r = App.state.winRates[name];
      const rate = r ? App.fmtPct(r.win_rate) : "--%";
      const signalCount = r ? r.wins + r.losses + (r.pending || 0) : 0;
      const counts = r ? `${r.wins}W / ${r.losses}L` : "0W / 0L";
      return `<div class="pair-row"><span class="name">${name}</span>
        <span><span class="signal-count">${signalCount} signals</span>
        <span class="rate">${rate}</span> <span class="counts">${counts}</span></span></div>`;
    }).join("");
  },

  renderLatest(signal) {
    App.state.latestSignal = signal;
    const el = document.getElementById("latestSignal");
    el.classList.remove("empty");
    el.innerHTML = `
      <div>
        <div style="font-weight:600">${signal.pair}</div>
        <div style="color:var(--text-dim);font-size:12px">${App.fmtTime(signal.entry_ts)} · conf ${(signal.confidence * 100).toFixed(0)}%</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="dir-badge ${signal.direction}">${signal.direction}</span>
        <span class="result-badge ${signal.result || "PENDING"}">${signal.result || "PENDING"}</span>
      </div>`;
  },

  onSignal(msg) {
    this.renderLatest(msg.signal);
  },

  onGraded(msg) {
    if (App.state.latestSignal && App.state.latestSignal.id === msg.signal.id) {
      this.renderLatest(msg.signal);
    }
    App.refreshWinRates().then(() => this.renderWinRates());
  },
};
