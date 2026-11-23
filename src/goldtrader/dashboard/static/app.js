"use strict";
/* Shell of the dashboard (D-083): pages, navigation, theme, tooltips. The panels that existed
   before the redesign are still drawn by legacy.js; new pages are added here. */
const GT = {
  page: "overview",
  hooks: {},          // page key -> function called when the page is shown
  glossary: {},       // term -> plain explanation, filled from /api/glossary
  ready(key) {
    const p = document.getElementById("page-" + key);
    if (!p) return false;
    return p.querySelector("section") !== null || key in GT.hooks;
  },
};

function gtShow(key) {
  const pages = [...document.querySelectorAll(".page")];
  if (!pages.some(p => p.dataset.page === key) || !GT.ready(key)) key = "overview";
  GT.page = key;
  for (const p of pages) p.hidden = p.dataset.page !== key;
  for (const a of document.querySelectorAll("[data-nav]")) {
    const on = a.dataset.nav === key;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    a.hidden = !GT.ready(a.dataset.nav);
  }
  // canvases on a hidden page have no size, so draw them now that the page is visible
  window.dispatchEvent(new Event("resize"));
  if (typeof render === "function") render();
  if (GT.hooks[key]) GT.hooks[key](true);
  window.scrollTo(0, 0);
}

/* ---------- theme: system, dark, light ---------- */
const THEMES = ["system", "dark", "light"];
function gtTheme(next) {
  let t = "system";
  try { t = localStorage.getItem("gt-theme") || "system"; } catch (e) { /* private mode */ }
  if (next) {
    t = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
    try { localStorage.setItem("gt-theme", t); } catch (e) { /* ignore */ }
  }
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  const b = document.getElementById("themeBtn");
  if (b) b.textContent = "Theme: " + t;
  if (next) { window.dispatchEvent(new Event("resize")); if (typeof render === "function") render(); }
}

/* ---------- tooltips for explained terms: <span data-term="R">R</span> ---------- */
function gtTip(e) {
  const tip = document.getElementById("tip");
  const t = e.target.closest ? e.target.closest("[data-term]") : null;
  if (!t) { tip.hidden = true; return; }
  const key = t.dataset.term, text = GT.glossary[key] || t.dataset.tip;
  if (!text) { tip.hidden = true; return; }
  tip.textContent = "";
  const b = document.createElement("b"); b.textContent = key; tip.appendChild(b);
  tip.appendChild(document.createTextNode(text));
  tip.hidden = false;
  const r = t.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + "px";
  tip.style.top = (r.top - h - 8 > 0 ? r.top - h - 8 : r.bottom + 8) + "px";
}

document.addEventListener("mouseover", gtTip);
document.addEventListener("focusin", gtTip);
document.addEventListener("mouseout", e => {
  if (e.target.closest && e.target.closest("[data-term]")) document.getElementById("tip").hidden = true;
});
document.getElementById("themeBtn").addEventListener("click", () => gtTheme(true));
window.addEventListener("hashchange", () => gtShow(location.hash.slice(1)));
function gtBoot() { gtTheme(false); gtShow(location.hash.slice(1) || "overview"); }
