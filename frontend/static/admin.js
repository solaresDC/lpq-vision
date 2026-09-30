// LPQ_VISION admin.js: the mostrador's shell, floor 1 (SPEC 1.4, 1.5; sizing ruling v3.8).
// It owns what every section shares: the login, the header, WHICH site is being edited (the admin
// chooses Global or one site; a manager is locked to its own, and the server enforces that too), the
// section tabs and the Inicio view. Every other section is a module from a FIXED list, loaded only when
// its api family is mounted (/api/admin/overview says which); an unbuilt one shows "aún no construida",
// the same pattern as the hub.
//
// The section contract: a module exports `async function render(ctx)` returning a DOM node, with
// ctx = {sess, overview, site, onLeave, refresh, refreshOverview}. site null = the global values (admin
// only). onLeave(fn) registers a cleanup (a poll's stop) that runs when the person leaves the section or
// changes site; refresh() re-renders the current section; refreshOverview() re-reads the sites list.

import { api, el, mountHeader, requireLogin, session } from "/static/app.js";
import { SECCIONES, card, groupBySeccion, knobRow, placeholder } from "/static/admin-kit.js";

const SECTIONS = [
  { id: "inicio", label: "Inicio", family: null, module: null },
  { id: "perillas", label: "Perillas", family: "admin_knobs", module: "/static/admin-knobs.js" },
  { id: "voz", label: "Voz del bot", family: "admin_voice", module: "/static/admin-voice.js" },
  { id: "sitios", label: "Sitios y cuentas", family: "admin_sites", module: "/static/admin-sites.js" },
  { id: "menu", label: "Menú", family: "admin_menu", module: "/static/admin-menu.js" },
  { id: "operacion", label: "Cola, papelera y gasto", family: "admin_ops", module: "/static/admin-ops.js" },
];

const WORDS = {
  phone: "teléfono", pi: "Pi",
  return: "regreso", outgoing: "salida", both: "ambas",
  top: "arriba", right: "derecha", bottom: "abajo", left: "izquierda",
};
const word = (x) => WORDS[x] || x;

let sess = await session();
if (!sess) sess = await requireLogin();
mountHeader("Mostrador", sess);

const overview = await api("/api/admin/overview");
const state = { site: sess.admin ? null : sess.site };
const modules = new Map();
let cleanups = [];
let renderSeq = 0;

function leave() {
  for (const fn of cleanups.splice(0)) {
    try {
      fn();
    } catch (err) {
      console.warn("onLeave:", err.message);
    }
  }
}

function currentId() {
  const id = location.hash.replace("#", "");
  return SECTIONS.some((s) => s.id === id) ? id : "inicio";
}

// --- the site bar ------------------------------------------------------------------------

function renderSitebar() {
  const bar = document.getElementById("sitebar");
  bar.replaceChildren();
  if (!sess.admin) {
    bar.append(
      el("span", { class: "chip locked", text: "🔒 sitio: " + sess.site }),
      el("span", { class: "muted", text: "Tus cambios aplican solo a tu sitio." }));
    return;
  }
  const select = el("select", { id: "site-select" },
    el("option", { value: "", text: "Global (todos los sitios)" }),
    overview.sites.map((s) => el("option", { value: s.site, text: s.site + (s.active ? "" : " (inactivo)") })));
  select.value = state.site || "";
  select.addEventListener("change", () => {
    state.site = select.value || null;
    renderSection();
  });
  bar.append(el("label", { for: "site-select", text: "Estás editando:" }), select);
}

async function refreshOverview() {
  const fresh = await api("/api/admin/overview");
  overview.sites = fresh.sites;
  overview.families = fresh.families;
  if (state.site && !overview.sites.some((s) => s.site === state.site)) state.site = null;
  renderSitebar();
}

// --- the tabs and the section host -------------------------------------------------------

function renderTabs() {
  const id = currentId();
  document.getElementById("tabs").replaceChildren(...SECTIONS.map((s) =>
    el("a", { href: "#" + s.id, class: s.id === id ? "current" : "", text: s.label })));
}

async function renderSection() {
  leave();
  const seq = ++renderSeq;
  const host = document.getElementById("section");
  const section = SECTIONS.find((s) => s.id === currentId());
  host.replaceChildren(el("p", { class: "muted", text: "Cargando…" }));
  const ctx = {
    sess,
    overview,
    site: state.site,
    // A cleanup registered by a render that is already stale runs at once: nothing outlives its view.
    onLeave: (fn) => { if (seq === renderSeq) cleanups.push(fn); else fn(); },
    refresh: () => renderSection(),
    refreshOverview,
  };
  let node;
  try {
    if (section.family === null) {
      node = await renderInicio(ctx);
    } else if (!overview.families.includes(section.family)) {
      node = placeholder(section.label, section.family);
    } else {
      if (!modules.has(section.module)) modules.set(section.module, await import(section.module));
      node = await modules.get(section.module).render(ctx);
    }
  } catch (err) {
    node = card("No se pudo cargar " + section.label, el("p", { class: "kit-error", text: err.message }));
  }
  if (seq === renderSeq) host.replaceChildren(node);
}

// --- Inicio: the sites, the sections, and the read-only constants --------------------------

function camerasText(cameras) {
  const names = Object.keys(cameras || {});
  if (!names.length) return "sin cámaras";
  return names.map((name) => {
    const c = cameras[name];
    const edge = c.kitchen_edge ? ", cocina: " + word(c.kitchen_edge) : "";
    return name + " (" + word(c.source) + ", " + word(c.role) + edge + ")";
  }).join(" · ");
}

async function renderInicio(ctx) {
  const constantes = await api("/api/admin/constantes");

  const sitesTable = el("table", {},
    el("thead", {}, el("tr", {},
      ["Sitio", "Activo", "Chat de Telegram", "Mantenimiento", "Cámaras"].map((h) => el("th", { text: h })))),
    el("tbody", {}, ctx.overview.sites.map((s) => el("tr", {},
      el("td", { text: s.site }),
      el("td", { text: s.active ? "sí" : "no" }),
      el("td", { text: s.chat_set ? "asignado" : "sin asignar" }),
      el("td", { text: s.mantenimiento ? "en mantenimiento" : "no" }),
      el("td", { text: s.in_config ? camerasText(s.cameras) : "falta en config.yaml" })))));

  const sectionsList = el("ul", {}, SECTIONS.filter((s) => s.family).map((s) => {
    const built = ctx.overview.families.includes(s.family);
    return el("li", {},
      el("span", { text: s.label + ": " }),
      el("span", { class: built ? "chip ok" : "chip", text: built ? "lista" : "aún no construida" }));
  }));

  const constantsTable = el("table", {}, el("tbody", {},
    Object.entries(constantes.constants).map(([k, v]) =>
      el("tr", {}, el("td", { text: k }), el("td", { class: "num", text: String(v) })))));

  // The admin reads the global values; a manager reads its own site's, painted propio or heredado.
  const paintSite = ctx.sess.admin ? null : ctx.sess.site;
  const knobsBox = el("div", {});
  for (const [seccion, list] of groupBySeccion(constantes.knobs)) {
    const floor = list[0].floor === "maquinas" ? " (cuarto de máquinas)" : "";
    knobsBox.append(el("h3", { text: (SECCIONES[seccion] || seccion) + floor }));
    for (const knob of list) knobsBox.append(knobRow(knob, { site: paintSite, readOnly: true }));
  }

  return el("div", {},
    card("Sitios", sitesTable),
    card("Secciones del mostrador", sectionsList),
    card("Constantes (solo lectura)",
      el("p", { class: "muted", text: "Cada perilla con sus tres ayudas. Se cambian en su sección; aquí solo se leen." }),
      constantsTable,
      knobsBox));
}

// --- boot ----------------------------------------------------------------------------------

window.addEventListener("hashchange", () => {
  renderTabs();
  renderSection();
});
renderSitebar();
renderTabs();
renderSection();
