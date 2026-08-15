App.tabs.settings = {
  patternPairFilter: "",

  onInit() {
    const select = document.getElementById("patternPerfPairFilter");
    App.allPairNames().forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      select.appendChild(opt);
    });
    select.addEventListener("change", () => {
      this.patternPairFilter = select.value;
      this.loadPatternPerformance();
    });
    this.loadPatternPerformance();

    document.getElementById("sessionUpdateBtn").addEventListener("click", () => this.submitSessionUpdate());
  },

  async submitSessionUpdate() {
    const btn = document.getElementById("sessionUpdateBtn");
    const statusEl = document.getElementById("sessionUpdateStatus");
    const adminToken = document.getElementById("sessionAdminToken").value;
    const sessionToken = document.getElementById("sessionToken").value.trim();
    const sessionCookies = document.getElementById("sessionCookies").value.trim();

    if (!sessionToken) {
      statusEl.textContent = "Session token is required.";
      statusEl.className = "settings-note error-text";
      return;
    }

    btn.disabled = true;
    statusEl.textContent = "Updating…";
    statusEl.className = "settings-note";
    try {
      const res = await App.apiPost("/api/session", {
        admin_token: adminToken,
        session_token: sessionToken,
        session_cookies: sessionCookies,
      });
      statusEl.textContent = res.message || "Updated, reconnecting…";
      document.getElementById("sessionToken").value = "";
      document.getElementById("sessionCookies").value = "";
      document.getElementById("sessionAdminToken").value = "";
    } catch (e) {
      statusEl.textContent = e.message || "Update failed";
      statusEl.className = "settings-note error-text";
    } finally {
      btn.disabled = false;
    }
  },

  onShow() {
    this.renderPairs();
    this.loadPatternPerformance();
  },

  onStatus(s) {
    const conn = document.getElementById("settingsConn");
    conn.textContent = s.quotex_connected
      ? `Connected (${s.account_mode} account)`
      : (s.error ? "Failed to connect" : "Connecting…");
    document.getElementById("settingsError").textContent = s.error || "";
    document.getElementById("settingsAuthMode").textContent =
      s.auth_mode === "session_token" ? "session token" : "email/password";
    document.getElementById("settingsLoginBackoff").textContent = this.describeBackoff(s);
    document.getElementById("settingsPersistence").textContent = this.describePersistence(s.session_persistence);
    document.getElementById("settingsFeedHealth").textContent = this.describeFeedHealth(s);
    document.getElementById("settingsMinConf").textContent = (s.min_confidence * 100).toFixed(0) + "%";
    document.getElementById("settingsAlwaysSignal").textContent = s.always_signal ? "on" : "off";
    this._active = s.active_pairs || [];
    this.renderPairs();
  },

  describeBackoff(s) {
    const remaining = s.login_backoff_seconds_remaining || 0;
    const failures = s.password_failure_count || 0;
    if (!remaining) return failures ? `none (${failures} past failures)` : "none";
    const mins = Math.floor(remaining / 60);
    const secs = remaining % 60;
    const left = mins ? `${mins}m ${secs}s` : `${secs}s`;
    const why = {
      cloudflare_block: "Cloudflare blocked the login",
      invalid_credentials: "Quotex rejected the credentials",
    }[s.last_login_failure];
    const reason = why ? ` — ${why}` : "";
    return `${left} left after ${failures} failed login${failures === 1 ? "" : "s"}${reason}`;
  },

  describeFeedHealth(s) {
    const reconnects = s.reconnects || 0;
    const suffix = reconnects ? ` (${reconnects} auto-reconnect${reconnects === 1 ? "" : "s"})` : "";
    if (s.feed_stale_seconds === null || s.feed_stale_seconds === undefined) return `not streaming${suffix}`;
    return `last tick ${s.feed_stale_seconds}s ago${suffix}`;
  },

  describePersistence(p) {
    if (!p) return "--";
    if (!p.configured) {
      const missing = (p.missing_settings || []).join(", ");
      return missing ? `off (set ${missing})` : "off";
    }
    return p.attempted_at ? `on — ${p.detail}` : "on — nothing to store yet";
  },

  renderPairs() {
    const list = document.getElementById("settingsPairs");
    const names = App.allPairNames();
    const active = new Set(this._active || []);
    list.innerHTML = names.map((name) => `
      <div class="pair-row">
        <span class="name">${name}</span>
        <span class="result-badge ${active.has(name) ? "WIN" : "PENDING"}">${active.has(name) ? "streaming" : "waiting"}</span>
      </div>`).join("");
  },

  async loadPatternPerformance() {
    const q = this.patternPairFilter ? `?pair=${encodeURIComponent(this.patternPairFilter)}` : "";
    const rows = await App.api(`/api/patterns${q}`);
    this.renderPatternPerformance(rows);
  },

  renderPatternPerformance(rows) {
    const list = document.getElementById("patternPerfList");
    if (!rows.length) {
      const scope = this.patternPairFilter ? `for ${this.patternPairFilter}` : "yet";
      list.innerHTML = `<div style="color:var(--text-dim);font-size:13px;padding:8px 0">No signals ${scope}</div>`;
      return;
    }
    list.innerHTML = rows.map((r) => {
      const rate = r.win_rate;
      const graded = r.wins + r.losses;
      // The rate is computed on graded signals only, so lead with that
      // number. Showing the fired count instead made a source measured on
      // 68 trades look like it had 564 behind it.
      const rateClass = rate === null ? "" : graded < 30 ? "rate-thin"
        : rate >= 0.55 ? "rate-good" : rate <= 0.45 ? "rate-bad" : "rate-mid";
      const extras = [];
      if (r.draws) extras.push(`${r.draws} no-move`);
      if (r.pending) extras.push(`${r.pending} pending`);
      const extraNote = extras.length ? ` (${extras.join(", ")})` : "";
      const thin = graded > 0 && graded < 30 ? ' <span class="thin-flag">too few to judge</span>' : "";
      return `<div class="pair-row">
        <span class="name">${r.pattern}${thin}</span>
        <span><span class="signal-count">${graded} graded</span>
        <span class="rate ${rateClass}">${App.fmtPct(rate)}</span>
        <span class="counts">${r.wins}W / ${r.losses}L${extraNote}</span></span>
      </div>`;
    }).join("");
  },

  onGraded() {
    this.loadPatternPerformance();
  },

  onSignal(msg) {
    if (!this.patternPairFilter || msg.pair === this.patternPairFilter) {
      this.loadPatternPerformance();
    }
  },
};
