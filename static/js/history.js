/* হিস্টোরি ভিউ — পেয়ার / ডিরেকশন / টিয়ার / রেজাল্ট ফিল্টার + পেজিনেশন */
App.tabs.history = {
  rows: [],
  offset: 0,
  PAGE: 50,
  loading: false,

  onInit() {
    const pairSel = document.getElementById("historyPairFilter");
    const names = App.allPairNames();
    pairSel.innerHTML =
      `<option value="">সব পেয়ার</option>` +
      names.map((p) => `<option value="${p}">${p}</option>`).join("");

    const perfSel = document.getElementById("patternPerfPairFilter");
    if (perfSel) {
      perfSel.innerHTML =
        `<option value="">সব পেয়ার (মিলিত)</option>` +
        names.map((p) => `<option value="${p}">${p}</option>`).join("");
      perfSel.addEventListener("change", () => {
        if (App.tabs.settings) App.tabs.settings.loadPatternPerf();
      });
    }

    for (const id of ["historyPairFilter", "historyDirFilter", "historyTierFilter", "historyResultFilter"]) {
      document.getElementById(id).addEventListener("change", () => {
        this.offset = 0;
        this.rows = [];
        this.load();
      });
    }
    document.getElementById("historyMore").addEventListener("click", () => this.load(true));
  },

  onShow() {
    if (!this.rows.length) this.load();
  },

  filterParams() {
    const p = new URLSearchParams();
    const pair = document.getElementById("historyPairFilter").value;
    const dir = document.getElementById("historyDirFilter").value;
    const tier = document.getElementById("historyTierFilter").value;
    const result = document.getElementById("historyResultFilter").value;
    if (pair) p.set("pair", pair);
    if (dir) p.set("direction", dir);
    if (tier) p.set("tier", tier);
    if (result) p.set("result", result);
    return p;
  },

  async load(append) {
    if (this.loading) return;
    this.loading = true;
    const btn = document.getElementById("historyMore");
    btn.disabled = true;
    try {
      const p = this.filterParams();
      p.set("limit", String(this.PAGE));
      p.set("offset", String(this.offset));
      const rows = await App.api(`/api/history?${p.toString()}`);
      this.rows = append ? this.rows.concat(rows) : rows;
      this.offset = this.rows.length;
      this.render(rows.length);
    } finally {
      this.loading = false;
      btn.disabled = false;
    }
  },

  render(fetched) {
    const list = document.getElementById("historyList");
    const meta = document.getElementById("historyMeta");
    meta.textContent = `${this.rows.length} টি সিগন্যাল দেখানো হচ্ছে`;

    if (!this.rows.length) {
      list.innerHTML = `<div class="empty-note">এই ফিল্টারে কোনো সিগন্যাল নেই</div>`;
      document.getElementById("historyMore").style.display = "none";
      return;
    }

    list.innerHTML = this.rows.map((s) => `
      <div class="history-row">
        <div class="left">
          <span class="pair-name">${s.pair}</span>
          <span class="time">${App.fmtTime(s.entry_ts)} · ${s.source ? s.source.split(",")[0] : ""}${s.tier && s.tier !== "confirmed" ? " · fallback" : ""}</span>
        </div>
        <div class="right">
          <span class="px">${s.entry_price ? App.fmtPrice(s.entry_price) : "--"} → ${s.close_price ? App.fmtPrice(s.close_price) : "--"}</span>
          ${App.dirBadge(s.direction, true)}
          ${App.resultBadge(s.result)}
        </div>
      </div>`).join("");

    document.getElementById("historyMore").style.display = fetched === this.PAGE ? "block" : "none";
  },

  onGraded() {
    // A grade changed — refresh the first page if the user is looking at it.
    if (!document.getElementById("view-history").classList.contains("active")) return;
    this.offset = 0;
    this.rows = [];
    this.load();
  },
};
