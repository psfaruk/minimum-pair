/* স্ট্যাটস ভিউ — প্রতি পেয়ার, CALL/PUT আলাদা উইন রেট টেবিল
   ("প্রত্যেক পেয়ার... Call ও put কোনো সিগন্যাল গুলো কেমন win রেট দিচ্ছে") */
App.tabs.stats = {
  days: 7,
  sort: "total",

  onInit() {
    document.querySelectorAll("#periodFilter .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#periodFilter .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        this.days = Number(btn.dataset.days);
        this.load();
      });
    });
    document.getElementById("statsSort").addEventListener("change", (e) => {
      this.sort = e.target.value;
      this.renderTable();
    });
  },

  onShow() {
    this.load();
  },

  async load() {
    await App.refreshWinRates(this.days);
    this.renderSummary();
    this.renderTable();
  },

  renderSummary() {
    const s = App.state.summary;
    const el = document.getElementById("statsSummary");
    if (!s) { el.innerHTML = ""; return; }
    el.innerHTML = `
      <div class="sum-card">
        <div class="k">সামগ্রিক</div>
        <div class="v ${App.rateClass(s.win_rate, s.wins + s.losses)}">${App.fmtPct(s.win_rate)}</div>
        <div class="s">${s.wins}W / ${s.losses}L · মোট ${s.total}</div>
      </div>
      <div class="sum-card call">
        <div class="k">CALL</div>
        <div class="v">${App.fmtPct(s.call.win_rate)}</div>
        <div class="s">${s.call.wins}W / ${s.call.losses}L</div>
      </div>
      <div class="sum-card put">
        <div class="k">PUT</div>
        <div class="v">${App.fmtPct(s.put.win_rate)}</div>
        <div class="s">${s.put.wins}W / ${s.put.losses}L</div>
      </div>
      <div class="sum-card confirmed">
        <div class="k">কনফার্মড</div>
        <div class="v">${App.fmtPct(s.confirmed.win_rate)}</div>
        <div class="s">${s.confirmed.wins}W / ${s.confirmed.losses}L</div>
      </div>
      <div class="sum-card fallback">
        <div class="k">ফলব্যাক (লেগেসি)</div>
        <div class="v">${App.fmtPct(s.fallback.win_rate)}</div>
        <div class="s">${s.fallback.wins}W / ${s.fallback.losses}L</div>
      </div>`;
  },

  _cell(label, block, cls) {
    const n = block.wins + block.losses;
    const rate = block.win_rate;
    const pct = rate !== null ? Math.round(rate * 100) : 0;
    return `<div class="rate-cell ${cls || ""}">
      <div class="top"><span class="r ${App.rateClass(rate, n)}">${App.fmtPct(rate)}</span><span class="c">${block.wins}W/${block.losses}L</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div>
    </div>`;
  },

  renderTable() {
    const el = document.getElementById("statsTable");
    let rows = Object.values(App.state.winRates);

    const key = this.sort;
    rows.sort((a, b) => {
      if (key === "total") {
        const ta = a.wins + a.losses + a.pending, tb = b.wins + b.losses + b.pending;
        return tb - ta;
      }
      const ra = key === "call" ? a.call.win_rate : key === "put" ? a.put.win_rate : a.win_rate;
      const rb = key === "call" ? b.call.win_rate : key === "put" ? b.put.win_rate : b.win_rate;
      return (rb ?? -1) - (ra ?? -1);
    });

    if (!rows.length) {
      el.innerHTML = `<div class="empty-note">এই সময়ে কোনো গ্রেড করা সিগন্যাল নেই</div>`;
      return;
    }

    el.innerHTML = `
      <div class="stats-row head">
        <span>পেয়ার</span><span>CALL</span><span>PUT</span><span>সামগ্রিক</span>
      </div>
      ${rows.map((r) => {
        const n = r.wins + r.losses;
        return `<div class="stats-row">
          <span class="p-name">${r.pair}<small>মোট ${r.wins + r.losses + r.pending} সিগন্যাল${r.pending ? ` (${r.pending} পেন্ডিং)` : ""}</small></span>
          ${this._cell("CALL", r.call)}
          ${this._cell("PUT", r.put)}
          ${this._cell("সামগ্রিক", { wins: r.wins, losses: r.losses, win_rate: r.win_rate })}
        </div>`;
      }).join("")}`;
  },

  onGraded() {
    if (!document.getElementById("view-stats").classList.contains("active")) return;
    App.refreshWinRates(this.days).then(() => {
      this.renderSummary();
      this.renderTable();
    });
  },
};
