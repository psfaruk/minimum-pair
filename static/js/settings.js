/* সেটিংস ভিউ — সংযোগ, টোকেন, থ্রেশহোল্ড, স্ট্র্যাটেজি পারফরম্যান্স */
App.tabs.settings = {
  patternPairFilter: "",

  onInit() {
    document.getElementById("sessionUpdateBtn").addEventListener("click", () => this.submitSessionUpdate());
    document.getElementById("diagnoseBtn").addEventListener("click", () => this.runDiagnose());
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
        : s.auth_mode === "no_token"
          ? "টোকেনের অপেক্ষায়"
          : s.error
            ? "সংযোগ বিচ্ছিন্ন"
            : "সংযোগ হচ্ছে…"
    }</span>`;
    document.getElementById("settingsError").textContent = s.error || "";
    document.getElementById("settingsAuthMode").textContent =
      s.auth_mode === "session_token" ? "সেশন টোকেন" : "এখনো টোকেন দেওয়া হয়নি";
    document.getElementById("settingsPersistence").textContent = "চালু (ডাটাবেজে সংরক্ষিত)";
    document.getElementById("settingsFeedHealth").textContent = this.describeFeedHealth(s);
    document.getElementById("settingsCodeVersion").textContent = s.code_version || "--";
    document.getElementById("settingsMinConf").textContent = (s.min_confidence * 100).toFixed(0) + "%";
    this._active = s.active_pairs || [];
    this.renderPairs();
  },

  describeFeedHealth(s) {
    const reconnects = s.reconnects || 0;
    const suffix = reconnects ? ` (${reconnects} বার অটো-রিকানেক্ট)` : "";
    const ticks = s.total_ticks || 0;
    const tickNote = ` — মোট ${ticks.toLocaleString()} টিক`;
    if (s.feed_stale_seconds === null || s.feed_stale_seconds === undefined)
      return `স্ট্রিম চলছে না${tickNote}${suffix}`;
    return `শেষ টিক ${s.feed_stale_seconds}s আগে${tickNote}${suffix}`;
  },

  // Runs the broker pipeline self-test against /api/diagnose and renders
  // which step breaks with the matching Bengali fix. This is how a
  // deployed instance answers "কোথায় সমস্যা?" without anyone reading logs.
  async runDiagnose() {
    const btn = document.getElementById("diagnoseBtn");
    const out = document.getElementById("diagnoseOutput");
    btn.disabled = true;
    out.classList.remove("hidden");
    out.innerHTML = '<div class="diag-running">ডায়াগনোসিস চলছে… (~২০ সেকেন্ড)</div>';
    try {
      const d = await App.api("/api/diagnose");
      out.innerHTML = this.renderDiagnose(d);
    } catch (e) {
      out.innerHTML = `<div class="diag-line bad">ডায়াগনোসিস চালানো যায়নি: ${shortReason(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  },

  renderDiagnose(d) {
    const stepLabel = {
      token: "১. সেশন টোকেন",
      connect: "২. ব্রোকার সংযোগ + লগইন",
      authorized: "৩. অথরাইজেশন",
      assets: "৪. ইনস্ট্রুমেন্ট তালিকা",
      stream: "৫. লাইভ প্রাইস স্ট্রিম",
      history: "৬. ক্যান্ডেল হিস্ট্রি",
    };
    const row = (name, ok, detail) => {
      if (detail === undefined) return "";
      const cls = ok ? "ok" : "bad";
      const mark = ok ? "✔" : "✘";
      return `<div class="diag-line ${cls}"><span>${mark} ${stepLabel[name] || name}</span><span>${detail}</span></div>`;
    };
    let html = "";
    if (d.token) {
      const okTok = d.token.present;
      html += row("token", okTok, okTok ? `আছে (${d.token.source})` : "নেই");
    }
    if (d.connect) html += row("connect", d.connect.ok, d.connect.ok ? `সফল (${d.connect.seconds}s)` : shortReason(d.connect.reason));
    if (d.authorized) html += row("authorized", d.authorized.ok, d.authorized.ok ? "অনুমোদিত" : "ব্যর্থ");
    if (d.assets) html += row("assets", d.assets.count > 0, `${d.assets.count} টি অ্যাসেট`);
    if (d.stream) {
      const t = d.stream.total || 0;
      const detail = d.stream.error || `${t} টিক (${d.stream.assets ? d.stream.assets.join(", ") : ""})`;
      html += row("stream", t > 0, detail);
    }
    if (d.history) html += row("history", d.history.ok, d.history.ok ? `${d.history.candles} ক্যান্ডেল` : "সাড়া নেই");
    const v = d.verdict || {};
    if (v.ok) {
      html += `<div class="diag-verdict ok">✔ ${v.all_ok_bn || "সব ধাপ পাস"}</div>`;
    } else {
      html += `<div class="diag-verdict bad">✘ ${v.problem_bn || "সমস্যা ধরা পড়েছে"}</div>`;
      if (v.fix_bn) html += `<div class="diag-fix">করণীয়: ${v.fix_bn}</div>`;
    }
    return html;
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
