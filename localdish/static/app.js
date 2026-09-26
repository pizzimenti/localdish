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
  const PING_SHOWN = 10;

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
  // the dish keeps its power-save schedule in minutes after midnight UTC; people think in their own clock
  function utcToLocalMin(m) { const d = new Date(); d.setUTCHours(Math.floor(m / 60) % 24, m % 60, 0, 0); return d.getHours() * 60 + d.getMinutes(); }
  function localToUtcMin(m) { const d = new Date(); d.setHours(Math.floor(m / 60) % 24, m % 60, 0, 0); return d.getUTCHours() * 60 + d.getUTCMinutes(); }
  // clock times people read, in the browser's own style ("3:15 AM" or "03:15")
  const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
  const hourFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric" });
  function timeOfMin(m, hourOnly) { m = ((m % 1440) + 1440) % 1440; return (hourOnly ? hourFmt : timeFmt).format(new Date(2000, 0, 1, Math.floor(m / 60), m % 60)); }
  function timeOfUnix(unix) { return isNum(unix) ? timeFmt.format(new Date(unix * 1000)) : "—"; }
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

    // proto3 leaves a zero off the wire: with a status in hand, no drop rate means none dropped
    const lat = st.pop_ping_latency_ms, drop = isNum(st.pop_ping_drop_rate) ? st.pop_ping_drop_rate : isNum(lat) ? 0 : null;
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
    // held_s: the dish stopped saying where it wants to point (it does while searching), so this is its last reading
    const held = "held_s" in a;
    $("aim-card").classList.toggle("held", held);
    if (held) { state.textContent = "last reading"; state.className = "badge"; }
    else if (a.ok === true) { state.textContent = "aimed well"; state.className = "badge tone-ok"; }
    else if (a.ok === false) { state.textContent = "needs a nudge"; state.className = "badge tone-warn"; }
    else { state.textContent = words(a.confidence || "not converged"); state.className = "badge tone-warn"; }

    const list = $("aim-text");
    list.replaceChildren(...(a.text || []).map((t) => node("li", null, t)));
    $("aim-nums").textContent = "facing " + deg(a.az_now) + " at " + deg(a.el_now, 1) + " elevation · wants " +
      deg(a.az_want) + " at " + deg(a.el_want, 1) + (isNum(a.uncertainty_deg) ? " · ±" + a.uncertainty_deg.toFixed(1) + "°" : "");

    // top-down: north up, clockwise; the now arrow in the text colour, the wanted one in green, the turn between them
    const c = 80, r = 60;
    const pt = (az, rr) => [c + rr * Math.sin(az * Math.PI / 180), c - rr * Math.cos(az * Math.PI / 180)];
    // colours come from classes (k-ok, k-fg …), never style attributes: the page runs under style-src 'self'
    const arrow = (az, colour, dash) => {
      const [x, y] = pt(az, r - 6), [hx1, hy1] = pt(az - 7, r - 16), [hx2, hy2] = pt(az + 7, r - 16);
      return '<g class="arrow k-' + colour + '"><line x1="' + c + '" y1="' + c + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1) + '" stroke-width="3" stroke-linecap="round"' + (dash ? ' stroke-dasharray="5 4"' : "") + "/>" +
        '<path d="M' + x.toFixed(1) + " " + y.toFixed(1) + "L" + hx1.toFixed(1) + " " + hy1.toFixed(1) + "L" + hx2.toFixed(1) + " " + hy2.toFixed(1) + 'Z"/></g>';
    };
    let svg = '<svg viewBox="0 0 160 160" role="img"><circle class="aim-ring" cx="80" cy="80" r="' + r + '" stroke-width="1.5"/>';
    for (const [lab, az] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
      const [x, y] = pt(az, r + 11);
      svg += '<text x="' + x.toFixed(1) + '" y="' + (y + 4).toFixed(1) + '" text-anchor="middle" class="aim-label">' + lab + "</text>";
    }
    if (isNum(a.turn_deg) && Math.abs(a.turn_deg) >= 0.5) {
      const [x1, y1] = pt(a.az_now, r - 22), [x2, y2] = pt(a.az_now + a.turn_deg, r - 22);
      svg += '<path d="M' + x1.toFixed(1) + " " + y1.toFixed(1) + "A" + (r - 22) + " " + (r - 22) + " 0 0 " + (a.turn_deg > 0 ? 1 : 0) + " " + x2.toFixed(1) + " " + y2.toFixed(1) +
        '" class="aim-turn k-' + (a.ok ? "ok" : "warn") + '" stroke-width="2"/>';
    }
    if (isNum(a.az_want)) svg += arrow(a.az_want, "ok", true);
    if (isNum(a.az_now)) svg += arrow(a.az_now, "fg", false);
    svg += '<circle class="aim-hub" cx="80" cy="80" r="3.5"/></svg>';
    $("aim-svg").innerHTML = svg;
  }

  // ---- alerts --------------------------------------------------------------------------------------------------

  function renderAlerts() {
    const list = (S.explain && S.explain.alerts) || [];
    $("alerts-card").hidden = !list.length;
    $("alerts").replaceChildren(...list.map((a) => {
      const li = node("li", toneClass(a.tone));
      li.append(node("span", null, a.text || words(a.name || "alert")));
      if (a.name) li.append(node("code", null, a.name));
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
    // bursts of seconds-long latency would flatten the usual tens of ms into the axis: when the peak is over
    // 5× the 90th percentile, scale to 4× it and print how high the peaks went
    let peak = null;
    if (opts.clip && all.length > 20) {
      const p90 = all.slice().sort((a, b) => a - b)[Math.floor(all.length * 0.9)];
      if (p90 > 0 && hi > 5 * p90) { peak = hi; hi = p90 * 4; }
    }
    const step = niceStep(hi - lo || 1);
    if (opts.max === undefined) hi = Math.ceil(hi / step) * step || step;
    const X = (i) => L + (n < 2 ? pw : i / (n - 1) * pw);
    const Y = (v) => T + ph - (v - lo) / (hi - lo || 1) * ph;

    let head = '<div class="chart-head"><b>' + esc(title) + (peak !== null ? ' <i class="chart-cut">peaks to ' + esc(opts.tick(peak)) + " cut off</i>" : "") + '</b><span class="chart-now">';
    for (const s of series) {
      const lastV = [...s.values].reverse().find(isNum);
      head += '<span class="sw k-' + s.colour + '"></span>' + (series.length > 1 ? "<i>" + esc(s.label) + " </i>" : "") + esc(lastV === undefined ? "—" : s.fmt(lastV));
    }
    head += "</span></div>";
    if (n < 2) { box.innerHTML = head + '<div class="empty chart-empty">no samples yet</div>'; return; }

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
      if (opts.area && first !== null) area = '<path d="' + d + "L" + last + " " + (T + ph) + "L" + first + " " + (T + ph) + 'Z" class="area k-' + s.colour + '"/>';
      svg += area + '<path class="line k-' + s.colour + '" d="' + d + '"/>';
    }
    box.innerHTML = head + svg + "</svg>";
  }

  function renderCharts() {
    const ring = (H && H.ring) || {};
    const v = (k) => Array.isArray(ring[k]) ? ring[k] : [];
    const toMb = (a) => a.map((x) => isNum(x) ? x / 1e6 : null);
    drawChart($("chart-latency"), "pop latency", [{ label: "latency", values: v("latency_ms"), colour: "sky", fmt: fmtMs }],
      { min: 0, clip: true, tick: (x) => x.toFixed(0) + " ms" });
    drawChart($("chart-loss"), "ping loss", [{ label: "loss", values: v("drop").map((x) => isNum(x) ? x * 100 : null), colour: "bad", fmt: (x) => x.toFixed(0) + " %" }],
      { min: 0, max: 100, area: true, tick: (x) => x.toFixed(0) + " %" });
    const mb = (x) => (x < 1 ? x.toFixed(2) : x < 10 ? x.toFixed(1) : x.toFixed(0)) + " Mb/s";
    drawChart($("chart-throughput"), "throughput", [
      { label: "down", values: toMb(v("down_bps")), colour: "ok", fmt: mb },
      { label: "up", values: toMb(v("up_bps")), colour: "violet", fmt: mb }],
      { min: 0, tick: (x) => (x < 10 && x % 1 ? x.toFixed(1) : x.toFixed(0)) + " Mb/s" });
    drawChart($("chart-power"), "dish power", [{ label: "power", values: v("power_w"), colour: "warn", fmt: fmtW }],
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
      (recent.length ? recent.length + " in the last 15 min (" + fmtDur(down) + " without internet)" : "none in the last 15 min") + " · " + all.length + " on record";
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
      const rate = node("td", "clients-rate", isNum(rx) ? rx.toFixed(0) + " Mb/s" : "—");
      if (isNum(tx)) rate.append(node("small", null, "send " + tx.toFixed(0) + " Mb/s"));
      tr.append(name, node("td", null, BANDS[c.iface] || (c.iface ? words(c.iface) : "—")), sig, rate,
        node("td", null, isNum(c.associated_time_s) ? fmtDur(c.associated_time_s) : "—"));
      return tr;
    }));
  }

  // ---- ping test ------------------------------------------------------------------------------------------------

  // router.ping = {results: {address: {latencyMs|null, dropRate, target: {service, location, address}}}}, filled only
  // when the ping control runs; router.ping_age_s says how long ago
  let showAllPings = false;
  function renderPing() {
    const card = $("ping-card");
    card.hidden = !S.router;
    if (!S.router) return;
    const results = S.router.ping && S.router.ping.results;
    const age = ageOf(S.router.ping_age_s, got.state);
    $("ping-age").textContent = age === null ? "" : "ran " + fmtAgo(age);
    const tbody = $("ping-rows");
    if (!results || typeof results !== "object" || !Object.keys(results).length) {
      $("ping-summary").textContent = "";
      const tr = node("tr"), td = node("td", "muted", "no results yet: press ping above");
      td.colSpan = 4; tr.append(td); tbody.replaceChildren(tr);
      return;
    }
    // a zero drop rate is left off the wire, so a missing one is 0; no latency means nothing came back
    const rows = Object.keys(results).map((addr) => {
      const r = results[addr] || {}, t = r.target || {};
      const drop = isNum(r.dropRate) ? r.dropRate : 0;
      return { service: t.service || addr, where: t.location || "", addr: t.address || addr,
               ms: isNum(r.latencyMs) ? r.latencyMs : null, drop: drop, dead: !isNum(r.latencyMs) || drop >= 1 };
    });
    rows.sort((a, b) => (a.dead - b.dead) || (a.dead ? (a.service + a.where).localeCompare(b.service + b.where) : a.ms - b.ms));
    const live = rows.filter((r) => !r.dead).map((r) => r.ms).sort((a, b) => a - b);
    $("ping-summary").textContent = rows.length + " hosts · " + live.length + " answered" +
      (live.length ? " · fastest " + fmtMs(live[0]) + " · median " + fmtMs(live[Math.floor(live.length / 2)]) : "");
    const more = $("ping-more");
    more.hidden = rows.length <= PING_SHOWN;
    more.textContent = showAllPings ? "show fewer" : "show all " + rows.length;
    tbody.replaceChildren(...(showAllPings ? rows : rows.slice(0, PING_SHOWN)).map((r) => {
      const tr = node("tr", r.dead ? "dead" : "");
      const svc = node("td", "name", r.service);
      if (r.addr !== r.service) svc.append(node("small", "mono", r.addr));
      tr.append(svc, node("td", null, r.where || "—"), node("td", null, r.ms === null ? "no answer" : fmtMs(r.ms)),
        node("td", r.dead ? "" : r.drop > 0 ? "t-warn" : "", fmtPct(r.drop, 0)));
      return tr;
    }));
  }

  // ---- software ------------------------------------------------------------------------------------------------

  // explain.software = {dish: {version, state, text, progress, restart_at}, router: {version, state, text} | null,
  // update_hour, update_waiting}
  function renderSoftware() {
    const sw = (S.explain && S.explain.software) || null;
    const d = (sw && sw.dish) || {};
    $("sw-dish-version").textContent = d.version || "—";
    const st = $("sw-dish-state");
    st.textContent = d.text || (sw ? "update state not reported" : "not read yet");
    st.className = "sw-state " + (d.state === "FAULTED" ? "t-bad" : sw && sw.update_waiting ? "t-warn" : d.state === "IDLE" ? "t-ok" : "muted");
    const prog = $("sw-dish-progress");
    prog.hidden = !isNum(d.progress);
    if (isNum(d.progress)) { prog.value = d.progress; st.textContent += " · " + Math.round(d.progress * 100) + " %"; }
    const now = (S.localdish && S.localdish.now) || Date.now() / 1000;
    const rs = $("sw-dish-restart");
    rs.hidden = !isNum(d.restart_at);
    if (isNum(d.restart_at)) rs.textContent = (d.restart_at > now ? "restarts on its own at ~" : "was due to restart at ~") + timeOfUnix(d.restart_at);
    const r = sw && sw.router;
    $("sw-router").hidden = !r;
    if (r) {
      $("sw-router-version").textContent = r.version || "—";
      $("sw-router-state").textContent = r.text || "";
      $("sw-router-state").className = "sw-state " + (r.state === "IDLE" ? "t-ok" : r.state ? "t-warn" : "muted");
    }
    $("sw-hour").textContent = sw && isNum(sw.update_hour) ? timeOfMin(sw.update_hour * 60) : "—";
  }

  // ---- controls ------------------------------------------------------------------------------------------------

  // Each control the server lists has a home: restart and install in the software card, clear in the map card,
  // speed test and ping in the tests card, and the three dish settings in their own cards. A control this page does
  // not know is drawn from its params under "other", so one added later still shows.
  // ctl = {name, def, btn, result, reason, busy, dirty, read() → params, lines(params) → [text], sync(def), cancel()}
  const ctls = {};
  let pending = null;

  function post(body) { return { method: "POST", headers: { "X-Localdish": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) }; }

  const isChoice = (p) => Array.isArray(p.choices) || p.type === "enum" || p.type === "choice";
  const isDanger = (c) => c.group === "software" || c.group === "maintenance";
  function labelOf(p) { return p.label || words(p.name); }
  function showParam(p, v) {
    if (v === undefined || v === null) return "—";
    if (p.type === "bool") return v ? "on" : "off";
    if (isChoice(p)) return words(v);
    return String(v);
  }
  const sameParams = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function newCtl(name, btn, notes, more) {
    const ctl = Object.assign({ name: name, def: null, btn: btn, busy: false, dirty: false, hold: null,
                                result: node("div", "result"), reason: node("div", "reason") }, more || {});
    notes.append(ctl.reason, ctl.result);
    return (ctls[name] = ctl);
  }

  // after a setting is sent, the dish's config takes a moment to say so: until it does (15 s at most), keep showing
  // what was sent rather than snapping back to the old value
  function holding(ctl, cur) {
    if (ctl.hold && Date.now() < ctl.hold.until && !sameParams(cur, ctl.hold.params)) return true;
    ctl.hold = null;
    return false;
  }

  function reasonFor(ctl, c) { ctl.reason.textContent = c.available ? "" : (c.label || words(c.name)) + ": " + (c.reason || "not available now"); }

  // a button that sends a control with no params
  function actionCtl(name, host, notes, text, tone) {
    const btn = node("button", "btn small " + tone, text);
    btn.type = "button";
    host.append(btn);
    const ctl = newCtl(name, btn, notes, {
      sync(c) {
        // install is offered only while an update is waiting; the others stay in view, greyed, with the reason
        btn.hidden = name === "install_update" && !c.available;
        btn.disabled = ctl.busy || !c.available;
        btn.title = c.available ? "" : c.reason || "";
        if (btn.hidden) ctl.reason.textContent = ""; else reasonFor(ctl, c);
      },
    });
    btn.addEventListener("click", () => ask(ctl));
    return ctl;
  }

  // ---- sleep schedule ------------------------------------------------------------------------------------------

  // The dish keeps start and duration in UTC minutes; here they are sleep and wake on this computer's clock.
  const DIAL = { size: 320, c: 160, r: 128, band: 38, step: 5 };
  const dayMin = (m) => ((Math.round(m) % 1440) + 1440) % 1440;
  const asleepFor = (d) => dayMin(d.wake - d.sleep);

  function sleepOf(cur) {
    if (!cur || !isNum(cur.start_minutes)) return null;
    const sleep = utcToLocalMin(cur.start_minutes);
    return { enabled: !!cur.enabled, sleep: sleep, wake: dayMin(sleep + (isNum(cur.duration_minutes) ? cur.duration_minutes : 0)) };
  }

  function dialPoint(min, rr) {
    const a = min / 1440 * 2 * Math.PI;
    return [DIAL.c + rr * Math.sin(a), DIAL.c - rr * Math.cos(a)];
  }
  const f1 = (v) => v.toFixed(1);

  function buildDial() {
    const { size, c, r, band } = DIAL;
    let svg = '<svg viewBox="0 0 ' + size + " " + size + '" class="dial-svg">' +
      '<circle class="dial-rim" cx="' + c + '" cy="' + c + '" r="' + (r + band / 2 + 8) + '"/>' +
      '<circle class="dial-track" cx="' + c + '" cy="' + c + '" r="' + r + '" stroke-width="' + band + '"/>' +
      '<path class="dial-arc" stroke-width="' + band + '"/>' +
      '<circle class="dial-inner" cx="' + c + '" cy="' + c + '" r="' + (r - band / 2 - 4) + '"/>';
    // a tick every 15 min, a long one on the hour; ticks under the sleep arc light up
    for (let i = 0; i < 96; i++) {
      const hour = i % 4 === 0, [x1, y1] = dialPoint(i * 15, r - (hour ? 11 : 6)), [x2, y2] = dialPoint(i * 15, r + (hour ? 11 : 6));
      svg += '<line class="dial-tick' + (hour ? " hour" : "") + '" data-min="' + i * 15 + '" x1="' + f1(x1) + '" y1="' + f1(y1) + '" x2="' + f1(x2) + '" y2="' + f1(y2) + '"/>';
    }
    svg += '<line class="dial-now k-bad"/>';
    // 12 AM at the top with a moon, 6 AM right, 12 PM at the bottom with a sun, 6 PM left: as the Starlink app draws it
    const lr = r - band / 2 - 22;
    for (const [m, anchor, dx, dy] of [[0, "middle", 0, 4], [360, "end", 8, 4], [720, "middle", 0, 4], [1080, "start", -8, 4]]) {
      const [x, y] = dialPoint(m, lr);
      svg += '<text class="dial-label" x="' + f1(x + dx) + '" y="' + f1(y + dy) + '" text-anchor="' + anchor + '">' + esc(timeOfMin(m, true)) + "</text>";
    }
    const my = c - lr + 18, sy = c + lr - 22;
    svg += '<path class="dial-icon" d="M' + (c + 4) + " " + (my - 8) + "A8 8 0 1 0 " + (c + 4) + " " + (my + 8) + "A10 10 0 0 1 " + (c + 4) + " " + (my - 8) + 'Z"/>';
    svg += '<g class="dial-icon"><circle cx="' + c + '" cy="' + sy + '" r="4"/>';
    for (let k = 0; k < 8; k++) {
      const a = k * Math.PI / 4;
      svg += '<line x1="' + f1(c + 6.5 * Math.cos(a)) + '" y1="' + f1(sy + 6.5 * Math.sin(a)) + '" x2="' + f1(c + 9 * Math.cos(a)) + '" y2="' + f1(sy + 9 * Math.sin(a)) + '"/>';
    }
    svg += "</g>" +
      '<text class="dial-length" x="' + c + '" y="' + (c - 2) + '" text-anchor="middle"></text>' +
      '<text class="dial-sub" x="' + c + '" y="' + (c + 15) + '" text-anchor="middle">asleep each day</text>';
    for (const which of ["sleep", "wake"]) {
      svg += '<circle class="dial-handle" data-which="' + which + '" r="' + (band / 2 - 2) + '" tabindex="0" role="slider" aria-label="' + which +
        ' time" aria-valuemin="0" aria-valuemax="1435"/>';
    }
    $("sleep-dial").innerHTML = svg + "</svg>";
  }

  function drawDial(d) {
    const box = $("sleep-dial"), svg = box.querySelector("svg");
    if (!svg) return;
    const { r } = DIAL, len = asleepFor(d);
    const [x1, y1] = dialPoint(d.sleep, r), [x2, y2] = dialPoint(d.wake, r);
    svg.querySelector(".dial-arc").setAttribute("d", len ? "M" + f1(x1) + " " + f1(y1) + "A" + r + " " + r + " 0 " + (len > 720 ? 1 : 0) + " 1 " + f1(x2) + " " + f1(y2) : "");
    for (const t of svg.querySelectorAll(".dial-tick")) t.classList.toggle("on", dayMin(+t.dataset.min - d.sleep) < len);
    for (const h of svg.querySelectorAll(".dial-handle")) {
      const m = d[h.dataset.which], [x, y] = dialPoint(m, r);
      h.setAttribute("cx", f1(x)); h.setAttribute("cy", f1(y));
      h.setAttribute("aria-valuenow", String(m));
      h.setAttribute("aria-valuetext", timeOfMin(m));
    }
    const now = new Date(), nowMin = now.getHours() * 60 + now.getMinutes();
    const [nx1, ny1] = dialPoint(nowMin, r - DIAL.band / 2), [nx2, ny2] = dialPoint(nowMin, r + DIAL.band / 2);
    const nl = svg.querySelector(".dial-now");
    nl.setAttribute("x1", f1(nx1)); nl.setAttribute("y1", f1(ny1)); nl.setAttribute("x2", f1(nx2)); nl.setAttribute("y2", f1(ny2));
    svg.querySelector(".dial-length").textContent = len ? fmtDur(len * 60) : "no time";
  }

  function sleepCtl() {
    const ctl = newCtl("power_save", $("sleep-save"), $("sleep-notes"), { draft: null, base: null, drag: null });
    buildDial();

    function show() {
      const d = ctl.draft;
      $("sleep-enabled").checked = !!(d && d.enabled);
      $("sleep-at").textContent = d ? timeOfMin(d.sleep) : "—";
      $("wake-at").textContent = d ? timeOfMin(d.wake) : "—";
      $("sleep-body").classList.toggle("off", !(d && d.enabled));
      if (d) drawDial(d);
      ctl.dirty = !!(d && ctl.base && !sameParams(d, ctl.base));
      $("sleep-undo").hidden = !ctl.dirty;
      const c = ctl.def;
      ctl.btn.disabled = ctl.busy || !d || !c || !c.available || !ctl.dirty;
    }
    function edit(change) {
      if (!ctl.draft) return;
      ctl.draft = Object.assign({}, ctl.draft, change);
      ctl.result.textContent = "";
      show();
    }

    ctl.sync = (c) => {
      const cur = c.current || {};
      const held = holding(ctl, cur);
      ctl.base = held ? sleepOf(ctl.hold.params) : sleepOf(cur);
      if ((!ctl.dirty || !ctl.draft) && !ctl.drag && document.activeElement !== $("sleep-input") && document.activeElement !== $("wake-input")) {
        ctl.draft = ctl.base && Object.assign({}, ctl.base);
      }
      for (const el of [$("sleep-enabled"), $("sleep-at"), $("wake-at")]) el.disabled = ctl.busy || !c.available || !ctl.draft;
      reasonFor(ctl, c);
      show();
    };
    ctl.read = () => {
      const d = ctl.draft, len = asleepFor(d);
      // equal times would be 24 h asleep: refused while the schedule is on; switched off, the length doesn't matter
      if (!len && d.enabled) throw new Error("sleep and wake are the same time: pick a different wake time");
      return { enabled: d.enabled, start_minutes: localToUtcMin(d.sleep), duration_minutes: len || 60 };
    };
    ctl.lines = (p) => p.enabled
      ? ["sleep at " + timeOfMin(ctl.draft.sleep) + ", wake at " + timeOfMin(ctl.draft.wake) + " (this computer's clock)",
         "asleep " + fmtDur(p.duration_minutes * 60) + " every day"]
      : ["sleep schedule off: the dish stays awake"];

    $("sleep-enabled").addEventListener("change", (e) => edit({ enabled: e.target.checked }));
    $("sleep-undo").addEventListener("click", () => { ctl.draft = ctl.base && Object.assign({}, ctl.base); ctl.result.textContent = ""; show(); });
    ctl.btn.addEventListener("click", () => ask(ctl));

    // the big times: a click swaps in a time input
    for (const which of ["sleep", "wake"]) {
      const shown = $(which + "-at"), input = $(which + "-input");
      const close = () => { input.hidden = true; shown.hidden = false; };
      shown.addEventListener("click", () => {
        if (!ctl.draft) return;
        input.value = clockMin(ctl.draft[which]);
        shown.hidden = true; input.hidden = false; input.focus();
      });
      input.addEventListener("change", () => {
        const m = /^(\d{1,2}):(\d{2})/.exec(input.value);
        if (m) edit({ [which]: dayMin(parseInt(m[1], 10) * 60 + parseInt(m[2], 10)) });
      });
      input.addEventListener("blur", close);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); close(); shown.focus(); } });
    }

    // the handles: drag them round the dial in 5-minute steps, or use the arrow keys; a click on the ring moves the
    // nearer handle there
    const svg = $("sleep-dial").querySelector("svg");
    const minAt = (e) => {
      const b = svg.getBoundingClientRect();
      const x = (e.clientX - b.left) / b.width * DIAL.size - DIAL.c, y = (e.clientY - b.top) / b.height * DIAL.size - DIAL.c;
      return { min: dayMin(Math.round(((Math.atan2(x, -y) / (2 * Math.PI)) * 1440) / DIAL.step) * DIAL.step), dist: Math.hypot(x, y) };
    };
    svg.addEventListener("pointerdown", (e) => {
      if (!ctl.draft || !ctl.draft.enabled || ctl.busy || !(ctl.def && ctl.def.available)) return;
      const at = minAt(e);
      let which = e.target.dataset && e.target.dataset.which;
      if (!which) {
        if (Math.abs(at.dist - DIAL.r) > DIAL.band / 2 + 6) return;       // not on the ring
        which = Math.abs(dayMin(at.min - ctl.draft.sleep + 720) - 720) <= Math.abs(dayMin(at.min - ctl.draft.wake + 720) - 720) ? "sleep" : "wake";
      }
      e.preventDefault();
      ctl.drag = which;
      svg.setPointerCapture(e.pointerId);
      svg.querySelector('[data-which="' + which + '"]').focus();
      edit({ [which]: at.min });
    });
    svg.addEventListener("pointermove", (e) => { if (ctl.drag) { const m = minAt(e).min; if (m !== ctl.draft[ctl.drag]) edit({ [ctl.drag]: m }); } });
    const drop = () => { ctl.drag = null; };
    svg.addEventListener("pointerup", drop);
    svg.addEventListener("pointercancel", drop);
    svg.addEventListener("keydown", (e) => {
      const which = e.target.dataset && e.target.dataset.which;
      const by = { ArrowUp: 5, ArrowRight: 5, ArrowDown: -5, ArrowLeft: -5, PageUp: 60, PageDown: -60 }[e.key];
      if (!which || !by || !ctl.draft || !ctl.draft.enabled || $("sleep-enabled").disabled) return;
      e.preventDefault();
      edit({ [which]: dayMin(Math.round((ctl.draft[which] + by) / DIAL.step) * DIAL.step) });
    });
    return ctl;
  }

  // ---- snow melt and location ------------------------------------------------------------------------------------

  // the Starlink app's three choices, in its words
  const SNOW = {
    AUTO: ["automatic", "melts snow when the dish senses it"],
    ALWAYS_ON: ["pre-heat", "keeps the dish warm so snow can't settle; uses the most power"],
    ALWAYS_OFF: ["off", "never heats: snow can pile up and block the signal"],
  };
  const snowName = (v) => (SNOW[v] ? SNOW[v][0] : words(v));

  function snowCtl() {
    const ctl = newCtl("snow_melt", $("snow-save"), $("snow-notes"), { draft: null, base: null });
    const box = $("snow-modes");
    function show() {
      for (const inp of box.querySelectorAll("input")) inp.checked = inp.value === ctl.draft;
      ctl.dirty = !!ctl.draft && ctl.draft !== ctl.base;
      $("snow-hint").textContent = ctl.draft ? (SNOW[ctl.draft] ? SNOW[ctl.draft][1] : "") +
        (ctl.dirty && ctl.base ? " · now " + snowName(ctl.base) : "") : "not read yet";
      ctl.btn.disabled = ctl.busy || !(ctl.def && ctl.def.available) || !ctl.dirty;
    }
    ctl.sync = (c) => {
      if (!box.children.length) {
        const p = (c.params || [])[0] || {};
        for (const v of p.choices || Object.keys(SNOW)) {
          const lab = node("label", "seg"), inp = node("input");
          inp.type = "radio"; inp.name = "snow-mode"; inp.value = v;
          inp.addEventListener("change", () => { ctl.draft = v; ctl.result.textContent = ""; show(); });
          lab.append(inp, node("span", null, snowName(v)));
          box.append(lab);
        }
      }
      const cur = c.current || {};
      ctl.base = holding(ctl, cur) ? ctl.hold.params.mode : cur.mode || null;
      if (!ctl.dirty) ctl.draft = ctl.base;
      for (const inp of box.querySelectorAll("input")) inp.disabled = ctl.busy || !c.available;
      reasonFor(ctl, c);
      show();
    };
    ctl.read = () => ({ mode: ctl.draft });
    ctl.lines = (p) => ["snow melt: " + snowName(p.mode) + (ctl.base ? " (now " + snowName(ctl.base) + ")" : "")];
    ctl.btn.addEventListener("click", () => ask(ctl));
    return ctl;
  }

  // flipping the switch asks at once; cancelling puts it back
  function shareCtl() {
    const box = $("share-location");
    const ctl = newCtl("share_location", box, $("share-notes"));
    ctl.sync = (c) => {
      const cur = c.current || {};
      const on = holding(ctl, cur) ? ctl.hold.params.share : cur.share;
      if (!ctl.dirty) box.checked = !!on;
      box.disabled = ctl.busy || !c.available || on === undefined;
      reasonFor(ctl, c);
    };
    ctl.read = () => ({ share: box.checked });
    ctl.lines = (p) => [p.share ? "any device on the dish's wifi can then ask for its exact location" : "devices on the dish's wifi can no longer read its location"];
    ctl.cancel = () => { ctl.dirty = false; if (S) renderControls(); };
    ctl.done = () => { ctl.dirty = false; };
    box.addEventListener("change", () => { ctl.dirty = true; ask(ctl); });
    return ctl;
  }

  // ---- other controls, drawn from their params -------------------------------------------------------------------

  function paramInput(ctl, p) {
    const wrap = node("label", "param");
    let input;
    if (p.type === "bool") {
      wrap.className = "param toggle";
      input = node("input"); input.type = "checkbox";
      wrap.append(input, node("span", "track"), node("span", null, labelOf(p)));
    } else {
      wrap.append(node("span", null, labelOf(p)));
      if (isChoice(p)) {
        input = node("select");
        for (const c of p.choices || []) { const o = node("option", null, words(c)); o.value = c; input.append(o); }
      } else {
        input = node("input"); input.type = "number"; input.step = 1;
        if (isNum(p.min)) input.min = p.min;
        if (isNum(p.max)) input.max = p.max;
      }
      wrap.append(input);
    }
    input.addEventListener("input", () => { ctl.dirty = true; });
    input.addEventListener("change", () => { ctl.dirty = true; });
    ctl.inputs[p.name] = { p: p, el: input };
    return wrap;
  }

  function genericCtl(c) {
    let group = $("controls").querySelector('[data-group="' + CSS.escape(c.group) + '"]');
    if (!group) {
      group = node("div", "control-group");
      group.dataset.group = c.group;
      group.append(node("h3", null, words(c.group)));
      $("controls").append(group);
    }
    const root = node("div", "control");
    root.dataset.control = c.name;
    const title = node("div", "control-title");
    title.append(node("span", null, c.label || words(c.name)));
    const now = node("div", "control-now");
    root.append(title, now);
    const btn = node("button", "btn " + (isDanger(c) ? "danger" : "primary"),
      (c.params || []).length ? "apply" : c.group === "tests" ? "run" : (c.label || words(c.name)).split(" ")[0]);
    btn.type = "button";
    const notes = node("div", "notes");
    const ctl = newCtl(c.name, btn, notes, { inputs: {}, root: root });
    for (const p of c.params || []) root.append(paramInput(ctl, p));
    root.append(btn, notes);
    group.append(root);
    btn.addEventListener("click", () => ask(ctl));

    ctl.sync = (d) => {
      // stow only exists on dishes with motors: on one the dish says has none, it is left out rather than greyed
      root.hidden = d.name === "stow" && !d.available && hasNoMotors();
      const cur = d.current || {}, params = d.params || [];
      const known = params.filter((p) => cur[p.name] !== undefined);
      now.textContent = known.length ? "now: " + known.map((p) => showParam(p, cur[p.name])).join(" · ") : "";
      if (!ctl.dirty) for (const p of known) {
        const el = ctl.inputs[p.name].el;
        if (p.type === "bool") el.checked = !!cur[p.name]; else el.value = String(cur[p.name]);
      }
      btn.disabled = ctl.busy || !d.available;
      for (const k in ctl.inputs) ctl.inputs[k].el.disabled = ctl.busy || !d.available;
      ctl.reason.textContent = d.available ? "" : d.reason || "not available now";
    };
    ctl.read = () => {
      const out = {};
      for (const k in ctl.inputs) {
        const { p, el } = ctl.inputs[k];
        if (p.type === "bool") out[k] = el.checked;
        else if (isChoice(p)) out[k] = el.value;
        else {
          const n = Number(el.value);
          if (el.value === "" || !Number.isInteger(n)) throw new Error(labelOf(p) + ": a whole number, please");
          if ((isNum(p.min) && n < p.min) || (isNum(p.max) && n > p.max)) throw new Error(labelOf(p) + ": between " + p.min + " and " + p.max);
          out[k] = n;
        }
      }
      return out;
    };
    ctl.lines = (p) => (c.params || []).map((q) => labelOf(q) + ": " + showParam(q, p[q.name]));
    return ctl;
  }

  function hasNoMotors() {
    const st = (S.dish && S.dish.status) || {};
    const v = st.has_actuators || (st.alignment_stats && st.alignment_stats.has_actuators);
    return v === "HAS_ACTUATORS_NO";
  }

  // where each known control lives; made in this order the first time the server lists it
  const PLACES = {
    install_update: () => actionCtl("install_update", $("software-actions"), $("software-notes"), "install update now", "primary"),
    restart: () => actionCtl("restart", $("software-actions"), $("software-notes"), "restart", "danger"),
    clear_obstructions: () => actionCtl("clear_obstructions", $("map-actions"), $("map-notes"), "clear", "danger"),
    speedtest: () => actionCtl("speedtest", $("tests-actions"), $("tests-notes"), "speed test", "primary"),
    ping: () => actionCtl("ping", $("tests-actions"), $("tests-notes"), "ping", "primary"),
    power_save: sleepCtl,
    snow_melt: snowCtl,
    share_location: shareCtl,
  };

  function renderControls() {
    const list = Array.isArray(S.controls) ? S.controls : [];
    const listed = {};
    for (const c of list) listed[c.name] = c;
    for (const name in PLACES) if (listed[name] && !ctls[name]) PLACES[name]();
    for (const c of list) {
      const ctl = ctls[c.name] || genericCtl(c);
      ctl.def = c;
      if (!pending || pending.ctl !== ctl) ctl.sync(c);
    }
    $("sleep-card").hidden = !listed.power_save;
    $("snow-block").hidden = !listed.snow_melt;
    $("share-block").hidden = !listed.share_location;
    $("settings-card").hidden = !listed.snow_melt;
    for (const g of $("controls").children) g.hidden = ![...g.querySelectorAll(".control")].some((e) => !e.hidden);
    $("other-card").hidden = ![...$("controls").children].some((g) => !g.hidden);
    renderSpeedtest();
  }

  // running.speedtest = {started, status: {status: {running, up: {throughputs_mbps[], err}, down: {…}}}, done, error}
  function renderSpeedtest() {
    const run = S.running && S.running.speedtest;
    $("speedtest").hidden = !run;
    if (!run) return;
    const st = inner(run.status, "status") || {};
    const errs = [];
    for (const dir of ["down", "up"]) {
      const d = st[dir] || {}, t = Array.isArray(d.throughputs_mbps) ? d.throughputs_mbps.filter(isNum) : [];
      $("speedtest-" + dir).textContent = t.length ? t[t.length - 1].toFixed(1) : "—";
      if (d.err) errs.push(dir + ": " + words(d.err));
    }
    if (run.error) errs.push(run.error);
    const secs = isNum(S.localdish && S.localdish.now) && isNum(run.started) ? S.localdish.now - run.started : null;
    const note = $("speedtest-note");
    note.className = "summary " + (errs.length ? "t-bad" : "");
    note.textContent = (run.done ? "ran at " + timeOfUnix(run.started) : "running" + (secs !== null ? ", " + fmtDur(secs) : "") + "…") +
      (errs.length ? " · " + errs.join(" · ") : "") + " · measured by the router";
    $("speedtest").classList.toggle("running", !run.done);
  }

  function ask(ctl) {
    let params;
    try { params = ctl.read ? ctl.read() : {}; } catch (e) {
      ctl.result.className = "result t-bad"; ctl.result.textContent = e.message;
      if (ctl.cancel) ctl.cancel();
      return;
    }
    pending = { ctl: ctl, params: params };
    const c = ctl.def;
    $("confirm-title").textContent = c.label || words(c.name);
    $("confirm-text").textContent = c.confirm || "send this to the " + (c.target || "device") + "?";
    $("confirm-params").replaceChildren(...(ctl.lines ? ctl.lines(params) : []).map((t) => node("li", null, t)));
    $("confirm-ok").className = "btn " + (isDanger(c) ? "danger" : "primary");
    $("confirm").returnValue = "";
    $("confirm").showModal();
  }

  async function send(ctl, params) {
    ctl.busy = true;
    ctl.btn.disabled = true; ctl.btn.classList.add("busy");
    ctl.result.className = "result muted"; ctl.result.textContent = "sending…";
    let text, tone, ok = false;
    try {
      const r = await request("/api/control/" + encodeURIComponent(ctl.name), post({ params: params }), CONTROL_TIMEOUT_MS);
      ok = !!(r.ok && r.data && r.data.ok);
      if (ok) { text = r.data.text || "done"; tone = "t-ok"; ctl.dirty = false; ctl.hold = { until: Date.now() + 15000, params: params }; }
      else { text = (r.data && r.data.error) || "http " + r.status; tone = "t-bad"; }
    } catch (e) { text = e.message; tone = "t-bad"; }
    ctl.busy = false;
    ctl.btn.classList.remove("busy");
    if (ctl.done) ctl.done(ok);
    ctl.result.className = "result " + tone;
    ctl.result.textContent = clock(Date.now() / 1000) + " · " + text;
    if (S) renderControls();
    loops.state.now();
  }

  $("confirm").addEventListener("close", () => {
    const p = pending;
    pending = null;
    if (p && $("confirm").returnValue === "ok") send(p.ctl, p.params);
    else if (p && p.ctl.cancel) p.ctl.cancel();
    else if (S) renderControls();
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
    for (const [card, el] of [[$("graph-card-1"), $("history-age-1")], [$("graph-card-2"), $("history-age-2")], [$("outages-card"), $("outages-age")]]) {
      markAge(card, el, hAge, STALE_S.history, "history");
    }
    markAge($("map-card"), $("map-age"), ageOf(O && O.age_s, got.obstruction), STALE_S.obstruction, "the map");
    const dAge = ageOf(S.localdish && S.localdish.dish && S.localdish.dish.age_s, got.state);
    markAge($("aim-card"), null, dAge, STALE_S.dish);
    markAge($("software-card"), null, dAge, STALE_S.dish);
    markAge($("alerts-card"), null, dAge, STALE_S.dish);
    if (H && H.error) $("history-age-1").textContent = $("history-age-2").textContent = $("outages-age").textContent = "history: " + H.error;
    if (O && O.error) $("map-age").textContent = "map: " + O.error;
    renderLive();
  }

  function onState(data) {
    S = data;
    renderHeader();
    renderAim();
    renderAlerts();
    renderSoftware();
    renderClients();
    renderPing();
    renderControls();
    renderEvents();
    renderFacts();
    renderRaw();
  }
  function onHistory(data) { H = data; renderCharts(); renderOutages(); renderRaw(); }
  let mapRetry = null;
  function onObstruction(data) {
    O = data; renderMap(); renderRaw();
    // the server fetches the map once someone is watching; until it has one, ask again soon instead of in a minute
    const empty = !(data && data.map && data.map.num_rows);
    if (empty && !mapRetry) mapRetry = setTimeout(() => { mapRetry = null; loops.obstruction.now(); }, 5000);
  }

  const loops = {
    state: poller("state", "/api/state", onState),
    history: poller("history", "/api/history", onHistory),
    obstruction: poller("obstruction", "/api/obstruction", onObstruction),
  };

  document.addEventListener("visibilitychange", () => {
    for (const k in loops) document.visibilityState === "visible" ? loops[k].wake() : loops[k].sleep();
  });
  $("outages-more").addEventListener("click", () => { showAllOutages = !showAllOutages; renderOutages(); });
  $("ping-more").addEventListener("click", () => { showAllPings = !showAllPings; if (S) renderPing(); });
  $("map-refresh").addEventListener("click", refreshMap);
  $("raw").addEventListener("toggle", () => { rawText = ""; renderRaw(); });

  // redraw what is drawn to size when the size or the colour scheme changes
  let redraw = null;
  const again = () => { clearTimeout(redraw); redraw = setTimeout(() => { if (H) renderCharts(); if (O) renderMap(); }, 120); };
  window.addEventListener("resize", again);
  if (window.matchMedia) window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", again);

  for (const k in loops) loops[k].now();
})();
