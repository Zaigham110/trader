"use strict";
/* Research lab page (D-085): the funnel of rules tested on history, the ones that got furthest,
   where rules fail and what was learned. Read-only. Helpers come from legacy.js and pages.js. */
const LB = {built: false, open: new Set()};
const LAB_GATE = [
  ["trades", "Enough trades", "At least 100 trades on history, and 25 in each of three periods. Fewer says nothing either way."],
  ["periods", "Three periods", "A positive average result and a positive net in each of three separate periods."],
  ["robust", "Robust", "Still positive with the spread 1.5 times wider and with the stop and target moved 20% each way."],
  ["luck", "Beats luck", "Four checks: better than random entries in the same hours in all 20 runs; the entry alone beats matched random bars; the average stays above zero when whole trading days are resampled; and the result is still credible after counting every rule ever tried."],
  ["blind", "Blind test", "Positive on prices no experiment has touched. Only data from 24 November 2026 onward counts."],
];
const LAB_SOURCE = {agent: "AI agent", optimiser: "made by code", owner: "owner", seed: "seed"};
const LAB_STATUS = {passed: "passed all five", awaiting_blind: "waiting for blind data", failed: "stopped", proposed: "being tested", error: "error"};

function buildLab() {
  const root = $("labNew"); clear(root); LB.built = true;
  const s0 = el("section"), how = card("How a rule is judged");
  how.appendChild(el("div", "tm-note", "The lab proposes trading rules and tests each one on stored price history. A rule must pass five gates in order; the thresholds are fixed in code and no agent can change them. A rule that passes four goes to paper trading. Moving one to the demo account is always your decision."));
  const ol = el("ol", "steps"); for (const [, name, text] of LAB_GATE) { const li = el("li"); li.appendChild(el("b", null, name)); li.appendChild(el("span", null, text)); ol.appendChild(li); }
  how.appendChild(ol); s0.appendChild(how); root.appendChild(s0);
  root.appendChild(el("div", "tm-cards")).id = "lbFunnel";
  const s1 = el("section", "grid g-top");
  const best = card("Rules that got furthest"); best.appendChild(el("div", "tm-note", "Results are the average per trade in R (multiples of the amount risked) in each of the three periods. Click a rule for its reasoning and its numbers.")); best.appendChild(el("div", "scroll-x")).id = "lbBest"; s1.appendChild(best);
  const side = el("div");
  const fails = card("Where rules stop"); fails.appendChild(el("div")).id = "lbFails"; side.appendChild(fails);
  const runs = card("Sessions"); runs.classList.add("gap-top"); runs.appendChild(el("div", "scroll")).id = "lbRuns"; side.appendChild(runs);
  s1.appendChild(side); root.appendChild(s1);
  const s2 = el("section"), les = card("What the lab has learned"); les.appendChild(el("div", "scroll")).id = "lbLessons"; s2.appendChild(les); root.appendChild(s2);
}
function drawLab(d) {
  const f = d.funnel, k = $("lbFunnel"); clear(k);
  tmCard(k, "Rules proposed", String(f.proposed), "", f.tested + " tested" + (d.running ? ", a session is running now" : ""));
  let prev = f.tested;
  for (const [key, name] of LAB_GATE) { tmCard(k, name, String(f[key]), f[key] ? "up" : "", prev ? "of " + prev + " that reached this gate" : "none reached this gate"); prev = f[key]; }
  tmCard(k, "On paper", String(f.on_paper), f.on_paper ? "up" : "", f.awaiting_blind + " waiting for blind data");

  const fb = $("lbFails"); clear(fb);
  if (!d.fails.length) fb.appendChild(el("div", "empty", "No rule has been tested yet."));
  else { const top = d.fails[0][1], b = el("div", "bars"), names = Object.fromEntries(LAB_GATE.map(g => [g[0], g[1]]));
    for (const [gate, n] of d.fails) { const row = el("div", "bar-row"); row.appendChild(el("span", "bar-k", names[gate] || gate)); const t = el("span", "bar-track"), i = el("i", "mid"); i.style.width = Math.max(2, n / top * 100) + "%"; t.appendChild(i); row.appendChild(t); row.appendChild(el("span", "bar-v num", String(n))); b.appendChild(row); }
    fb.appendChild(b);
    const src = Object.entries(d.by_source).map(([s, v]) => (LAB_SOURCE[s] || s) + ": " + v.n + " proposed, " + v.past_periods + " past three periods").join(" · ");
    fb.appendChild(el("div", "tm-note", src)); }

  const bb = $("lbBest"); clear(bb);
  if (!d.best.length) bb.appendChild(el("div", "empty", "Nothing tested yet. The lab runs every night during the market break."));
  else {
    const t = el("table", "sortable"), h = el("tr"); for (const c of ["#", "Rule", "From", "Gates passed", "Period A", "Period B", "Period C"]) h.appendChild(el("th", null, c)); t.appendChild(h);
    for (const x of d.best) {
      const r = el("tr", "click"), res = x.results || {}; r.tabIndex = 0;
      r.appendChild(el("td", "num dim", String(x.id))); r.appendChild(el("td", "wrap", x.what)); r.appendChild(el("td", "dim", LAB_SOURCE[x.source] || x.source));
      r.appendChild(el("td", x.gate >= 4 ? "up" : "", x.gate + " of 5" + (x.failed_at ? ", stopped at " + (LAB_GATE.find(g => g[0] === x.failed_at) || [0, x.failed_at])[1].toLowerCase() : ", " + (LAB_STATUS[x.status] || x.status))));
      for (let i = 0; i < 3; i++) { const p = (res.periods || [])[i]; r.appendChild(el("td", "num " + (p ? tone(p.mean_r) : "dim"), p ? rTxt(p.mean_r) + " (" + p.n + ")" : "–")); }
      const more = el("tr", "more"), td = el("td"); td.colSpan = 7; more.appendChild(td); more.hidden = !LB.open.has(x.id);
      if (x.rationale) para(td, "Why it was proposed:", x.rationale);
      if (x.expectation) para(td, "What was expected:", x.expectation);
      if (res.why) para(td, "Outcome:", res.why + ".");
      if (res.robust) para(td, "Net result under stress (USD):", Object.entries(res.robust).map(([a, b]) => a + " " + signed(b)).join(" · "));
      if (res.entry && res.entry.horizons) { const hs = Object.entries(res.entry.horizons).map(([h, v]) => h + " min: " + (v.mean_atr >= 0 ? "+" : "") + v.mean_atr.toFixed(2) + " ATR against " + (v.random_mean_atr >= 0 ? "+" : "") + v.random_mean_atr.toFixed(2) + " for random (p " + v.p.toFixed(2) + ")").join(" · "); if (hs) para(td, "The entry alone, after the spread:", hs + "."); }
      if (res.luck) para(td, "Against luck:", "beat random entries in " + res.luck.beats_random + " of " + res.luck.of + " runs (needs all)" + (res.luck.entry_p != null ? "; entry-only p " + res.luck.entry_p.toFixed(2) + " (needs 0.05 or less)" : "") + (res.luck.by_day_lower_r != null ? "; by-day lower bound " + (res.luck.by_day_lower_r >= 0 ? "+" : "") + res.luck.by_day_lower_r.toFixed(2) + " R over " + res.luck.trading_days + " days (needs above 0)" : "") + "; credibility after counting " + res.luck.rules_counted + " rules tried: " + res.luck.deflated_sharpe.toFixed(2) + " (needs 0.90).");
      if (res.blind) para(td, "Blind data:", res.blind.n + " trades, " + rTxt(res.blind.mean_r) + " per trade.");
      if (x.paper_bundle) para(td, "On paper as:", x.paper_bundle);
      if (x.lesson) para(td, "Lesson:", x.lesson);
      const flip = () => { more.hidden = !more.hidden; if (more.hidden) LB.open.delete(x.id); else LB.open.add(x.id); };
      r.addEventListener("click", flip); r.addEventListener("keydown", e => { if (e.key === "Enter") flip(); });
      t.appendChild(r); t.appendChild(more);
    }
    bb.appendChild(t);
  }
  const rb = $("lbRuns"); clear(rb);
  if (!d.runs.length) rb.appendChild(el("div", "empty", "No session yet."));
  for (const r of d.runs) { const w = el("div", "costday"), top = el("div", "meter-top"); top.appendChild(el("b", null, dateHm(r.started_ms))); top.appendChild(el("span", "num", r.status === "running" ? "running" : r.finished_ms ? Math.round((r.finished_ms - r.started_ms) / 60000) + " min" : r.status)); w.appendChild(top);
    w.appendChild(el("div", "dim small", r.tested + " tested, " + r.passed_gate2 + " past three periods, " + r.passed_gate4 + " past four gates" + (r.cost_usd ? " · AI $" + r.cost_usd.toFixed(2) : ""))); rb.appendChild(w); }
  const lb = $("lbLessons"); clear(lb);
  if (!d.lessons.length) lb.appendChild(el("div", "empty", "Lessons are written by the research agent after each session. The agent is switched off, so the lab is testing code-made rules only."));
  for (const l of d.lessons) { const row = el("div", "row"); row.appendChild(el("span", "t", "#" + l.id)); const x = el("span", "x"); x.appendChild(document.createTextNode(l.lesson)); x.appendChild(el("div", "dim small", l.what)); row.appendChild(x); lb.appendChild(row); }
  if (typeof annotate === "function") annotate($("page-lab"));
}
async function loadLab() {
  if (!LB.built) buildLab();
  let d; try { d = await get("/api/lab"); } catch (e) { return; }
  if (d && d.funnel) drawLab(d);
  else { clear($("lbFunnel")); $("lbFunnel").appendChild(el("div", "empty", "The lab has not run yet.")); }
}
GT.hooks.lab = () => loadLab();
setInterval(() => { if (GT.page === "lab" && !document.hidden) loadLab(); }, 30000);
// this file loads after the first page was shown: show the menu entry, and the page if asked for
gtShow(location.hash.slice(1) === "lab" ? "lab" : GT.page);
