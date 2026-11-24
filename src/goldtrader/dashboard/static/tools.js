"use strict";
/* Owner utilities (D-084): alerts (page, sound, desktop), price alerts on the chart, a live-link
   indicator, risk-used meters on the overview, a P&L calendar and CSV export on the trades page,
   a countdown to the next event and keyboard shortcuts. Everything here only reads; nothing can
   open a trade or change a limit. Settings and price alerts live in this browser only.
   Uses $, el, clear, f2, signed, hm, get, snap, lastOk (legacy.js), GT, gtShow, gtTheme (app.js),
   LV, TR, dateHm, setExplain (pages.js). */

/* ---------- stored settings ---------- */
const AL = {
  log: [], unread: 0, sound: true, desk: false, prices: [],   // prices: [{id, price, dir: "above"|"below"}]
  prev: null, lastBid: null, linkBad: false, eventWarned: {},
};
function alLoad() {
  try {
    const s = JSON.parse(localStorage.getItem("gt-alerts") || "{}");
    AL.sound = s.sound !== false; AL.desk = !!s.desk && "Notification" in window && Notification.permission === "granted";
    AL.prices = Array.isArray(s.prices) ? s.prices.filter(p => isFinite(p.price)) : [];
    AL.log = Array.isArray(s.log) ? s.log.slice(0, 60) : [];
  } catch (e) { /* private mode or bad data: start empty */ }
}
function alSave() {
  try { localStorage.setItem("gt-alerts", JSON.stringify({sound: AL.sound, desk: AL.desk, prices: AL.prices, log: AL.log.slice(0, 60)})); } catch (e) { /* ignore */ }
}

/* ---------- raising an alert ---------- */
let audioCtx = null;
function beep(level) {
  if (!AL.sound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const notes = level === "bad" ? [440, 330] : level === "good" ? [660, 880] : [740];
    notes.forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = audioCtx.currentTime + i * 0.16;
      o.type = "sine"; o.frequency.value = f; g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.18, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.15);
      o.connect(g); g.connect(audioCtx.destination); o.start(t); o.stop(t + 0.16);
    });
  } catch (e) { /* no audio device */ }
}
function alert_(title, text, level) {
  const item = {at: Date.now(), title, text: text || "", level: level || "info"};
  AL.log.unshift(item); AL.log = AL.log.slice(0, 60); AL.unread++; alSave();
  toast(item); beep(item.level); drawBell();
  if ($("alPanel") && !$("alPanel").hidden) drawAlertPanel();
  if (AL.desk && document.hidden && "Notification" in window && Notification.permission === "granted") {
    try { new Notification("Goldtrader: " + title, {body: item.text, tag: "gt-" + item.at}); } catch (e) { /* ignore */ }
  }
}
function toast(item) {
  const box = $("toasts"), t = el("div", "toast " + item.level);
  t.setAttribute("role", item.level === "bad" ? "alert" : "status");
  t.appendChild(el("b", null, item.title)); if (item.text) t.appendChild(el("span", null, item.text));
  const x = el("button", "toast-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Dismiss");
  x.addEventListener("click", () => t.remove()); t.appendChild(x);
  box.prepend(t); while (box.children.length > 4) box.lastChild.remove();
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, item.level === "bad" ? 15000 : 8000);
}

/* ---------- watching the snapshot for changes ---------- */
const posKey = p => String(p.ticket != null ? p.ticket : p.position_id);
function watch(s) {
  const g = s.gateway || {}, prev = AL.prev;
  if (prev) {
    const before = new Set(prev.positions.map(posKey));
    for (const p of s.positions) if (!before.has(posKey(p)))
      alert_("Trade opened", (p.side === "long" || p.side === "buy" ? "Long " : "Short ") + p.volume + " lot at " + f2(p.price_open) + (p.sl ? ", stop " + f2(p.sl) : ""), "info");
    const seen = new Set(prev.trades.recent.map(t => t.trade_id));
    for (const t of s.trades.recent.slice().reverse()) if (!seen.has(t.trade_id) && t.exit_ms > Date.now() - 3600e3)
      alert_("Trade closed " + signed(t.net_pnl) + " USD", t.side + " " + f2(t.entry_price) + " → " + f2(t.exit_price) + " (" + (typeof exitLabel === "function" ? exitLabel(t.exit_reason) : t.exit_reason) + ")", t.net_pnl > 0 ? "good" : "bad");
    const pg = prev.gateway || {};
    if (s.gateway && prev.gateway) {
      if (g.kill_switch && !pg.kill_switch) alert_("Kill switch is ON", "Everything is closed and trading is stopped. It is reset from Telegram.", "bad");
      if (!g.kill_switch && pg.kill_switch) alert_("Kill switch reset", "Trading may resume.", "good");
      if (g.paused && !pg.paused) alert_("Entries paused", "No new trades until resumed.", "warn");
      if (!g.paused && pg.paused) alert_("Entries resumed", "", "good");
      const lp = g.cooldown_until_ms > s.now_ms, plp = pg.cooldown_until_ms > prev.now_ms;
      if (lp && !plp) alert_("Loss pause", "Losses in a row: no new entries until " + hm(g.cooldown_until_ms) + ".", "warn");
    }
    if (prev.gateway_reachable && !s.gateway_reachable) alert_("Gateway not reachable", "Prices and the account are not updating.", "bad");
    if (!prev.gateway_reachable && s.gateway_reachable) alert_("Gateway back", "Prices and the account are updating again.", "good");
    const oldInc = new Set(prev.incidents.map(i => i.id));
    for (const i of s.incidents) if (!oldInc.has(i.id)) alert_("Incident: " + i.kind, i.summary, "bad");
    const staleBefore = new Set(prev.processes.filter(p => p.stale).map(p => p.process));
    for (const p of s.processes) if (p.stale && !staleBefore.has(p.process)) alert_(p.process + " stopped reporting", "Last heartbeat " + Math.round(p.age_s) + " s ago.", "bad");
  }
  for (const e of s.calendar) {   // high-impact news 15 minutes ahead, once per event
    const left = e.time_ms - s.now_ms, key = e.time_ms + e.title;
    if (e.impact === "high" && left > 0 && left <= 15 * 60e3 && !AL.eventWarned[key]) {
      AL.eventWarned[key] = 1; alert_("High-impact news in " + Math.ceil(left / 60e3) + " min", e.title + " (" + e.country + "). Entries pause around it.", "warn");
    }
  }
  if (g.bid != null) checkPrices(g.bid);
  AL.prev = s;
}
function checkPrices(bid) {
  const last = AL.lastBid; AL.lastBid = bid;
  if (last == null) return;
  let hit = false;
  for (const p of AL.prices.slice()) {
    if ((p.dir === "above" && last < p.price && bid >= p.price) || (p.dir === "below" && last > p.price && bid <= p.price)) {
      alert_("Price alert " + f2(p.price), "Gold went " + p.dir + " " + f2(p.price) + " (now " + f2(bid) + ").", "warn");
      AL.prices = AL.prices.filter(x => x !== p); hit = true;
    }
  }
  if (hit) { alSave(); drawPriceLines(); if ($("alPanel") && !$("alPanel").hidden) drawAlertPanel(); }
}

/* ---------- price alert lines on the live chart ---------- */
let priceLines = [];
function drawPriceLines() {
  if (!LV.series) return;
  for (const l of priceLines) { try { LV.series.removePriceLine(l); } catch (e) { /* chart rebuilt */ } }
  priceLines = AL.prices.map(p => LV.series.createPriceLine({price: p.price, color: css("--ai"), lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "alert " + (p.dir === "above" ? "▲" : "▼")}));
}
function addPrice(v) {
  const price = Number(String(v).replace(/,/g, ""));
  const bid = snap && snap.gateway ? snap.gateway.bid : AL.lastBid;
  if (!isFinite(price) || price <= 0) return "Type a price, for example 3650.";
  if (bid == null) return "No live price yet, so the direction is unknown. Try again in a moment.";
  if (Math.abs(price - bid) < 0.01) return "That is the current price.";
  if (AL.prices.length >= 20) return "At most 20 price alerts.";
  AL.prices.push({id: Date.now(), price, dir: price > bid ? "above" : "below"}); alSave(); drawPriceLines(); return "";
}

/* ---------- the bell and its panel ---------- */
function drawBell() {
  const n = $("bellN"); n.textContent = AL.unread > 9 ? "9+" : String(AL.unread); n.hidden = !AL.unread;
}
function drawAlertPanel() {
  const p = $("alPanel"); clear(p);
  const head = el("div", "al-head"); head.appendChild(el("b", null, "Alerts"));
  const clr = el("button", "btn", "Clear"); clr.type = "button";
  clr.addEventListener("click", () => { AL.log = []; alSave(); drawAlertPanel(); }); head.appendChild(clr); p.appendChild(head);

  const opts = el("div", "al-opts");
  const tog = (label, on, fn) => { const b = el("button", "btn chipbtn" + (on ? " on" : ""), label); b.type = "button"; b.setAttribute("aria-pressed", on ? "true" : "false"); b.addEventListener("click", fn); opts.appendChild(b); };
  tog(AL.sound ? "Sound on" : "Sound off", AL.sound, () => { AL.sound = !AL.sound; alSave(); if (AL.sound) beep("info"); drawAlertPanel(); });
  if ("Notification" in window) tog(AL.desk ? "Desktop on" : "Desktop off", AL.desk, async () => {
    if (!AL.desk && Notification.permission !== "granted") { try { await Notification.requestPermission(); } catch (e) { /* ignore */ } }
    AL.desk = !AL.desk && Notification.permission === "granted"; alSave(); drawAlertPanel();
  });
  p.appendChild(opts);

  p.appendChild(el("div", "al-sub", "Price alerts"));
  const f = el("form", "al-form"), inp = el("input", "sel"); inp.type = "text"; inp.inputMode = "decimal"; inp.placeholder = "Gold price, e.g. " + (AL.lastBid ? Math.round(AL.lastBid + 5) : "3650");
  inp.setAttribute("aria-label", "Alert price"); const add = el("button", "btn primary", "Add"); add.type = "submit";
  const msg = el("div", "al-msg");
  f.appendChild(inp); f.appendChild(add);
  f.addEventListener("submit", e => { e.preventDefault(); const err = addPrice(inp.value); msg.textContent = err; if (!err) drawAlertPanel(); });
  p.appendChild(f); p.appendChild(msg);
  if (!AL.prices.length) p.appendChild(el("div", "al-empty", "None set. You are told when gold crosses the price, even with the page in the background."));
  for (const x of AL.prices.slice().sort((a, b) => b.price - a.price)) {
    const r = el("div", "al-price"); r.appendChild(el("span", "num", (x.dir === "above" ? "▲ above " : "▼ below ") + f2(x.price)));
    const rm = el("button", "btn", "Remove"); rm.type = "button";
    rm.addEventListener("click", () => { AL.prices = AL.prices.filter(y => y !== x); alSave(); drawPriceLines(); drawAlertPanel(); });
    r.appendChild(rm); p.appendChild(r);
  }

  p.appendChild(el("div", "al-sub", "Recent"));
  const list = el("div", "al-list");
  if (!AL.log.length) list.appendChild(el("div", "al-empty", "Nothing yet. Trades, pauses, the kill switch, incidents, news and price alerts appear here."));
  for (const a of AL.log) { const r = el("div", "al-item " + a.level); r.appendChild(el("span", "t num", dateHm(a.at)));
    const x = el("span"); x.appendChild(el("b", null, a.title)); if (a.text) x.appendChild(el("span", "dim", " " + a.text)); r.appendChild(x); list.appendChild(r); }
  p.appendChild(list);
  p.appendChild(el("div", "al-foot", "Alerts work while this page is open in a browser tab. Telegram stays the alert channel when it is closed."));
}
function openAlerts(focusPrice) {
  const p = $("alPanel"); p.hidden = !p.hidden;
  if (!p.hidden) { AL.unread = 0; drawBell(); drawAlertPanel(); if (focusPrice) { const i = p.querySelector(".al-form input"); if (i) i.focus(); } }
}

/* ---------- live link: how old the newest data is ---------- */
function drawLink() {
  const b = $("link"); if (!b) return;
  const age = lastOk ? (Date.now() - lastOk) / 1000 : Infinity, gwOk = snap && snap.gateway_reachable;
  const state = age > 30 ? "bad" : age > 12 || !gwOk ? "warn" : "ok";
  b.className = "link " + state;
  b.lastChild.textContent = age === Infinity ? "connecting" : state === "ok" ? "Live" : age > 30 ? "No data " + Math.round(age) + "s" : !gwOk ? "Gateway down" : "Delayed " + Math.round(age) + "s";
  b.title = "Newest data " + (age === Infinity ? "not received yet" : Math.round(age) + " s old") + (gwOk ? "" : "; the gateway is not reachable");
  // Old data alone is not an alarm: a browser slows the timers of a tab in the background to one a
  // minute, so the data ages past 30 s while the server is fine. Alarm only when a request failed.
  if (state === "bad" && snapFailed && !AL.linkBad) { AL.linkBad = true; alert_("Dashboard lost its data", "The server has not answered for 30 seconds.", "bad"); }
  if (state !== "bad") AL.linkBad = false;
}

/* ---------- risk used, on the overview ---------- */
async function loadRiskMini() {
  if (GT.page !== "overview" || document.hidden) return;
  let r; try { r = await get("/api/risk"); } catch (e) { return; }
  let c = $("riskMini");
  if (!c) { c = el("div", "card t-risk"); c.id = "riskMini"; $("ctlSlot").after(c); }
  clear(c); c.appendChild(el("h2", null, "Risk used"));
  const want = ["day", "week", "drawdown"], names = {day: "Today", week: "This week", drawdown: "From peak"};
  for (const m of (r.meters || []).filter(m => want.includes(m.key))) {
    const row = el("div", "rm"), pct = m.pct == null ? 0 : m.pct;
    row.appendChild(el("span", "rm-k", names[m.key] || m.label));
    const tr = el("span", "bar-track"), fi = el("i", pct >= 0.8 ? "neg" : pct >= 0.5 ? "mid" : "pos"); fi.style.width = Math.max(2, pct * 100) + "%"; tr.appendChild(fi); row.appendChild(tr);
    row.appendChild(el("span", "rm-v num", m.pct == null ? "–" : Math.round(pct * 100) + "%"));
    row.title = m.label + ": " + (m.used != null ? f2(m.used) : "–") + " of " + (m.limit != null ? f2(m.limit) : "–") + " " + m.unit;
    c.appendChild(row);
  }
}

/* ---------- next event countdown in the stat strip ---------- */
function countdown() {
  if (!snap || GT.page !== "overview") return;
  const ev = snap.calendar.find(e => e.impact === "high") || snap.calendar[0], strip = $("ovCards");
  if (!ev || !strip || !strip.lastChild) return;
  const left = ev.time_ms - (snap.now_ms + (Date.now() - lastOk)), s = strip.lastChild.querySelector(".s");
  if (!s || left < 0) return;
  const d = Math.floor(left / 86400e3), h = Math.floor(left / 3600e3) % 24, m = Math.floor(left / 60e3) % 60;
  s.textContent = "in " + (d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m + "m " + Math.floor(left / 1000) % 60 + "s") + " · " + ev.title;
}

/* ---------- trades page: P&L calendar and CSV export ---------- */
let calTrades = null, calAt = 0;
async function drawPnlCalendar() {
  const kpi = $("trKpi"); if (!kpi) return;
  if (!calTrades || Date.now() - calAt > 60e3) { try { calTrades = (await get("/api/trades?period=all")).trades; calAt = Date.now(); } catch (e) { return; } }
  let c = $("pnlCal");
  if (!c) { c = el("section"); c.id = "pnlCal"; kpi.after(c); }
  clear(c); const card_ = el("div", "card"); c.appendChild(card_);
  const head = el("div", "chart-head"); head.appendChild(el("h2", null, "Daily results"));
  const legend = el("span", "dim small", "Each square is one day, darker means a bigger result. Hover for the day."); head.appendChild(legend);
  const exp = el("button", "btn", "Export CSV"); exp.type = "button"; exp.style.marginLeft = "auto"; exp.addEventListener("click", exportCsv); head.appendChild(exp);
  card_.appendChild(head);
  const byDay = {};
  for (const t of calTrades) { const d = new Date(t.exit_ms), k = d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
    const o = byDay[k] || (byDay[k] = {net: 0, n: 0, w: 0}); o.net += t.net_pnl; o.n++; if (t.net_pnl > 0) o.w++; }
  const weeks = 26, today = new Date(); today.setHours(0, 0, 0, 0);
  const start = new Date(today); start.setDate(start.getDate() - start.getDay() - (weeks - 1) * 7);   // a Sunday
  const top = Math.max(1, ...Object.values(byDay).map(o => Math.abs(o.net)));
  const grid = el("div", "cal"), labels = el("div", "cal-days");
  for (const d of ["Sun", "", "Tue", "", "Thu", "", "Sat"]) labels.appendChild(el("span", null, d));
  const wrap = el("div", "cal-wrap"); wrap.appendChild(labels); wrap.appendChild(grid);
  let best = null, worst = null, green = 0, red = 0;
  for (let i = 0; i < weeks * 7; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const k = d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate(), o = byDay[k], cell = el("i");
    if (d > today) cell.className = "future";
    else if (o) { const a = 0.25 + 0.75 * Math.min(1, Math.abs(o.net) / top); cell.className = o.net >= 0 ? "pos" : "neg"; cell.style.opacity = a.toFixed(2);
      cell.title = d.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}) + ": " + signed(o.net) + " USD, " + o.n + (o.n === 1 ? " trade, " : " trades, ") + o.w + " won";
      if (o.net >= 0) green++; else red++;
      if (!best || o.net > best[1]) best = [d, o.net]; if (!worst || o.net < worst[1]) worst = [d, o.net]; }
    else cell.title = d.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"}) + ": no trades";
    grid.appendChild(cell);
  }
  card_.appendChild(wrap);
  const fmt = x => x ? x[0].toLocaleDateString([], {month: "short", day: "numeric"}) + " " + signed(x[1]) : "–";
  card_.appendChild(el("div", "cal-sum dim", green + " green days, " + red + " red days in the last " + weeks + " weeks · best " + fmt(best) + " · worst " + fmt(worst)));
}
function exportCsv() {
  const ts = (TR.data && TR.data.trades) || [];
  const cols = ["trade_id", "side", "volume", "entry_time", "exit_time", "entry_price", "exit_price", "gross_pnl", "spread_cost", "slippage_cost", "commission", "swap", "net_pnl", "r_multiple", "exit_reason", "ai"];
  const iso = ms => ms ? new Date(ms).toISOString() : "";
  const q = v => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
  const lines = [cols.join(",")].concat(ts.map(t => cols.map(c => q(c === "entry_time" ? iso(t.entry_ms) : c === "exit_time" ? iso(t.exit_ms) : t[c])).join(",")));
  const a = el("a"); a.href = URL.createObjectURL(new Blob([lines.join("\n")], {type: "text/csv"}));
  a.download = "goldtrader-trades-" + (TR.period || "all") + "-" + new Date().toISOString().slice(0, 10) + ".csv";
  document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

/* ---------- keyboard shortcuts ---------- */
const KEYS = [["1", "overview"], ["2", "trades"], ["3", "strategies"], ["4", "agents"], ["5", "research"], ["6", "risk"], ["7", "assistant"], ["8", "help"]];
function keyHelp() {
  if (typeof closeSheet === "function") closeSheet();
  const sh = el("div", "sheet"); sh.id = "sheet"; sh.appendChild(el("h1", null, "Keyboard shortcuts"));
  const t = el("div", "keys");
  for (const [k, page] of KEYS) { t.appendChild(el("kbd", null, k)); t.appendChild(el("span", null, document.querySelector('[data-nav="' + page + '"] .nav-txt').textContent)); }
  for (const [k, what] of [["A", "Alerts and price alerts"], ["E", "Show or hide explanations"], ["T", "Theme"], ["?", "This list"], ["Esc", "Close a panel"]]) { t.appendChild(el("kbd", null, k)); t.appendChild(el("span", null, what)); }
  sh.appendChild(t);
  const row = el("div", "sheet-btns"), b = el("button", "btn", "Close"); b.type = "button"; b.addEventListener("click", () => closeSheet()); row.appendChild(b); sh.appendChild(row);
  document.body.appendChild(sh); $("scrim").hidden = false; b.focus();
}
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
  if ($("sheet") && e.key !== "Escape") return;
  const page = KEYS.find(k => k[0] === e.key);
  if (page) { location.hash = page[1]; return; }
  const k = e.key.toLowerCase();
  if (k === "a") openAlerts(false);
  else if (k === "e") $("explainBtn").click();
  else if (k === "t") $("themeBtn").click();
  else if (e.key === "?") keyHelp();
  else if (e.key === "Escape" && !$("alPanel").hidden) $("alPanel").hidden = true;
});

/* ---------- wiring ---------- */
alLoad(); drawBell();
$("bellBtn").addEventListener("click", () => openAlerts(false));
$("priceAlertBtn").addEventListener("click", () => openAlerts(true));
document.addEventListener("click", e => { const p = $("alPanel"); if (!p.hidden && !p.contains(e.target) && !e.target.closest("#bellBtn, #priceAlertBtn")) p.hidden = true; });
$("keysBtn").addEventListener("click", keyHelp);
const _renderTools = render;
render = function () { _renderTools(); if (snap) watch(snap); };
const _drawChartTools = drawChart;
drawChart = function () { const had = LV.series; _drawChartTools(); if (LV.series && (!had || priceLines.length !== AL.prices.length)) drawPriceLines(); };
const _drawTradesTools = drawTrades;
drawTrades = function () { _drawTradesTools(); drawPnlCalendar(); };
const _ovHook = GT.hooks.overview;
GT.hooks.overview = (...a) => { if (_ovHook) _ovHook(...a); loadRiskMini(); };
setInterval(loadRiskMini, 10000); setInterval(drawLink, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadSnap(); });  // fresh data at once on return setInterval(countdown, 1000);
loadRiskMini();
