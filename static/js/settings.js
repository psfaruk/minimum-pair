/* সেটিংস ভিউ — সংযোগ, টোকেন, থ্রেশহোল্ড, স্ট্র্যাটেজি পারফরম্যান্স */
App.tabs.settings = {
  patternPairFilter: "",

  onInit() {
    document.getElementById("sessionUpdateBtn").addEventListener("click", () => this.submitSessionUpdate());
  },

  async submitSessionUpdate() {
    const btn = document.getElementById("sessionUpdateBtn");
    const statusEl = document.getElementById("sessionUpdateStatus");
    // Token-only auth surface: the only credential the user pastes is
    // the Quotex SSID session token (plus its optional cookie header).
    // No admin passcode, no API keys, no other auth field is asked of
    // the user — the entire frontend auth story is "paste a token".
    const sessionToken = document.getElementById("sessionToken").value.trim();
    const sessionCookies = document.getElementById("sessionCookies").value.trim();

    if (!sessionToken) {
      statusEl.textContent = "সেশন টোকেন দিন।";
      statusEl.className = "settings-note error-text";
      return;
    }

    btn.disabled = true;
    statusEl.textContent = "আপডেট হচ্ছে…";
    statusEl.className = "settings-note";
    try {
      const res = await App.apiPost("/api/session", {
        session_token: sessionToken,
        session_cookies: sessionCookies,
      });
      statusEl.textContent = res.message || "আপডেট হয়েছে, রিকানেক্ট হচ্ছে…";
      document.getElementById("sessionToken").value = "";
      document.getElementById("sessionCookies").value = "";
    } catch (e) {
      statusEl.textContent = e.message || "আপডেট ব্যর্থ";
      statusEl.className = "settings-note error-text";
    } finally {
      btn.disabled = false;
    }
  },

  onShow() {
    this.renderPairs();
    this.loadPatternPerf();
  },

  onStatus(s) {
    const conn = document.getElementById("settingsConn");
    conn.innerHTML = `<span>অবস্থা</span><span>${
      s.quotex_connected
        ? `সংযুক্ত (${s.account_mode} অ্যাকাউন্ট)`
        : s.error
          ? "টোকেনের অপেক্ষায়"
          : "সংযোগ হচ্ছে…"
    }</span>`;
    document.getElementById("settingsError").textContent = s.error || "";
    document.getElementById("settingsAuthMode").textContent =
      s.auth_mode === "session_token" ? "সেশন টোকেন" : "এখনো টোকেন দেওয়া হয়নি";
    document.getElementById("settingsPersistence").textContent = "চালু (ডাটাবেজে সংরক্ষিত)";
    document.getElementById("settingsFeedHealth").textContent = this.describeFeedHealth(s);
    document.getElementById("settingsMinConf").textContent = (s.min_confidence * 100).toFixed(0) + "%";
    this._active = s.active_pairs || [];
    this.renderPairs();
  },

  describeFeedHealth(s) {
    const reconnects = s.reconnects || 0;
    const suffix = reconnects ? ` (${reconnects} বার অটো-রিকানেক্ট)` : "";
    if (s.feed_stale_seconds === null || s.feed_stale_seconds === undefined) return `স্ট্রিম চলছে না${suffix}`;
    return `শেষ টিক ${s.feed_stale_seconds}s আগে${suffix}`;
  },

  renderPairs() {
    const list = document.getElementById("settingsPairs");
    const names = App.allPairNames();
    const active = new Set(this._active || []);
    list.innerHTML = names.map((name) => `
      <span class="pair-chip ${active.has(name) ? "active" : ""}">${name}${active.has(name) ? " ●" : ""}</span>`).join("");
  },

  async loadPatternPerf() {
    const sel = document.getElementById("patternPerfPairFilter");
    this.patternPairFilter = sel ? sel.value : "";
    const q = this.patternPairFilter ? `?pair=${encodeURIComponent(this.patternPairFilter)}` : "";
    const rows = await App.api(`/api/patterns${q}`);
    this.renderPatternPerf(rows);
  },

  renderPatternPerf(rows) {
    const list = document.getElementById("patternPerfList");
    if (!rows.length) {
      const scope = this.patternPairFilter ? ` (${this.patternPairFilter})` : "";
      list.innerHTML = `<div class="empty-note">এখনো কোনো গ্রেড করা সিগন্যাল নেই${scope}</div>`;
      return;
    }
    list.innerHTML = rows.map((r) => {
      const rate = r.win_rate;
      const graded = r.wins + r.losses;
      const rateClass = rate === null ? "rate-thin" : App.rateClass(rate, graded);
      const extras = [];
      if (r.draws) extras.push(`${r.draws} no-move`);
      if (r.pending) extras.push(`${r.pending} pending`);
      const extraNote = extras.length ? ` (${extras.join(", ")})` : "";
      return `<div class="strategy-row">
        <div><div class="s-name">${r.pattern}</div>
        <div class="s-meta">${graded} graded${extraNote}</div></div>
        <span class="s-rate ${rateClass}">${App.fmtPct(rate)}</span>
      </div>`;
    }).join("");
  },

  onGraded() {
    this.loadPatternPerf();
  },

  onSignal(msg) {
    if (!this.patternPairFilter || msg.pair === this.patternPairFilter) {
      this.loadPatternPerf();
    }
  },
};
