// localdish page: polls the local server, draws what the dish and router said, and sends controls on request.
// No framework, no external requests. Everything the devices send is data: it reaches the DOM as text, never as markup.
"use strict";
(function () {
  const $ = (id) => document.getElementById(id);

  // how often each reply is asked for, and how old a source may get before its card dims (seconds)
  const EVERY_MS = { state: 1000, history: 5000, obstruction: 60000 };
  const STALE_S = { dish: 5, router: 75, history: 15, obstruction: 150 };
  const TIMEOUT_MS = 8000;
  const CONTROL_TIMEOUT_MS = 30000;
  const OUTAGES_SHOWN = 12;

  let S = null, H = null, O = null;                        // the last good /api/state, /api/history, /api/obstruction
  const got = { state: 0, history: 0, obstruction: 0 };     // Date.now() when each arrived
  const failed = { state: null, history: null, obstruction: null };
  let failingSince = 0;

  // ---- formatting ---------------------------------------------------------------------------------------------

  const isNum = (v) => typeof v === "number" && isFinite(v);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const words = (k) => String(k).replace(/_/g, " ").toLowerCase();
  const pad = (n) => String(n).padStart(2, "0");

  function fmtMs(v) { return isNum(v) ? (v < 10 ? v.toFixed(1) : v.toFixed(0)) + " ms" : "—"; }
  function mbps(bps) { const m = bps / 1e6; return m < 1 ? m.toFixed(2) : m < 10 ? m.toFixed(1) : m.toFixed(0); }
  function fmtRate(bps) { return isNum(bps) ? mbps(bps) + " Mb/s" : "—"; }
  function fmtPct(fraction, digits) { return isNum(fraction) ? (fraction * 100).toFixed(digits === undefined ? 1 : digits) + " %" : "—"; }
  function fmtW(v) { return isNum(v) ? v.toFixed(v < 10 ? 1 : 0) + " W" : "—"; }
  function fmtDur(s) {
    if (!isNum(s)) return "—";
    if (s < 10) return s.toFixed(1) + " s";
    if (s < 60) return Math.round(s) + " s";
    s = Math.round(s);
    if (s < 3600) return Math.floor(s / 60) + " min" + (s % 60 && s < 600 ? " " + (s % 60) + " s" : "");
    if (s < 86400) return Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min";
    return Math.floor(s / 86400) + " d " + Math.floor(s % 86400 / 3600) + " h";
  }
  function fmtAgo(s) { return !isNum(s) ? "" : s < 1.5 ? "just now" : fmtDur(s) + " ago"; }
  function clock(unix) { if (!isNum(unix)) return "—"; const d = new Date(unix * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); }
  function clockMin(m) { m = ((m % 1440) + 1440) % 1440; return pad(Math.floor(m / 60)) + ":" + pad(m % 60); }
  function deg(v, digits) { return isNum(v) ? v.toFixed(digits === undefined ? 0 : digits) + "°" : "—"; }

  // a response body may arrive wrapped ({"device_info": {...}}) or bare; take the inner one when it is there
  function inner(v, key) { return v && typeof v === "object" ? (v[key] && typeof v[key] === "object" ? v[key] : v) : null; }
  function get(obj, path) { return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj); }

  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function toneClass(tone) { return tone === "ok" ? "tone-ok" : tone === "bad" ? "tone-bad" : tone === "warn" ? "tone-warn" : ""; }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // ---- fetching -----------------------------------------------------------------------------------------------

  // Python's json writes NaN and Infinity (the dish reports some); JSON.parse refuses them, so they become null
  function parse(text) {
    try { return JSON.parse(text); } catch (e) {
      return JSON.parse(text.replace(/([:\[,]\s*)-?(?:NaN|Infinity)(?=\s*[,\]}])/g, "$1null"));
    }
  }

  async function request(url, opts, timeoutMs) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, Object.assign({ cache: "no-store", signal: ctl.signal }, opts || {}));
      const text = await r.text();
      let data = null;
      try { data = text ? parse(text) : null; } catch (e) { data = null; }
      return { status: r.status, ok: r.ok, data: data, text: text };
    } catch (e) {
      throw new Error(e.name === "AbortError" ? "no answer in " + timeoutMs / 1000 + " s" : "not reachable");
    } finally { clearTimeout(timer); }
  }

  // one loop per endpoint: single-flight, only while the page is visible, never faster than its cadence
  function poller(name, url, onData) {
    let timer = null, busy = false, lastTry = 0;
    async function tick() {
      timer = null;
      if (document.visibilityState !== "visible") return;
      if (!busy) {
        busy = true; lastTry = Date.now();
        try {
          const r = await request(url, null, TIMEOUT_MS);
          if (!r.ok || !r.data) throw new Error("http " + r.status);
          got[name] = Date.now(); failed[name] = null;
          onData(r.data);
        } catch (e) { failed[name] = e.message; }
        busy = false;
        if (name === "state") afterState();
      }
      schedule(EVERY_MS[name]);
    }
    function schedule(ms) { if (timer) clearTimeout(timer); if (document.visibilityState === "visible") timer = setTimeout(tick, ms); }
    return {
      // on becoming visible: ask now if a turn is due, else wait out the rest of it
      wake() { schedule(Math.max(0, EVERY_MS[name] - (Date.now() - lastTry))); },
      now() { schedule(0); },
      sleep() { if (timer) clearTimeout(timer); timer = null; },
    };
  }

  // seconds since the device answered: the server's age plus the time since this page last heard from the server
  function ageOf(serverAge, when) { return isNum(serverAge) && when ? serverAge + (Date.now() - when) / 1000 : null; }

  function markAge(card, ageEl, age, limit, what) {
    const stale = age === null || age > limit;
    card.classList.toggle("stale", stale);
    if (ageEl) ageEl.textContent = age === null ? "no data yet" : stale ? what + " is " + fmtDur(age) + " old" : "";
  }

  // ---- header and banner ---------------------------------------------------------------------------------------

  function renderHeader() {
    const L = S.localdish || {};
    const h = (S.explain && S.explain.headline) || { text: "unknown", tone: "warn", detail: "" };
    const pill = $("headline");
    pill.textContent = h.text;
    pill.className = "badge pill " + toneClass(h.tone);
    $("headline-detail").textContent = h.detail || "";
    $("demo-badge").hidden = !L.demo;
    $("version").textContent = L.version ? "v" + L.version : "";
    document.title = "localdish · " + h.text;
  }

  function afterState() {
    const lines = [];
    if (failed.state) {
      if (!failingSince) failingSince = Date.now();
      lines.push("localdish is not answering (" + failed.state + ") since " + clock(failingSince / 1000) +
        (S ? ". showing what it said at " + clock(S.localdish && S.localdish.now) + "." : "."));
    } else failingSince = 0;
    if (S && !failed.state) {
      for (const dev of ["dish", "router"]) {
        const l = S.localdish && S.localdish[dev];
        if (l && l.reachable === false) lines.push("the " + dev + " at " + l.host + " is not answering" + (l.error ? ": " + l.error : ""));
      }
    }
    const banner = $("banner");
    banner.hidden = !lines.length;
    banner.textContent = lines.join(" · ");
    const ref = $("refreshed");
    ref.textContent = S ? "refreshed " + clock(S.localdish && S.localdish.now) : "refreshed —";
    ref.className = "num " + (failed.state ? "t-warn" : "");
    if (S) renderStaleness();
  }

  // ---- live cards ----------------------------------------------------------------------------------------------

  const ICONS = {
    gauge: '<path d="M12 14l4-4"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/>',
    arrows: '<path d="M7 4v16"/><path d="m3 16 4 4 4-4"/><path d="M17 20V4"/><path d="m13 8 4-4 4 4"/>',
    tree: '<path d="M12 22v-6"/><path d="M5 16h14L12 3z"/>',
    timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2"/><path d="M10 2h4"/>',
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    cable: '<path d="M4 9h4v6H4z"/><path d="M8 12h8"/><path d="M16 8h4v8h-4z"/>',
    signal: '<path d="M2 20h.01"/><path d="M7 20v-4"/><path d="M12 20v-8"/><path d="M17 20V8"/><path d="M22 4v16"/>',
    sat: '<path d="m13 7 4 4"/><path d="M8.5 11.5 12.5 7.5l4 4-4 4z"/><path d="m3 21 3-3"/><path d="M4 14a6 6 0 0 0 6 6"/>',
  };
  const liveCards = {};

  function liveCard(key, label, icon) {
    if (liveCards[key]) return liveCards[key];
    const card = node("div", "card metric");
    const ic = node("div", "metric-icon");
    ic.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[icon] + "</svg>";
    const body = node("div");
    body.style.minWidth = "0";
    const value = node("p", "metric-value");
    const detail = node("p", "metric-detail");
    body.append(node("p", "eyebrow", label), value, detail);
    card.append(ic, body);
    $("live").append(card);
    return (liveCards[key] = { card: card, icon: ic, value: value, detail: detail });
  }

  function setCard(key, label, icon, value, unit, detail, tone, age, limit, what) {
    const c = liveCard(key, label, icon);
    c.value.textContent = value;
    if (unit) { const u = node("small", null, unit); c.value.append(u); }
    c.detail.textContent = detail || "";
    c.icon.className = "metric-icon " + toneClass(tone);
    const stale = age === null || age > limit;
    c.card.classList.toggle("stale", stale);
    if (stale) c.detail.textContent = (age === null ? "no data yet" : what + " is " + fmtDur(age) + " old") + (detail ? " · " + detail : "");
  }

  // "19 ms" → ["19", "ms"], so the unit can be drawn smaller
  function split(text) { const m = /^(.*\d)\s(\S+)$/.exec(text); return m ? [m[1], m[2]] : [text, ""]; }

  function renderLive() {
    const st = (S.dish && S.dish.status) || {};
    const L = S.localdish || {};
    const age = ageOf(L.dish && L.dish.age_s, got.state);
    const lim = STALE_S.dish;
    const put = (key, label, icon, text, detail, tone, a, l, what) => {
      const [v, u] = split(text);
      setCard(key, label, icon, v, u, detail, tone, a === undefined ? age : a, l || lim, what || "dish status");
    };
    const alerts = st.alerts || {};

    const lat = st.pop_ping_latency_ms, drop = st.pop_ping_drop_rate;
    put("latency", "pop latency", "gauge", fmtMs(lat),
      isNum(drop) ? fmtPct(drop, 0) + " of pings lost now" : "",
      !isNum(lat) ? "" : drop >= 1 ? "bad" : drop > 0 ? "warn" : "ok");

    put("throughput", "download now", "arrows", fmtRate(st.downlink_throughput_bps),
      "upload " + fmtRate(st.uplink_throughput_bps), isNum(st.downlink_throughput_bps) ? "ok" : "");

    const ob = st.obstruction_stats || {};
    const now = ob.currently_obstructed === true;
    put("obstructed", "obstructed sky", "tree", fmtPct(ob.fraction_obstructed),
      now ? "blocked right now" : isNum(ob.time_obstructed) ? "blocked " + fmtPct(ob.time_obstructed) + " of the time" : "",
      now ? "bad" : isNum(ob.fraction_obstructed) ? "ok" : "");

    const up = get(st, "device_state.uptime_s");
    const info = inner(S.dish && S.dish.device_info, "device_info") || st.device_info || {};
    put("uptime", "uptime", "timer", fmtDur(up), isNum(info.bootcount) ? "boot count " + info.bootcount : "", isNum(up) ? "ok" : "");

    // power only comes from the history ring
    const ring = H && H.ring;
    const pw = ring && ring.power_w ? ring.power_w.filter(isNum) : [];
    const avg = pw.length ? pw.reduce((a, b) => a + b, 0) / pw.length : null;
    put("power", "power", "bolt", fmtW(pw.length ? pw[pw.length - 1] : null),
      avg !== null ? fmtW(avg) + " average over " + fmtDur(pw.length) : "",
      pw.length ? "ok" : "", ageOf(H && H.age_s, got.history), STALE_S.history, "history");

    const eth = st.eth_speed_mbps;
    const ethWarn = alerts.no_ethernet_link || alerts.slow_ethernet_speeds || alerts.slow_ethernet_speeds_100;
    put("ethernet", "ethernet", "cable", isNum(eth) ? eth + " Mb/s" : "—",
      alerts.no_ethernet_link ? "no link" : ethWarn ? "slower than expected" : isNum(eth) ? "link speed" : "",
      ethWarn ? "warn" : isNum(eth) ? "ok" : "");

    // the dish reports whether its signal clears the noise floor, not a number
    const above = st.is_snr_above_noise_floor, low = st.is_snr_persistently_low;
    put("signal", "signal", "signal", above === true ? "good" : above === false ? "weak" : "—",
      above === undefined ? "" : (above ? "above" : "below") + " the noise floor" + (low ? ", weak for a while" : ""),
      above === true && !low ? "ok" : above === undefined ? "" : "warn");

    const gps = st.gps_stats || {};
    const loc = inner(S.dish && S.dish.location, "lla");
    const where = loc && isNum(loc.lat) ? loc.lat.toFixed(4) + ", " + loc.lon.toFixed(4) :
      S.dish && S.dish.location_error ? "location not shared" : "";
    put("gps", "gps", "sat", isNum(gps.gps_sats) ? gps.gps_sats + " sats" : "—",
      [gps.gps_valid === true ? "fix valid" : gps.gps_valid === false ? "no fix" : "", where].filter(Boolean).join(" · "),
      gps.gps_valid === true ? "ok" : gps.gps_valid === false ? "warn" : "");
  }

  // ---- aim -----------------------------------------------------------------------------------------------------

  function renderAim() {
    const a = S.explain && S.explain.aim;
    $("aim-card").hidden = !a;
    if (!a) return;
    const state = $("aim-state");
    if (a.ok === true) { state.textContent = "aimed well"; state.className = "badge tone-ok"; }
    else if (a.ok === false) { state.textContent = "needs a nudge"; state.className = "badge tone-warn"; }
    else { state.textContent = words(a.confidence || "not converged"); state.className = "badge tone-warn"; }

    const list = $("aim-text");
    list.replaceChildren(...(a.text || []).map((t) => node("li", null, t)));
    $("aim-nums").textContent = "facing " + deg(a.az_now) + " at " + deg(a.el_now, 1) + " elevation · wants " +
      deg(a.az_want) + " at " + deg(a.el_want, 1) + (isNum(a.uncertainty_deg) ? " · ±" + a.uncertainty_deg.toFixed(1) + "°" : "");

    // top-down: north up, clockwise; the now arrow in the text colour, the wanted one in green, the turn between them
    const c = 80, r = 60;
    const pt = (az, rr) => [c + rr * Math.sin(az * Math.PI / 180), c - rr * Math.cos(az * Math.PI / 180)];
    const arrow = (az, colour, dash) => {
      const [x, y] = pt(az, r - 6), [hx1, hy1] = pt(az - 7, r - 16), [hx2, hy2] = pt(az + 7, r - 16);
      return '<line x1="' + c + '" y1="' + c + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1) + '" style="stroke:' + colour + '" stroke-width="3" stroke-linecap="round"' + (dash ? ' stroke-dasharray="5 4"' : "") + "/>" +
        '<path d="M' + x.toFixed(1) + " " + y.toFixed(1) + "L" + hx1.toFixed(1) + " " + hy1.toFixed(1) + "L" + hx2.toFixed(1) + " " + hy2.toFixed(1) + 'Z" style="fill:' + colour + '"/>';
    };
    let svg = '<svg viewBox="0 0 160 160" role="img"><circle cx="80" cy="80" r="' + r + '" fill="none" style="stroke:var(--border)" stroke-width="1.5"/>';
    for (const [lab, az] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
      const [x, y] = pt(az, r + 11);
      svg += '<text x="' + x.toFixed(1) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="middle" style="fill:var(--muted);font:600 11px var(--sans)">' + lab + "</text>";
    }
    if (isNum(a.turn_deg) && Math.abs(a.turn_deg) >= 0.5) {
      const [x1, y1] = pt(a.az_now, r - 22), [x2, y2] = pt(a.az_now + a.turn_deg, r - 22);
      svg += '<path d="M' + x1.toFixed(1) + " " + y1.toFixed(1) + "A" + (r - 22) + " " + (r - 22) + " 0 0 " + (a.turn_deg > 0 ? 1 : 0) + " " + x2.toFixed(1) + " " + y2.toFixed(1) +
        '" fill="none" style="stroke:' + (a.ok ? "var(--ok)" : "var(--warn)") + '" stroke-width="2"/>';
    }
    if (isNum(a.az_want)) svg += arrow(a.az_want, "var(--ok)", true);
    if (isNum(a.az_now)) svg += arrow(a.az_now, "var(--fg)", false);
    svg += '<circle cx="80" cy="80" r="3.5" style="fill:var(--fg)"/></svg>';
    $("aim-svg").innerHTML = svg;
  }

  // ---- alerts --------------------------------------------------------------------------------------------------

  function renderAlerts() {
    const list = (S.explain && S.explain.alerts) || [];
    $("alerts-card").hidden = !list.length;
    $("alerts").replaceChildren(...list.map((a) => {
      const li = node("li", toneClass(a.tone));
      li.append(node("span", null, a.text), node("code", null, a.key));
      return li;
    }));
  }

  // ---- graphs --------------------------------------------------------------------------------------------------

  function niceStep(span) {
    const raw = span / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }

  // series: [{label, values (number|null, oldest→newest, 1 per second), colour, fmt}]; opts: {min, max, area}
  function drawChart(box, title, series, opts) {
    const W = Math.max(240, Math.round(box.clientWidth || 400)), Ht = 136;
    const L = 38, R = 6, T = 6, B = 18, pw = W - L - R, ph = Ht - T - B;
    const all = [].concat(...series.map((s) => s.values.filter(isNum)));
    const n = Math.max(0, ...series.map((s) => s.values.length));
    let lo = opts.min !== undefined ? opts.min : Math.min(0, ...all);
    let hi = opts.max !== undefined ? opts.max : Math.max(lo + 1e-9, ...all);
    // a few spikes would flatten everything else: scale to the 98th percentile and say that the peaks are cut off
    let peak = null;
    if (opts.clip && all.length > 20) {
      const p98 = all.slice().sort((a, b) => a - b)[Math.floor(all.length * 0.98)];
      if (p98 > 0 && hi > 3 * p98) { peak = hi; hi = p98 * 1.5; }
    }
    const step = niceStep(hi - lo || 1);
    if (opts.max === undefined) hi = Math.ceil(hi / step) * step || step;
    const X = (i) => L + (n < 2 ? pw : i / (n - 1) * pw);
    const Y = (v) => T + ph - (v - lo) / (hi - lo || 1) * ph;

    let head = '<div class="chart-head"><b>' + esc(title) + '</b><span class="chart-now">';
    for (const s of series) {
      const lastV = [...s.values].reverse().find(isNum);
      head += '<span class="sw" style="background:' + s.colour + '"></span>' + (series.length > 1 ? "<i>" + esc(s.label) + " </i>" : "") + esc(lastV === undefined ? "—" : s.fmt(lastV));
    }
    head += "</span></div>";
    if (n < 2) { box.innerHTML = head + '<div class="empty" style="margin-top:.5rem">no samples yet</div>'; return; }

    let svg = '<svg class="chart-svg" viewBox="0 0 ' + W + " " + Ht + '" role="img" aria-label="' + esc(title) + ' over the last ' + esc(fmtDur(n)) + '">';
    for (let v = lo; v <= hi + step / 1e6; v += step) {
      const y = Y(v).toFixed(1);
      svg += '<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/>' +
        '<text class="tick" x="' + (L - 5) + '" y="' + (+y + 3.5) + '" text-anchor="end">' + esc(opts.tick(v)) + "</text>";
    }
    // x ticks every 5 min back from now, as far as the samples reach
    for (let back = 0; back < n; back += 300) {
      const x = X(n - 1 - back);
      svg += '<line class="grid" x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' + T + '" y2="' + (T + ph) + '" opacity="0.5"/>' +
        '<text class="tick" x="' + x.toFixed(1) + '" y="' + (Ht - 4) + '" text-anchor="' + (back === 0 ? "end" : "middle") + '">' + (back === 0 ? "now" : "−" + back / 60 + " min") + "</text>";
    }
    for (const s of series) {
      let d = "", pen = false, area = "", first = null, last = null;
      s.values.forEach((v, i) => {
        if (!isNum(v)) { pen = false; return; }
        const x = X(i).toFixed(1), y = Y(Math.min(hi, Math.max(lo, v))).toFixed(1);
        d += (pen ? "L" : "M") + x + " " + y;
        pen = true;
        if (first === null) first = x;
        last = x;
      });
      if (opts.area && first !== null) area = '<path class="area" d="' + d + "L" + last + " " + (T + ph) + "L" + first + " " + (T + ph) + 'Z" style="fill:' + s.colour + '"/>';
      svg += area + '<path class="line" d="' + d + '" style="stroke:' + s.colour + '"/>';
    }
    if (peak !== null) svg += '<text class="tick" x="' + (W - R) + '" y="' + (T + 9) + '" text-anchor="end">peaks to ' + esc(opts.tick(peak)) + " cut off</text>";
    box.innerHTML = head + svg + "</svg>";
  }

  function renderCharts() {
    const ring = (H && H.ring) || {};
    const v = (k) => Array.isArray(ring[k]) ? ring[k] : [];
    const toMb = (a) => a.map((x) => isNum(x) ? x / 1e6 : null);
    drawChart($("chart-latency"), "pop latency", [{ label: "latency", values: v("latency_ms"), colour: "var(--sky)", fmt: fmtMs }],
      { min: 0, clip: true, tick: (x) => x.toFixed(0) + " ms" });
    drawChart($("chart-loss"), "ping loss", [{ label: "loss", values: v("drop").map((x) => isNum(x) ? x * 100 : null), colour: "var(--bad)", fmt: (x) => x.toFixed(0) + " %" }],
      { min: 0, max: 100, area: true, tick: (x) => x.toFixed(0) + " %" });
    const mb = (x) => (x < 1 ? x.toFixed(2) : x < 10 ? x.toFixed(1) : x.toFixed(0)) + " Mb/s";
    drawChart($("chart-throughput"), "throughput", [
      { label: "down", values: toMb(v("down_bps")), colour: "var(--ok)", fmt: mb },
      { label: "up", values: toMb(v("up_bps")), colour: "var(--violet)", fmt: mb }],
      { min: 0, tick: (x) => (x < 10 && x % 1 ? x.toFixed(1) : x.toFixed(0)) + " Mb/s" });
    drawChart($("chart-power"), "dish power", [{ label: "power", values: v("power_w"), colour: "var(--warn)", fmt: fmtW }],
      { min: 0, tick: (x) => x.toFixed(0) + " W" });
  }

  // ---- outages -------------------------------------------------------------------------------------------------

  let showAllOutages = false;
  function renderOutages() {
    const all = (H && H.outages) || [];
    const since = (Date.now() - got.history) / 1000;
    const recent = all.filter((o) => o.ago_s + since <= 900);
    const down = recent.reduce((t, o) => t + (o.duration_s || 0), 0);
    $("outages-summary").textContent = !all.length ? "" :
      recent.length + " in the last 15 min (" + fmtDur(down) + " without internet) · " + all.length + " on record";
    const shown = showAllOutages ? all : all.slice(0, OUTAGES_SHOWN);
    const list = $("outages");
    if (!all.length) { list.replaceChildren(node("li", "empty", H ? "no outages on record" : "waiting for history")); }
    else list.replaceChildren(...shown.map((o) => {
      const li = node("li");
      li.append(node("span", null, o.cause_text || words(o.cause || "unknown")),
        node("span", "right", fmtDur(o.duration_s)),
        node("span", "sub", clock(o.start_unix) + " · " + fmtAgo(o.ago_s + since) + (o.did_switch ? " · switched satellite" : "")));
      return li;
    }));
    const more = $("outages-more");
    more.hidden = all.length <= OUTAGES_SHOWN;
    more.textContent = showAllOutages ? "show fewer" : "show all " + all.length;
  }

  // ---- obstruction map -----------------------------------------------------------------------------------------

  function rgb(hex) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [128, 128, 128];
  }

  function renderMap() {
    const canvas = $("obstruction-canvas"), note = $("map-note");
    const m = O && O.map;
    const size = Math.round((canvas.clientWidth || 320) * (window.devicePixelRatio || 1));
    canvas.width = size; canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, size, size);
    if (!m || !Array.isArray(m.snr) || !m.num_rows || !m.num_cols) {
      note.textContent = O && O.error ? "no map: " + O.error : "waiting for the map";
      return;
    }
    // the dish's grid is already polar: the centre cell is the zenith, the edge circle is max_theta_deg from it
    const rows = m.num_rows, cols = m.num_cols;
    const grid = document.createElement("canvas");
    grid.width = cols; grid.height = rows;
    const g = grid.getContext("2d"), img = g.createImageData(cols, rows);
    const ok = rgb(css("--ok")), bad = rgb(css("--bad"));
    let seen = 0, blocked = 0;
    m.snr.forEach((v, i) => {
      if (!isNum(v) || v < 0) return;              // −1: no satellite has passed there yet
      seen++;
      const o = i * 4, c = v === 0 ? bad : ok;
      if (v === 0) blocked++;
      img.data[o] = c[0]; img.data[o + 1] = c[1]; img.data[o + 2] = c[2];
      img.data[o + 3] = v === 0 ? 255 : Math.round(90 + 165 * Math.min(1, v));
    });
    g.putImageData(img, 0, 0);
    ctx.save();
    ctx.beginPath(); ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2); ctx.clip();
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(grid, 0, 0, size, size);
    ctx.restore();

    const maxT = isNum(m.max_theta_deg) && m.max_theta_deg > 0 ? m.max_theta_deg : 90;
    const dpr = size / (canvas.clientWidth || size);
    ctx.strokeStyle = css("--muted"); ctx.globalAlpha = 0.45; ctx.lineWidth = dpr;
    ctx.fillStyle = css("--muted"); ctx.font = (10 * dpr) + "px " + css("--mono");
    for (const el of [30, 60]) {
      const rr = (90 - el) / maxT * size / 2;
      ctx.beginPath(); ctx.arc(size / 2, size / 2, rr, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 0.8; ctx.fillText(el + "°", size / 2 + 3 * dpr, size / 2 - rr - 3 * dpr); ctx.globalAlpha = 0.45;
    }
    ctx.beginPath(); ctx.moveTo(size / 2, 0); ctx.lineTo(size / 2, size); ctx.moveTo(0, size / 2); ctx.lineTo(size, size / 2); ctx.stroke();
    ctx.globalAlpha = 1;
    const earth = m.map_reference_frame === "FRAME_EARTH";
    if (earth) { ctx.fillStyle = css("--fg"); ctx.font = "600 " + (11 * dpr) + "px " + css("--sans"); ctx.textAlign = "center"; ctx.fillText("N", size / 2, 13 * dpr); ctx.textAlign = "start"; }

    const edge = isNum(m.min_elevation_deg) ? m.min_elevation_deg : 90 - maxT;
    note.textContent = seen + " cells seen · " + blocked + " obstructed (" + (seen ? (100 * blocked / seen).toFixed(1) : "0") + " %) · edge " +
      deg(edge) + " above the horizon · " + (earth ? "north up" : m.map_reference_frame ? "turned with the dish" : "orientation unknown");
  }

  async function refreshMap() {
    const btn = $("map-refresh");
    btn.disabled = true; btn.classList.add("busy");
    try {
      const r = await request("/api/refresh/obstruction", post({}), TIMEOUT_MS);
      if (r.status === 429) $("map-note").textContent = "asked recently: try again in " + fmtDur((r.data && r.data.retry_after_s) || 10);
      else if (!r.ok) $("map-note").textContent = "refresh failed: " + ((r.data && r.data.error) || "http " + r.status);
      else setTimeout(() => loops.obstruction.now(), 3000);    // the server fetches it; pick it up once it has
    } catch (e) { $("map-note").textContent = "refresh failed: " + e.message; }
    btn.disabled = false; btn.classList.remove("busy");
  }

  // ---- wifi clients --------------------------------------------------------------------------------------------

  const BANDS = { RF_2GHZ: "2.4 GHz", RF_5GHZ: "5 GHz", RF_5GHZ_HIGH: "5 GHz high", ETH: "ethernet" };

  function bars(dbm) {
    const n = dbm >= -55 ? 4 : dbm >= -65 ? 3 : dbm >= -75 ? 2 : 1;
    const tone = n >= 3 ? "t-ok" : n === 2 ? "t-warn" : "t-bad";
    const s = node("span", "bars " + tone);
    for (let i = 1; i <= 4; i++) { const b = node("span", i <= n ? "on" : ""); b.style.height = (i * 25) + "%"; s.append(b); }
    return s;
  }

  function renderClients() {
    const card = $("clients-card");
    const list = S.router && Array.isArray(S.router.clients) ? S.router.clients : null;
    card.hidden = !S.router;
    if (!S.router) return;
    const L = S.localdish || {};
    markAge(card, $("clients-age"), ageOf(L.router && L.router.age_s, got.state), STALE_S.router, "router data");
    const tbody = $("clients");
    if (!list || !list.length) { const tr = node("tr"); const td = node("td", "muted", list ? "no clients" : "not read yet"); td.colSpan = 5; tr.append(td); tbody.replaceChildren(tr); return; }
    const sorted = list.slice().sort((a, b) => (isNum(b.signal_strength) ? b.signal_strength : -999) - (isNum(a.signal_strength) ? a.signal_strength : -999));
    tbody.replaceChildren(...sorted.map((c) => {
      const tr = node("tr");
      const name = node("td", "name", c.name || c.given_name || c.mac_address || "unnamed");
      if (c.ip_address) name.append(node("small", "mono", c.ip_address));
      const sig = node("td");
      if (isNum(c.signal_strength)) { sig.append(bars(c.signal_strength), document.createTextNode(c.signal_strength.toFixed(0) + " dBm")); }
      else sig.textContent = "—";
      const rx = get(c, "rx_stats.rate_mbps"), tx = get(c, "tx_stats.rate_mbps");
      const rate = node("td", null, isNum(rx) ? rx.toFixed(0) + " Mb/s" : "—");
      if (isNum(tx)) rate.append(node("small", null, "send " + tx.toFixed(0) + " Mb/s"));
      tr.append(name, node("td", null, BANDS[c.iface] || (c.iface ? words(c.iface) : "—")), sig, rate,
        node("td", null, isNum(c.associated_time_s) ? fmtDur(c.associated_time_s) : "—"));
      return tr;
    }));
  }

  // ---- controls ------------------------------------------------------------------------------------------------

  const GROUPS = ["restart", "settings", "maintenance", "tests"];
  const ctls = {};
  let pending = null;

  function post(body) { return { method: "POST", headers: { "X-Localdish": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) }; }

  const isChoice = (p) => Array.isArray(p.choices) || p.type === "enum" || p.type === "choice";
  const isClock = (p) => /start_minutes$/.test(p.name);

  function showParam(p, v) {
    if (v === undefined || v === null) return "—";
    if (p.type === "bool") return v ? "on" : "off";
    if (isChoice(p)) return words(v);
    if (isClock(p)) return clockMin(v);
    if (/_minutes$/.test(p.name)) return fmtDur(v * 60);
    return String(v);
  }

  function paramInput(ctl, p) {
    const wrap = node("label", "param");
    let input;
    if (p.type === "bool") {
      wrap.className = "param toggle";
      input = node("input"); input.type = "checkbox";
      wrap.append(input, node("span", "track"), node("span", null, p.label || words(p.name)));
    } else {
      wrap.append(node("span", null, p.label || words(p.name)));
      if (isChoice(p)) {
        input = node("select");
        for (const c of p.choices || []) { const o = node("option", null, words(c)); o.value = c; input.append(o); }
      } else if (isClock(p)) {
        input = node("input"); input.type = "time"; input.step = 60;
      } else {
        input = node("input"); input.type = "number"; input.step = 1;
        if (isNum(p.min)) input.min = p.min;
        if (isNum(p.max)) input.max = p.max;
      }
      wrap.append(input);
      if (/_minutes$/.test(p.name) && !isClock(p)) {
        const hint = node("span", "hint");
        const upd = () => { const n = parseInt(input.value, 10); hint.textContent = isNum(n) ? "= " + fmtDur(n * 60) : ""; };
        input.addEventListener("input", upd);
        ctl.hints.push(upd);
        wrap.append(hint);
      }
    }
    input.addEventListener("input", () => { ctl.dirty = true; });
    input.addEventListener("change", () => { ctl.dirty = true; });
    ctl.inputs[p.name] = { p: p, el: input };
    return wrap;
  }

  function setInputs(ctl, cur) {
    for (const k in ctl.inputs) {
      const { p, el } = ctl.inputs[k], v = cur[k];
      if (v === undefined) continue;
      if (p.type === "bool") el.checked = !!v;
      else if (isClock(p)) el.value = clockMin(v);
      else el.value = String(v);
    }
    ctl.hints.forEach((f) => f());
  }

  // read the inputs back into params, or say what is wrong with them
  function readInputs(ctl) {
    const out = {};
    for (const k in ctl.inputs) {
      const { p, el } = ctl.inputs[k];
      if (p.type === "bool") out[k] = el.checked;
      else if (isChoice(p)) out[k] = el.value;
      else if (isClock(p)) {
        const m = /^(\d{1,2}):(\d{2})/.exec(el.value);
        if (!m) throw new Error((p.label || words(k)) + ": pick a time");
        out[k] = parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
      } else {
        const n = Number(el.value);
        if (el.value === "" || !Number.isInteger(n)) throw new Error((p.label || words(k)) + ": a whole number, please");
        if ((isNum(p.min) && n < p.min) || (isNum(p.max) && n > p.max)) throw new Error((p.label || words(k)) + ": between " + p.min + " and " + p.max);
        out[k] = n;
      }
    }
    return out;
  }

  function buildControl(c) {
    let group = $("controls").querySelector('[data-group="' + CSS.escape(c.group) + '"]');
    if (!group) {
      group = node("div", "control-group");
      group.dataset.group = c.group;
      group.append(node("h3", null, words(c.group)));
      const at = GROUPS.indexOf(c.group);
      const after = [...$("controls").children].find((g) => { const i = GROUPS.indexOf(g.dataset.group); return at >= 0 && (i < 0 || i > at); });
      $("controls").insertBefore(group, after || null);
    }
    const ctl = { name: c.name, inputs: {}, hints: [], dirty: false, busy: false, root: node("div", "control") };
    ctl.root.dataset.control = c.name;
    const title = node("div", "control-title");
    title.append(node("span", null, c.label || words(c.name)));
    ctl.now = node("div", "control-now");
    ctl.root.append(title, ctl.now);
    for (const p of c.params || []) ctl.root.append(paramInput(ctl, p));
    ctl.btn = node("button", "btn " + (c.group === "restart" || c.group === "maintenance" ? "danger" : "primary"),
      (c.params || []).length ? "apply" : c.group === "tests" ? "run" : c.label || words(c.name));
    ctl.btn.type = "button";
    ctl.btn.addEventListener("click", () => ask(ctl));
    ctl.reason = node("div", "reason");
    ctl.result = node("div", "result");
    ctl.root.append(ctl.btn, ctl.reason, ctl.result);
    group.append(ctl.root);
    return (ctls[c.name] = ctl);
  }

  function renderControls() {
    const list = Array.isArray(S.controls) ? S.controls : [];
    for (const c of list) {
      const ctl = ctls[c.name] || buildControl(c);
      ctl.def = c;
      const cur = c.current || {};
      const params = c.params || [];
      ctl.now.textContent = params.some((p) => cur[p.name] !== undefined)
        ? "now: " + params.filter((p) => cur[p.name] !== undefined).map((p) => showParam(p, cur[p.name])).join(" · ")
        : "";
      if (!ctl.dirty) setInputs(ctl, cur);
      ctl.btn.disabled = ctl.busy || !c.available;
      ctl.reason.textContent = c.available ? "" : c.reason || "not available now";
      for (const k in ctl.inputs) ctl.inputs[k].el.disabled = ctl.busy || !c.available;
    }
    renderSpeedtest();
  }

  function renderSpeedtest() {
    const ctl = ctls.speedtest, run = S.running && S.running.speedtest;
    if (!ctl || !run || ctl.busy) return;
    const secs = isNum(S.localdish && S.localdish.now) && isNum(run.started) ? S.localdish.now - run.started : null;
    const nums = [];
    (function walk(o, path) {
      if (nums.length >= 6 || !o || typeof o !== "object") return;
      for (const k in o) {
        const v = o[k];
        if (isNum(v)) nums.push(words(path.concat(k).join(" ")) + " " + (Math.abs(v) < 1e4 ? +v.toFixed(2) : Math.round(v)));
        else if (v && typeof v === "object" && !Array.isArray(v)) walk(v, path.concat(k));
      }
    })(run.status, []);
    ctl.result.className = "result " + (run.done ? "t-ok" : "muted");
    ctl.result.textContent = (run.done ? "finished" : "running" + (secs !== null ? ", " + fmtDur(secs) : "")) + (nums.length ? ": " + nums.join(" · ") : "");
  }

  function ask(ctl) {
    let params;
    try { params = readInputs(ctl); } catch (e) { ctl.result.className = "result t-bad"; ctl.result.textContent = e.message; return; }
    pending = { ctl: ctl, params: params };
    const c = ctl.def;
    $("confirm-title").textContent = c.label || words(c.name);
    $("confirm-text").textContent = c.confirm || "send this to the " + (c.target || "device") + "?";
    $("confirm-params").replaceChildren(...(c.params || []).map((p) => node("li", null, (p.label || words(p.name)) + ": " + showParam(p, params[p.name]))));
    $("confirm-ok").className = "btn " + (c.group === "restart" || c.group === "maintenance" ? "danger" : "primary");
    $("confirm").returnValue = "";
    $("confirm").showModal();
  }

  async function send(ctl, params) {
    ctl.busy = true;
    ctl.btn.disabled = true; ctl.btn.classList.add("busy");
    ctl.result.className = "result muted"; ctl.result.textContent = "sending…";
    let text, tone;
    try {
      const r = await request("/api/control/" + encodeURIComponent(ctl.name), post({ params: params }), CONTROL_TIMEOUT_MS);
      if (r.ok && r.data && r.data.ok) { text = r.data.text || "done"; tone = "t-ok"; ctl.dirty = false; }
      else { text = (r.data && r.data.error) || "http " + r.status; tone = "t-bad"; }
    } catch (e) { text = e.message; tone = "t-bad"; }
    ctl.busy = false;
    ctl.btn.classList.remove("busy");
    ctl.result.className = "result " + tone;
    ctl.result.textContent = clock(Date.now() / 1000) + " · " + text;
    if (S) renderControls();
    loops.state.now();
  }

  $("confirm").addEventListener("close", () => {
    const p = pending;
    pending = null;
    if (p && $("confirm").returnValue === "ok") send(p.ctl, p.params);
  });

  // ---- events, facts, raw --------------------------------------------------------------------------------------

  function renderEvents() {
    const list = (Array.isArray(S.events) ? S.events : []).slice().reverse();
    if (!list.length) { $("events").replaceChildren(node("li", "empty", "nothing yet")); return; }
    $("events").replaceChildren(...list.map((e) => {
      const li = node("li");
      li.append(node("span", "num muted", clock(e.t)), node("span", "kind", e.kind), node("span", null, e.text));
      return li;
    }));
  }

  function renderFacts() {
    const rows = (S.explain && S.explain.facts) || [];
    $("facts").replaceChildren(...rows.map((r) => {
      const d = node("div");
      d.append(node("dt", null, r[0]), node("dd", null, r[1]));
      return d;
    }));
  }

  const RAW = [
    ["dish status", () => S && S.dish && S.dish.status],
    ["dish config", () => S && S.dish && S.dish.config],
    ["dish device info", () => S && S.dish && S.dish.device_info],
    ["dish diagnostics", () => S && S.dish && S.dish.diagnostics],
    ["dish location", () => S && S.dish && (S.dish.location || (S.dish.location_error ? { error: S.dish.location_error } : null))],
    ["dish event log", () => H && H.event_log],
    ["obstruction map", () => O && O.map],
    ["router status", () => S && S.router && S.router.status],
    ["router clients", () => S && S.router && S.router.clients],
    ["router device info", () => S && S.router && S.router.device_info],
    ["router ping", () => S && S.router && S.router.ping],
  ];
  let rawPick = 0, rawText = "";

  function renderRaw() {
    if (!$("raw").open) return;
    const bar = $("raw-bar");
    if (!bar.children.length) RAW.forEach(([label], i) => {
      const b = node("button", "btn small", label);
      b.type = "button";
      b.addEventListener("click", () => { rawPick = i; rawText = ""; renderRaw(); });
      bar.append(b);
    });
    [...bar.children].forEach((b, i) => b.setAttribute("aria-pressed", String(i === rawPick)));
    const v = RAW[rawPick][1]();
    const text = v === undefined || v === null ? "(not read yet)" : JSON.stringify(v, null, 1);
    if (text === rawText) return;
    const pre = $("raw-json"), top = pre.scrollTop;
    pre.textContent = rawText = text;
    pre.scrollTop = top;
  }

  // ---- staleness and the loops ---------------------------------------------------------------------------------

  function renderStaleness() {
    const hAge = ageOf(H && H.age_s, got.history);
    for (const [card, el] of [["graph-card-1", "history-age-1"], ["graph-card-2", "history-age-2"], ["outages-card", "outages-age"]]) {
      markAge($(card), $(el), hAge, STALE_S.history, "history");
    }
    markAge($("map-card"), $("map-age"), ageOf(O && O.age_s, got.obstruction), STALE_S.obstruction, "the map");
    if (H && H.error) $("history-age-1").textContent = $("history-age-2").textContent = $("outages-age").textContent = "history: " + H.error;
    if (O && O.error) $("map-age").textContent = "map: " + O.error;
    renderLive();
  }

  function onState(data) {
    S = data;
    renderHeader();
    renderAim();
    renderAlerts();
    renderClients();
    renderControls();
    renderEvents();
    renderFacts();
    renderRaw();
  }
  function onHistory(data) { H = data; renderCharts(); renderOutages(); renderRaw(); }
  function onObstruction(data) { O = data; renderMap(); renderRaw(); }

  const loops = {
    state: poller("state", "/api/state", onState),
    history: poller("history", "/api/history", onHistory),
    obstruction: poller("obstruction", "/api/obstruction", onObstruction),
  };

  document.addEventListener("visibilitychange", () => {
    for (const k in loops) document.visibilityState === "visible" ? loops[k].wake() : loops[k].sleep();
  });
  $("outages-more").addEventListener("click", () => { showAllOutages = !showAllOutages; renderOutages(); });
  $("map-refresh").addEventListener("click", refreshMap);
  $("raw").addEventListener("toggle", () => { rawText = ""; renderRaw(); });

  // redraw what is drawn to size when the size or the colour scheme changes
  let redraw = null;
  const again = () => { clearTimeout(redraw); redraw = setTimeout(() => { if (H) renderCharts(); if (O) renderMap(); }, 120); };
  window.addEventListener("resize", again);
  if (window.matchMedia) window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", again);

  for (const k in loops) loops[k].now();
})();
