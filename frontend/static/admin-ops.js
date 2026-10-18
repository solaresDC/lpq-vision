// LPQ_VISION admin-ops.js: the "Cola, papelera y gasto" tab of the mostrador (family admin_ops; SPEC 1.5, 1.9;
// Section 3.14).
// Cola: the brake (ONE for the whole chain, the admin's), what waits, what is being analyzed and what failed, with
// Reintentar (a manager retries its own site's; the admin, the site it is looking at or every site). The card follows
// the queue by itself every LIVE_MS while it is on screen: it repaints only the part that changed, never under a
// pressed mouse, a finger or a text selection, never with an answer older than the one on screen, and that re-read
// never opens the login (a failed or silent round says so on the card).
// Papelera: the plates discarded in Review wait here papelera_dias days (a Perillas knob) and then the worker deletes
// them for good. Restaurar sends them back to Review's Pendientes with their corrections; Eliminar ya deletes them now,
// behind the double confirm. A photo opens in the viewer inside the page.
// Gasto (the admin's): gasto de filas vivas, tokens per site, local day and model, and the Leer ficha readings in the
// Global view; money only where a price was entered (copied by a human from the provider's own page), "sin precio"
// otherwise. No cache number anywhere (owner, 8 Oct). It loads after the other cards, so the Cola never waits for it.
// A price row being typed keeps its text, its focus and its caret while any other row saves or the range changes.
// Constantes: they are read in Inicio; this tab links there instead of repeating them.
// Each card loads on its own: one that fails says so inside itself and the others still work.
// Section 3.17.1 (the owner's ✋ notes, 16 Oct): the Papelera (every LIVE_MS) and the Gasto (every GASTO_LIVE_MS) also
// follow the system by themselves while on screen, on the Cola's terms: they repaint only when their data changed,
// never under a pressed mouse, a finger, a text selection, an open window or a field in use, never with an answer older
// than the one on screen, and their re-read never opens the login; a repaint keeps the person's place (the focus, a
// table's sideways scroll, a row's error line). A price or an exchange rate shows at least two decimals (2.20; 2.57
// stays 2.57). The Gasto can show its money in USD, MXN or CAD: the exchange rates are typed by
// the admin (1 USD = 18.45 MXN), with the day each was saved; the prices stay in USD per million tokens.
// Section 3.17.4 (owner, 18 Oct): under Moneda, a line says the provider charges in dollars whatever the country or
// the card, and the bank converts, so the money in pesos or Canadian dollars is an estimate.

import { api, el, fmtDate, poll, qs, toast } from "/static/app.js";
import { busy, card, confirmFlow, fieldHelp, photoViewer } from "/static/admin-kit.js";

const LIVE_MS = 5000;               // the Cola and the Papelera re-read themselves this often while on screen
const GASTO_LIVE_MS = 10000;        // the Gasto (a heavier read) this often
const READ_TIMEOUT_MS = 2 * LIVE_MS; // a re-read that has not answered by then counts as failed
const HOLD_MAX_MS = 30000;          // a press or a touch older than this no longer holds the Cola (a lost release)
// Where each price sits on the provider's price page (checked 13 Oct 2026); this system keeps its caché 5 minutes.
const PRICE_COLUMNS = "Input → Entrada · Output → Salida · 5m writes → Escritura de caché · Hits and refreshes → Lectura de caché";
const PRICE_NOTE = "Los cuatro números salen de la página de precios del proveedor, en dólares por millón de tokens (MTok). "
  + "Este sistema guarda la caché 5 minutos: la escritura de caché es la columna «5m writes».";
const PRICE_FIELDS = [
  ["in_mtok", "Entrada"],
  ["out_mtok", "Salida"],
  ["cache_write_mtok", "Escritura de caché"],
  ["cache_read_mtok", "Lectura de caché"],
];
// A price as people type it: digits, then at most one point and more digits (a comma becomes the point); nothing else.
function isDecimal(raw) {
  const m = /^\d+(\.\d+)?/.exec(raw);
  return Boolean(m) && m[0] === raw;
}
// The currencies the Gasto can show besides USD, and how an exchange rate is typed (Section 3.17.1).
const MONEDA_NOMBRE = { USD: "dólares (USD)", MXN: "pesos (MXN)", CAD: "dólares canadienses (CAD)" };
const MONEDA_PALABRA = { MXN: "pesos", CAD: "dólares canadienses" };
const MONEDA_EJEMPLO = { MXN: "18.45", CAD: "1.38" };
const MONEDA_KEY = "lpq.gasto.moneda";   // this browser's last currency choice (a convenience: absent = USD)
const COBRO_USD = "Anthropic (Claude) cobra su API siempre en dólares (USD), sin importar el país o la tarjeta; tu banco "
  + "lo pasa a tu moneda con su tipo de cambio, así que el gasto en pesos o en dólares canadienses es una estimación.";

const STYLE = `
.ops-tiles { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); margin: 10px 0; }
.ops-tile { background: var(--bg); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 12px; }
.ops-tile .label { font-size: 13px; color: var(--muted); }
.ops-scroll { overflow-x: auto; max-width: 100%; }
.ops-thumb-btn { padding: 0; min-height: 0; border: 0; background: none; cursor: zoom-in; }
.ops-thumb { width: 64px; height: 64px; object-fit: cover; border-radius: 6px; background: #ddd; display: block; }
.ops-pick { width: 36px; }
.ops-pick input { width: 22px; height: 22px; min-height: 0; margin: 0; }
.ops-error { font-size: 13px; color: var(--muted); max-width: 420px; overflow-wrap: anywhere; }
.ops-price { border-top: 1px solid var(--line); padding: 10px 0; }
.ops-price input { width: 110px; min-width: 0; }
.ops-price label { display: inline-flex; flex-direction: column; gap: 2px; margin: 0 8px 6px 0; font-size: 13px; color: var(--muted); }
.ops-pager { display: flex; gap: 10px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
.ops-live { font-size: 13px; color: var(--muted); margin-top: 6px; }
.ops-pair { display: inline-flex; align-items: center; gap: 6px; }
.ops-live.bad { color: var(--warn); }
`;

function injectStyle() {
  if (document.getElementById("admin-ops-style")) return;
  document.head.append(el("style", { id: "admin-ops-style", text: STYLE }));
}

export async function render(ctx) {
  injectStyle();
  const bus = { refreshCola: () => {}, refreshGasto: () => {} };   // the papelera's actions ask the others to look again
  const box = el("div", {});
  box.append(await guarded("Cola de análisis", () => colaCard(ctx, bus)));
  box.append(await guarded("Papelera", () => papeleraCard(ctx, bus)));
  box.append(ctx.sess.admin
    ? gastoCard(ctx, bus)
    : card("Gasto", el("p", { class: "muted", text: "El gasto lo ve el admin." })));
  box.append(constantesCard());
  return box;
}

// A card that could not load says so inside itself; the rest of the tab still works.
async function guarded(title, build) {
  try {
    return await build();
  } catch (err) {
    return card(title, el("p", { class: "kit-error", text: "No se pudo cargar: " + err.message }));
  }
}

// --- small pieces ------------------------------------------------------------------------

function scopeText(site) {
  return site ? "de " + site : "de todos los sitios";
}

function opsTile(label, value) {
  return el("div", { class: "ops-tile" }, el("div", { class: "label", text: label }), el("div", { class: "big", text: String(value) }));
}

function fmtInt(n) {
  return Number(n || 0).toLocaleString("es-MX");
}

// money: {code, rate} = the currency the Gasto shows and how many of it one USD is worth (1 for USD itself).
function fmtCash(usd, money) {
  return money.code + " " + (Number(usd) * money.rate).toLocaleString("es-MX", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

// usd null with nothing unpriced = nothing was spent; usd null otherwise = only unpriced rows.
function fmtMoney(usd, sinPrecio, money) {
  if (usd === null || usd === undefined) return sinPrecio ? "sin precio" : "sin gasto";
  const extra = sinPrecio ? " · " + sinPrecio + (sinPrecio === 1 ? " fila sin precio" : " filas sin precio") : "";
  return fmtCash(usd, money) + extra;
}

// A price or an exchange rate as the page shows it (owner, 16 Oct): at least two decimals, more only when it has
// them (up to 10, more than anyone types): 2 shows 2.00, 0.2 shows 0.20, 2.57 stays 2.57, 0.075 stays 0.075. No
// thousands mark: it is a field's value.
function fmtPrice(value) {
  const n = Number(value);
  let plain = n.toFixed(10);
  while (plain.endsWith("0")) plain = plain.slice(0, -1);
  if (plain.endsWith(".")) plain = plain.slice(0, -1);
  const point = plain.indexOf(".");
  return point === -1 || plain.length - point - 1 < 2 ? n.toFixed(2) : plain;
}

function dmy(iso) {
  const [y, m, d] = String(iso).slice(0, 10).split("-");
  return d + "/" + m + "/" + y;
}

function fmtDay(iso) {
  const day = new Date(iso + "T12:00:00");
  return day.toLocaleDateString("es-MX", { weekday: "short", day: "numeric", month: "short" });
}

function plural(n, one, many) {
  return n === 1 ? "1 " + one : n + " " + many;
}

function clock() {
  return new Date().toLocaleTimeString("es-MX");
}

// The "actualizado" line under a live card, and what it says when a re-read failed.
function stamp(live) {
  live.classList.remove("bad");
  live.textContent = "actualizado " + clock();
}

function failed(live, what) {
  live.classList.add("bad");
  live.textContent = "No se pudo actualizar " + what + " (" + clock() + "). Si tu sesión venció, recarga la página.";
}

// Hands on a card: a mouse press or a finger on it holds its repaint until it is released (a touch that starts a
// scroll fires pointercancel at once, so fingers are followed with the touch events instead; a press older than
// HOLD_MAX_MS is a lost release), and so does a text selection inside it. stop() when the tab is left.
function hands(node) {
  const state = { pressedAt: 0, touchedAt: 0 };
  const onPointerDown = (ev) => { if (ev.pointerType !== "touch") state.pressedAt = Date.now(); };
  const onPointerUp = (ev) => { if (ev.pointerType !== "touch") state.pressedAt = 0; };
  const onTouchStart = () => { state.touchedAt = Date.now(); };
  const onTouchEnd = (ev) => { if (!ev.touches || ev.touches.length === 0) state.touchedAt = 0; };
  node.addEventListener("pointerdown", onPointerDown);
  node.addEventListener("touchstart", onTouchStart, { passive: true });
  window.addEventListener("pointerup", onPointerUp, true);
  window.addEventListener("pointercancel", onPointerUp, true);
  window.addEventListener("touchend", onTouchEnd, true);
  window.addEventListener("touchcancel", onTouchEnd, true);
  const recent = (at) => at > 0 && Date.now() - at < HOLD_MAX_MS;
  return {
    held: () => {
      if (recent(state.pressedAt) || recent(state.touchedAt)) return true;
      const sel = window.getSelection();
      return Boolean(sel && !sel.isCollapsed && sel.anchorNode && node.contains(sel.anchorNode));
    },
    stop: () => {
      window.removeEventListener("pointerup", onPointerUp, true);
      window.removeEventListener("pointercancel", onPointerUp, true);
      window.removeEventListener("touchend", onTouchEnd, true);
      window.removeEventListener("touchcancel", onTouchEnd, true);
    },
  };
}

// An open window anywhere (a confirm, the photo viewer, the login, a dialog): the live cards wait under it.
function windowOpen() {
  return Boolean(document.querySelector(".overlay, [aria-modal='true']"));
}

// The person's focus inside a card, as a key that survives a repaint (a button's or a box's data-focus, a help's "?"
// by its data-help); refocus() puts it back on the new element with that key, only when the focus is still on the
// card or nowhere (a person who moved on elsewhere keeps their place) and the element can take it.
function focusKeyIn(root) {
  const active = document.activeElement;
  if (!active || active === document.body || !root.contains(active)) return null;
  if (active.dataset && active.dataset.focus) return "f:" + active.dataset.focus;
  const help = active.classList.contains("kit-ask") ? active.closest("[data-help]") : null;
  return help ? "h:" + help.dataset.help : null;
}

function refocus(root, key) {
  if (!key) return;
  const active = document.activeElement;
  if (active && active !== document.body && !root.contains(active)) return;
  let target = null;
  if (key.startsWith("f:")) {
    target = [...root.querySelectorAll("[data-focus]")].find((x) => x.dataset.focus === key.slice(2)) || null;
  } else {
    const help = [...root.querySelectorAll("[data-help]")].find((x) => x.dataset.help === key.slice(2));
    target = help ? help.querySelector(".kit-ask") : null;
  }
  if (target && !target.disabled && target !== document.activeElement) target.focus({ preventScroll: true });
}

// A read that never opens the login (an expired session simply waits for the person's next click) and never hangs:
// no answer within READ_TIMEOUT_MS counts as a failed read.
async function quietGet(path) {
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), READ_TIMEOUT_MS);
  try {
    const res = await fetch(path, { credentials: "same-origin", cache: "no-store", signal: stop.signal });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// heads: a string, a {num: text} for a right-aligned number column, or a ready <th>. The table scrolls sideways
// inside its card on a narrow screen.
function table(heads, rows) {
  const th = (h) => {
    if (typeof h === "string") return el("th", { text: h });
    if (h && h.num !== undefined) return el("th", { class: "num", text: h.num });
    return h;
  };
  return el("div", { class: "ops-scroll" },
    el("table", {}, el("thead", {}, el("tr", {}, heads.map(th))), el("tbody", {}, rows)));
}

// --- Cola ----------------------------------------------------------------------------------

async function colaCard(ctx, bus) {
  const path = "/api/admin/cola" + qs({ site: ctx.site });
  const firstRead = await api(path);  // before any listener: a card that cannot load leaves nothing behind
  const head = el("div", {});       // en marcha or EN PAUSA       } repainted only when the brake changes, so its
  const tiles = el("div", {});      // the counts                   } help stays open while the counts move
  const brake = el("div", {});      // the brake and its help       }
  const fails = el("div", {});      // the failed plates and Reintentar
  const live = el("div", { class: "ops-live" });
  const node = card("Cola de análisis " + scopeText(ctx.site), head, tiles, brake, fails, live);
  const state = { brakeSig: "", countSig: "", ticket: 0, painted: 0, acting: 0, running: false, gone: false };
  const hand = hands(node);
  const held = hand.held;

  // focusKey: the data-focus of the button the person was on (an action passes it: its button is disabled while it
  // runs, so the browser has already moved the focus away). The focus comes back only when it is on the card or
  // nowhere: a person who moved on elsewhere keeps their place.
  const paint = (data, ticket, focusKey) => {
    if (ticket < state.painted) return;                 // an answer older than the one on screen never rolls it back
    state.painted = ticket;
    stamp(live);
    const brakeSig = JSON.stringify([data.pausada, data.puede_pausar, data.ayuda_pausa]);
    const countSig = JSON.stringify([data.site, data.esperando, data.en_proceso, data.fallidos, data.fallidos_lista]);
    if (brakeSig === state.brakeSig && countSig === state.countSig) return;   // nothing moves under the person's eyes
    const active = document.activeElement;
    const free = !active || active === document.body || node.contains(active);
    const key = focusKey || (active && node.contains(active) ? active.getAttribute("data-focus") : null);
    if (brakeSig !== state.brakeSig) {
      state.brakeSig = brakeSig;
      const helpOpen = Boolean(brake.querySelector(".kit-more:not(.hidden)"));
      head.replaceChildren(statusLine(data));
      brake.replaceChildren(...brakeBody(data, act).filter(Boolean));
      const more = brake.querySelector(".kit-more");
      const ask = brake.querySelector(".kit-ask");
      if (helpOpen && more && ask) {
        more.classList.remove("hidden");
        ask.setAttribute("aria-expanded", "true");
      }
    }
    if (countSig !== state.countSig) {
      state.countSig = countSig;
      const scroller = fails.querySelector(".ops-scroll");
      const left = scroller ? scroller.scrollLeft : 0;
      tiles.replaceChildren(el("div", { class: "ops-tiles" },
        opsTile("Esperando", data.esperando),
        opsTile("En proceso", data.en_proceso),
        opsTile("Fallidos (todos)", data.fallidos)));
      fails.replaceChildren(...failBody(data, act).filter(Boolean));
      const again = fails.querySelector(".ops-scroll");
      if (again && left) again.scrollLeft = left;
    }
    if (key && free) {
      const target = node.querySelector("[data-focus='" + key + "']");
      if (target && target !== document.activeElement) target.focus({ preventScroll: true });
    }
  };
  const act = async (fn, focusKey) => {
    state.acting += 1;
    try {
      await fn();
      const ticket = ++state.ticket;
      const data = await quietGet(path);
      if (data && !state.gone) paint(data, ticket, focusKey);   // a failed read: the next round paints
    } finally {
      state.acting -= 1;
    }
  };
  const round = async () => {
    if (state.gone || state.acting || state.running || held()) return;
    state.running = true;
    try {
      const ticket = ++state.ticket;
      const data = await quietGet(path);
      if (state.gone || state.acting || held()) return;      // a press, a selection or an action began meanwhile
      if (data) {
        paint(data, ticket);
      } else if (ticket >= state.painted) {
        live.classList.add("bad");
        live.textContent = "No se pudo actualizar la cola (" + clock() + "). Si tu sesión venció, recarga la página.";
      }
    } finally {
      state.running = false;
    }
  };
  bus.refreshCola = () => { round().catch((err) => console.warn("cola:", err.message)); };

  paint(firstRead, ++state.ticket);
  let first = true;
  const stop = poll(async () => {
    if (first) { first = false; return; }     // the render just read it: the first round is the next one
    await round();
  }, LIVE_MS);
  ctx.onLeave(() => {
    state.gone = true;
    stop();
    hand.stop();
  });
  return node;
}

function statusLine(data) {
  return el("p", {},
    data.pausada
      ? el("span", { class: "chip warn", text: "⏸ EN PAUSA" })
      : el("span", { class: "chip ok", text: "▶ en marcha" }),
    el("span", {
      class: "muted",
      text: data.pausada
        ? " Los platos siguen llegando y esperan: nadie los analiza hasta que el admin reanude la cola."
        : " Los analistas van tomando los platos que esperan.",
    }));
}

function brakeBody(data, act) {
  if (!data.puede_pausar) {
    return [el("p", { class: "muted", text: "La pausa es una sola para toda la cadena: solo el admin la cambia." })];
  }
  const turnOn = !data.pausada;
  const button = el("button", {
    type: "button", class: turnOn ? "warn" : "ok", "data-focus": "pausa",
    text: turnOn ? "⏸ Pausar la cola" : "▶ Reanudar la cola",
  });
  button.addEventListener("click", () => {
    if (turnOn && !window.confirm("¿Pausar la cola de TODOS los sitios? Los platos siguen llegando y esperan; nadie los analiza hasta que la reanudes.")) return;
    busy(button, () => act(async () => {
      try {
        await api("/api/admin/cola/pausa", { method: "POST", json: { on: turnOn } });
        toast(turnOn ? "cola en pausa" : "cola reanudada", "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
    }, "pausa"));
  });
  return [
    fieldHelp({
      que_es: data.ayuda_pausa.que_es,
      ejemplo: "Pausar al empezar un mantenimiento y reanudar al terminar.",
      recomendacion: data.ayuda_pausa.recomendacion,
      copiar: false,
    }),
    el("div", { class: "kit-edit" }, button),
  ];
}

function failBody(data, act) {
  if (!data.fallidos) return [el("p", { class: "muted", text: "Ningún plato fallido." })];
  const rows = data.fallidos_lista.map((f) => el("tr", {},
    el("td", { text: "#" + f.plato }),
    el("td", { text: f.site + (f.camera ? " · " + f.camera : "") }),
    el("td", { text: f.flujo }),
    el("td", { class: "num", text: String(f.intentos) }),
    el("td", { class: "ops-error", text: f.error }),
    el("td", { text: fmtDate(f.cuando) })));
  const one = data.fallidos === 1;
  const what = one ? "el plato fallido" : "los " + data.fallidos + " platos fallidos";
  const retry = el("button", { type: "button", class: "primary", "data-focus": "reintentar", text: "↻ Reintentar " + what });
  retry.addEventListener("click", () => {
    const back = one
      ? " Vuelve a la cola con sus intentos en cero: es otra llamada al modelo."
      : " Vuelven a la cola con sus intentos en cero: cada uno es otra llamada al modelo.";
    if (!window.confirm("¿Reintentar " + what + " " + scopeText(data.site) + "?" + back)) return;
    busy(retry, () => act(async () => {
      try {
        const r = await api("/api/admin/cola/reintentar", { method: "POST", json: { site: data.site } });
        toast(plural(r.reintentados, "plato", "platos") + " de vuelta en la cola", "ok");
      } catch (err) {
        toast(err.message, "bad");
      }
    }, "reintentar"));
  });
  return [
    el("h3", { text: "Fallidos" }),
    table(["Plato", "Sitio · cámara", "Flujo", { num: "Intentos" }, "Por qué falló", "Cuándo"], rows),
    data.fallidos > data.fallidos_lista.length
      ? el("p", { class: "muted", text: "Se ven los " + data.fallidos_lista.length + " más recientes; Reintentar los toma todos." })
      : null,
    el("div", { class: "kit-edit" }, retry),
  ];
}

// --- Papelera ------------------------------------------------------------------------------

// The papelera follows itself every LIVE_MS while on screen (Section 3.17.1): a plate discarded in Review shows up
// here without reloading. It repaints only when its data changed, keeps the plates ticked (those still listed), the
// page, the table's sideways scroll and the focus, and waits while an action runs, a window is open or hands are on it.
async function papeleraCard(ctx, bus) {
  const body = el("div", {});
  const live = el("div", { class: "ops-live" });
  const node = card("Papelera " + scopeText(ctx.site), body, live);
  const view = { page: 1, selected: new Set(), ticket: 0, painted: 0, sig: "", acting: 0, running: false, gone: false };
  let hand = null;                      // hands on the card once it stands: a card that cannot load leaves nothing behind
  const path = () => "/api/admin/papelera" + qs({ site: ctx.site, page: view.page });
  view.act = async (fn) => {
    view.acting += 1;
    try {
      return await fn();
    } finally {
      view.acting -= 1;
    }
  };
  // quiet: a live round's answer, so a step back reads quietly too (never the login, never a hang) and paints only
  // while the card is still free; otherwise a person's read (api).
  const paint = async (data, ticket, quiet) => {
    if (ticket < view.painted) return;   // an answer older than the one on screen never rolls it back
    if (data.page > data.pages && data.pages >= 1 && view.page !== data.pages) {
      view.page = data.pages;           // the last page emptied itself: show the one before
      if (!quiet) return load();
      const step = ++view.ticket;
      const before = await quietGet(path());
      if (!free()) return;
      if (before) return paint(before, step, true);
      if (step >= view.painted) failed(live, "la papelera");
      return;
    }
    view.painted = ticket;
    stamp(live);
    const sig = JSON.stringify(data);
    if (sig === view.sig) return;       // nothing moves under the person's eyes
    view.sig = sig;
    for (const id of [...view.selected]) if (!data.rows.some((r) => r.id === id)) view.selected.delete(id);
    const scroller = body.querySelector(".ops-scroll");
    const left = scroller ? scroller.scrollLeft : 0;
    const focusKey = focusKeyIn(body);
    body.replaceChildren(...papeleraBody(ctx, data, view, load, bus).filter(Boolean));
    const again = body.querySelector(".ops-scroll");
    if (again && left) again.scrollLeft = left;
    refocus(body, focusKey);
  };
  const load = async () => {
    const ticket = ++view.ticket;
    const data = await api(path());
    if (!view.gone) await paint(data, ticket, false);
  };
  const free = () => !view.gone && !view.acting && !(hand && hand.held()) && !windowOpen();
  const round = async () => {
    if (view.running || !free()) return;
    view.running = true;
    try {
      const ticket = ++view.ticket;
      const data = await quietGet(path());
      if (!free()) return;              // an action, a press, a selection or a window began meanwhile
      if (data) await paint(data, ticket, true);
      else if (ticket >= view.painted) failed(live, "la papelera");
    } finally {
      view.running = false;
    }
  };
  await load();
  hand = hands(node);
  let first = true;
  const stop = poll(async () => {
    if (first) { first = false; return; }     // the render just read it: the first round is the next one
    await round();
  }, LIVE_MS);
  ctx.onLeave(() => {
    view.gone = true;
    stop();
    hand.stop();
  });
  return node;
}

function papeleraBody(ctx, data, view, load, bus) {
  const note = el("p", { class: "muted" },
    "Los platos descartados en Review esperan aquí " + data.dias + " días y luego se borran solos, con su foto. "
    + "«Restaurar» los regresa a Pendientes con sus correcciones. ",
    ctx.sess.admin
      ? el("a", { href: "#perillas", "data-focus": "perillas", text: "Los días se cambian en Perillas (papelera_dias)." })
      : el("span", { text: "Los días los fija el admin." }));
  if (!data.total) return [note, el("p", { text: "La papelera está vacía." })];

  const restore = el("button", { type: "button", class: "ok", "data-focus": "restaurar" });
  const kill = el("button", { type: "button", class: "bad", "data-focus": "eliminar" });
  const sync = () => {
    const n = view.selected.size;
    restore.textContent = "↩ Restaurar" + (n ? " (" + n + ")" : "");
    kill.textContent = "🗑 Eliminar ya" + (n ? " (" + n + ")" : "");
    restore.disabled = n === 0;
    kill.disabled = n === 0;
  };
  // After an action, or a refusal (another tab or the hourly purge got there first), the list is read again, and the
  // Cola and the Gasto look again too.
  const reload = async () => {
    try {
      await load();
    } catch (err) {
      toast(err.message, "bad");
    }
    bus.refreshCola();
    bus.refreshGasto();
  };
  restore.addEventListener("click", () => busy(restore, () => view.act(async () => {
    try {
      const r = await api("/api/admin/papelera/restaurar", { method: "POST", json: { ids: [...view.selected] } });
      toast(plural(r.restaurados, "plato", "platos") + " de vuelta en Review (Pendientes)", "ok");
      view.selected.clear();
    } catch (err) {
      toast(err.message, "bad");
    }
    sync();
    await reload();
  })));
  kill.addEventListener("click", () => busy(kill, () => view.act(async () => {
    try {
      const result = await confirmFlow(() =>
        api("/api/admin/papelera/eliminar", { method: "POST", json: { ids: [...view.selected] } }));
      if (!result) return;              // cancelled: nothing changed
      toast(plural(result.eliminados, "plato borrado", "platos borrados") + " para siempre", "ok");
      view.selected.clear();
    } catch (err) {
      toast(err.message, "bad");
    }
    sync();
    await reload();
  })));

  const photos = data.rows.map((r) => ({ src: r.photo_url, caption: "#" + r.id + " · " + (r.platillo || "sin analizar") }));
  const boxes = [];
  const all = el("input", { type: "checkbox", "aria-label": "Elegir todos", "data-focus": "pick:all" });
  all.checked = data.rows.length > 0 && data.rows.every((r) => view.selected.has(r.id));
  all.addEventListener("change", () => {
    for (const r of data.rows) {
      if (all.checked) view.selected.add(r.id);
      else view.selected.delete(r.id);
    }
    for (const b of boxes) b.checked = all.checked;
    sync();
  });
  const rows = data.rows.map((r, i) => {
    const box = el("input", { type: "checkbox", "aria-label": "Elegir el plato " + r.id, "data-focus": "pick:" + r.id });
    box.checked = view.selected.has(r.id);
    box.addEventListener("change", () => {
      if (box.checked) view.selected.add(r.id);
      else view.selected.delete(r.id);
      all.checked = data.rows.every((x) => view.selected.has(x.id));
      sync();
    });
    boxes.push(box);
    const thumb = el("button", {
      type: "button", class: "ops-thumb-btn", "aria-label": "Ver la foto del plato " + r.id, "data-focus": "foto:" + r.id,
    },
      el("img", { class: "ops-thumb", src: r.photo_url, loading: "lazy", alt: "plato " + r.id }));
    thumb.addEventListener("click", () => photoViewer(photos, i));
    return el("tr", {},
      el("td", { class: "ops-pick" }, box),
      el("td", {}, thumb),
      el("td", { text: "#" + r.id }),
      el("td", { text: r.site + (r.camera ? " · " + r.camera : "") }),
      el("td", { text: r.flujo }),
      el("td", { text: r.platillo || "sin analizar" }),
      el("td", { text: r.confianza || "" }),
      el("td", { text: fmtDate(r.descartado) }),
      el("td", { text: fmtDate(r.se_borra) }));
  });
  sync();
  const turn = (page) => () => {
    view.page = page;
    load().catch((err) => toast(err.message, "bad"));
  };
  const counter = data.pages > 1
    ? el("div", { class: "ops-pager" },
      el("button", { type: "button", "data-focus": "anterior", text: "◀ Anterior", disabled: data.page <= 1, onclick: turn(data.page - 1) }),
      el("span", { class: "muted", text: "página " + data.page + " de " + data.pages + " · " + data.total + " en total" }),
      el("button", { type: "button", "data-focus": "siguiente", text: "Siguiente ▶", disabled: data.page >= data.pages, onclick: turn(data.page + 1) }))
    : el("p", { class: "muted", text: plural(data.total, "plato", "platos") + " en la papelera." });
  return [
    note,
    el("div", { class: "kit-edit" }, restore, kill),
    table([el("th", { class: "ops-pick" }, all), "Foto", "Plato", "Sitio · cámara", "Flujo", "Platillo", "Confianza", "Descartado", "Se borra desde"], rows),
    counter,
  ];
}

// --- Gasto (the admin's) ---------------------------------------------------------------------

// The card shows at once and fills itself in: the gasto never holds up the Cola or the papelera. It follows itself
// every GASTO_LIVE_MS while on screen (Section 3.17.1): it repaints only when its data changed, and waits while an
// action runs, a window is open, hands are on it, or the cursor is in one of its fields or menus. A repaint keeps
// what was typed and not saved, the focus and caret, an open "?", a row's error line and the tables' sideways scroll.
function gastoCard(ctx, bus) {
  const body = el("div", {}, el("p", { class: "muted", text: "Cargando el gasto…" }));
  const live = el("div", { class: "ops-live" });
  const node = card("Gasto " + scopeText(ctx.site), body, live);
  const view = {
    dias: 30, moneda: savedMoneda(), data: null, ticket: 0, painted: 0, sig: "", acting: 0, running: false, gone: false,
  };
  const hand = hands(node);
  const path = () => "/api/admin/gasto" + qs({ site: ctx.site, dias: view.dias });
  // savedKey: the field (or row of fields) just saved; it shows the saved value (and loses its error line), every
  // other field keeps what was typed in it and not saved, with its focus and caret. An open "?" stays open.
  // focusAfter: the data-focus of the button an action ran from (disabled while it ran, so the focus left it).
  const paint = (data, ticket, savedKey, focusAfter) => {
    if (ticket < view.painted) return;   // an answer older than the one on screen never rolls it back
    view.painted = ticket;
    stamp(live);
    const sig = JSON.stringify(data) + "|" + view.moneda;
    if (sig === view.sig && !savedKey) return;
    view.sig = sig;
    view.data = data;
    const typed = new Map();
    let focus = null;
    const active = document.activeElement;
    for (const input of body.querySelectorAll("[data-keep]")) {
      const key = input.dataset.keep;
      const justSaved = savedKey && key.startsWith(savedKey);
      if (!justSaved && input.value !== input.dataset.saved) typed.set(key, input.value);
      if (input === active) focus = { key: key, start: input.selectionStart, end: input.selectionEnd };
    }
    const menu = active && body.contains(active) && active.tagName === "SELECT" ? active.getAttribute("aria-label") : null;
    const focusKey = focusAfter ? "f:" + focusAfter : focusKeyIn(body);
    const helpsOpen = [...body.querySelectorAll("[data-help]")]
      .filter((h) => h.querySelector(".kit-more:not(.hidden)")).map((h) => h.dataset.help);
    const errors = new Map([...body.querySelectorAll("[data-error]")]
      .filter((x) => x.textContent && !(savedKey && x.dataset.error.startsWith(savedKey)))
      .map((x) => [x.dataset.error, x.textContent]));
    const scrolls = [...body.querySelectorAll(".ops-scroll")].map((x) => x.scrollLeft);
    body.replaceChildren(...gastoBody(data, view, load, act, repaint).filter(Boolean));
    for (const line of body.querySelectorAll("[data-error]")) {
      if (errors.has(line.dataset.error)) line.textContent = errors.get(line.dataset.error);
    }
    [...body.querySelectorAll(".ops-scroll")].forEach((x, i) => { if (scrolls[i]) x.scrollLeft = scrolls[i]; });
    for (const input of body.querySelectorAll("[data-keep]")) {
      if (typed.has(input.dataset.keep)) input.value = typed.get(input.dataset.keep);
      if (focus && input.dataset.keep === focus.key) {
        input.focus({ preventScroll: true });
        try { input.setSelectionRange(focus.start, focus.end); } catch { /* the caret stays at the end */ }
      }
    }
    if (menu) {
      const again = [...body.querySelectorAll("select")].find((x) => x.getAttribute("aria-label") === menu);
      if (again) again.focus({ preventScroll: true });
    }
    for (const name of helpsOpen) {
      const help = [...body.querySelectorAll("[data-help]")].find((h) => h.dataset.help === name);
      const more = help && help.querySelector(".kit-more");
      const ask = help && help.querySelector(".kit-ask");
      if (more && ask) {
        more.classList.remove("hidden");
        ask.setAttribute("aria-expanded", "true");
      }
    }
    if (!focus && !menu) refocus(body, focusKey);
  };
  const repaint = () => { if (view.data) paint(view.data, view.painted, null, null); };
  const load = async (savedKey, focusAfter) => {
    const ticket = ++view.ticket;
    const data = await api(path());
    if (!view.gone) paint(data, ticket, savedKey || null, focusAfter || null);
  };
  const act = async (fn) => {
    view.acting += 1;
    try {
      return await fn();
    } finally {
      view.acting -= 1;
    }
  };
  const free = () => {
    if (view.gone || view.acting || hand.held() || windowOpen()) return false;
    const active = document.activeElement;
    return !(active && body.contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT"));
  };
  const round = async () => {
    if (view.running || !free()) return;
    view.running = true;
    try {
      const ticket = ++view.ticket;
      const data = await quietGet(path());
      if (!free()) return;
      if (data) paint(data, ticket, null, null);
      else if (ticket >= view.painted) failed(live, "el gasto");
    } finally {
      view.running = false;
    }
  };
  bus.refreshGasto = () => { round().catch((err) => console.warn("gasto:", err.message)); };
  load(null).catch((err) => {
    // only while nothing is painted: a live round that already brought the gasto keeps it on screen
    if (!view.gone && view.painted === 0) {
      body.replaceChildren(el("p", { class: "kit-error", text: "No se pudo cargar el gasto: " + err.message }));
    }
  });
  let first = true;
  const stop = poll(async () => {
    if (first) { first = false; return; }     // the first read is the one above: the first round is the next one
    await round();
  }, GASTO_LIVE_MS);
  ctx.onLeave(() => {
    view.gone = true;
    stop();
    hand.stop();
  });
  return node;
}

// This browser's last currency (a convenience): storage that is missing or refused means USD.
function savedMoneda() {
  try {
    const m = window.localStorage.getItem(MONEDA_KEY);
    return m && MONEDA_NOMBRE[m] ? m : "USD";
  } catch {
    return "USD";
  }
}

function saveMoneda(m) {
  try {
    window.localStorage.setItem(MONEDA_KEY, m);
  } catch { /* the choice lasts while the page is open */ }
}

// The currency the money shows in: the chosen one when its exchange rate exists, else USD.
function moneyOf(data, view) {
  const rate = view.moneda !== "USD" ? data.tipos_cambio[view.moneda] : null;
  return rate ? { code: view.moneda, rate: rate.valor, fecha: rate.fecha } : { code: "USD", rate: 1, fecha: null };
}

function gastoBody(data, view, load, act, repaint) {
  const money = moneyOf(data, view);
  const range = el("select", { "aria-label": "Rango", style: "width: auto;" },
    data.dias_opciones.map((d) => el("option", { value: String(d), text: "Últimos " + d + " días" })));
  range.value = String(data.dias);
  range.addEventListener("change", () => {
    view.dias = Number(range.value);
    load(null).catch((err) => toast(err.message, "bad"));
  });
  const currency = el("select", { "aria-label": "Moneda", style: "width: auto;" }, ["USD", ...data.monedas].map((m) => {
    const rate = m === "USD" ? null : data.tipos_cambio[m];
    const text = m === "USD" ? MONEDA_NOMBRE.USD : MONEDA_NOMBRE[m] + (rate ? "" : ": falta su tipo de cambio");
    return el("option", { value: m, text: text, disabled: m !== "USD" && !rate });
  }));
  currency.value = money.code;
  currency.addEventListener("change", () => {
    view.moneda = currency.value;
    saveMoneda(view.moneda);
    repaint();
  });
  const out = [
    el("p", { class: "muted", text:
      "Gasto de filas vivas: lo que gastaron los platos que siguen en la base"
      + (data.site ? "" : " y las lecturas de fichas del Menú")
      + ". No cuenta intentos fallidos, pruebas de comparación de modelos ni platos ya borrados de la papelera: la cuenta"
      + " exacta es la consola del proveedor. Cada sitio cuenta sus días con su propia hora." }),
    el("div", { class: "kit-edit" }, range, el("span", { class: "ops-pair" }, el("span", { class: "muted", text: "Moneda" }), currency)),
    el("p", { class: "muted", text: COBRO_USD }),
  ];
  if (money.code !== "USD") {
    out.push(el("p", { class: "muted", text: "El gasto se ve en " + MONEDA_NOMBRE[money.code] + ": 1 USD = "
      + fmtPrice(money.rate) + " " + money.code + ", guardado el " + dmy(money.fecha) + ". Los precios siguen en USD, como los da el proveedor." }));
  }

  const sumRow = (label, s, count) => el("tr", {},
    el("td", { text: label }),
    el("td", { class: "num", text: count }),
    el("td", { class: "num", text: fmtInt(s.entrada) }),
    el("td", { class: "num", text: fmtInt(s.salida) }),
    el("td", { class: "num", text: fmtMoney(s.usd, s.sin_precio, money) }));
  const sums = data.sitios.map((s) => sumRow(s.site, s, plural(s.platos, "plato", "platos")));
  if (data.fichas) sums.push(sumRow("Leer ficha (Menú)", data.fichas, plural(data.fichas.lecturas, "lectura", "lecturas")));
  const totalRow = sumRow("Total", data.total, "");
  totalRow.style.fontWeight = "700";
  sums.push(totalRow);
  out.push(table(["", { num: "Cuántos" }, { num: "Tokens que entraron" }, { num: "Tokens que salieron" }, { num: "Gasto" }], sums));

  out.push(el("h3", { text: "Por día" }));
  if (!data.filas.length) {
    out.push(el("p", { class: "muted", text: "Sin gasto en estos días." }));
  } else {
    out.push(table(["Día", "De dónde", "Modelo", { num: "Cuántos" }, { num: "Entraron" }, { num: "Salieron" }, { num: "Gasto" }], data.filas.map((f) => el("tr", {},
      el("td", { text: fmtDay(f.dia) }),
      el("td", { text: f.site || "Leer ficha (Menú)" }),
      el("td", {}, el("code", { text: f.modelo || "?" })),
      el("td", { class: "num", text: f.site ? plural(f.platos, "plato", "platos") : plural(f.lecturas, "lectura", "lecturas") }),
      el("td", { class: "num", text: fmtInt(f.entrada) }),
      el("td", { class: "num", text: fmtInt(f.salida) }),
      el("td", { class: "num", text: fmtMoney(f.usd, f.usd === null ? 1 : 0, money) })))));
  }

  const priceHelp = fieldHelp({
    que_es: data.ayuda_precios.que_es,
    ejemplo: PRICE_COLUMNS,
    recomendacion: data.ayuda_precios.recomendacion,
    copiar: false,
  });
  priceHelp.dataset.help = "precios";
  out.push(el("h3", { text: "Precios (USD por millón de tokens)" }), el("p", { class: "muted", text: PRICE_NOTE }), priceHelp);
  for (const model of data.modelos) out.push(priceRow(model, data.precios[model] || null, load, act));

  const rateHelp = fieldHelp({
    que_es: data.ayuda_tipos_cambio.que_es,
    ejemplo: data.ayuda_tipos_cambio.ejemplo,
    recomendacion: data.ayuda_tipos_cambio.recomendacion,
    copiar: false,
  });
  rateHelp.dataset.help = "tipos_cambio";
  out.push(el("h3", { text: "Tipos de cambio (cuánto vale 1 USD)" }), rateHelp);
  for (const moneda of data.monedas) out.push(rateRow(moneda, data.tipos_cambio[moneda] || null, load, act));
  return out;
}

function priceRow(model, price, load, act) {
  const inputs = {};
  const fields = PRICE_FIELDS.map(([key, label]) => {
    const saved = price ? fmtPrice(price[key]) : "";
    const input = el("input", {
      type: "text", inputmode: "decimal", autocomplete: "off", placeholder: "USD por MTok",
      "data-keep": "precio:" + model + ":" + key, "data-saved": saved,
    });
    input.value = saved;
    inputs[key] = input;
    return el("label", {}, el("span", { text: label }), input);
  });
  const error = el("div", { class: "kit-error", "data-error": "precio:" + model + ":" });
  const back = "guardar-precio:" + model;
  const save = el("button", { type: "button", class: "primary", "data-focus": back, text: "Guardar precio" });
  save.addEventListener("click", () => busy(save, () => act(async () => {
    error.textContent = "";
    const json = { model: model };
    for (const [key, label] of PRICE_FIELDS) {
      const raw = inputs[key].value.trim().replace(",", ".");
      if (!raw) {
        error.textContent = "Falta el precio de " + label + ": van los cuatro.";
        return;
      }
      if (!isDecimal(raw)) {
        error.textContent = label + " debe ser un número como 2.50 (sin signos ni letras).";
        return;
      }
      json[key] = Number(raw);
    }
    try {
      await api("/api/admin/gasto/precio", { method: "POST", json: json });
      toast("precio guardado: " + model, "ok");
      await load("precio:" + model + ":", back);
    } catch (err) {
      error.textContent = err.message;
    }
  })));
  const actions = el("div", { class: "kit-edit" }, save);
  if (price) {
    const remove = el("button", { type: "button", "data-focus": "quitar-precio:" + model, text: "Quitar precio" });
    remove.addEventListener("click", () => busy(remove, () => act(async () => {
      if (!window.confirm("¿Quitar el precio de " + model + "? Su gasto vuelve a mostrarse solo en tokens (sin precio).")) return;
      try {
        await api("/api/admin/gasto/precio/quitar", { method: "POST", json: { model: model } });
        toast("precio quitado: " + model, "ok");
        await load("precio:" + model + ":", back);
      } catch (err) {
        error.textContent = err.message;
      }
    })));
    actions.append(remove);
  }
  return el("div", { class: "ops-price" },
    el("div", { class: "kit-head" },
      el("code", { text: model }),
      el("span", { class: price ? "chip ok" : "chip", text: price ? "con precio" : "sin precio" })),
    el("div", {}, ...fields),
    actions,
    error);
}

// One currency's exchange rate (Section 3.17.1): how many of it 1 USD is worth, typed by the admin.
function rateRow(moneda, rate, load, act) {
  const saved = rate ? fmtPrice(rate.valor) : "";
  const input = el("input", {
    type: "text", inputmode: "decimal", autocomplete: "off", placeholder: MONEDA_EJEMPLO[moneda], style: "width: 110px;",
    "aria-label": "Cuántos " + MONEDA_PALABRA[moneda] + " vale 1 USD", "data-keep": "cambio:" + moneda, "data-saved": saved,
  });
  input.value = saved;
  const error = el("div", { class: "kit-error", "data-error": "cambio:" + moneda });
  const back = "guardar-cambio:" + moneda;
  const save = el("button", { type: "button", class: "primary", "data-focus": back, text: "Guardar" });
  save.addEventListener("click", () => busy(save, () => act(async () => {
    error.textContent = "";
    const raw = input.value.trim().replace(",", ".");
    if (!raw) {
      error.textContent = "Escribe cuántos " + MONEDA_PALABRA[moneda] + " vale 1 dólar, como " + MONEDA_EJEMPLO[moneda] + ".";
      return;
    }
    if (!isDecimal(raw)) {
      error.textContent = "El tipo de cambio es un número como " + MONEDA_EJEMPLO[moneda] + " (sin signos ni letras).";
      return;
    }
    try {
      await api("/api/admin/gasto/tipo_cambio", { method: "POST", json: { moneda: moneda, valor: Number(raw) } });
      toast("tipo de cambio guardado: " + moneda, "ok");
      await load("cambio:" + moneda, back);
    } catch (err) {
      error.textContent = err.message;
    }
  })));
  const actions = el("div", { class: "kit-edit" },
    el("span", { text: "1 USD =" }), input, el("span", { text: MONEDA_NOMBRE[moneda] }), save);
  if (rate) {
    const remove = el("button", { type: "button", "data-focus": "quitar-cambio:" + moneda, text: "Quitar" });
    remove.addEventListener("click", () => busy(remove, () => act(async () => {
      if (!window.confirm("¿Quitar el tipo de cambio de " + moneda + "? El gasto deja de verse en " + MONEDA_PALABRA[moneda]
        + " hasta que escribas otro.")) return;
      try {
        await api("/api/admin/gasto/tipo_cambio/quitar", { method: "POST", json: { moneda: moneda } });
        toast("tipo de cambio quitado: " + moneda, "ok");
        await load("cambio:" + moneda, back);
      } catch (err) {
        error.textContent = err.message;
      }
    })));
    actions.append(remove);
  }
  return el("div", { class: "ops-price" },
    el("div", { class: "kit-head" },
      el("strong", { text: moneda }),
      el("span", { class: rate ? "chip ok" : "chip", text: rate ? "guardado el " + dmy(rate.fecha) : "sin tipo de cambio" })),
    actions,
    error);
}

// --- Constantes ------------------------------------------------------------------------------

function constantesCard() {
  return card("Constantes de máquina",
    el("p", { class: "muted", text: "Las constantes de máquina y todas las perillas, con sus tres ayudas, se leen en Inicio: aquí no se repiten." }),
    el("p", {}, el("a", { href: "#constantes", text: "Ver las constantes en Inicio" })));
}
