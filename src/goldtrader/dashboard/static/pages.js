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
  return {layout: {background: {type: "solid", color: "transparent"}, textColor: css("--dim"), attributionLogo: false, fontFamily: css("--sans"), fontSize: 11},
    grid: {vertLines: {color: css("--grid")}, horzLines: {color: css("--grid")}},
    rightPriceScale: {borderColor: css("--line")}, timeScale: {borderColor: css("--line"), timeVisible: true, secondsVisible: false},
    crosshair: {mode: 0, vertLine: {color: css("--faint"), labelBackgroundColor: css("--line2"), style: 3}, horzLine: {color: css("--faint"), labelBackgroundColor: css("--line2"), style: 3}}};
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
      const area = ch.addSeries(LightweightCharts.AreaSeries, {lineColor: css("--gold"), topColor: "rgba(242,193,78,.32)", bottomColor: "rgba(242,193,78,0)", lineWidth: 2, priceFormat: {type: "price", precision: 2, minMove: 0.01}});
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
  drawTradeTable(); annotate($("page-trades"));
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

/* ====================================================================== Strategies page */
const ST = {data: null, open: new Set()};
const diagrams = [];   // [canvas, diagram] pairs to redraw on resize or theme change
function drawDiagram(canvas, d) {
  if (!canvas.clientWidth) return;
  const [g, W, H] = setup(canvas), padL = 8, padR = 8, padT = 14, padB = 14;
  const X = x => padL + x / 100 * (W - padL - padR), Y = y => H - padB - y / 100 * (H - padT - padB);
  const col = {stop: css("--down"), target: css("--up"), guide: css("--dim"), entry: css("--info")};
  g.font = "11px 'Segoe UI', system-ui, sans-serif";
  for (const l of d.levels) { g.strokeStyle = col[l.kind] || col.guide; g.setLineDash([5, 4]); g.beginPath(); g.moveTo(padL, Y(l.y)); g.lineTo(W - padR, Y(l.y)); g.stroke(); g.setLineDash([]); g.fillStyle = col[l.kind] || col.guide; g.textAlign = "left"; g.fillText(l.text, padL + 2, Y(l.y) - 8); }
  g.strokeStyle = css("--gold"); g.lineWidth = 2; g.lineJoin = "round"; g.beginPath();
  d.line.forEach((p, i) => i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))); g.stroke(); g.lineWidth = 1;
  for (const m of d.marks) {
    const c = col[m.kind] || col.guide; g.fillStyle = c; g.beginPath(); g.arc(X(m.x), Y(m.y), 5, 0, 6.3); g.fill();
    const w = g.measureText(m.text).width, right = X(m.x) + w + 12 > W;
    g.textAlign = right ? "right" : "left"; g.fillText(m.text, X(m.x) + (right ? -10 : 10), Y(m.y) + 14);
  }
}
function redrawDiagrams() { for (const [c, d] of diagrams) drawDiagram(c, d); }
function ruleBody(doc) {
  const b = el("div", "rule");
  para(b, "When it enters:", doc.enters); para(b, "When it closes:", doc.exits);
  if (doc.works_when) para(b, "Works best when:", doc.works_when);
  if (doc.struggles_when) para(b, "Struggles when:", doc.struggles_when);
  return b;
}
function paramTable(doc, params) {
  const names = Object.keys(params).filter(k => k in doc.params);
  if (!names.length) return el("div", "empty", "This rule has no settings.");
  const t = el("table", "params"), h = el("tr"); for (const c of ["Setting", "Value", "What it means"]) h.appendChild(el("th", null, c)); t.appendChild(h);
  for (const k of names) { const d = doc.params[k], r = el("tr");
    r.appendChild(el("td", null, d.label)); r.appendChild(el("td", "num", Number(params[k]) + (d.unit ? " " + d.unit : ""))); r.appendChild(el("td", "wrap dim", d.meaning)); t.appendChild(r); }
  const sx = el("div", "scroll-x"); sx.appendChild(t); return sx;
}
function workedExample(params, risk, atr) {
  const b = el("div", "worked");
  if (!atr || !params.stop_atr) { b.appendChild(el("div", "dim", "A worked example appears when the gateway reports the current ATR.")); return b; }
  const stop = params.stop_atr * atr, target = (params.target_atr || 0) * atr;
  const p = el("p", "para");
  p.appendChild(document.createTextNode("Right now the ATR is " + f2(atr) + ". A trade opened this minute would have its stop " + f2(stop) + " away from the entry and its target " + f2(target) + " away. At 0.01 lot on this account that is a loss of about " + f2(stop) + " USD if the stop is hit (1 R) and a gain of about " + f2(target) + " USD at the target (" + (target / stop).toFixed(1) + " R)."));
  b.appendChild(p);
  if (risk && risk.trail_distance_r > 0) {
    const start = risk.trail_after_r * stop, follow = risk.trail_distance_r * stop;
    para(b, "Trailing stop:", "once the trade is " + f2(start) + " in profit (" + risk.trail_after_r + " R), the stop starts following the best price at a distance of " + f2(follow) + ", so from then on it closes in profit unless the price jumps straight past the stop.");
  } else para(b, "Trailing stop:", "off for this account.");
  return b;
}
function buildStrategies() {
  const root = $("strategiesNew"), d = ST.data; clear(root); diagrams.length = 0;
  const atr = snap && snap.gateway ? snap.gateway.atr : null;

  const basics = el("section", "grid g-2");
  const c1 = card("How every trade is sized and protected");
  para(c1, "ATR.", d.common.atr); para(c1, "R.", d.common.r); para(c1, "Trailing stop.", d.common.trailing); para(c1, "Daily break.", d.common.session);
  basics.appendChild(c1);
  const c2 = card("The limits around every strategy");
  const kvb = el("div", "kv num"), r = d.risk;
  if (r && r.risk_per_trade_pct != null) {
    kv(kvb, "Most risked on one trade", r.risk_per_trade_pct + "% of the account"); kv(kvb, "Positions at once", String(r.max_positions));
    kv(kvb, "Largest trade size", r.max_volume_lots + " lot"); kv(kvb, "Stop allowed between", r.stop_atr_min + " and " + r.stop_atr_max + " x ATR");
    kv(kvb, "Stop trading for the day after", "-" + r.daily_loss_pct + "%"); kv(kvb, "Stop trading for the week after", "-" + r.weekly_loss_pct + "%");
    kv(kvb, "Pause after losses in a row", r.consecutive_loss_pause + " (for " + r.cooldown_min + " min)"); kv(kvb, "No entry when spread is above", r.max_spread_pips + " pips");
    c2.appendChild(kvb);
    c2.appendChild(el("div", "tm-note", "No strategy and no AI agent can change these. They are checked in code on every order; the Risk & health page shows how much of each is used."));
  } else c2.appendChild(el("div", "empty", "The limits are shown when the dashboard is started with a config."));
  basics.appendChild(c2); root.appendChild(basics);

  const live = d.configured.filter(s => s.enabled).concat(d.configured.filter(s => !s.enabled));
  for (const s of live) {
    const doc = d.docs[s.kind] || d.docs.flat, sec = el("section"), c = el("div", "card strat");
    const head = el("div", "strat-head");
    head.appendChild(el("h1", null, doc.name)); head.appendChild(el("span", "chip", doc.family));
    head.appendChild(el("span", "a-state " + (s.enabled ? "ok" : ""), s.enabled ? "trading on this account" : "configured, not enabled"));
    head.appendChild(el("span", "dim num", s.id)); c.appendChild(head);
    c.appendChild(el("p", "lead", doc.summary));
    const two = el("div", "grid g-2");
    const left = el("div"); left.appendChild(ruleBody(doc)); two.appendChild(left);
    const right = el("div"), cv = el("canvas", "diagram"); cv.setAttribute("aria-label", "Schematic of the rule: " + doc.summary); right.appendChild(cv);
    right.appendChild(el("div", "tm-note", "A schematic of the idea, not real prices.")); two.appendChild(right); diagrams.push([cv, doc.diagram]);
    c.appendChild(two);
    c.appendChild(el("h2", "sub", "Its settings on this account")); c.appendChild(paramTable(doc, s.params));
    c.appendChild(el("h2", "sub", "What that means in money right now")); c.appendChild(workedExample(s.params, d.risk, atr));
    const res = d.results[s.id]; c.appendChild(el("h2", "sub", "Results on this account so far"));
    if (res) { const k = el("div", "tm-cards");
      tmCard(k, "Closed trades", String(res.n), "", res.wins + " won"); tmCard(k, "Net result", signed(res.net) + " USD", tone(res.net), "after all costs");
      tmCard(k, "Average per trade", rTxt(res.mean_r), tone(res.mean_r), "R = multiples of the amount risked"); c.appendChild(k);
      const a = el("a", "btn linkbtn", "See these trades"); a.href = "#trades"; c.appendChild(a);
    } else c.appendChild(el("div", "empty", "No closed trade yet."));
    sec.appendChild(c); root.appendChild(sec);
  }
  if (!d.configured.length) { const sec = el("section"), c = card("Strategies"); c.appendChild(el("div", "empty", "The configured strategy is shown when the dashboard is started with a config.")); sec.appendChild(c); root.appendChild(sec); }

  // paper candidates, compared side by side
  const sec = el("section"), pc = card("Versions being tried on paper", "Paper test");
  pc.appendChild(el("div", "tm-note", d.common.paper + " Click a row to read the rule and its settings. With fewer than about 30 closed trades a result says very little."));
  if (!d.paper.length) pc.appendChild(el("div", "empty", "No paper test is running."));
  else {
    const t = el("table", "sortable"), h = el("tr"); for (const c of ["Version", "Rule", "Running since", "Closed", "Won", "Net USD", "Average R", "Open now"]) h.appendChild(el("th", null, c)); t.appendChild(h);
    for (const c of d.paper.slice().sort((a, b) => b.net_pnl - a.net_pnl)) {
      const doc = d.docs[c.kind] || d.docs.flat, r = el("tr", "click"); r.tabIndex = 0;
      const nm = el("td", "wrap"); nm.appendChild(el("b", null, c.bundle_id)); nm.appendChild(el("div", "dim small", c.label + (c.active ? "" : " (stopped)"))); r.appendChild(nm);
      r.appendChild(el("td", null, doc.name)); r.appendChild(el("td", "num dim", dateHm(c.created_ms))); r.appendChild(el("td", "num", String(c.closed)));
      r.appendChild(el("td", "num", c.closed ? pct(c.wins / c.closed) : "–")); r.appendChild(el("td", "num " + tone(c.net_pnl), signed(c.net_pnl)));
      r.appendChild(el("td", "num " + tone(c.mean_r), c.mean_r != null ? c.mean_r.toFixed(2) : "–")); r.appendChild(el("td", "num dim", c.open.length ? c.open[0].side + " @ " + f2(c.open[0].entry_price) : "none"));
      const more = el("tr", "more"), td = el("td"); td.colSpan = 8; more.appendChild(td); more.hidden = !ST.open.has(c.bundle_id);
      td.appendChild(el("p", "lead", doc.summary)); td.appendChild(ruleBody(doc)); td.appendChild(paramTable(doc, c.params));
      const flip = () => { more.hidden = !more.hidden; if (more.hidden) ST.open.delete(c.bundle_id); else ST.open.add(c.bundle_id); };
      r.addEventListener("click", flip); r.addEventListener("keydown", e => { if (e.key === "Enter") flip(); });
      t.appendChild(r); t.appendChild(more);
    }
    const sx = el("div", "scroll-x"); sx.appendChild(t); pc.appendChild(sx);
  }
  sec.appendChild(pc); root.appendChild(sec);
  redrawDiagrams(); annotate(root);
}
async function loadStrategies() {
  try { ST.data = await get("/api/strategies"); } catch (e) { return; }
  buildStrategies();
  // the live ATR comes with the first snapshot: draw the money example once it is there
  if (!(snap && snap.gateway) && !ST.retry) { ST.retry = true; setTimeout(() => { if (GT.page === "strategies") buildStrategies(); }, 2500); }
}

/* ====================================================================== Risk & health page */
const RK = {built: false};
const REFUSED = {max_positions: "A position was already open", risk_per_trade: "The stop was too far for the risk limit", cooldown: "Paused after losses in a row", confidence: "The AI was not confident enough", daily_loss: "Daily loss limit reached", weekly_loss: "Weekly loss limit reached", spread_above_cap: "Spread too wide", spread: "Spread too wide", kill_switch: "Kill switch on", paused: "Paused by owner", already_entered: "This decision had already entered", session_closed: "Market closed", drawdown: "Drawdown limit reached", incident_open: "An incident was open", news_window: "Inside a news window"};
const CHECK = {uptime: "Running in market hours", stops: "Every trade had a stop at the broker", crashes: "No crashes", incidents: "Incidents acknowledged", journal: "Every trade fully recorded", reconciliation: "Results match the broker's records", days: "Enough days of demo trading", trades: "Enough closed trades"};
function buildRisk() {
  const root = $("riskNew"); clear(root); RK.built = true;
  const s1 = el("section", "grid g-2");
  const m = card("Safety limits and how much of each is used"); m.appendChild(el("div")).id = "rkState"; m.appendChild(el("div", "meters")).id = "rkMeters"; s1.appendChild(m);
  const rf = card("Why entries were refused in the last 7 days"); rf.appendChild(el("div")).id = "rkRefused";
  rf.appendChild(el("div", "tm-note", "Each line is a rule doing its job: a signal arrived and code stopped it before any order was sent."));
  s1.appendChild(rf); root.appendChild(s1);
  const s2 = el("section", "grid g-2");
  const gl = card("Ready for a live account?", "Go-live checklist"); gl.appendChild(el("div")).id = "rkGolive"; s2.appendChild(gl);
  const pr = card("Processes"); pr.appendChild(el("div")).id = "rkProc"; pr.appendChild(el("h2", "sub", "Restarts by the watchdog and crashes")); pr.appendChild(el("div")).id = "rkExits"; s2.appendChild(pr);
  root.appendChild(s2);
  const s3 = el("section", "grid g-3");
  for (const [id, title] of [["rkAlerts", "Alerts sent to you"], ["rkInc", "Incidents"], ["rkCost", "What the AI calls cost"]]) { const c = card(title); c.appendChild(el("div", "scroll")).id = id; s3.appendChild(c); }
  root.appendChild(s3);
}
function drawRisk(r, h) {
  const st = $("rkState"); clear(st);
  const line = el("div", "pills left");
  line.appendChild(pill(r.reachable ? "gateway connected" : "gateway not reachable", r.reachable ? "ok" : "bad"));
  if (r.reachable) {
    line.appendChild(pill(r.entries_allowed ? "new entries allowed" : "new entries blocked", r.entries_allowed ? "ok" : "warn"));
    line.appendChild(pill(r.kill_switch ? "KILL SWITCH ON" : "kill switch off", r.kill_switch ? "bad" : "ok"));
    if (r.paused) line.appendChild(pill("paused by owner", "warn"));
    if (r.cooldown_until_ms) line.appendChild(pill("loss pause until " + hm(r.cooldown_until_ms), "warn"));
  }
  st.appendChild(line);
  const box = $("rkMeters"); clear(box);
  if (!r.meters.length) box.appendChild(el("div", "empty", "The limits are shown when the dashboard is started with a config."));
  for (const m of r.meters) {
    const row = el("div", "meter"), top = el("div", "meter-top");
    top.appendChild(el("span", "meter-k", m.label));
    const used = m.used == null ? "–" : (m.unit === "USD" ? f2(m.used) : String(m.used)), lim = m.limit == null ? "–" : (m.unit === "USD" ? f2(m.limit) : String(m.limit));
    top.appendChild(el("span", "num", used + " of " + lim + (m.unit ? " " + m.unit : "")));
    row.appendChild(top);
    const track = el("span", "bar-track big"), fill = el("i", m.pct == null ? "" : m.pct >= 0.9 ? "neg" : m.pct >= 0.6 ? "mid" : "pos");
    fill.style.width = (m.pct == null ? 0 : Math.max(1.5, m.pct * 100)) + "%"; track.appendChild(fill); row.appendChild(track);
    row.appendChild(el("div", "dim small", m.text)); box.appendChild(row);
  }
  const rf = $("rkRefused"); clear(rf);
  if (!r.refused.length) rf.appendChild(el("div", "empty", "No entry was refused in the last 7 days."));
  else { const top = r.refused[0][1], b = el("div", "bars");
    for (const [code, n] of r.refused) { const row = el("div", "bar-row"); row.appendChild(el("span", "bar-k", REFUSED[code] || code.replace(/_/g, " "))); const t = el("span", "bar-track"), i = el("i", "mid"); i.style.width = Math.max(2, n / top * 100) + "%"; t.appendChild(i); row.appendChild(t); row.appendChild(el("span", "bar-v num", String(n))); b.appendChild(row); }
    rf.appendChild(b); }

  const gl = $("rkGolive"); clear(gl);
  if (!h.golive) gl.appendChild(el("div", "empty", "The checklist is worked out every 15 minutes by the brain; it will appear shortly."));
  else { const pass = h.golive.checks.filter(c => c.ok).length;
    gl.appendChild(el("div", "lead", pass + " of " + h.golive.checks.length + " checks pass. These are the project's own conditions for the end of demo trading; going live is still your decision."));
    for (const c of h.golive.checks) { const row = el("div", "check " + (c.ok ? "ok" : "no")); row.appendChild(el("span", "check-mark", c.ok ? "PASS" : "NOT YET")); const x = el("div"); x.appendChild(el("b", null, CHECK[c.name] || c.name)); x.appendChild(el("div", "dim small", c.detail)); row.appendChild(x); gl.appendChild(row); } }

  const pr = $("rkProc"); clear(pr); const pt = el("table"), ph = el("tr"); for (const c of ["Process", "State", "Last sign of life"]) ph.appendChild(el("th", null, c)); pt.appendChild(ph);
  for (const p of h.processes) { const row = el("tr"); row.appendChild(el("td", null, p.process)); row.appendChild(el("td", p.stale ? "down" : "up", p.stale ? "not responding" : p.status)); row.appendChild(el("td", "num dim", Math.round(p.age_s) + " s ago")); pt.appendChild(row); }
  pr.appendChild(pt);
  const ex = $("rkExits"); clear(ex);
  if (!h.exits.length) ex.appendChild(el("div", "empty", "None recorded."));
  for (const e of h.exits) { const row = el("div", "row " + (e.reason === "stopped" || e.reason === "exited" ? "" : "bad")); row.appendChild(el("span", "t", dateHm(e.exited_ms))); const x = el("span", "x"); x.appendChild(el("span", "who", e.process)); x.appendChild(document.createTextNode(e.reason || "ended")); row.appendChild(x); ex.appendChild(row); }

  const list = (id, items, empty, make) => { const b = $(id); clear(b); if (!items.length) b.appendChild(el("div", "empty", empty)); for (const it of items) b.appendChild(make(it)); };
  list("rkAlerts", h.alerts, "No alerts yet.", a => { const row = el("div", "row " + (a.severity === "critical" ? "bad" : a.severity === "warning" ? "warn" : "")); row.appendChild(el("span", "t", dateHm(a.created_ms))); const x = el("span", "x"); x.appendChild(el("span", "who", a.kind.replace(/_/g, " "))); x.appendChild(document.createTextNode(a.message)); row.appendChild(x); return row; });
  list("rkInc", h.incidents, "No incident has ever been opened.", i => { const row = el("div", "row " + (i.closed_ms ? "" : "bad")); row.appendChild(el("span", "t", dateHm(i.opened_ms))); const x = el("span", "x"); x.appendChild(el("span", "who", i.kind.replace(/_/g, " "))); x.appendChild(document.createTextNode(i.summary + (i.closed_ms ? " (closed " + dateHm(i.closed_ms) + ")" : " (OPEN)"))); row.appendChild(x); return row; });
  list("rkCost", h.ai_cost, "No AI calls in the last 7 days.", d => { const w = el("div", "costday"); const top = el("div", "meter-top"); top.appendChild(el("b", null, d.day)); top.appendChild(el("span", "num", "$" + d.cost.toFixed(2) + " · " + d.calls + " calls" + (d.failed ? ", " + d.failed + " failed" : ""))); w.appendChild(top); w.appendChild(el("div", "dim small", d.agents.slice(0, 5).map(a => a.agent.replace(/_/g, " ") + " $" + a.cost.toFixed(2)).join(" · "))); return w; });
}
async function loadRisk() {
  if (!RK.built) buildRisk();
  try { const [r, h] = await Promise.all([get("/api/risk"), get("/api/health")]); drawRisk(r, h); annotate($("page-risk")); } catch (e) { /* the banner covers a dead server */ }
}

/* ====================================================================== Overview: live chart and summary */
const LV = {ch: null, series: null, marks: null, lines: [], key: "", tf: null};
drawChart = function () {   // replaces the hand-drawn canvas chart of legacy.js
  const host = $("liveChart");
  if (!host || !host.clientWidth || !window.LightweightCharts || !candles.length) return;
  if (!LV.ch) {
    LV.ch = makeChart(host);
    // The chart keeps its bar width when its box grows (a wider window, cards loading beside it),
    // which left the left third empty. With both edges fixed it cannot show space before the
    // first candle or after the last: it widens the bars to fill the box instead.
    LV.ch.applyOptions({timeScale: {fixLeftEdge: true, fixRightEdge: true}});
    LV.series = LV.ch.addSeries(LightweightCharts.CandlestickSeries, {upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down"), priceLineColor: css("--gold")});
    LV.marks = LightweightCharts.createSeriesMarkers(LV.series, []);
    LV.ch.subscribeCrosshairMove(p => {
      const c = p && p.seriesData ? p.seriesData.get(LV.series) : null, last = candles[candles.length - 1];
      $("ohlc").textContent = c ? "O " + f2(c.open) + "  H " + f2(c.high) + "  L " + f2(c.low) + "  C " + f2(c.close) : "O " + f2(last[1]) + "  H " + f2(last[2]) + "  L " + f2(last[3]) + "  C " + f2(last[4]);
    });
  }
  const span = minutes * 60, bar = c => ({time: c[0] + TZ_S, open: c[1], high: c[2], low: c[3], close: c[4]});
  const key = minutes + ":" + candles[0][0] + ":" + candles.length;
  if (key !== LV.key) { LV.series.setData(candles.map(bar)); if (LV.tf !== minutes) { LV.ch.timeScale().setVisibleLogicalRange({from: Math.max(0, candles.length - 240), to: candles.length}); LV.tf = minutes; } LV.key = key; }
  const last = bar(candles[candles.length - 1]), bid = snap && snap.gateway ? snap.gateway.bid : null;
  if (bid != null && Date.now() / 1000 - candles[candles.length - 1][0] < span * 2) { last.close = bid; last.high = Math.max(last.high, bid); last.low = Math.min(last.low, bid); }
  LV.series.update(last);
  for (const l of LV.lines) LV.series.removePriceLine(l);
  LV.lines = [];
  for (const p of (snap ? snap.positions : [])) {
    for (const [price, title, col] of [[p.price_open, "entry", css("--info")], [p.sl, "stop", css("--down")], [p.tp, "target", css("--up")]]) {
      if (price) LV.lines.push(LV.series.createPriceLine({price, color: col, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title}));
    }
  }
  const t0 = candles[0][0] * 1000, t1 = candles[candles.length - 1][0] * 1000 + span * 1000, marks = [];
  const snapT = ms => { const s = Math.floor(ms / 1000); return s - s % span + TZ_S; };
  for (const t of (snap ? snap.trades.recent : [])) {
    const long = t.side === "long";
    if (t.entry_ms >= t0 && t.entry_ms <= t1) marks.push({time: snapT(t.entry_ms), position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: css("--info"), text: long ? "buy" : "sell"});
    if (t.exit_ms >= t0 && t.exit_ms <= t1) marks.push({time: snapT(t.exit_ms), position: long ? "aboveBar" : "belowBar", shape: long ? "arrowDown" : "arrowUp", color: t.net_pnl > 0 ? css("--up") : css("--down"), text: signed(t.net_pnl)});
  }
  marks.sort((a, b) => a.time - b.time); LV.marks.setMarkers(marks);
};
const DECISION = {sent: "It opened a trade", wait: "It is waiting: the rule gave no signal, or the AI said wait", gated: "A signal was stopped by a code check", blocked: "Entries are blocked right now", flat: "The AI was unavailable, so nothing was proposed", gateway_unavailable: "The gateway could not be reached", error: "The decision cycle failed", running: "A decision is being made"};
const DECISION_SHORT = {sent: "Entered", wait: "Waiting", gated: "Stopped by a check", blocked: "Entries blocked", flat: "AI unavailable", gateway_unavailable: "Gateway down", error: "Cycle failed", running: "Thinking…"};
function stat(box, k, v, cls, sub, tip) {
  const c = el("div", "stat"); c.appendChild(el("div", "k", k)); const vv = el("div", "v " + (cls || ""), v); vv.title = v; c.appendChild(vv);
  const ss = el("div", "s", sub || ""); ss.title = tip || sub || ""; c.appendChild(ss); box.appendChild(c);
}
function drawOverview() {
  const root = $("overviewNew"); if (!root || !snap) return;
  let k = $("ovCards"); if (!k) { k = el("div", "statbar"); k.id = "ovCards"; root.appendChild(k); }
  clear(k);
  const g = snap.gateway, p = snap.positions[0], c = snap.cycles[0], ev = snap.calendar.find(e => e.impact === "high") || snap.calendar[0], all = snap.trades.all, today = snap.trades.today;
  stat(k, "Equity", g && g.equity != null ? f2(g.equity) : "–", "", g ? "balance " + f2(g.balance) + " USD" : "gateway not reachable");
  stat(k, "Day P&L", g ? signed(g.day_pnl) : "–", g ? tone(g.day_pnl) : "", today.n + (today.n === 1 ? " trade" : " trades") + " closed today, " + signed(today.net_pnl) + " USD");
  if (p) { const long = p.side === "long" || p.side === "buy", px = g ? (long ? g.bid : g.ask) : null;
    stat(k, "Open position", (long ? "Long " : "Short ") + p.volume + " @ " + f2(p.price_open), tone(p.profit), (p.profit != null ? signed(p.profit) + " USD now" : "") + (px != null && p.sl ? " · stop " + f2(Math.abs(px - p.sl)) + " away" : ""));
  } else stat(k, "Open position", "Flat", "", g && g.entries_allowed ? "waiting for a signal" : "new entries are blocked");
  // "flat" has two causes; say which. The usual one is the daily AI budget, which is not a fault.
  const spent = c && c.status === "flat" && /budget/.test(c.gate_reason || "");
  const why = !c ? "" : spent ? "Today's AI budget is used up (" + c.gate_reason + "). The AI decides again after midnight UTC. The open position keeps its stop and target."
    : (DECISION[c.status] || c.status) + (c.gate_reason && (c.status === "gated" || c.status === "flat") ? " (" + (REFUSED[c.gate_reason] || c.gate_reason) + ")" : "");
  stat(k, "AI decision", !c ? "–" : spent ? "AI budget used up" : (DECISION_SHORT[c.status] || c.status), "ai", c ? hm(c.started_ms) + " · " + why : "no decision yet", why);
  stat(k, "Track record", all.n ? (all.hit_rate != null ? Math.round(all.hit_rate * 100) + "% wins" : all.n + " trades") : "–", all.n ? tone(all.net_pnl) : "", all.n ? all.n + " trades · net " + signed(all.net_pnl) + " USD" : "no closed trades yet");
  stat(k, "Next event", ev ? dateHm(ev.time_ms) : "none", ev && ev.impact === "high" ? "gold" : "", ev ? ev.title : "", ev ? ev.title + (ev.impact === "high" ? " (high impact: entries pause around it)" : "") : "");
}
const _legacyRender = render;
render = function () { _legacyRender(); if (GT.page === "overview") { drawOverview(); drawControls(); } };

/* ====================================================================== Help page and tooltips */
const HP = {data: null, q: ""};
function annotate(root) {
  if (!HP.data) return;
  const keys = {}; for (const t of Object.keys(HP.data.terms)) keys[t.toLowerCase()] = t;
  for (const n of (root || document).querySelectorAll("h2, th, .tmc .k, .meter-k, .bar-k")) {
    if (n.dataset.term || n.childElementCount) continue;
    const hit = keys[n.textContent.replace(/[▲▼]/g, "").trim().toLowerCase()];
    if (hit) { n.dataset.term = hit; n.tabIndex = 0; }
  }
}
function drawHelp() {
  const root = $("helpNew"), d = HP.data; if (!d) return; clear(root);
  const s1 = el("section"), w = card("How a trade happens, from price to result"), ol = el("ol", "steps");
  for (const [title, text] of d.walkthrough) { const li = el("li"); li.appendChild(el("b", null, title)); li.appendChild(el("span", null, text)); ol.appendChild(li); }
  w.appendChild(ol); s1.appendChild(w); root.appendChild(s1);
  const s2 = el("section"), gcard = card("What the words mean");
  const inp = el("input", "sel wide"); inp.type = "search"; inp.placeholder = "Search a word, for example spread"; inp.setAttribute("aria-label", "Search the glossary"); inp.value = HP.q;
  const list = el("div", "gloss");
  const fill = () => { clear(list); const q = HP.q.trim().toLowerCase();
    const hits = Object.entries(d.terms).filter(([k, v]) => !q || k.toLowerCase().includes(q) || v.toLowerCase().includes(q)).sort((a, b) => a[0].localeCompare(b[0]));
    if (!hits.length) list.appendChild(el("div", "empty", "No word matches. Try the Assistant page for anything not listed here."));
    for (const [k, v] of hits) { const it = el("div", "gl"); it.appendChild(el("b", null, k)); it.appendChild(el("span", null, v)); list.appendChild(it); } };
  inp.addEventListener("input", () => { HP.q = inp.value; fill(); });
  gcard.appendChild(inp); gcard.appendChild(list); fill(); s2.appendChild(gcard); root.appendChild(s2);
  root.appendChild(el("div", "tm-note", "On every page, a word with a dotted underline shows its meaning when you point at it. Charts: TradingView Lightweight Charts, Apache License 2.0."));
}
get("/api/glossary").then(g => { HP.data = g; GT.glossary = g.terms; annotate(document); if (GT.page === "help") drawHelp(); }).catch(() => {});

/* ====================================================================== Owner controls (D-083) */
const CT = {on: false, instance: "demo", timer: null, recent: []};
const CMD_NAME = {pause: "Pause entries", resume: "Resume entries", kill: "Kill switch", close: "Close position", breakeven: "Stop to entry"};
const CMD_RESULT = {done: "Done", refused: "Refused", failed: "Failed", expired: "Not carried out: it waited too long", queued: "Waiting for the system", working: "Being carried out"};
async function postCmd(body) {
  const r = await fetch("/api/command", {method: "POST", headers: {"Content-Type": "application/json", "X-Dashboard": "1"}, body: JSON.stringify(body)});
  let out = {}; try { out = await r.json(); } catch (e) { /* no body */ }
  return [r.status, out];
}
function cmdText(kind, pos) {
  const g = snap && snap.gateway, long = pos && pos.side === "long", px = g && pos ? (long ? g.bid : g.ask) : null;
  if (kind === "pause") return "New entries will be blocked. An open trade keeps its stop and its target. You can resume from this page.";
  if (kind === "resume") return "New entries will be allowed again.";
  if (kind === "kill") return "Every open position is closed at the market price now, pending orders are cancelled and trading stops. It can be switched back on only from Telegram.";
  if (kind === "close") return "Close the " + pos.side + " of " + pos.volume + " lot opened at " + f2(pos.price_open) + " at the market price" + (px != null ? ", now about " + f2(px) + ": about " + signed((long ? px - pos.price_open : pos.price_open - px) * pos.volume * 100) + " USD before costs." : ".");
  return "Move the stop of the " + pos.side + " from " + f2(pos.sl) + " to its entry price " + f2(pos.price_open) + ". This is refused unless the price is on the winning side of the entry, because a stop may only move toward profit.";
}
function closeSheet() { clearInterval(CT.timer); const s = $("sheet"); if (s) s.remove(); $("scrim").hidden = !$("drawer").hidden ? false : true; }
async function startCmd(kind, positionId) {
  const [code, prep] = await postCmd({step: "prepare", kind, position_id: positionId == null ? undefined : positionId});
  if (code !== 200) { alertSheet("That cannot be done right now", code === 404 ? "That position is no longer open." : code === 503 ? "Commands are switched off for this dashboard." : "The dashboard server refused the request (" + code + ")."); return; }
  closeSheet();
  const sh = el("div", "sheet" + (prep.instance === "live" ? " live" : "")); sh.id = "sheet"; sh.setAttribute("role", "dialog"); sh.setAttribute("aria-modal", "true");
  if (prep.instance === "live") sh.appendChild(el("div", "sheet-live", "LIVE ACCOUNT: this acts on real money"));
  sh.appendChild(el("h1", null, CMD_NAME[kind] + "?"));
  sh.appendChild(el("p", "lead", cmdText(kind, prep.position)));
  let typed = null;
  if (kind === "kill") { typed = el("input", "sel wide"); typed.placeholder = "Type KILL to confirm"; typed.setAttribute("aria-label", "Type KILL to confirm"); typed.autocomplete = "off"; sh.appendChild(typed); }
  const left = el("div", "dim small"); sh.appendChild(left);
  const row = el("div", "sheet-btns"), cancel = el("button", "btn", "Cancel"), ok = el("button", "btn danger", "Confirm: " + CMD_NAME[kind].toLowerCase());
  cancel.type = ok.type = "button"; row.appendChild(cancel); row.appendChild(ok); sh.appendChild(row);
  document.body.appendChild(sh); $("scrim").hidden = false; (typed || cancel).focus();
  const tick = () => { const s = Math.max(0, Math.ceil((prep.expires_ms - (snap ? snap.now_ms + (Date.now() - lastOk) : Date.now())) / 1000)); left.textContent = s > 0 ? "This confirmation is valid for " + s + " more seconds." : "This confirmation has expired. Close it and start again."; ok.disabled = s <= 0 || (typed && typed.value !== "KILL"); };
  tick(); CT.timer = setInterval(tick, 500); if (typed) typed.addEventListener("input", tick);
  cancel.addEventListener("click", closeSheet);
  ok.addEventListener("click", async () => {
    ok.disabled = true; clearInterval(CT.timer);
    const [c2, done] = await postCmd({step: "confirm", token: prep.token, typed: typed ? typed.value : undefined});
    if (c2 !== 200) { left.textContent = c2 === 429 ? "Two commands are already waiting. Try again in a moment." : "Not sent: the confirmation was no longer valid. Close this and start again."; return; }
    row.removeChild(ok); cancel.textContent = "Close"; left.textContent = "Sent. Waiting for the system to carry it out…";
    let n = 0; CT.timer = setInterval(async () => {
      n++; let list = []; try { list = await get("/api/commands"); } catch (e) { /* keep waiting */ }
      const me = list.find(x => x.id === done.id);
      if (me && me.status !== "queued" && me.status !== "working") { clearInterval(CT.timer); left.className = "small " + (me.status === "done" ? "up" : "down"); left.textContent = CMD_RESULT[me.status] + (me.detail && me.status !== "done" ? ": " + (REFUSED[me.detail] || me.detail.replace(/_/g, " ")) : "") + "."; loadSnap(); drawControls(list); }
      else if (n > 25) { clearInterval(CT.timer); left.textContent = "No answer yet. The brain may not be running; an unanswered command is dropped after one minute."; }
    }, 1000);
  });
}
function alertSheet(title, text) {
  closeSheet(); const sh = el("div", "sheet"); sh.id = "sheet"; sh.appendChild(el("h1", null, title)); sh.appendChild(el("p", "lead", text));
  const row = el("div", "sheet-btns"), b = el("button", "btn", "Close"); b.type = "button"; b.addEventListener("click", closeSheet); row.appendChild(b); sh.appendChild(row);
  document.body.appendChild(sh); $("scrim").hidden = false; b.focus();
}
function drawControls(list) {
  if (!CT.on || !snap) return;
  if (list) CT.recent = list;
  const root = $("ctlSlot") || $("overviewNew"); let c = $("ctlCard");
  if (!c) { c = card("Controls"); c.id = "ctlCard"; c.classList.add("controls"); root.appendChild(c); }
  const g = snap.gateway;
  // rebuild only when something the card shows has changed, so a button keeps its focus
  const sig = JSON.stringify([!!g, g && g.paused, g && g.kill_switch, snap.positions.map(p => [p.ticket != null ? p.ticket : p.position_id, p.side]), CT.recent.slice(0, 4).map(r => [r.id, r.status])]);
  if (c.dataset.sig === sig) return;
  c.dataset.sig = sig;
  while (c.childElementCount > 1) c.removeChild(c.lastChild);
  if (!g) { c.appendChild(el("div", "empty", "The gateway is not reachable, so commands cannot be carried out right now. The kill switch also works from Telegram.")); return; }
  c.appendChild(el("div", "tm-note", "Each button asks you to confirm before anything happens. None of them can open a trade or change a limit."));
  const row = el("div", "ctl-row"), btn = (label, cls, fn) => { const b = el("button", "btn " + cls, label); b.type = "button"; b.addEventListener("click", fn); row.appendChild(b); return b; };
  if (g.paused) btn("Resume entries", "go", () => startCmd("resume")); else btn("Pause entries", "", () => startCmd("pause"));
  for (const p of snap.positions) { const id = p.ticket != null ? p.ticket : p.position_id; btn("Close the " + p.side, "", () => startCmd("close", id)); btn("Stop to entry", "", () => startCmd("breakeven", id)); }
  const k = btn("Kill switch", "danger", () => startCmd("kill")); k.disabled = !!g.kill_switch; if (g.kill_switch) k.textContent = "Kill switch is on";
  c.appendChild(row);
  if (CT.recent.length) { const box = el("div", "ctl-log");
    for (const r of CT.recent.slice(0, 4)) box.appendChild(el("div", "small " + (r.status === "done" ? "dim" : r.status === "queued" || r.status === "working" ? "" : "down"), dateHm(r.created_ms) + "  " + CMD_NAME[r.kind] + ": " + CMD_RESULT[r.status].toLowerCase() + (r.detail && r.status !== "done" ? " (" + (REFUSED[r.detail] || r.detail.replace(/_/g, " ")) + ")" : "")));
    c.appendChild(box); }
}

/* ---------- panels that moved to their own page ---------- */
(function movePanels() {
  const chat = document.querySelector(".card.chat"), a = $("assistantNew");
  if (chat && a) { const grid = chat.parentElement; a.appendChild(chat); if (grid) grid.classList.remove("g-live"); }
  const cot = $("cotHead"), research = $("page-research");
  if (cot && research) { const c = cot.closest(".card"), grid = c.parentElement, sec = el("section"); sec.appendChild(c); research.appendChild(sec); grid.classList.remove("g-live"); }
})();

/* ---------- wiring ---------- */
GT.hooks.overview = () => { drawOverview(); drawControls(); };
GT.hooks.assistant = () => { const m = $("msgs"); if (m) m.scrollTop = m.scrollHeight; };
GT.hooks.help = () => drawHelp();
GT.hooks.agents = () => annotate($("page-agents"));
GT.hooks.research = () => annotate($("page-research"));
GT.hooks.risk = () => loadRisk();
setInterval(() => { if (GT.page === "risk" && !document.hidden) loadRisk(); }, 10000);
GT.hooks.strategies = () => loadStrategies();
window.addEventListener("resize", redrawDiagrams);
setInterval(() => { if (GT.page === "strategies" && !document.hidden) loadStrategies(); }, 60000);
get("/api/meta").then(m => { const b = $("inst"); b.textContent = m.instance === "live" ? "LIVE ACCOUNT" : "DEMO ACCOUNT"; b.className = "inst " + (m.instance === "live" ? "live" : "demo"); document.body.dataset.instance = m.instance; CT.on = !!m.commands; CT.instance = m.instance; if (CT.on) get("/api/commands").then(l => drawControls(l)).catch(() => {}); }).catch(() => {});
GT.hooks.trades = () => loadTrades();
setInterval(() => { if (GT.page === "trades" && !document.hidden && $("drawer").hidden) loadTrades(); }, 30000);
$("drawerClose").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", () => { if ($("sheet")) closeSheet(); else closeDrawer(); });
document.addEventListener("keydown", e => { if (e.key !== "Escape") return; if ($("sheet")) closeSheet(); else if (!$("drawer").hidden) closeDrawer(); });
document.getElementById("themeBtn").addEventListener("click", () => setTimeout(rethemeCharts, 0));
// the system light/dark setting can change while the page is open; charts read colours only when told
if (window.matchMedia) matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { rethemeCharts(); redrawDiagrams(); });
document.getElementById("themeBtn").addEventListener("click", () => setTimeout(redrawDiagrams, 0));
/* ---------- Overview tabs: AI feed, closed trades, events, news ---------- */
for (const b of document.querySelectorAll(".tabs [data-tab]")) b.addEventListener("click", () => {
  for (const x of document.querySelectorAll(".tabs [data-tab]")) { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b ? "true" : "false"); }
  for (const pnl of document.querySelectorAll(".tab-body [data-panel]")) pnl.hidden = pnl.dataset.panel !== b.dataset.tab;
  $("tradeSum").hidden = b.dataset.tab !== "trades";
});
$("tradeSum").hidden = true;

/* ---------- explanations: off by default; every card with a note gets an (i) that shows it ---------- */
function setExplain(on) {
  document.body.classList.toggle("explain", on);
  const b = $("explainBtn"); b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
  try { localStorage.setItem("gt-explain", on ? "1" : "0"); } catch (e) { /* private mode */ }
}
function infoIcons() {
  for (const n of document.querySelectorAll(".tm-note")) {
    const host = n.closest(".card, section"); if (!host) continue;
    const h = host.querySelector("h2"); if (!h) continue;
    let i = h.querySelector(".info");
    if (!i) { i = el("span", "info", "i"); i.tabIndex = 0; i.dataset.term = "About this"; i.setAttribute("aria-label", "About this panel"); h.appendChild(i); }
    if (i.dataset.tip !== n.textContent) i.dataset.tip = n.textContent;
  }
}
let infoQueued = false;
new MutationObserver(() => { if (!infoQueued) { infoQueued = true; requestAnimationFrame(() => { infoQueued = false; infoIcons(); }); } })
  .observe(document.body, {childList: true, subtree: true});
$("explainBtn").addEventListener("click", () => setExplain(!document.body.classList.contains("explain")));
try { setExplain(localStorage.getItem("gt-explain") === "1"); } catch (e) { setExplain(false); }
gtBoot();
