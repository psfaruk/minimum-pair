/* লাইভ ভিউ — hero stats + ফিল্টারেবল লাইভ সিগন্যাল গ্রিড */
App.tabs.live = {
  onInit() {
    this.renderHero();

    document.querySelectorAll("#tierFilter .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#tierFilter .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        App.state.tierFilter = btn.dataset.tier;
        this.renderGrid();
      });
    });
    document.querySelectorAll("#dirFilter .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#dirFilter .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        App.state.dirFilter = btn.dataset.dir;
        this.renderGrid();
      });
    });

    // Countdown ring refresh for every visible live card.
    setInterval(() => this.tickCountdowns(), 500);
  },

  onShow() {
    App.refreshWinRates().then(() => {
      this.renderHero();
      this.renderGrid();
    });
    this.renderGrid();
  },

  renderHero() {
    const s = App.state.summary;
    const rows = Object.values(App.state.winRates);
    let wins = 0, losses = 0, pending = 0, total = 0;
    if (s) {
      wins = s.wins; losses = s.losses; pending = s.pending; total = s.total;
    } else {
      for (const r of rows) { wins += r.wins; losses += r.losses; pending += r.pending || 0; }
    }

    const wr = wins + losses ? wins / (wins + losses) : null;
    const ring = document.getElementById("winRing");
    const C = 2 * Math.PI * 52;
    document.getElementById("overallWinRate").textContent = App.fmtPct(wr);
    ring.classList.remove("bad", "mid");
    if (wr !== null) {
      ring.style.strokeDashoffset = String(C * (1 - wr));
      if (wr < 0.45) ring.classList.add("bad");
      else if (wr < 0.53) ring.classList.add("mid");
    } else {
      ring.style.strokeDashoffset = String(C);
    }
    document.getElementById("overallCounts").textContent =
      `${wins}W / ${losses}L${pending ? ` · ${pending} পেন্ডিং` : ""}`;

    const sum = App.state.summary;
    const call = sum ? sum.call : null;
    const put = sum ? sum.put : null;
    const callWr = call && call.win_rate !== null ? call.win_rate : null;
    const putWr = put && put.win_rate !== null ? put.win_rate : null;
    document.getElementById("callWinRate").textContent = App.fmtPct(callWr);
    document.getElementById("putWinRate").textContent = App.fmtPct(putWr);
    document.getElementById("callCounts").textContent =
      `${call ? call.wins : 0}W / ${call ? call.losses : 0}L`;
    document.getElementById("putCounts").textContent =
      `${put ? put.wins : 0}W / ${put ? put.losses : 0}L`;
    document.getElementById("callBar").style.width = callWr !== null ? `${callWr * 100}%` : "0%";
    document.getElementById("putBar").style.width = putWr !== null ? `${putWr * 100}%` : "0%";

    document.getElementById("chipTotal").textContent = total;
    document.getElementById("chipPending").textContent = pending;
    const conf = sum ? sum.confirmed : null;
    document.getElementById("chipConfirmed").textContent =
      conf && conf.win_rate !== null ? App.fmtPct(conf.win_rate) : "--%";
  },

  visiblePairs() {
    let names = App.allPairNames();
    const tierF = App.state.tierFilter;
    const dirF = App.state.dirFilter;
    return names.filter((name) => {
      const sig = App.state.liveSignals[name];
      if (tierF && (!sig || (sig.tier !== tierF))) return false;
      if (dirF && (!sig || sig.direction !== dirF)) return false;
      return true;
    });
  },

  renderGrid() {
    const grid = document.getElementById("liveGrid");
    const names = this.visiblePairs();
    if (!names.length) {
      grid.innerHTML = `<div class="empty-note" style="grid-column:1/-1">এখনো কোনো সিগন্যাল আসেনি — পেয়ার কানেক্ট হওয়ার সাথে সাথে প্রতিটি ক্যান্ডেলে সিগন্যাল আসবে।</div>`;
      return;
    }
    // Most-recent signal first so the freshest calls are always on top.
    const sorted = [...names].sort((a, b) => {
      const sa = App.state.liveSignals[a], sb = App.state.liveSignals[b];
      return (sb ? sb.created_at : 0) - (sa ? sa.created_at : 0);
    });
    grid.innerHTML = sorted.map((name) => this.cardHTML(name)).join("");
    this.tickCountdowns();
  },

  cardHTML(name) {
    const sig = App.state.liveSignals[name];
    const wr = App.state.winRates[name];
    const status = App.state.status;
    const regime = (status.regimes || {})[name];
    const dir = sig ? sig.direction : null;
    const price = App.state.livePrices[name];

    const regimeTag = regime
      ? `<span class="regime-tag ${regime}">${regime === "trend" ? "ট্রেন্ড" : regime === "range" ? "রেঞ্জ" : "নিউট্রাল"}</span>`
      : "";

    const dirHtml = sig
      ? App.dirBadge(dir)
      : `<span class="dir-badge small" style="background:rgba(148,163,184,0.12);color:var(--text-dim)">অপেক্ষা…</span>`;

    const tierHtml = sig ? App.tierBadge(sig.tier) : "";
    const conf = sig && sig.confidence !== null && sig.confidence !== undefined
      ? `${(sig.confidence * 100).toFixed(0)}% কনফ`
      : sig ? "কনফ —" : "";

    const priceHtml = price !== undefined && price !== null
      ? `<span class="live-price">${App.fmtPrice(price)}</span>`
      : sig && sig.entry_price
        ? `<span class="live-price">${App.fmtPrice(sig.entry_price)}</span>`
        : "";

    const wrHtml = wr
      ? `<span class="live-wr">রেট <b class="${App.rateClass(wr.win_rate, wr.wins + wr.losses)}">${App.fmtPct(wr.win_rate)}</b> · ${wr.wins}W/${wr.losses}L</span>`
      : `<span class="live-wr">রেট —</span>`;

    const resultHtml = sig && sig.result && sig.result !== "PENDING" ? App.resultBadge(sig.result) : "";

    return `<div class="live-card ${dir || ""}" data-pair="${name}">
      <div class="live-head">
        <span class="live-pair">${name}</span>
        ${regimeTag}
      </div>
      <div class="live-dir">
        ${dirHtml}
        <div class="countdown" data-pair="${name}">
          <svg viewBox="0 0 34 34"><circle class="cd-bg" cx="17" cy="17" r="14"></circle>
          <circle class="cd-fg" cx="17" cy="17" r="14" stroke-dasharray="87.96" stroke-dashoffset="0"></circle></svg>
          <span>--</span>
        </div>
      </div>
      <div class="live-meta">
        <span>${tierHtml}</span>
        <span>${conf}</span>
      </div>
      <div class="live-foot">
        ${wrHtml}
        ${resultHtml || priceHtml}
      </div>
    </div>`;
  },

  tickCountdowns() {
    document.querySelectorAll(".countdown").forEach((el) => {
      const pair = el.dataset.pair;
      const ts = App.state.candleTs[pair];
      const span = el.querySelector("span");
      const fg = el.querySelector(".cd-fg");
      if (ts === undefined || ts === null) {
        span.textContent = "--";
        return;
      }
      const remaining = Math.max(0, 60 - (Date.now() / 1000 - ts));
      span.textContent = `${Math.ceil(remaining)}s`;
      const C = 2 * Math.PI * 14;
      fg.style.strokeDashoffset = String(C * (1 - remaining / 60));
      el.classList.toggle("closing", remaining <= 10);
    });
  },

  onSignal(msg) {
    const sig = msg.signal;
    App.state.liveSignals[msg.pair] = { ...sig, created_at: sig.entry_ts - 60 };
    this.renderGrid();
  },

  onGraded(msg) {
    const sig = App.state.liveSignals[msg.pair];
    if (sig && sig.id === msg.signal.id) {
      sig.result = msg.signal.result;
      this.renderGrid();
    }
    // The live hero always reports all-time numbers — the windowed
    // views (stats) have their own selectors.
    App.refreshWinRates(0).then(() => {
      this.renderHero();
      this.renderGrid();
    });
  },

  onCandle(msg) {
    App.state.livePrices[msg.pair] = msg.candle.close;
    if (!msg.final) {
      App.state.candleTs[msg.pair] = msg.candle.ts;
      const priceEl = document.querySelector(`.live-card[data-pair="${CSS.escape(msg.pair)}"] .live-price`);
      if (priceEl) priceEl.textContent = App.fmtPrice(msg.candle.close);
    }
  },
};
