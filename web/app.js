/* app.js: Gjallarhorn display logic.
 *
 * SECURITY MODEL (review 2026-10-24, updated v1.1):
 *  - The page makes ZERO external network requests. CSP: default-src 'none',
 *    connect-src 'self' (live mode polls the same-origin events.json only),
 *    img-src 'none'. No <a>/<img> is ever created from feed data; no URL
 *    field is even loaded into the artifact.
 *  - ALL feed strings render via textContent/createTextNode only.
 *    Never innerHTML, never href/src/style/on* assignment.
 *  - events.js / events.json are produced by fetcher.py: type-gated,
 *    control/bidi/zero-width stripped, NFKC-normalized, length-capped,
 *    ascii-escaped JSON.
 *  - Unknown-country events go to the Unlocated rail, never to 0,0.
 *
 * Map geometry: dot matrix derived by sampling NASA Blue Marble
 * (public domain) imagery. Credit: Courtesy NASA.
 */

(function () {
  "use strict";

  var EVENTS = (window.EVENTS || []).slice();
  var CENTROIDS = window.CENTROIDS || {};
  var NAMES = window.COUNTRY_NAMES || {};
  // hasOwnProperty guard: feed-authored ev.c ("constructor", "__proto__",
  // ...) must not hit inherited Object.prototype members (audit F1/F2 class)
  function countryName(c) {
    return c && Object.prototype.hasOwnProperty.call(NAMES, c) ? NAMES[c] : null;
  }

  // ---- palette (matches the reference screenshot) ----
  var BG = "#0b1016", DOT = "#2c3e50", CYAN = "#00e5ff", RED = "#ff3d00",
      TEXT = "#c7d3dc", DIM = "#5b6b78";

  // ---- significance tiers (data-integrity review) ----
  // Set/Map (not object literals): hostile group_name/activity values like
  // "__proto__" must not hit inherited Object.prototype keys (audit F1/F2)
  var TIER_A_SECTORS = new Set([
    "Healthcare", "Energy & Utilities", "Financial Services",
    "Government & Defense", "Transportation"
  ]);
  var BURST_WINDOW_MS = 60 * 60 * 1000; // rolling 1h per group (real time)

  var canvas = document.getElementById("map");
  var ctx = canvas.getContext("2d");
  var cards = document.getElementById("cards");
  var feed = document.getElementById("feed");
  var unlocated = document.getElementById("unlocated-count");
  var counterEl = document.getElementById("count");

  var W = 960, H = 480;           // map coordinate space
  var scale = 1, ox = 0, oy = 0;  // canvas transform
  var pings = [];                 // active animations
  var decay = [];                 // settled pings persisting as dim markers
  var shown = 0;
  var FEED_CAP = 9;               // raised to 200 in live mode

  // ---- unpack dot matrix ----
  var dots = [];
  try {
    var bin = atob(window.DOTS_B64 || "");
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    var words = new Uint16Array(bytes.buffer);
    for (var j = 0; j < words.length; j++) {
      var v = words[j];
      dots.push([(Math.floor(v / 96) * 5) + 2, ((v % 96) * 5) + 2]); // cell center px
    }
  } catch (e) { dots = []; }

  function project(lat, lon) {
    return [(lon + 180) / 360 * W, (90 - lat) / 180 * H];
  }

  // static base map rendered ONCE per resize to an offscreen canvas, then
  // blitted each frame, which keeps per-frame cost ~zero (Pi Zero 2 friendly)
  var baseCanvas = document.createElement("canvas");

  function resize() {
    var box = canvas.parentElement.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    canvas.width = box.width * dpr;
    canvas.height = box.height * dpr;
    canvas.style.width = box.width + "px";
    canvas.style.height = box.height + "px";
    var s = Math.min(box.width / W, box.height / H);
    scale = s * dpr;
    ox = (box.width * dpr - W * scale) / 2;
    oy = (box.height * dpr - H * scale) / 2;
    baseCanvas.width = canvas.width;
    baseCanvas.height = canvas.height;
    decayCanvas.width = canvas.width;
    decayCanvas.height = canvas.height;
    decayDirty = true;
    var bctx = baseCanvas.getContext("2d");
    bctx.fillStyle = BG;
    bctx.fillRect(0, 0, baseCanvas.width, baseCanvas.height);
    bctx.setTransform(scale, 0, 0, scale, ox, oy);
    bctx.fillStyle = DOT;
    for (var i = 0; i < dots.length; i++) {
      bctx.fillRect(dots[i][0] - 1, dots[i][1] - 1, 2.2, 2.2);
    }
  }
  window.addEventListener("resize", resize);

  function drawBase() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(baseCanvas, 0, 0);
  }

  // ---- pings ----
  var PING_MS = 4200;
  function spawnPing(ev, tier, xy, extra) {
    pings.push({ x: xy[0], y: xy[1], t0: performance.now(), tier: tier, k: ev.k });
    shown++;
    counterEl.textContent = String(shown);
    spawnCard(ev, tier, xy, extra);
    addFeedRow(ev, tier);
  }

  function spawnCard(ev, tier, xy, extra) {
    var card = document.createElement("div");
    card.className = "card tier" + tier;
    card.addEventListener("click", function (e) { e.stopPropagation(); openDetail(ev.k); });
    var l1 = document.createElement("div"); l1.className = "apt";
    l1.textContent = "APT: " + ev.g + (extra ? " \u00d7" + extra : "");
    var l2 = document.createElement("div"); l2.className = "vic";
    l2.textContent = ev.t;
    var l3 = document.createElement("div"); l3.className = "loc";
    l3.textContent = countryName(ev.c) || "location unknown";
    card.appendChild(l1); card.appendChild(l2); card.appendChild(l3);
    // position in map-space, converted at layout time via one transform read
    var box = canvas.getBoundingClientRect();
    var px = box.left + ox / (window.devicePixelRatio || 1) + xy[0] * scale / (window.devicePixelRatio || 1);
    var py = box.top + oy / (window.devicePixelRatio || 1) + xy[1] * scale / (window.devicePixelRatio || 1);
    card.style.left = Math.min(Math.max(px + 14, box.left), box.right - 240) + "px";
    card.style.top = Math.max(py - 44, box.top + 6) + "px";
    cards.appendChild(card);
    setTimeout(function () { card.classList.add("fade"); }, PING_MS - 700);
    setTimeout(function () { if (card.parentNode) card.parentNode.removeChild(card); }, PING_MS);
  }

  function addFeedRow(ev, tier) {
    var row = document.createElement("div");
    row.className = "frow tier" + tier;
    var g = document.createElement("span"); g.className = "fg"; g.textContent = ev.g;
    var t = document.createElement("span"); t.className = "ft"; t.textContent = ev.t;
    var c = document.createElement("span"); c.className = "fc";
    c.textContent = " " + (countryName(ev.c) || "??");
    row.appendChild(g); row.appendChild(t); row.appendChild(c);
    row.addEventListener("click", function (e) { e.stopPropagation(); openDetail(ev.k); });
    feed.insertBefore(row, feed.firstChild);
    while (feed.children.length > FEED_CAP) feed.removeChild(feed.lastChild);
  }

  function drawPings(now) {
    ctx.setTransform(scale, 0, 0, scale, ox, oy);
    for (var i = pings.length - 1; i >= 0; i--) {
      var p = pings[i];
      var age = (now - p.t0) / PING_MS;
      if (age >= 1) {
        decay.push({ x: p.x, y: p.y, tier: p.tier, t0: p.t0 });
        if (decay.length > 800) decay.shift();
        decayDirty = true;
        pings.splice(i, 1);
        continue;
      }
      var col = p.tier === 1 ? RED : CYAN;
      var base = p.tier === 1 ? 3.2 : 2.2;
      // core glow
      ctx.globalAlpha = 1 - age * 0.6;
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.arc(p.x, p.y, base * (1 + age * 0.4), 0, 6.2832);
      ctx.fill();
      // ripple rings
      for (var r = 0; r < 3; r++) {
        var phase = (age + r * 0.28) % 1;
        ctx.globalAlpha = (1 - phase) * 0.55;
        ctx.strokeStyle = col;
        ctx.lineWidth = p.tier === 1 ? 1.6 : 1.0;
        ctx.beginPath();
        ctx.arc(p.x, p.y, base + phase * (p.tier === 1 ? 26 : 14), 0, 6.2832);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
  }

  // settled events persist as small dim markers, fading across ~2h of wall
  // time to a visible floor: the map accumulates a heat-trail of claims.
  // Rendered to an offscreen layer refreshed ~1x/s (alpha drifts on a
  // minutes scale), not per frame: 800 arcs/frame would pin a Pi Zero 2
  // core forever; one drawImage per frame costs ~nothing.
  var decayCanvas = document.createElement("canvas");
  var decayDirty = true;
  var decayLast = 0;

  function renderDecayLayer(now) {
    var dctx = decayCanvas.getContext("2d");
    dctx.setTransform(1, 0, 0, 1, 0, 0);
    dctx.clearRect(0, 0, decayCanvas.width, decayCanvas.height);
    dctx.setTransform(scale, 0, 0, scale, ox, oy);
    for (var i = 0; i < decay.length; i++) {
      var d = decay[i];
      var mins = (now - d.t0) / 60000;
      var fade = Math.max(0.22, 1 - mins / 120);
      dctx.globalAlpha = fade * (d.tier === 1 ? 0.85 : d.tier === 2 ? 0.55 : 0.30);
      dctx.fillStyle = d.tier === 1 ? RED : d.tier === 2 ? CYAN : DIM;
      dctx.beginPath();
      dctx.arc(d.x, d.y, d.tier === 1 ? 1.8 : 1.4, 0, 6.2832);
      dctx.fill();
    }
    dctx.globalAlpha = 1;
    decayDirty = false;
    decayLast = now;
  }

  function frame(now) {
    if (decay.length && (decayDirty || now - decayLast > 1000)) renderDecayLayer(now);
    drawBase();
    ctx.drawImage(decayCanvas, 0, 0);
    drawPings(now);
    requestAnimationFrame(frame);
  }

  // ---- event scheduling ----
  function classify(ev) {
    if (TIER_A_SECTORS.has(ev.a)) return 1;
    if (ev.d && ev.d.length >= 250 && ev.c) return 2;
    return 3; // dimmed ambient
  }

  var burstState = new Map(); // group -> array of timestamps (performance.now)
  function burstCount(group, now) {
    var a = burstState.get(group) || [];
    a.push(now);
    burstState.set(group, a);
    // display-time window = real window / compress (audit F3: was *, i.e. 500x too long)
    while (a.length && now - a[0] > BURST_WINDOW_MS / TIMELINE_COMPRESS) a.shift();
    return a.length;
  }

  var TIMELINE_COMPRESS = 1;     // live: real time; replay: set per sweep
  var REPLAY_MS = 240000;        // replay sweep duration (4 min), then loop

  function spawnEvent(ev) {
    var now = performance.now();
    var tier = classify(ev);
    var b = burstCount(ev.g, now);
    var extra = !filling && b >= 3 ? b : 0; // during a paced catch-up fill, arrival time is not burst signal
    if (ev.c && Object.prototype.hasOwnProperty.call(CENTROIDS, ev.c)) {
      var xy = project(CENTROIDS[ev.c][0], CENTROIDS[ev.c][1]);
      // small deterministic jitter so same-country events don't stack
      var h = 0; for (var q = 0; q < ev.k.length; q += 2) h = (h * 31 + ev.k.charCodeAt(q)) % 997;
      xy[0] += (h % 40) - 20; xy[1] += ((h / 40 | 0) % 24) - 12;
      spawnPing(ev, tier, xy, extra);
    } else {
      unlocated.textContent = String((parseInt(unlocated.textContent, 10) || 0) + 1);
      addFeedRow(ev, tier); // visible + countable, never Null Island
    }
  }

  // replay: the whole snapshot as one compressed timeline, then loop.
  // This is the demo mode: it always has something moving.
  function schedule() {
    if (!EVENTS.length) return; // Math.max.apply(null, []) is -Infinity; would throw below
    shown = 0; counterEl.textContent = "0"; unlocated.textContent = "0"; burstState = new Map(); decay.length = 0; // reset per sweep (audit F4); live mode never clears the trail
    var times = EVENTS.map(function (e) {
      return Date.parse((e.di || e.pu).replace(" ", "T")) || 0;
    });
    var t0 = Math.min.apply(null, times);
    var tN = Math.max.apply(null, times);
    stampDataAge(tN);
    var span = Math.max(tN - t0, 1);
    var start = performance.now();
    // real compression ratio for this dataset: keeps the burst window an
    // honest 1h of event time regardless of span (was hardcoded 500)
    TIMELINE_COMPRESS = span / (REPLAY_MS * 0.8);
    EVENTS.forEach(function (ev, idx) {
      var at = start + ((times[idx] - t0) / span) * (REPLAY_MS * 0.8);
      setTimeout(function () { spawnEvent(ev); }, at - start);
    });
    setTimeout(schedule, REPLAY_MS); // loop the sweep
  }

  // live: poll the same-origin events.json, spawn only genuinely new
  // events, never loop. This is the wall default.
  var seen = new Set();
  var POLL_MS = 60000;
  function stampDataAge(tN) {
    var dao = document.getElementById("data-as-of");
    if (dao && tN) dao.textContent = "DATA " + new Date(tN).toISOString().slice(0, 16).replace("T", " ") + "Z";
  }
  var filling = false; // true during a large paced catch-up fill
  function ingest(arr) {
    EVENTS = arr;
    var tN = 0;
    var fresh = [];
    for (var i = 0; i < arr.length; i++) {
      BY_KEY.set(arr[i].k, arr[i]);
      if (!seen.has(arr[i].k)) { seen.add(arr[i].k); fresh.push(arr[i]); }
      var t = Date.parse((arr[i].di || arr[i].pu || "").replace(" ", "T")) || 0;
      if (t > tN) tN = t;
    }
    stampDataAge(tN);
    // prune lookup state to the current snapshot window: kiosk uptime is
    // weeks, and dead entries (rotated-out events) would grow without bound
    var win = new Set();
    for (var w = 0; w < arr.length; w++) win.add(arr[w].k);
    seen.forEach(function (k) { if (!win.has(k)) seen.delete(k); });
    BY_KEY.forEach(function (v, k) { if (!win.has(k)) BY_KEY.delete(k); });
    burstState.forEach(function (a, g) { if (!a.length) burstState.delete(g); });
    // pace by concurrency, not total duration: at most ~100 simultaneous
    // pings regardless of snapshot size (Pi-friendly cold load). Small
    // catch-up batches drop in over ~250ms steps.
    filling = fresh.length > 10;
    var step = fresh.length <= 10 ? 250 : Math.max(30, Math.floor(PING_MS / 100));
    for (var n = 0; n < fresh.length; n++) {
      (function (ev, delay, last) {
        setTimeout(function () {
          spawnEvent(ev);
          if (last) filling = false;
        }, delay);
      })(fresh[n], n * step, n === fresh.length - 1);
    }
  }
  function liveTick() {
    fetch("events.json", { cache: "no-cache" }).then(function (r) { return r.json(); })
      .then(ingest)
      .catch(function () { /* transient: keep last state, retry next tick */ });
  }

  // ---- mode selection ----
  // ?replay / ?live force a mode. Default: synthetic demo data replays
  // (it is a demo), real data runs live (wall behavior). If live cannot
  // fetch (file://, missing artifact, old CSP), fall back to replay.
  var MODE = (function () {
    var q = (location.search || "").toLowerCase();
    if (q.indexOf("replay") >= 0) return "replay";
    if (q.indexOf("live") >= 0) return "live";
    var demo = EVENTS.length > 0 && EVENTS.every(function (e) {
      return e.s && e.s.length === 1 && e.s[0] === "demo";
    });
    return demo ? "replay" : "live";
  })();


  // ---- detail overlay: shared drill-down for ping / card / feed-row clicks.
  // Same security rules as the rest of the page: textContent only, no
  // innerHTML, no attributes derived from feed data. Attacker-authored
  // description text renders inert by construction.
  var DETAIL_IDLE_MS = 30000; // kiosk auto-dismiss; interaction inside re-arms
  var detail = document.getElementById("detail");
  var detailTimer = 0;
  var BY_KEY = new Map();
  for (var bi = 0; bi < EVENTS.length; bi++) BY_KEY.set(EVENTS[bi].k, EVENTS[bi]);

  function armDetailTimer() {
    clearTimeout(detailTimer);
    detailTimer = setTimeout(closeDetail, DETAIL_IDLE_MS);
  }
  function closeDetail() {
    clearTimeout(detailTimer);
    detail.classList.remove("open");
  }
  function metaLine(label, value) {
    var d = document.createElement("div");
    d.className = "d-meta";
    var s = document.createElement("span"); s.className = "d-k"; s.textContent = label;
    d.appendChild(s);
    d.appendChild(document.createTextNode(" " + value));
    return d;
  }
  function openDetail(k) {
    var ev = BY_KEY.get(k);
    if (!ev) return;
    while (detail.firstChild) detail.removeChild(detail.firstChild);
    var tier = classify(ev);
    var head = document.createElement("div"); head.className = "d-head tier" + tier;
    var badge = document.createElement("span"); badge.className = "d-badge";
    badge.textContent = "TIER " + tier;
    var close = document.createElement("span"); close.className = "d-close";
    close.textContent = "[ esc ]";
    close.addEventListener("click", function (e) { e.stopPropagation(); closeDetail(); });
    head.appendChild(badge); head.appendChild(close);
    var title = document.createElement("div"); title.className = "d-title"; title.textContent = ev.t;
    detail.appendChild(head); detail.appendChild(title);
    detail.appendChild(metaLine("group:", ev.g));
    detail.appendChild(metaLine("sector:", ev.a || "unknown"));
    detail.appendChild(metaLine("country:", countryName(ev.c) || "unknown"));
    var when = ev.di || ev.pu || "";
    var wd = when ? new Date(when.replace(" ", "T")) : NaN;
    detail.appendChild(metaLine("reported:", isFinite(wd) ? wd.toISOString().slice(0, 16).replace("T", " ") + "Z" : (when || "unknown")));
    if (ev.s && ev.s.length) {
      detail.appendChild(metaLine("sources:", ev.s.join(" + ") + (ev.s.length > 1 ? " \u00b7 corroborated" : "")));
    }
    if (ev.d) {
      var lbl = document.createElement("div"); lbl.className = "d-meta";
      var ls = document.createElement("span"); ls.className = "d-k";
      ls.textContent = "post text:";
      lbl.appendChild(ls);
      detail.appendChild(lbl);
      var p = document.createElement("p"); p.className = "d-desc"; p.textContent = ev.d;
      detail.appendChild(p);
    }
    var foot = document.createElement("div"); foot.className = "d-foot";
    foot.textContent = "claim by " + ev.g + " via leak-site aggregation (unverified)";
    detail.appendChild(foot);
    detail.classList.add("open");
    detail.scrollTop = 0;
    armDetailTimer();
  }

  // any interaction with the open overlay keeps it alive (reading long
  // descriptions must not be cut off by the idle timer)
  detail.addEventListener("pointerdown", armDetailTimer);
  detail.addEventListener("wheel", armDetailTimer, { passive: true });
  window.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDetail();
  });
  // click anywhere else closes (triggers call stopPropagation first)
  document.addEventListener("click", function (e) {
    if (detail.classList.contains("open") && !detail.contains(e.target)) closeDetail();
  });
  // click on the map itself: nearest live ping within tolerance (map px)
  canvas.addEventListener("click", function (e) {
    e.stopPropagation();
    var box = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var x = ((e.clientX - box.left) * dpr - ox) / scale;
    var y = ((e.clientY - box.top) * dpr - oy) / scale;
    var best = -1, bd = 34 * 34;
    for (var i = 0; i < pings.length; i++) {
      var dx = pings[i].x - x, dy = pings[i].y - y;
      var d2 = dx * dx + dy * dy;
      if (d2 < bd) { bd = d2; best = i; }
    }
    if (best >= 0) openDetail(pings[best].k);
    else if (detail.classList.contains("open")) closeDetail();
  });

  resize();
  requestAnimationFrame(frame);
  if (MODE === "live") {
    fetch("events.json", { cache: "no-cache" }).then(function (r) { return r.json(); }).then(function (arr) {
      FEED_CAP = 200; // before ingest: first fill must not trim to the replay cap
      ingest(arr);
      setInterval(liveTick, POLL_MS);
    }).catch(function () {
      // no same-origin artifact or no fetch support: replay the snapshot
      FEED_CAP = 9; // undo the live cap so the fallback keeps replay semantics
      MODE = "replay";
      schedule();
    });
  } else {
    schedule();
  }
})();
