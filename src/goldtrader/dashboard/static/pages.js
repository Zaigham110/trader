"use strict";
/* New pages of the dashboard (D-083). Helpers $, el, clear, f2, f3, signed, hm, dayHm, ago, css,
   get and tmCard come from legacy.js; GT, gtShow and gtTheme from app.js. */

/* ---------- small building blocks ---------- */
const TZ_S = -new Date().getTimezoneOffset() * 60;   // the chart library draws UTC: shift to local time
const dateHm = ms => ms ? new Date(ms).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
const pct = x => x == null ? "–" : Math.round(x * 100) + "%";
const rTxt = x => x == null ? "–" : (x >= 0 ? "+" : "") + Number(x).toFixed(2) + " R";
const tone = x => x > 0 ? "up" : x < 0 ? "down" : "";
const AI_LABEL = {agree: "AI agreed", low_confidence: "AI unsure", wait: "AI said wait", unavailable: "AI unavailable"};
const EXIT_LABEL = {stop: "Stop hit", target: "Target hit", trail: "Trailing stop", session_flatten: "Closed before market break", breakeven: "Stop at entry", kill_switch: "Kill switch", manual: "Closed by owner", no_stop: "Closed: no stop", opposite_signal: "Opposite signal", unknown: "Unknown"};
const exitLabel = k => EXIT_LABEL[k] || k;

function term(text, key) { const s = el("span", null, text); s.dataset.term = key || text; s.tabIndex = 0; return s; }
function card(title, termKey) {
  const c = el("div", "card"), h = el("h2");
  if (termKey) h.appendChild(term(title, termKey)); else h.textContent = title;
  c.appendChild(h); return c;
}
function seg(options, value, onPick) {
  const box = el("div", "tf"); box.setAttribute("role", "group");
  for (const [v, label] of options) {
    const b = el("button", v === value ? "on" : "", label); b.type = "button";
    b.addEventListener("click", () => { for (const x of box.children) x.classList.toggle("on", x === b); onPick(v); });
    box.appendChild(b);
  }
  return box;
}
function select(options, onPick) {
  const s = el("select", "sel");
  for (const [v, label] of options) { const o = el("option", null, label); o.value = v; s.appendChild(o); }
  s.addEventListener("change", () => onPick(s.value)); return s;
}
/* horizontal bars for a breakdown: rows = [[label, {n, wins, net, sum_r}]] */
function bars(rows, note) {
  const box = el("div", "bars");
  if (!rows.length) { box.appendChild(el("div", "empty", "Nothing in this period yet.")); return box; }
  const top = Math.max(...rows.map(r => Math.abs(r[1].net)), 0.01);
  for (const [label, g] of rows) {
    const r = el("div", "bar-row");
    r.appendChild(el("span", "bar-k", label));
    const track = el("span", "bar-track"), fill = el("i", g.net >= 0 ? "pos" : "neg");
    fill.style.width = Math.max(2, Math.abs(g.net) / top * 100) + "%"; track.appendChild(fill); r.appendChild(track);
    r.appendChild(el("span", "bar-v num " + tone(g.net), signed(g.net)));
    r.appendChild(el("span", "bar-n dim", g.n + (g.n === 1 ? " trade, " : " trades, ") + g.wins + " won"));
    box.appendChild(r);
  }
  if (note) box.appendChild(el("div", "tm-note", note));
  return box;
}

/* ---------- charts (TradingView Lightweight Charts, stored in this project) ---------- */
const charts = new Set();
function chartColors() {
  return {layout: {background: {type: "solid", color: "transparent"}, textColor: css("--dim"), attributionLogo: false, fontFamily: "ui-monospace, Consolas, monospace", fontSize: 11},
    grid: {vertLines: {color: css("--line")}, horzLines: {color: css("--line")}},
    rightPriceScale: {borderColor: css("--line")}, timeScale: {borderColor: css("--line"), timeVisible: true, secondsVisible: false}};
}
function makeChart(host, extra) {
  if (!window.LightweightCharts) { host.appendChild(el("div", "empty", "The chart library did not load.")); return null; }
  const ch = LightweightCharts.createChart(host, Object.assign({autoSize: true, crosshair: {mode: 0}}, chartColors(), extra || {}));
  charts.add(ch); return ch;
}
function dropChart(ch) { if (ch) { charts.delete(ch); ch.remove(); } }
function rethemeCharts() { for (const ch of charts) ch.applyOptions(chartColors()); }

/* ====================================================================== Trades page */
const TR = {period: "all", data: null, sort: ["exit_ms", -1], filter: {side: "", result: "", exit: ""}, page: 0, chart: null, built: false};
const TR_COLS = [
  ["exit_ms", "Closed", t => dateHm(t.exit_ms), "num dim"],
  ["side", "Side", null, ""],
  ["entry_price", "Entry", t => f2(t.entry_price), "num"],
  ["exit_price", "Exit", t => f2(t.exit_price), "num"],
  ["held", "Held", t => holdTxt(t.exit_ms - t.entry_ms), "num dim"],
  ["r_multiple", "R", t => t.r_multiple != null ? t.r_multiple.toFixed(2) : "–", "num"],
  ["net_pnl", "Net USD", t => signed(t.net_pnl), "num"],
  ["exit_reason", "How it closed", t => exitLabel(t.exit_reason), "dim"],
  ["ai", "AI at entry", t => AI_LABEL[t.ai] || t.ai, "dim"],
];
function holdTxt(ms) { const m = Math.round(ms / 60000); return m < 60 ? m + "m" : Math.floor(m / 60) + "h " + (m % 60) + "m"; }

function buildTrades() {
  const root = $("tradesNew"); clear(root); TR.built = true;
  const bar = el("div", "toolbar");
  bar.appendChild(seg([["today", "Today"], ["week", "This week"], ["month", "This month"], ["all", "All"]], TR.period, v => { TR.period = v; TR.page = 0; loadTrades(); }));
  bar.appendChild(el("span", "dim", "")).id = "trUpdated";
  root.appendChild(bar);
  root.appendChild(el("div", "tm-cards")).id = "trKpi";

  const g1 = el("section", "grid g-top");
  const eq = card("Result over time");
  eq.appendChild(el("div", "tm-note", "The line adds up the net result of each closed trade. The red bars show how far the total is below its best point so far (drawdown)."));
  eq.appendChild(el("div", "chart-box")).id = "trCurve";
  g1.appendChild(eq);
  const cost = card("What the trades cost", "Costs"); cost.appendChild(el("div")).id = "trCosts";
  const hist = el("div"); hist.id = "trHist"; cost.appendChild(el("h2", "sub", "Size of wins and losses")); cost.appendChild(hist);
  g1.appendChild(cost); root.appendChild(g1);

  const g2 = el("section", "grid g-3");
  for (const [id, title, key] of [["trByExit", "By how the trade closed", null], ["trBySide", "By direction", null], ["trByAi", "By what the AI said", "AI opinion"], ["trByDay", "By weekday", null], ["trByHour", "By hour of entry (UTC)", null]]) {
    const c = card(title, key); c.appendChild(el("div")).id = id; g2.appendChild(c);
  }
  root.appendChild(g2);

  const tc = card("All trades in this period");
  const f = el("div", "toolbar");
  f.appendChild(select([["", "Both directions"], ["long", "Long only"], ["short", "Short only"]], v => { TR.filter.side = v; TR.page = 0; drawTradeTable(); }));
  f.appendChild(select([["", "Wins and losses"], ["win", "Wins only"], ["loss", "Losses only"]], v => { TR.filter.result = v; TR.page = 0; drawTradeTable(); }));
  const ex = select([["", "Any close"]], v => { TR.filter.exit = v; TR.page = 0; drawTradeTable(); }); ex.id = "trExitSel"; f.appendChild(ex);
  f.appendChild(el("span", "dim", "Click a row to see why the trade was taken and what happened inside it."));
  tc.appendChild(f);
  tc.appendChild(el("div", "scroll-x")).id = "trTable";
  tc.appendChild(el("div", "pager")).id = "trPager";
  root.appendChild(el("section")).appendChild(tc);
}

async function loadTrades() {
  if (!TR.built) buildTrades();
  try { TR.data = await get("/api/trades?period=" + TR.period); } catch (e) { return; }
  drawTrades();
}
function drawTrades() {
  const d = TR.data, s = d.stats, k = $("trKpi"); clear(k);
  $("trUpdated").textContent = s.n + (s.n === 1 ? " closed trade" : " closed trades") + ", after all costs";
  tmCard(k, "Net result", signed(s.net) + " USD", tone(s.net), s.n ? "average " + signed(s.expectancy_usd) + " per trade" : "no trades in this period");
  tmCard(k, "Win rate", pct(s.hit_rate), "", s.wins + " won, " + s.losses + " lost");
  tmCard(k, "Average per trade", rTxt(s.expectancy_r), tone(s.expectancy_r), "R = multiples of the amount risked");
  tmCard(k, "Profit factor", s.profit_factor != null ? s.profit_factor.toFixed(2) : "–", s.profit_factor > 1 ? "up" : s.profit_factor != null ? "down" : "", "money won divided by money lost; above 1 is a net gain");
  tmCard(k, "Average win / loss", (s.avg_win != null ? signed(s.avg_win) : "–") + " / " + (s.avg_loss != null ? signed(s.avg_loss) : "–"), "", "best " + signed(s.best) + ", worst " + signed(s.worst));
  tmCard(k, "Deepest fall", s.max_drawdown_usd ? "-" + f2(s.max_drawdown_usd) + " USD" : "0.00 USD", s.max_drawdown_usd ? "down" : "", "largest drop from a high point of the total");
  tmCard(k, "Time in a trade", s.avg_hold_min != null ? holdTxt(s.avg_hold_min * 60000) : "–", "", "average from entry to close");

  // result over time
  const host = $("trCurve"); dropChart(TR.chart); clear(host); TR.chart = null;
  if (s.curve.length < 2) host.appendChild(el("div", "empty", "The curve appears after two closed trades."));
  else {
    const ch = TR.chart = makeChart(host, {leftPriceScale: {visible: false}});
    if (ch) {
      const seen = new Set(), pts = [], dd = [];
      for (const [ms, cum, down] of s.curve) { let t = Math.floor(ms / 1000) + TZ_S; while (seen.has(t)) t++; seen.add(t); pts.push({time: t, value: cum}); dd.push({time: t, value: down}); }
      const area = ch.addSeries(LightweightCharts.AreaSeries, {lineColor: css("--gold"), topColor: "rgba(232,184,74,.30)", bottomColor: "rgba(232,184,74,0)", lineWidth: 2, priceFormat: {type: "price", precision: 2, minMove: 0.01}});
      area.setData(pts);
      const h = ch.addSeries(LightweightCharts.HistogramSeries, {color: "rgba(246,70,93,.45)", priceScaleId: "dd", priceFormat: {type: "price", precision: 2, minMove: 0.01}, lastValueVisible: false, priceLineVisible: false});
      h.setData(dd); ch.priceScale("dd").applyOptions({scaleMargins: {top: 0.75, bottom: 0}, visible: false});
      ch.timeScale().fitContent();
    }
  }
  // costs and distribution
  const c = $("trCosts"); clear(c); const kvb = el("div", "kv num");
  kv(kvb, "Spread paid", f2(s.costs.spread) + " USD"); kv(kvb, "Slippage", f2(s.costs.slippage) + " USD");
  kv(kvb, "Commission", f2(s.costs.commission) + " USD"); kv(kvb, "Swap", f2(s.costs.swap) + " USD");
  kv(kvb, "All costs", f2(s.costs.total) + " USD"); c.appendChild(kvb);
  c.appendChild(el("div", "tm-note", "These are already inside the net result above. Spread is the gap between the buy and sell price; slippage is the difference between the price asked for and the price received."));
  const hb = $("trHist"); clear(hb); const topN = Math.max(...s.r_hist.map(x => x[1]), 1), hw = el("div", "hist-r");
  for (const [name, n] of s.r_hist) { const col = el("div", "hcol"), b = el("i", name.startsWith("-") || name.startsWith("under") ? "neg" : "pos"); b.style.height = Math.max(2, n / topN * 64) + "px"; col.appendChild(el("span", "num dim", n ? String(n) : "")); col.appendChild(b); col.appendChild(el("span", "hl dim", name)); hw.appendChild(col); }
  hb.appendChild(hw); hb.appendChild(el("div", "tm-note", "How many trades ended in each range of R. A trade that hits its original stop is about -1 R."));

  const ord = (o, keys) => keys.filter(x => o[x]).map(x => [x, o[x]]);
  const fill = (id, rows, note) => { const b = $(id); clear(b); b.appendChild(bars(rows, note)); };
  fill("trByExit", Object.entries(s.by.exit_reason).sort((a, b) => b[1].n - a[1].n).map(([key, g]) => [exitLabel(key), g]));
  fill("trBySide", ord(s.by.side, ["long", "short"]).map(([key, g]) => [key === "long" ? "Long (buy)" : "Short (sell)", g]));
  fill("trByAi", ord(s.by.ai, ["agree", "low_confidence", "wait", "unavailable"]).map(([key, g]) => [AI_LABEL[key], g]), "On this account the rule's signals are taken whatever the AI says, so this shows how its opinion would have sorted them.");
  fill("trByDay", ord(s.by.weekday, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]));
  fill("trByHour", Object.entries(s.by.hour).sort((a, b) => a[0].localeCompare(b[0])).map(([key, g]) => [key + ":00", g]), "With few trades per hour this is noise; it becomes useful after a few dozen trades.");

  const sel = $("trExitSel"), keep = TR.filter.exit; clear(sel);
  for (const [v, label] of [["", "Any close"], ...Object.keys(s.by.exit_reason).map(x => [x, exitLabel(x)])]) { const o = el("option", null, label); o.value = v; sel.appendChild(o); }
  sel.value = keep in s.by.exit_reason ? keep : ""; TR.filter.exit = sel.value;
  drawTradeTable();
}
function drawTradeTable() {
  const box = $("trTable"), pager = $("trPager"); clear(box); clear(pager);
  const f = TR.filter, [key, dir] = TR.sort;
  let list = TR.data.trades.filter(t => (!f.side || t.side === f.side) && (!f.exit || t.exit_reason === f.exit) && (!f.result || (f.result === "win") === (t.net_pnl > 0)));
  const val = t => key === "held" ? t.exit_ms - t.entry_ms : t[key];
  list = list.slice().sort((a, b) => { const x = val(a), y = val(b); return (x == null) - (y == null) || (x > y ? 1 : x < y ? -1 : 0) * dir; });
  if (!list.length) { box.appendChild(el("div", "empty", TR.data.trades.length ? "No trade matches these filters." : "No closed trades in this period yet.")); return; }
  const per = 25, pages = Math.ceil(list.length / per); TR.page = Math.min(TR.page, pages - 1);
  const t = el("table", "sortable"), h = el("tr");
  for (const [k, label] of TR_COLS) {
    const th = el("th", k === key ? "sorted" : "", label + (k === key ? (dir > 0 ? " ▲" : " ▼") : "")); th.tabIndex = 0; th.setAttribute("role", "button");
    const go = () => { TR.sort = [k, k === key ? -dir : -1]; drawTradeTable(); };
    th.addEventListener("click", go); th.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
    h.appendChild(th);
  }
  t.appendChild(h);
  for (const x of list.slice(TR.page * per, TR.page * per + per)) {
    const r = el("tr", "click"); r.tabIndex = 0;
    for (const [k, , fmt, cls] of TR_COLS) {
      if (k === "side") { const td = el("td"); td.appendChild(el("span", "tag " + x.side, x.side)); r.appendChild(td); continue; }
      r.appendChild(el("td", cls + (k === "net_pnl" || k === "r_multiple" ? " " + tone(x.net_pnl) : ""), fmt(x)));
    }
    const open = () => openTrade(x.trade_id);
    r.addEventListener("click", open); r.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
    t.appendChild(r);
  }
  box.appendChild(t);
  pager.appendChild(el("span", "dim", list.length + (list.length === 1 ? " trade" : " trades")));
  if (pages > 1) {
    const prev = el("button", "btn", "Previous"), next = el("button", "btn", "Next"); prev.type = next.type = "button";
    prev.disabled = TR.page === 0; next.disabled = TR.page >= pages - 1;
    prev.addEventListener("click", () => { TR.page--; drawTradeTable(); }); next.addEventListener("click", () => { TR.page++; drawTradeTable(); });
    pager.appendChild(prev); pager.appendChild(el("span", "dim", "page " + (TR.page + 1) + " of " + pages)); pager.appendChild(next);
  }
}

/* ---------- trade detail drawer ---------- */
function stepText(st) {
  if (st.step === "code_only") return st.ok ? "signal taken" : "not taken: " + st.reason;
  if (st.step === "intent") return (st.kind || "order") + " " + (st.status || "") + (st.delay_ms != null ? " after " + (st.delay_ms / 1000).toFixed(1) + " s" : "") + (st.risk_code ? " (" + st.risk_code + ")" : "");
  return stepDetail(st);
}
function slipText(o) {
  if (o.slippage_points == null || o.kind === "sltp") return "–";
  const p = Math.round(Math.abs(o.slippage_points));
  return p === 0 ? "none" : p + " pts " + (o.slippage_points < 0 ? "better" : "worse");
}
let drawerChart = null;
function closeDrawer() { dropChart(drawerChart); drawerChart = null; $("drawer").hidden = true; $("scrim").hidden = true; document.body.classList.remove("no-scroll"); }
function openDrawer(title) {
  const d = $("drawer"); clear($("drawerBody")); $("drawerTitle").textContent = title;
  d.hidden = false; $("scrim").hidden = false; document.body.classList.add("no-scroll"); $("drawerClose").focus();
  return $("drawerBody");
}
function para(parent, label, text) { if (!text) return; const p = el("p", "para"); p.appendChild(el("b", null, label + " ")); p.appendChild(document.createTextNode(text)); parent.appendChild(p); }
async function openTrade(id) {
  const body = openDrawer("Trade");
  body.appendChild(el("div", "empty", "Loading…"));
  let d; try { d = await get("/api/trade?id=" + encodeURIComponent(id)); } catch (e) { clear(body); body.appendChild(el("div", "empty", "This trade could not be loaded.")); return; }
  clear(body);
  const t = d.trade, x = d.excursion, long = t.side === "long";
  $("drawerTitle").textContent = (long ? "Long" : "Short") + " " + t.volume + " lot, " + dateHm(t.entry_ms) + " to " + dateHm(t.exit_ms);
  const k = el("div", "tm-cards");
  tmCard(k, "Net result", signed(t.net_pnl) + " USD", tone(t.net_pnl), rTxt(t.r_multiple) + ", " + exitLabel(t.exit_reason).toLowerCase());
  tmCard(k, "Entry and exit", f2(t.entry_price) + " → " + f2(t.exit_price), "", "held " + holdTxt(t.exit_ms - t.entry_ms));
  tmCard(k, "Amount risked", d.position.risk_usd != null ? f2(d.position.risk_usd) + " USD" : "–", "", (d.position.risk_pct != null ? d.position.risk_pct.toFixed(2) + "% of the account, " : "") + "stop " + (x.stop_distance != null ? f2(x.stop_distance) : "–") + " away");
  tmCard(k, "Best point reached", x.mfe_r != null ? "+" + x.mfe_r.toFixed(2) + " R" : "–", "up", "+" + f2(x.mfe) + " in price, in the trade's favour");
  tmCard(k, "Worst point reached", x.mae_r != null ? "-" + x.mae_r.toFixed(2) + " R" : "–", "down", "-" + f2(x.mae) + " in price, against the trade");
  tmCard(k, "Costs", f2((t.spread_cost || 0) + (t.slippage_cost || 0) + (t.commission || 0) - (t.swap || 0)) + " USD", "", "spread " + f2(t.spread_cost) + ", slippage " + f2(t.slippage_cost));
  body.appendChild(k);

  const host = el("div", "chart-box tall"); body.appendChild(host);
  body.appendChild(el("div", "tm-note", "Blue line: entry. Red stepped line: the stop, including every move by the trailing stop. Green line: the target. Arrows mark the entry and the close. Candles are " + d.candle_minutes + "-minute bars in your local time."));
  if (d.candles.length) {
    const ch = drawerChart = makeChart(host);
    if (ch) {
      const span = d.candle_minutes * 60, snapT = ms => { const s = Math.floor(ms / 1000); return s - s % span + TZ_S; };
      const cs = ch.addSeries(LightweightCharts.CandlestickSeries, {upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down"), priceLineVisible: false, lastValueVisible: false});
      cs.setData(d.candles.map(c => ({time: c[0] + TZ_S, open: c[1], high: c[2], low: c[3], close: c[4]})));
      cs.createPriceLine({price: t.entry_price, color: css("--info"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "entry"});
      if (d.position.tp) cs.createPriceLine({price: d.position.tp, color: css("--up"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "target"});
      if (d.stops.length) {
        const st = ch.addSeries(LightweightCharts.LineSeries, {color: css("--down"), lineWidth: 2, lineType: 1, priceLineVisible: false, lastValueVisible: true, title: "stop", crosshairMarkerVisible: false});
        const pts = [], used = new Set();
        for (const [ms, sl] of [...d.stops, [t.exit_ms, d.stops[d.stops.length - 1][1]]]) { let tt = snapT(ms); while (used.has(tt)) tt += span; used.add(tt); pts.push({time: tt, value: sl}); }
        st.setData(pts);
      }
      LightweightCharts.createSeriesMarkers(cs, [
        {time: snapT(t.entry_ms), position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: css("--info"), text: long ? "buy" : "sell"},
        {time: snapT(t.exit_ms), position: long ? "aboveBar" : "belowBar", shape: long ? "arrowDown" : "arrowUp", color: t.net_pnl > 0 ? css("--up") : css("--down"), text: "close " + signed(t.net_pnl)},
      ]);
      ch.timeScale().fitContent();
    }
  } else host.appendChild(el("div", "empty", "No stored price bars cover this trade."));

  const why = el("div", "block"); why.appendChild(el("h2", null, "Why it was opened")); body.appendChild(why);
  const ob = d.opened_by;
  para(why, "Who sent the order:", ob ? (ob.bot === "code_only" ? "the code rule, without waiting for the AI (this account takes every signal of the rule)" : "the Strategy agent's own proposal") : "not recorded (the position was not opened by this system's decision cycle)");
  if (ob && ob.thesis) para(why, "Stated reason:", ob.thesis);
  para(why, "What the Strategy agent said:", d.agent ? (AI_LABEL[d.ai] || d.ai) + (d.agent.confidence != null ? " (confidence " + Number(d.agent.confidence).toFixed(2) + ")" : "") : "no answer on record");
  if (d.agent && d.agent.thesis) para(why, "The agent's reasoning:", d.agent.thesis);
  if (d.agent && d.agent.invalidation) para(why, "What would change its mind:", d.agent.invalidation);
  if (d.cycle && d.cycle.steps.length) {
    const flow = el("div", "flow");
    for (const st of d.cycle.steps) { const s = el("div", "step " + stepClass(st)); s.appendChild(el("b", null, st.step === "code_only" ? "Code rule" : (STEP_NAMES[st.step] || st.step))); s.appendChild(el("span", null, stepText(st))); flow.appendChild(s); }
    why.appendChild(el("h2", "sub", "The decision, step by step")); why.appendChild(flow);
  }

  const ord = el("div", "block"); ord.appendChild(el("h2", null, "Orders sent to the broker")); body.appendChild(ord);
  const sent = d.orders.filter(o => o.stage === "send");
  if (!sent.length) ord.appendChild(el("div", "empty", "No order requests are recorded for this trade."));
  else {
    const tb = el("table"), h = el("tr"); for (const c of ["Time", "What", "Asked", "Got", "Slippage", "Result"]) h.appendChild(el("th", null, c)); tb.appendChild(h);
    const what = o => o.kind === "entry" ? "Open the trade" : o.kind === "close" ? "Close the trade" : o.kind === "sltp" ? (o.trail ? "Trailing stop to " + f2(o.sl) : "Move stop to " + f2(o.sl)) : o.kind;
    for (const o of sent) { const r = el("tr");
      r.appendChild(el("td", "num dim", dateHm(o.sent_ms))); r.appendChild(el("td", null, what(o)));
      r.appendChild(el("td", "num", o.kind === "sltp" ? "–" : f2(o.requested_price))); r.appendChild(el("td", "num", o.kind === "sltp" ? "–" : f2(o.filled_price)));
      r.appendChild(el("td", "num " + (o.slippage_points < 0 ? "up" : o.slippage_points > 0 ? "down" : ""), slipText(o)));
      r.appendChild(el("td", o.retcode === 10009 ? "up" : "down", o.retcode === 10009 ? "accepted" : "refused (" + o.retcode + ")")); tb.appendChild(r); }
    const sx = el("div", "scroll-x"); sx.appendChild(tb); ord.appendChild(sx);
  }
}

/* ---------- wiring ---------- */
GT.hooks.trades = () => loadTrades();
setInterval(() => { if (GT.page === "trades" && !document.hidden && $("drawer").hidden) loadTrades(); }, 30000);
$("drawerClose").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
document.getElementById("themeBtn").addEventListener("click", () => setTimeout(rethemeCharts, 0));
gtBoot();
