App.tabs.history = {
  filterPair: "",
  rows: [],

  onInit() {
    const select = document.getElementById("historyPairFilter");
    App.allPairNames().forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      select.appendChild(opt);
    });
    select.addEventListener("change", () => {
      this.filterPair = select.value;
      this.load();
    });
  },

  onShow() {
    this.load();
  },

  async load() {
    const q = this.filterPair ? `?pair=${encodeURIComponent(this.filterPair)}` : "";
    this.rows = await App.api(`/api/history${q}`);
    this.render();
  },

  render() {
    const list = document.getElementById("historyList");
    if (!this.rows.length) {
      list.innerHTML = `<div style="color:var(--text-dim);font-size:13px;padding:20px 0;text-align:center">No signals yet</div>`;
      return;
    }
    list.innerHTML = this.rows.map((s) => `
      <div class="history-row" data-id="${s.id}">
        <div class="left">
          <span class="pair-name">${s.pair}</span>
          <span class="time">${App.fmtTime(s.entry_ts)} · ${s.source}</span>
        </div>
        <div class="right">
          <span class="conf">${(s.confidence * 100).toFixed(0)}%</span>
          <span class="dir-badge ${s.direction}">${s.direction}</span>
          <span class="result-badge ${s.result}">${s.result}</span>
        </div>
      </div>`).join("");
  },

  onSignal(msg) {
    if (this.filterPair && msg.pair !== this.filterPair) return;
    this.rows.unshift({ ...msg.signal, source: (msg.signal.sources || []).join(",") });
    this.render();
  },

  onGraded(msg) {
    const idx = this.rows.findIndex((r) => r.id === msg.signal.id);
    if (idx !== -1) {
      this.rows[idx] = { ...this.rows[idx], result: msg.signal.result, close_price: msg.signal.close_price };
      this.render();
    }
  },
};
