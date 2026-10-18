// LPQ_VISION admin-sites.js: the Sitios y cuentas tab (family admin_sites; SPEC 1.3, 1.5; owner ruling 3.10.3).
// Each visible site: the auditor (do the cameras' roles add up to the functions?), its two functions, its
// cameras and, for the admin, its gafete (the machines' key) and its active switch. Each camera (Section 3.13.2):
// its light (🟢 Pi conectada, 🟢 Teléfono conectado: «nombre», 🔴 Nada conectado), Mirilla (a window on this
// page), its link ("Dame el enlace" and "Cambiar enlace": the admin and the site's manager; only the admin picks
// how long it lasts), its phone with Sacar, the phones waiting with Aceptar and Rechazar (the admin's) and
// Relevo a Pi; a Pi that sends nothing shows its link, so a phone can take its place. Cameras are added,
// re-roled and removed by the admin only (new ones are Pi by default). The admin also gets Crear restaurante
// (site, cameras, functions, gafete, Telegram group, menu photos and manager: all or nothing; the group's chat
// can be tested before creating, and the knobs the new restaurant inherits are shown read-only) and Cuentas
// (create; password change through a window and a "¿seguro?"; activate; role and site; delete with the typed
// name; recover). Every editable field shows its three helps (admin-kit's fieldHelp). New passwords go through
// admin-kit's newPassword() or passwordDialog(): viewable with "ver" while typed, never copyable. Nobody can SEE
// a stored password; the page never keeps one. A gafete appears only after "Mostrar gafete".
// Live (Section 3.13.1): every LIVE_MS the tab re-reads its sites (and, for the admin, the accounts) and rebuilds
// in place each card whose data changed, so a relevo made from a phone or another window shows up by itself.
// A card someone is using waits, with a note, until it is free: a field with the cursor in it, a value typed or
// chosen and not saved, a button still working or still being pressed, a gafete on screen (until "ocultar"), or
// an open window. A site added or removed elsewhere rebuilds the whole tab on the same terms, checked again right
// before it happens. The re-read never opens the login (an expired session waits for the next click) and dies with
// the tab. An action inside a site's card rebuilds that card alone (Section 3.13.2): what is typed in the other
// cards stays, and so does a value typed or chosen and not saved in that same card (it is carried into the new
// one). Every read carries a ticket: an answer older than what a card already shows never rolls it back.
// Section 3.17.1 (the owner's ✋ notes, 16 Oct): each camera sits in its own box and "Agregar una cámara" in a block of
// its own, apart from them. A link ends at 12:00 of its last day on the site's clock, and the page shows its end on that
// clock ("hora de <sitio>"); the admin picks 1 día, 1 semana, 1 mes, permanente or personalizado: the box for its days
// (only digits, 1 to 365) or its last day (a calendar, tomorrow to a year from the site's today) shows only then.
// Section 3.17.3 (owner, 17 Oct): the admin sees the captura address alone (no code) on top, with copiar; a link that
// already expired gets no new duration (the server refuses it: «Dame el enlace» makes a new code).

import { api, el, fmtDate, poll, qs, toast } from "/static/app.js";
import {
  DIGITS_HINT, SECCIONES, busy, card, chatField, confirmDialog, confirmFlow, fieldHelp, fmtValue, infoDialog,
  newPassword, passwordDialog, typeToConfirm,
} from "/static/admin-kit.js";

const SOURCES = [["phone", "teléfono"], ["pi", "Pi"]];
const ROLES = [["return", "regreso"], ["outgoing", "salida"], ["both", "ambas"]];
const ACCOUNT_ROLES = [["manager", "manager (un sitio)"], ["admin", "admin (todos los sitios)"]];
const EDGES = { top: "arriba", right: "derecha", bottom: "abajo", left: "izquierda" };
const INLINE = "width: auto;";
const CHECK = "width: auto; min-height: 0; margin: 0;";
const CHECK_LABEL = "display: inline-flex; align-items: center; gap: 6px; margin: 0; color: var(--ink); font-size: 15px;";
const MAX_CAMERAS = 8;
const LIVE_MS = 5000;           // the tab re-reads itself this often while it is on screen (owner ruling 3.13.1)
const PRESS_MAX_MS = 10000;     // a finger or a button held down keeps its card still at most this long...
const PRESS_AFTER_MS = 300;     // ...and until this long after its click, so a rebuild never eats a click
const HOLD_RETRY_MS = 1000;     // a card held by a press or a working button is read again this often until it is free
const FIELD_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA"]);
const LIVE_NOTE_CARD = "Hay cambios nuevos en esta tarjeta: aparecen en cuanto termines lo que estás editando.";
const LIVE_NOTE_HOLD = "Hay cambios nuevos en esta tarjeta: aparecen en cuanto ocultes el gafete.";
const LIVE_NOTE_TAB = "Se agregó o se quitó un sitio en otra ventana: la pestaña se actualiza en cuanto termines lo que estás editando.";
const LUZ = { pi: "🟢 Pi conectada", telefono: "🟢 Teléfono conectado", nada: "🔴 Nada conectado" };
const DURACIONES = [
  ["1d", "1 día"], ["7d", "1 semana"], ["30d", "1 mes"], ["permanente", "permanente"], ["personalizado", "personalizado"],
];
const DIAS_KEY = { 1: "1d", 7: "7d", 30: "30d" };   // a link's days back to its choice; none = permanente; others = personalizado
const DIAS_MAX = 365;                                 // personalizado: 1 to 365 days, or a last day within a year
const DAY_MS = 86400000;
const CAM_BLOCK = "border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 12px; margin: 8px 0; display: grid; gap: 6px;";
const ADD_BLOCK = "border: 1px dashed var(--line); border-radius: var(--radius); padding: 10px 12px; margin: 18px 0 8px; background: var(--bg);";
const LIGHT_ON = "font-weight: 600; color: var(--ok);";
const LIGHT_OFF = "font-weight: 600; color: var(--bad);";

// The three helps of this tab's fields (owner ruling 3.10.3): the example is also the field's gray placeholder.
const AYUDAS = {
  funciones: {
    que_es: "Qué analiza el sitio. Merma: lo que regresa en los platos. Emplatado: cómo salen los platos de la cocina.",
    ejemplo: "Merma y Emplatado prendidas",
    recomendacion: "Las dos prendidas cuando las cámaras ven los dos lados; el auditor avisa si cámaras y funciones no cuadran.",
    copiar: false,
  },
  camara: {
    que_es: "El nombre de la cámara dentro del sitio; así aparece en las alertas y en la galería.",
    ejemplo: "barra-1",
    recomendacion: "Minúsculas y sin espacios, con número si hay varias: barra-1, barra-2.",
  },
  fuente: {
    que_es: "Qué aparato toma las fotos: una Raspberry Pi, o un teléfono que entra con el enlace de la cámara.",
    ejemplo: "Pi",
    recomendacion: "Pi. Si la Pi no manda imagen, un teléfono toma su lugar con el enlace de la cámara (Dame el enlace) cuando el admin lo acepta.",
    copiar: false,
  },
  duracion: {
    que_es: "Hasta qué día sirve el enlace para que un teléfono nuevo pida entrar: vence a las 12:00 de ese día, en la hora "
      + "del sitio. Los teléfonos que ya entraron siguen aunque venza o cambie.",
    ejemplo: "1 semana",
    recomendacion: "1 semana. «personalizado»: de 1 a 365 días, o un día del calendario dentro de un año. Si el enlace se "
      + "compartió de más, «Cambiar enlace»: el anterior deja de servir y nadie sale.",
    copiar: false,
  },
  rol: {
    que_es: "Qué platos ve la cámara: los que regresan a la cocina (merma), los que salen (emplatado) o los dos.",
    ejemplo: "ambas",
    recomendacion: "ambas si una sola cámara ve la barra por donde pasan los dos; después calibra su borde-cocina en /camara.",
    copiar: false,
  },
  sitio: {
    que_es: "El nombre corto del restaurante dentro del sistema: sale en el mostrador, en las alertas y en las fotos. No se cambia después.",
    ejemplo: "roma",
    recomendacion: "Minúsculas, sin espacios ni acentos, de 2 a 24 caracteres (como polanco-2). No puede ser reference ni brand.",
  },
  chat: {
    que_es: "El grupo de Telegram del restaurante, donde llegan sus alertas. Escribe /id en ese grupo: el bot contesta con el número.",
    ejemplo: "1001234567890",
    recomendacion: "Pulsa «Enviar prueba» antes de crear. Si el grupo aún no existe, déjalo vacío: se pone después en Voz del bot.",
    copiar: false,
  },
  clonar: {
    que_es: "Copia al restaurante nuevo las fotos de referencia LOCALES de otra sucursal (las dos usan los mismos archivos).",
    ejemplo: "No clonar",
    recomendacion: "Si ya hay fotos universales, no clones: el restaurante nuevo las usa. Si todavía no hay, clona la sucursal más parecida.",
    copiar: false,
  },
  usuario: {
    que_es: "El nombre con el que la persona entra al mostrador. Un gerente solo ve y cambia su propio sitio.",
    ejemplo: "gerente.roma",
    recomendacion: "Minúsculas y sin espacios: gerente. más el sitio. El nombre no se cambia después.",
  },
  contrasena: {
    que_es: "La contraseña con la que esa cuenta entra. Nadie puede verla después, ni el admin: solo cambiarla.",
    ejemplo: "una frase de tres o cuatro palabras",
    recomendacion: "Al menos 10 caracteres; mejor una frase fácil de recordar. Dásela a la persona en persona, no por chat.",
    copiar: false,
  },
  miContrasena: {
    que_es: "La contraseña con la que entras al mostrador. Nadie más puede verla.",
    ejemplo: "una frase de tres o cuatro palabras",
    recomendacion: "Al menos 10 caracteres; mejor una frase fácil de recordar que no uses en otro lado.",
    copiar: false,
  },
  rolCuenta: {
    que_es: "Qué puede hacer la cuenta: manager ve y cambia solo su sitio; admin ve y cambia todos los sitios.",
    ejemplo: "manager (un sitio)",
    recomendacion: "manager para cada gerente, con su sitio; admin solo para quien administra la cadena. Cambiarlo cierra sus sesiones.",
    copiar: false,
  },
};

const enc = encodeURIComponent;
const word = (pairs, value) => (pairs.find((p) => p[0] === value) || [value, value])[1];

function btn(text, cls) {
  return el("button", { class: cls || "", type: "button", text: text });
}

// wire(button, error, fn): fn runs on click with the button busy; an error lands in the card's error line.
function wire(button, error, fn) {
  button.addEventListener("click", () => busy(button, async () => {
    error.textContent = "";
    try {
      await fn();
    } catch (err) {
      error.textContent = err.message;
    }
  }));
  return button;
}

function choice(pairs, value) {
  const node = el("select", { style: INLINE }, pairs.map((p) => el("option", { value: p[0], text: p[1] })));
  if (value) node.value = value;
  return node;
}

function checkbox(label, checked) {
  const box = el("input", { type: "checkbox", style: CHECK });
  box.checked = Boolean(checked);
  return { box: box, node: el("label", { style: CHECK_LABEL }, box, label) };
}

function field(label, input, help) {
  return el("div", { style: "margin: 8px 0;" }, el("label", { text: label }), input, help ? fieldHelp(help) : null);
}

// The three helps of a camera's fields, once under its rows (they serve every row of the table too).
function cameraHelps() {
  return el("div", {},
    fieldHelp(AYUDAS.camara, { name: "Nombre" }),
    fieldHelp(AYUDAS.fuente, { name: "Fuente" }),
    fieldHelp(AYUDAS.rol, { name: "Rol" }));
}

function sitePath(site) {
  return "/api/admin/sites/" + enc(site);
}

// A phone's actions name the camera of the row clicked: a phone that moved since the card was drawn answers 409.
function phonePath(s, c, id) {
  return sitePath(s.site) + "/cameras/" + enc(c.name) + "/telefonos/" + id;
}

export async function render(ctx) {
  const data = await api("/api/admin/sites" + qs({ site: ctx.site }));
  const view = {
    names: siteNames(data), note: el("div", { class: "kit-note hidden" }), cards: new Map(), parts: [], users: null, ticket: 0,
  };
  const box = el("div", {}, view.note, ctx.sess.admin ? capturaCard() : null);
  if (!data.sites.length) box.append(card("Sitios", el("p", { class: "muted", text: "No hay sitios que mostrar." })));
  for (const s of data.sites) {
    const node = remember(siteCard(s, ctx, view));
    view.cards.set(s.site, { node: node, sig: JSON.stringify(s), ticket: 0 });
    box.append(node);
  }
  if (ctx.sess.admin) {
    const global = await api("/api/admin/knobs");          // the global values a new restaurant inherits
    view.parts.push(remember(createCard(ctx, global.knobs.filter((k) => k.per_site))));
    const accounts = await api("/api/admin/users");
    const recent = await api("/api/admin/password-history" + qs({ limit: HISTORY_PREVIEW }));
    view.users = { node: remember(usersCard(ctx, accounts, recent)), sig: JSON.stringify(accounts) };
    box.append(view.parts[0], view.users.node);
  } else {
    view.parts.push(remember(myPasswordCard()));          // the admin changes its own from its row in Cuentas
    box.append(view.parts[0]);
  }
  const stop = poll(liveTick(ctx, view), LIVE_MS);
  ctx.onLeave(() => { view.gone = true; stop(); });
  return box;
}

// --- live: the tab follows the system by itself (Section 3.13.1) ---------------------------------

const BASELINES = new WeakMap();   // card -> its fields and the values they had when the card was built

function siteNames(data) {
  return data.sites.map((s) => s.site).join("|");
}

function valueOf(field) {
  return field.type === "checkbox" || field.type === "radio" ? String(field.checked) : field.value;
}

function remember(node) {
  BASELINES.set(node, [...node.querySelectorAll("input, select, textarea")].map((f) => [f, valueOf(f)]));
  node.addEventListener("pointerdown", () => { node.dataset.pressUntil = String(Date.now() + PRESS_MAX_MS); });
  node.addEventListener("click", () => { node.dataset.pressUntil = String(Date.now() + PRESS_AFTER_MS); });
  return node;
}

// In use: a window is open, the cursor is in one of its fields, a value was typed or chosen and not saved, a
// button is still working (busy() disables it until its answer lands, error line included) or still being
// pressed (from the press until just after its click), or a gafete is on screen. A field added after the card
// was built (another camera row) counts once it holds anything. A card in use is never rebuilt under someone's
// hands.
function inUse(node) {
  if (document.querySelector(".overlay")) return true;
  if (Number(node.dataset.pressUntil || 0) > Date.now()) return true;
  const active = document.activeElement;
  if (active && node.contains(active) && FIELD_TAGS.has(active.tagName)) return true;
  if (node.querySelector("[data-live-hold], button:disabled")) return true;
  const base = new Map(BASELINES.get(node) || []);
  return [...node.querySelectorAll("input, select, textarea")].some((f) =>
    base.has(f) ? valueOf(f) !== base.get(f) : !["", "false"].includes(valueOf(f)));
}

// The card's note says what it is waiting for; it goes away with the rebuild, or by itself when the data comes
// back to what the card already shows.
function flag(node) {
  const text = node.querySelector("[data-live-hold]") ? LIVE_NOTE_HOLD : LIVE_NOTE_CARD;
  let note = node.querySelector("[data-live-note]");
  if (!note) {
    note = el("p", { class: "kit-note" });
    note.dataset.liveNote = "1";
    const title = node.querySelector("h2");
    if (title) title.after(note); else node.prepend(note);
  }
  if (note.textContent !== text) note.textContent = text;
}

function unflag(node) {
  const note = node.querySelector("[data-live-note]");
  if (note) note.remove();
}

// A plain read: a 401 or a network hiccup skips this round instead of opening the login.
async function liveGet(path) {
  try {
    const res = await fetch(path, { credentials: "same-origin", cache: "no-store" });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

// After every await the tick looks again: the tab may be gone (another section on screen) or in use by now.
function liveTick(ctx, view) {
  let running = false;
  let first = true;
  return async () => {
    if (first) { first = false; return; }   // the render just read everything: the first round is the next one
    if (running || view.gone) return;       // a slow answer never stacks a second round on top
    running = true;
    try {
      const ticket = ++view.ticket;
      const data = await liveGet("/api/admin/sites" + qs({ site: ctx.site }));
      if (!data || view.gone) return;
      if (siteNames(data) !== view.names) {
        const all = () => [...view.parts, ...[...view.cards.values()].map((c) => c.node), ...(view.users ? [view.users.node] : [])];
        const busyTab = () => {
          if (!all().some(inUse)) return false;
          view.note.textContent = LIVE_NOTE_TAB;
          view.note.classList.remove("hidden");
          return true;
        };
        if (busyTab()) return;
        await ctx.refreshOverview();
        if (view.gone || busyTab()) return;
        ctx.refresh();
        return;
      }
      view.note.classList.add("hidden");
      for (const s of data.sites) {
        const entry = view.cards.get(s.site);
        const sig = JSON.stringify(s);
        if (!entry || entry.ticket > ticket) continue;     // the card was read again after this answer left
        if (entry.sig === sig) { unflag(entry.node); continue; }
        if (inUse(entry.node)) { flag(entry.node); continue; }
        const fresh = remember(siteCard(s, ctx, view));
        entry.node.replaceWith(fresh);
        Object.assign(entry, { node: fresh, sig: sig, ticket: ticket });
      }
      if (view.users) {
        const accounts = await liveGet("/api/admin/users");
        if (!accounts || view.gone) return;
        const sig = JSON.stringify(accounts);
        if (sig === view.users.sig) { unflag(view.users.node); return; }
        if (inUse(view.users.node)) { flag(view.users.node); return; }
        const recent = await liveGet("/api/admin/password-history" + qs({ limit: HISTORY_PREVIEW }));
        if (!recent || view.gone) return;
        if (inUse(view.users.node)) { flag(view.users.node); return; }
        const fresh = remember(usersCard(ctx, accounts, recent));
        view.users.node.replaceWith(fresh);
        view.users.node = fresh;
        view.users.sig = sig;
      }
    } finally {
      running = false;
    }
  };
}

// After an action inside a site's card, that card alone is read again and rebuilt (the other cards keep what is
// being typed in them); the live re-read then finds it current. A value typed or chosen and not saved in the card
// itself is carried into the new one (its field's data-key), so it still counts as unsaved, and so is the card's
// error line. A gafete on screen holds the card until "ocultar"; a button still pressed or still working holds it
// until it is free (then it is read again). A site that is gone rebuilds the whole tab.
async function refreshCard(ctx, view, site) {
  const ticket = ++view.ticket;
  const data = await liveGet("/api/admin/sites" + qs({ site: site }));
  const entry = view.cards.get(site);
  if (!data || view.gone || !entry || entry.ticket > ticket) return;   // a hiccup: the next live round brings it
  const s = data.sites.find((x) => x.site === site);
  if (!s) { ctx.refresh(); return; }
  if (entry.node.querySelector("[data-live-hold]")) { flag(entry.node); return; }
  if (Number(entry.node.dataset.pressUntil || 0) > Date.now() || entry.node.querySelector("button:disabled")) {
    setTimeout(() => refreshCard(ctx, view, site), HOLD_RETRY_MS);   // never under a finger or a working button
    return;
  }
  const kept = unsaved(entry.node);
  const said = errorLine(entry.node).textContent;
  const fresh = remember(siteCard(s, ctx, view));
  restore(fresh, kept);
  errorLine(fresh).textContent = said;
  entry.node.replaceWith(fresh);
  Object.assign(entry, { node: fresh, sig: JSON.stringify(s), ticket: ticket });
}

// A site card's own error line: the last one directly in the card (the windows have their own).
function errorLine(node) {
  const lines = node.querySelectorAll(":scope > .kit-error");
  return lines.length ? lines[lines.length - 1] : el("div", {});
}

// The card's keyed fields whose value is not the one the card was built with: [key, value] pairs.
function unsaved(node) {
  const base = new Map(BASELINES.get(node) || []);
  return [...node.querySelectorAll("[data-key]")]
    .filter((f) => base.has(f) && valueOf(f) !== base.get(f))
    .map((f) => [f.dataset.key, valueOf(f)]);
}

function restore(node, kept) {
  for (const [key, value] of kept) {
    const f = [...node.querySelectorAll("[data-key]")].find((x) => x.dataset.key === key);
    if (!f) continue;                                 // its camera is gone: nothing to carry
    if (f.type === "checkbox" || f.type === "radio") f.checked = value === "true"; else f.value = value;
  }
}

// --- one site --------------------------------------------------------------------------------

// here: the ctx its own actions use, whose refresh() rebuilds this card alone. Activar and Desactivar keep the
// whole tab's refresh (the site list above changes with them).
function siteCard(s, ctx, view) {
  const here = { ...ctx, refresh: () => refreshCard(ctx, view, s.site) };
  const error = el("div", { class: "kit-error" });
  const parts = [
    el("div", { class: "kit-head" },
      el("span", { class: s.active ? "chip ok" : "chip bad", text: s.active ? "activo" : "inactivo" }),
      el("span", { class: s.chat_set ? "chip ok" : "chip", text: s.chat_set ? "chat asignado" : "sin chat" })),
    el("h3", { text: "Auditor" }),
    s.auditor.length
      ? el("ul", {}, s.auditor.map((n) => el("li", { class: "kit-note", text: n })))
      : el("p", { class: "kit-reco", text: "Cámaras y funciones en orden." }),
  ];
  if (s.in_config) {
    parts.push(el("h3", { text: "Funciones" }), funcionesRow(s, here, error), fieldHelp(AYUDAS.funciones));
    parts.push(el("h3", { text: "Cámaras" }), camerasBlock(s, here, error));
    if (ctx.sess.admin) parts.push(addCameraBlock(s, here, error));
  }
  if (ctx.sess.admin) parts.push(el("h3", { text: "Gafete y estado" }), adminRow(s, ctx, error));
  parts.push(error);
  return card("Sitio " + s.site, ...parts);
}

function funcionesRow(s, ctx, error) {
  const merma = checkbox("Merma (platos que regresan)", s.funciones.merma);
  const pres = checkbox("Emplatado (platos que salen)", s.funciones.presentacion);
  merma.box.dataset.key = "merma";                    // data-key: a card rebuilt after an action keeps it if unsaved
  pres.box.dataset.key = "presentacion";
  const save = wire(btn("Guardar funciones", "primary"), error, async () => {
    await api(sitePath(s.site) + "/funciones", {
      method: "POST", json: { merma: merma.box.checked, presentacion: pres.box.checked },
    });
    toast("funciones guardadas", "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit" }, merma.node, pres.node, save);
}

function camerasBlock(s, ctx, error) {
  if (!s.cameras.length) return el("p", { class: "muted", text: "Sin cámaras." });
  return el("div", {}, s.cameras.map((c) => cameraBlock(s, c, ctx, error)));
}

// One camera (Section 3.13.2). A phone camera always offers its link; a Pi camera only while its Pi sends
// nothing (then a phone can take its place).
function cameraBlock(s, c, ctx, error) {
  const piDown = c.source === "pi" && c.luz !== "pi";
  const light = c.luz === "telefono" && c.telefono ? LUZ.telefono + ": «" + c.telefono.nombre + "»" : LUZ[c.luz];
  const edge = c.role === "both" ? " · " + (c.kitchen_edge ? "cocina: " + EDGES[c.kitchen_edge] : "sin calibrar") : "";
  const parts = [
    el("div", { class: "kit-head" },
      el("span", { class: "kit-name", text: c.name }),
      el("span", { class: "muted", text: word(SOURCES, c.source) + " · " + word(ROLES, c.role) + edge })),
    el("div", { style: c.luz === "nada" ? LIGHT_OFF : LIGHT_ON, text: light }),
  ];
  if (piDown) {
    parts.push(el("div", { class: "kit-note", text: "La Pi no manda imagen: un teléfono puede tomar su lugar con el enlace de esta cámara." }));
  }
  if (c.telefono) parts.push(phoneLine(s, c, ctx, error));
  for (const p of c.pendientes) parts.push(pendingLine(s, c, p, ctx, error));
  const offersLink = c.source === "phone" || piDown;
  if (offersLink) parts.push(el("div", { class: "muted", text: linkText(c.enlace, s) }));
  const buttons = [wire(btn("Mirilla"), error, () => openMirilla(s.site, c.name))];
  if (offersLink) {
    buttons.push(wire(btn("Dame el enlace", "primary"), error, async () => {
      const answer = await api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/enlace", { method: "POST" });
      const changed = await linkDialog(s, c, ctx, answer, answer.nuevo ? "Enlace nuevo." : "");
      if (answer.nuevo || changed) ctx.refresh();        // the same link, untouched: the card already shows it
    }));
    buttons.push(wire(btn("Cambiar enlace"), error, async () => {
      const ask = "El enlace de " + c.name + " cambia: el anterior deja de servir para entrar. Los teléfonos que ya "
        + "entraron siguen. ¿Cambiar?";
      if (!window.confirm(ask)) return;
      const answer = await api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/enlace/nuevo", { method: "POST", json: {} });
      await linkDialog(s, c, ctx, answer, "Enlace nuevo: el anterior ya no sirve.");
      ctx.refresh();
    }));
  }
  if (c.source === "phone") {
    buttons.push(wire(btn("Relevo a Pi"), error, async () => {
      const who = c.telefono ? " El teléfono «" + c.telefono.nombre + "» deja de capturarla y sale de la cámara." : "";
      const lost = c.kitchen_edge ? " Pierde su calibración (borde-cocina)." : "";
      if (!window.confirm("La cámara " + c.name + " vuelve a su Pi." + who + lost + " ¿Seguir?")) return;
      await api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/source", { method: "POST", json: { source: "pi" } });
      toast("relevo hecho: " + c.name + " ahora es Pi", "ok");
      ctx.refresh();
    }));
  }
  parts.push(el("div", { class: "kit-edit" }, buttons));
  if (ctx.sess.admin) parts.push(cameraAdminRow(s, c, ctx, error));
  return el("div", { style: CAM_BLOCK }, parts);
}

function codeText(code) {
  return code.slice(0, 4) + "-" + code.slice(4);
}

// A moment on the site's own clock, with that clock named (a link ends at noon of its last day there).
function siteTime(iso, s) {
  try {
    return new Date(iso).toLocaleString("es-MX", { dateStyle: "short", timeStyle: "short", timeZone: s.zona }) + " (hora de " + s.site + ")";
  } catch {
    return fmtDate(iso) + " (hora de este navegador)";   // a zone this browser does not know: its own clock, named
  }
}

function linkText(link, s) {
  if (!link) return "Sin enlace todavía: «Dame el enlace» crea uno.";
  const life = link.vence ? (link.vencido ? "venció el " : "vence el ") + siteTime(link.vence, s) : "permanente";
  return "Enlace: " + codeText(link.codigo) + " · " + life + (link.vencido ? " («Dame el enlace» crea uno nuevo)" : "");
}

// A day on the site's calendar, n days after "YYYY-MM-DD" (calendar arithmetic in UTC: no clock can shift it).
function dayAfter(iso, n) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d) + n * DAY_MS).toISOString().slice(0, 10);
}

// personalizado (Section 3.17.1): a switch between its days and its last day, shown only while personalizado is
// chosen. read() answers the fields the server takes, or throws the reason in plain words.
function customLife(link, hoy) {
  const first = dayAfter(hoy, 1);
  const last = dayAfter(hoy, DIAS_MAX);
  const days = el("input", {
    type: "text", inputmode: "numeric", autocomplete: "off", placeholder: "45", style: "width: 90px;",
    "aria-label": "Cuántos días dura el enlace (de 1 a " + DIAS_MAX + ")",
  });
  if (link.dias && !DIAS_KEY[link.dias]) days.value = String(link.dias);
  days.addEventListener("input", () => {
    const digits = days.value.replace(/\D/g, "").slice(0, 3);
    if (digits !== days.value) days.value = digits;
  });
  const date = el("input", { type: "date", min: first, max: last, style: INLINE, "aria-label": "Último día del enlace" });
  const byDays = el("button", { type: "button", class: "kit-copy", text: "Por días" });
  const byDate = el("button", { type: "button", class: "kit-copy", text: "Por fecha" });
  const daysRow = el("div", { class: "kit-edit" }, days, el("span", { class: "muted", text: "días (de 1 a " + DIAS_MAX + ")" }));
  const dateRow = el("div", { class: "kit-edit hidden" }, date,
    el("span", { class: "muted", text: "de mañana (" + dmy(first) + ") a un año (" + dmy(last) + ")" }));
  let mode = "dias";
  const show = (next) => {
    mode = next;
    daysRow.classList.toggle("hidden", mode !== "dias");
    dateRow.classList.toggle("hidden", mode !== "fecha");
    byDays.setAttribute("aria-pressed", String(mode === "dias"));
    byDate.setAttribute("aria-pressed", String(mode === "fecha"));
    byDays.className = mode === "dias" ? "kit-copy primary" : "kit-copy";
    byDate.className = mode === "fecha" ? "kit-copy primary" : "kit-copy";
  };
  byDays.addEventListener("click", () => { show("dias"); days.focus(); });
  byDate.addEventListener("click", () => { show("fecha"); date.focus(); });
  show("dias");
  const node = el("div", { class: "hidden", style: "margin: 4px 0 8px;" },
    el("div", { class: "kit-edit" }, byDays, byDate), daysRow, dateRow,
    el("div", { class: "muted", text: "Vence a las 12:00 del último día, en la hora del sitio." }));
  const read = () => {
    if (mode === "dias") {
      const n = Number(days.value);
      if (!days.value || !Number.isInteger(n) || n < 1 || n > DIAS_MAX) {
        throw new Error("Escribe cuántos días: un número de 1 a " + DIAS_MAX + ".");
      }
      return { dias: n };
    }
    if (!date.value) throw new Error("Elige el último día en el calendario.");
    if (date.value < first || date.value > last) {
      throw new Error("El último día va de mañana (" + dmy(first) + ") a un año desde hoy (" + dmy(last) + ").");
    }
    return { fecha: date.value };
  };
  return { node: node, read: read };
}

function dmy(iso) {
  const [y, m, d] = iso.split("-");
  return d + "/" + m + "/" + y;
}

function phoneLine(s, c, ctx, error) {
  const nombre = c.telefono.nombre;
  const out = wire(btn("Sacar", "bad"), error, async () => {
    if (!window.confirm("¿Sacar a «" + nombre + "» de " + c.name + "? Para volver tendrá que abrir el enlace y esperar a que el admin lo acepte.")) return;
    await api(phonePath(s, c, c.telefono.id) + "/sacar", { method: "POST" });
    toast("«" + nombre + "» salió de " + c.name, "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit" }, el("span", { text: "📱 Teléfono de la cámara: «" + nombre + "»" }), out);
}

function pendingLine(s, c, p, ctx, error) {
  const from = p.viene_de ? " (ahora es " + p.viene_de + ")" : p.otro_sitio ? " (ahora es una cámara de otro sitio)" : "";
  const text = "⏳ «" + p.nombre + "»" + from + " pide entrar · " + fmtDate(p.at);
  if (!ctx.sess.admin) return el("div", { class: "kit-note", text: text + ". Lo acepta el admin." });
  const accept = wire(btn("Aceptar", "ok"), error, async () => {
    const notes = [];
    if (c.source === "pi") {
      notes.push("La cámara " + c.name + " deja la Pi y pasa al teléfono «" + p.nombre + "»" + (c.kitchen_edge ? " (pierde su calibración)" : "") + ".");
    }
    if (p.viene_de) notes.push("«" + p.nombre + "» deja " + p.viene_de + ", que queda libre.");
    else if (p.otro_sitio) notes.push("«" + p.nombre + "» deja su cámara de otro sitio, que queda libre.");
    if (notes.length && !window.confirm(notes.join(" ") + " ¿Seguir?")) return;
    await api(phonePath(s, c, p.id) + "/aceptar", { method: "POST" });
    toast("«" + p.nombre + "» es ahora la cámara " + c.name, "ok");
    ctx.refresh();
  });
  const reject = wire(btn("Rechazar"), error, async () => {
    await api(phonePath(s, c, p.id) + "/rechazar", { method: "POST" });
    toast("pedido de «" + p.nombre + "» rechazado", "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit" }, el("span", { text: text }), accept, reject);
}

// The admin's own row of a camera: its role, and Quitar (the double confirm).
function cameraAdminRow(s, c, ctx, error) {
  const role = choice(ROLES, c.role);
  role.dataset.key = "rol:" + c.name;
  const saveRole = wire(btn("Guardar rol"), error, async () => {
    await api(sitePath(s.site) + "/cameras", { method: "POST", json: { name: c.name, source: c.source, role: role.value } });
    toast("cámara guardada", "ok");
    ctx.refresh();
  });
  const remove = wire(btn("Quitar", "bad"), error, async () => {
    const result = await confirmFlow(() =>
      api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/delete", { method: "POST" }));
    if (result) {
      toast("cámara quitada", "ok");
      ctx.refresh();
    }
  });
  return el("div", { class: "kit-edit" }, el("span", { class: "muted", text: "Rol" }), role, saveRole, remove);
}

// --- the captura address and the links' helpers (Section 3.17.3) ----------------------------------

// "copiar" beside a link, an address or a gafete: what it copies is read at the click (the link's window repaints its
// link); its label names what it copies.
function copyButton(read, said, label) {
  const copy = el("button", { class: "kit-copy", type: "button", text: "copiar", "aria-label": label });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(read());
      toast(said, "ok");
    } catch {
      toast("no se pudo copiar: selecciónalo a mano", "bad");
    }
  });
  return copy;
}

// A phone opens the camera only with https, and "localhost" on a phone is the phone itself: the link's window and the
// captura address say so when this mostrador was opened on this computer or without https (nothing otherwise).
function httpsNote(that, the) {
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  if (!local && location.protocol === "https:") return null;
  return el("p", {
    class: "kit-note",
    text: local
      ? "Abriste el mostrador en esta computadora (" + location.host + "): en un teléfono " + that + " no abre nada. Abre el "
        + "mostrador con la dirección https del club para dar enlaces."
      : "Abriste el mostrador sin https, así que " + the + " también: en el teléfono la cámara no abrirá. Abre el mostrador "
        + "con la dirección https del club para dar enlaces.",
  });
}

// The captura address alone, without a code (owner, 17 Oct: the admin only). By itself it lets no new phone in: a phone
// joins only with a camera's current code (inside its link, or typed in the page's box) and then the admin's Aceptar.
function capturaCard() {
  const address = location.origin + "/captura";
  const shown = el("code", { style: "word-break: break-all;", text: address });
  return card("Dirección de captura (sin código)",
    el("div", {}, shown, copyButton(() => address, "dirección copiada", "copiar la dirección de captura")),
    el("p", {
      class: "muted",
      text: "La página de captura de todas las cámaras. Sola no deja entrar a ningún teléfono nuevo: el teléfono escribe ahí "
        + "el código de su cámara (el que da «Dame el enlace», a ti o al gerente de ese restaurante) y espera tu Aceptar. "
        + "Lo normal es mandar el enlace de la cámara, que ya trae su código.",
    }),
    httpsNote("esa dirección", "la dirección"));
}

// --- windows on this page: the link and the mirilla (Section 3.13.2) ------------------------------
// Both are overlays: while one is open the tab's live re-read waits (inUse), and Escape or Cerrar closes it.

function overlayWindow(title, ...body) {
  const close = el("button", { class: "primary", type: "button", text: "Cerrar" });
  const overlay = el("div", { class: "overlay kit-dialog", role: "dialog", "aria-modal": "true" },
    el("div", { class: "card", style: "max-height: 90vh; overflow: auto; max-width: 720px;" },
      el("h2", { text: title }), ...body, el("div", { class: "kit-actions" }, close)));
  return new Promise((resolve) => {
    const onKey = (ev) => { if (ev.key === "Escape") done(); };
    function done() {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve();
    }
    close.addEventListener("click", done);
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
    close.focus();
  });
}

// The link's code goes after the # (it never reaches a server's logs). Resolves, once closed, with whether the
// admin changed its duration (the card then shows the new date). After "Guardar duración" the window shows the link
// as the server has it now (if someone changed it meanwhile, the new code: the old one no longer opens anything).
async function linkDialog(s, c, ctx, answer, note) {
  const link = answer.enlace;
  const clock = { site: s.site, zona: answer.zona || s.zona };
  let url = "";
  let current = "";
  let changed = false;
  const shown = el("code", { style: "word-break: break-all;" });
  const code = el("strong", {});
  const life = el("div", {});
  const paint = (l) => {
    current = l.codigo;
    url = location.origin + "/captura#" + l.codigo;
    shown.textContent = url;
    code.textContent = codeText(l.codigo);
    life.textContent = l.vence ? "Vence: " + siteTime(l.vence, clock) : "Permanente: no vence.";
  };
  paint(link);
  const copy = copyButton(() => url, "enlace copiado", "copiar el enlace");
  const body = [
    note ? el("p", { class: "kit-reco", text: note }) : null,
    el("div", {}, shown, copy),
    el("p", {}, el("span", { class: "muted", text: "Código: " }), code),
    life,
    httpsNote("ese enlace", "el enlace"),
    el("p", {
      class: "muted",
      text: "Mándalo por WhatsApp al teléfono que va a ser " + c.name + ". Lo abre en su navegador (con Tailscale encendido), "
        + "escribe su nombre y espera a que el admin lo acepte. Cada día, el mismo enlace lo lleva directo a su cámara, "
        + "si lo abre siempre en el mismo navegador: cada navegador del teléfono (Safari, Chrome, el de WhatsApp) guarda "
        + "su propio pase.",
    }),
  ];
  if (ctx.sess.admin) {
    const error = el("div", { class: "kit-error" });
    const pick = choice(DURACIONES, link.dias === null ? "permanente" : DIAS_KEY[link.dias] || "personalizado");
    const custom = customLife(link, answer.hoy);
    const follow = () => custom.node.classList.toggle("hidden", pick.value !== "personalizado");
    pick.addEventListener("change", follow);
    follow();
    const save = wire(btn("Guardar duración"), error, async () => {
      const json = { duracion: pick.value, ...(pick.value === "personalizado" ? custom.read() : {}) };
      let done;
      try {
        done = await api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/enlace/duracion", { method: "POST", json: json });
      } catch (err) {
        if (err.status === 410) {                     // Section 3.17.3: it expired and stays so; nothing to copy
          life.textContent = "Venció: ya no deja entrar a ningún teléfono nuevo.";
          copy.disabled = true;
        }
        throw err;
      }
      const moved = done.enlace.codigo !== current;
      paint(done.enlace);
      changed = true;
      const ends = done.enlace.vence ? "vence el " + siteTime(done.enlace.vence, clock) : "no vence";
      toast(moved ? "duración guardada; el enlace había cambiado: la ventana muestra el de ahora" : "duración guardada: " + ends, "ok");
    });
    body.push(el("div", { class: "kit-edit" }, el("span", { class: "muted", text: "Duración" }), pick, save),
      custom.node, fieldHelp(AYUDAS.duracion), error);
  }
  await overlayWindow("Enlace de " + c.name + " (" + s.site + ")", ...body.filter(Boolean));
  return changed;
}

async function openMirilla(site, camera) {
  const st = await api("/api/camera/state?site=" + enc(site));
  const img = el("img", { alt: "", class: "hidden", style: "width: 100%; display: block; border-radius: 8px; background: #000;" });
  const empty = el("p", { class: "muted", text: "pidiendo la copia…" });
  const info = el("p", { class: "muted" });
  let url = null;
  const tick = async () => {
    const res = await fetch("/api/camera/frame?site=" + enc(site) + "&camera=" + enc(camera) + "&_=" + Date.now(),
      { credentials: "same-origin", cache: "no-store" });
    if (res.status === 404) {
      img.classList.add("hidden");
      empty.classList.remove("hidden");
      empty.textContent = "Sin copia fresca: nada está mandando imagen de esta cámara.";
      info.textContent = "";
      return;
    }
    if (!res.ok) { empty.textContent = "No se pudo leer la copia (HTTP " + res.status + ")."; return; }
    const sharp = parseFloat(res.headers.get("X-Sharpness"));
    const age = parseFloat(res.headers.get("X-Frame-Age-S"));
    const blob = await res.blob();
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(blob);
    img.src = url;
    img.classList.remove("hidden");
    empty.classList.add("hidden");
    const stale = Number.isFinite(age) && age > 3 * st.frame_poll_s;
    info.textContent = (stale ? "Copia vieja: la cámara no está mandando. " : "") + "Copia de hace "
      + (Number.isFinite(age) ? Math.round(age) : "?") + " s · nitidez " + (Number.isFinite(sharp) ? sharp : "?")
      + " (piso " + st.sharpness_min + ").";
  };
  const stop = poll(tick, st.frame_poll_s * 1000);
  await overlayWindow("Mirilla · " + camera + " (" + site + ")", img, empty, info,
    el("p", { class: "muted", text: "Se actualiza sola cada " + st.frame_poll_s + " s mientras está abierta. Para alinear o calibrar: pestaña Cámara." }));
  stop();
  if (url) URL.revokeObjectURL(url);
}

// "Agregar una cámara" (Section 3.17.1): a block of its own under the cameras, with its title and the three helps of
// its fields, so it never reads as one more camera.
function addCameraBlock(s, ctx, error) {
  return el("div", { style: ADD_BLOCK },
    el("div", { style: "font-weight: 600; margin-bottom: 4px;", text: "Agregar una cámara a " + s.site }),
    addCameraRow(s, ctx, error),
    cameraHelps());
}

function addCameraRow(s, ctx, error) {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: AYUDAS.camara.ejemplo, style: INLINE, "data-key": "nueva:nombre" });
  const source = choice(SOURCES, "pi");
  const role = choice(ROLES, "both");
  source.dataset.key = "nueva:fuente";
  role.dataset.key = "nueva:rol";
  const add = wire(btn("Agregar cámara"), error, async () => {
    const wanted = name.value.trim();
    // A name the site already has is refused here at once, and by the server too (nueva: never an edit of it).
    if (s.cameras.some((c) => c.name === wanted)) {
      throw new Error("Ya hay una cámara " + wanted + " en " + s.site + ": su rol se cambia en su propia fila.");
    }
    await api(sitePath(s.site) + "/cameras", {
      method: "POST", json: { name: wanted, source: source.value, role: role.value, nueva: true },
    });
    name.value = "";                                   // saved: the row goes back to empty, not carried as unsaved
    source.value = "pi";
    role.value = "both";
    toast("cámara agregada", "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit", style: "margin-top: 8px;" }, name, source, role, add);
}

function showKey(box, key) {
  box.dataset.liveHold = "1";        // a gafete on screen holds its card still until "ocultar" (the live re-read waits)
  const copy = copyButton(() => key, "gafete copiado", "copiar el gafete");
  const hide = el("button", { class: "kit-copy", type: "button", text: "ocultar" });
  hide.addEventListener("click", () => {
    box.replaceChildren();
    delete box.dataset.liveHold;
  });
  box.replaceChildren(el("div", { class: "kit-helps" },
    el("div", {}, el("span", { class: "muted", text: "Gafete: " }), el("code", { text: key }), copy, hide),
    el("div", {
      class: "kit-note",
      text: "El gafete es la llave de las máquinas del sitio (el bot lo lee solo; una Pi lo lleva). Un teléfono no lo usa: "
        + "entra con el enlace de su cámara. No lo compartas por chat.",
    })));
}

function adminRow(s, ctx, error) {
  const shown = el("div", {});
  const reveal = wire(btn("Mostrar gafete"), error, async () => {
    const answer = await api(sitePath(s.site) + "/gafete/reveal", { method: "POST" });
    showKey(shown, answer.gafete);
  });
  const regen = wire(btn("Generar gafete nuevo", "warn"), error, async () => {
    const result = await confirmFlow(() => api(sitePath(s.site) + "/gafete/regenerate", { method: "POST" }));
    if (result) {
      showKey(shown, result.gafete);
      toast("gafete nuevo creado: la Pi necesita el nuevo; el bot lo lee solo", "ok");
    }
  });
  const active = s.active
    ? wire(btn("Desactivar sitio", "bad"), error, async () => {
      const result = await confirmFlow(() =>
        api(sitePath(s.site) + "/edit", { method: "POST", json: { active: false } }));
      if (result) {
        toast("sitio desactivado", "ok");
        await ctx.refreshOverview();
        ctx.refresh();
      }
    })
    : wire(btn("Activar sitio", "ok"), error, async () => {
      await api(sitePath(s.site) + "/edit", { method: "POST", json: { active: true } });
      toast("sitio activado", "ok");
      await ctx.refreshOverview();
      ctx.refresh();
    });
  return el("div", {}, el("div", { class: "kit-edit" }, reveal, regen, active), shown);
}

// --- Crear restaurante (admin) -------------------------------------------------------------------

function camInputs() {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: "barra-1", style: INLINE });
  const source = choice(SOURCES, "pi");
  const role = choice(ROLES, "both");
  return {
    node: el("div", { class: "kit-edit", style: "margin-bottom: 6px;" }, name, source, role),
    read: () => ({ name: name.value.trim(), source: source.value, role: role.value }),
  };
}

// What a new restaurant inherits: every per-site knob at its global value, read-only. Each one is given its
// own value later, in Perillas, with the site chosen.
function inheritedBlock(perSite) {
  const groups = new Map();
  for (const knob of perSite) {
    if (!groups.has(knob.seccion)) groups.set(knob.seccion, []);
    groups.get(knob.seccion).push(knob);
  }
  const rows = [];
  for (const [seccion, list] of groups) {
    rows.push(el("tr", {}, el("th", { colspan: "2", text: SECCIONES[seccion] || seccion })));
    for (const knob of list) {
      rows.push(el("tr", {},
        el("td", {}, el("div", { text: knob.que_es }), el("div", { class: "muted", text: knob.name })),
        el("td", { class: "num kit-value", text: fmtValue(knob.global) })));
    }
  }
  return el("details", { style: "margin: 12px 0;" },
    el("summary", { text: "Lo que hereda del global (solo lectura): " + perSite.length + " perillas" }),
    el("p", {
      class: "muted",
      text: "El restaurante nace con estos valores globales. Para darle valores propios: Perillas, eligiendo el sitio arriba.",
    }),
    el("table", {}, el("tbody", {}, rows)));
}

function createCard(ctx, perSite) {
  const error = el("div", { class: "kit-error" });
  const slug = el("input", { type: "text", autocomplete: "off", placeholder: AYUDAS.sitio.ejemplo });
  const merma = checkbox("Merma (platos que regresan)", true);
  const pres = checkbox("Emplatado (platos que salen)", true);
  const cams = [camInputs()];
  const camBox = el("div", {}, cams[0].node);
  const more = wire(btn("Otra cámara"), error, async () => {
    if (cams.length >= MAX_CAMERAS) throw new Error("Máximo " + MAX_CAMERAS + " cámaras por sitio.");
    const extra = camInputs();
    cams.push(extra);
    camBox.append(extra.node);
  });
  const chat = chatField(null);
  const test = wire(btn("Enviar prueba"), error, async () => {
    const chatId = chat.read();
    if (!chatId) throw new Error("Escribe primero el número del chat (escribe /id en el grupo de Telegram).");
    const answer = await api("/api/admin/voice/test", { method: "POST", json: { chat_id: chatId } });
    toast("prueba enviada: " + answer.enviado, "ok");
  });
  const clone = el("select", {},
    el("option", { value: "", text: "No clonar (empieza sin fotos de referencia)" }),
    ctx.overview.sites.map((s) => el("option", { value: s.site, text: "Clonar las fotos de referencia de " + s.site })));
  const user = el("input", { type: "text", autocomplete: "off", placeholder: AYUDAS.usuario.ejemplo });
  const pass = newPassword();
  const create = wire(btn("Crear restaurante", "primary"), error, async () => {
    const password = pass.read();
    const chatId = chat.read();
    const answer = await api("/api/admin/sites", {
      method: "POST",
      json: {
        site: slug.value.trim(),
        funciones: { merma: merma.box.checked, presentacion: pres.box.checked },
        cameras: cams.map((c) => c.read()).filter((c) => c.name),
        chat_id: chatId,
        clone_menu_from: clone.value || null,
        manager_usuario: user.value.trim(),
        manager_password: password,
      },
    });
    pass.clear();
    toast("restaurante " + answer.site + " creado (" + answer.fotos_clonadas + " fotos clonadas"
      + (chatId ? ", con su grupo de Telegram" : "") + ")", "ok");
    await ctx.refreshOverview();
    ctx.refresh();
  });
  return card("Crear restaurante",
    el("p", {
      class: "muted",
      text: "Nace todo junto o nada: el sitio, sus cámaras, sus funciones, su gafete, su grupo de Telegram, las fotos "
        + "de referencia clonadas y la cuenta de su gerente.",
    }),
    field("Nombre del sitio", slug, AYUDAS.sitio),
    el("label", { text: "Funciones" }),
    el("div", { class: "kit-edit" }, merma.node, pres.node),
    fieldHelp(AYUDAS.funciones),
    el("h3", { text: "Cámaras" }), camBox, el("div", { class: "kit-edit" }, more), cameraHelps(),
    el("h3", { text: "Grupo de Telegram (opcional)" }),
    el("div", { class: "kit-edit" }, chat.node, test),
    el("div", { class: "kit-hint", text: DIGITS_HINT }),
    fieldHelp(AYUDAS.chat),
    field("Menú", clone, AYUDAS.clonar),
    field("Usuario del gerente", user, AYUDAS.usuario),
    field("Contraseña del gerente (escríbela dos veces)", pass.node, AYUDAS.contrasena),
    inheritedBlock(perSite),
    el("div", { class: "kit-edit" }, create),
    error);
}

// --- Cuentas (admin) ---------------------------------------------------------------------------------

function roleEditor(role, site, siteNames) {
  const roleSel = choice(ACCOUNT_ROLES, role);
  const siteSel = el("select", { style: INLINE },
    el("option", { value: "", text: "(sin sitio)" }),
    siteNames.map((n) => el("option", { value: n, text: n })));
  siteSel.value = site || "";
  const sync = () => {
    siteSel.disabled = roleSel.value === "admin";
    if (siteSel.disabled) siteSel.value = "";
  };
  roleSel.addEventListener("change", sync);
  sync();
  return {
    node: el("span", { class: "kit-edit" }, roleSel, siteSel),
    read: () => ({ role: roleSel.value, site: roleSel.value === "manager" ? (siteSel.value || null) : null }),
  };
}

function userRow(u, siteNames, ctx, error, recoveryDays, activeAdmins) {
  const self = u.usuario === ctx.sess.user;
  const lastAdmin = u.role === "admin" && u.active && activeAdmins <= 1;
  // Your own row and the only active admin's row offer nothing the server would refuse.
  const locked = self || lastAdmin;
  let roleCell;
  if (locked) {
    roleCell = el("span", { class: "muted", text: "admin (todos los sitios)" });
  } else {
    const editor = roleEditor(u.role, u.site, siteNames);
    const saveRole = wire(btn("Guardar rol"), error, async () => {
      await api("/api/admin/users/" + enc(u.usuario) + "/role", { method: "POST", json: editor.read() });
      toast("rol guardado: sus sesiones se cerraron", "ok");
      ctx.refresh();
    });
    roleCell = el("span", { class: "kit-edit" }, editor.node, saveRole);
  }
  const toggle = locked ? null : wire(btn(u.active ? "Desactivar" : "Activar", u.active ? "bad" : "ok"), error, async () => {
    await api("/api/admin/users/" + enc(u.usuario) + "/active", { method: "POST", json: { on: !u.active } });
    toast(u.active ? "cuenta desactivada" : "cuenta activada", "ok");
    ctx.refresh();
  });
  // Delete: gate 1 = type the exact name; gate 2 = the server's double confirm.
  const remove = locked ? null : wire(btn("Eliminar", "bad"), error, async () => {
    const typed = await typeToConfirm({
      title: "Eliminar la cuenta " + u.usuario + " (#" + u.uid + ")",
      name: u.usuario,
      warning: "La cuenta se cierra de inmediato y se borra para siempre en " + recoveryDays
        + " días. Hasta entonces se puede recuperar en «Eliminadas».",
    });
    if (!typed) return;
    const result = await confirmFlow(() =>
      api("/api/admin/users/" + enc(u.usuario) + "/delete", { method: "POST", json: { name: u.usuario } }));
    if (result) {
      toast("cuenta eliminada: se puede recuperar hasta el " + fmtDate(result.borra), "ok");
      ctx.refresh();
    }
  });
  // Nobody SEES a password: the admin changes one through a window, then the server's "¿seguro?".
  const setPass = wire(btn("Cambiar contraseña"), error, async () => {
    const typed = await passwordDialog({
      title: "Nueva contraseña para " + u.usuario + " (#" + u.uid + ")",
      note: "Escríbela dos veces. Con «ver» puedes leerla para dársela a la persona; después nadie podrá verla.",
      help: self ? AYUDAS.miContrasena : AYUDAS.contrasena,
    });
    if (!typed) return;
    const result = await confirmFlow(() =>
      api("/api/admin/users/" + enc(u.usuario) + "/password", { method: "POST", json: { password: typed.password } }));
    if (result) {
      toast("contraseña cambiada: sus sesiones se cerraron", "ok");
      if (self) location.reload();
      else ctx.refresh();
    }
  });
  const history = wire(el("button", { class: "kit-copy", type: "button", text: "historial" }), error,
    () => openHistory("Historial de " + u.usuario + " #" + u.uid, { uid: u.uid }));
  const by = u.password_changed_by;
  const changed = u.password_changed_at
    ? "cambiada el " + fmtDate(u.password_changed_at) + " por "
      + (by === u.usuario ? "la misma cuenta" : by === "set_password" ? "el servidor (set_password)" : by)
    : "sin cambios desde que se creó";
  return el("tr", {},
    el("td", {},
      el("span", { class: "kit-name", text: u.usuario }),
      el("span", { class: "muted", text: " #" + u.uid + (self ? " (tú)" : "") }),
      lastAdmin && !self ? el("span", { class: "chip warn", text: "único admin activo" }) : null),
    el("td", {}, roleCell),
    el("td", {}, el("span", { class: "kit-edit" },
      el("span", { class: u.active ? "chip ok" : "chip bad", text: u.active ? "activa" : "desactivada" }), toggle, remove)),
    el("td", {}, el("div", { class: "muted", text: changed }), el("span", { class: "kit-edit" }, setPass, history)));
}

function newUserForm(siteNames, ctx, error) {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: AYUDAS.usuario.ejemplo, style: INLINE });
  const pass = newPassword();
  const editor = roleEditor("manager", siteNames[0] || "", siteNames);
  const create = wire(btn("Crear cuenta", "primary"), error, async () => {
    const password = pass.read();
    await api("/api/admin/users", {
      method: "POST",
      json: Object.assign({ usuario: name.value.trim(), password: password }, editor.read()),
    });
    pass.clear();
    toast("cuenta creada", "ok");
    ctx.refresh();
  });
  return el("div", {},
    el("h3", { text: "Nueva cuenta" }),
    el("div", { class: "kit-edit" }, name, pass.node, editor.node, create),
    fieldHelp(AYUDAS.usuario, { name: "Usuario" }),
    fieldHelp(AYUDAS.contrasena, { name: "Contraseña" }));
}

function deletedTable(rows, ctx, error) {
  return el("table", {},
    el("thead", {}, el("tr", {}, ["Usuario", "Rol y sitio", "Eliminada", "Se borra", ""].map((h) => el("th", { text: h })))),
    el("tbody", {}, rows.map((u) => el("tr", {},
      el("td", {}, el("span", { class: "kit-name", text: u.usuario }), el("span", { class: "muted", text: " #" + u.uid })),
      el("td", { text: u.role + (u.site ? ", " + u.site : "") }),
      el("td", { text: fmtDate(u.deleted_at) }),
      el("td", { text: fmtDate(u.borra) }),
      el("td", {}, wire(btn("Recuperar", "ok"), error, async () => {
        await api("/api/admin/users/" + enc(u.usuario) + "/recover", { method: "POST" });
        toast("cuenta recuperada y activa", "ok");
        ctx.refresh();
      }))))));
}

function usersCard(ctx, data, recent) {
  const error = el("div", { class: "kit-error" });
  const siteNames = ctx.overview.sites.map((s) => s.site);
  const activeAdmins = data.users.filter((u) => u.role === "admin" && u.active).length;
  const parts = [
    el("p", {
      class: "muted",
      text: "Cambiar la contraseña, el rol o desactivar una cuenta cierra sus sesiones abiertas. Eliminar la cierra "
        + "de inmediato y la borra para siempre en " + data.recovery_days + " días; hasta entonces se recupera abajo. "
        + "El número (#) de cada cuenta es permanente y nunca se repite. Nadie puede ver una contraseña: solo cambiarla.",
    }),
    fieldHelp(AYUDAS.rolCuenta, { name: "Rol y sitio" }),
    el("table", {},
      el("thead", {}, el("tr", {}, ["Usuario", "Rol y sitio", "Estado", "Contraseña"].map((h) => el("th", { text: h })))),
      el("tbody", {}, data.users.map((u) => userRow(u, siteNames, ctx, error, data.recovery_days, activeAdmins)))),
    newUserForm(siteNames, ctx, error),
  ];
  if (data.eliminadas.length) {
    parts.push(el("h3", { text: "Eliminadas: se recuperan en " + data.recovery_days + " días" }), deletedTable(data.eliminadas, ctx, error));
  }
  parts.push(
    el("h3", { text: "Últimos cambios de contraseña" }),
    historyTable(recent.historial, "Todavía no hay cambios de contraseña registrados."),
    el("div", { class: "kit-edit", style: "margin-top: 8px;" },
      wire(btn("Ver historial completo"), error, () => openHistory("Historial de contraseñas", {}))));
  parts.push(error);
  return card("Cuentas", ...parts);
}

// --- Historial de contraseñas (admin) ---------------------------------------------------------------

const HISTORY_PREVIEW = 5;
const HISTORY_NOTE = "Del más nuevo al más viejo. Nadie puede ver una contraseña: aquí solo queda quién la puso y "
  + "cuándo. Los cambios hechos por el servidor (set_password por ssh) solo aparecen en la columna Contraseña.";

function historyTable(rows, emptyText) {
  if (!rows.length) return el("p", { class: "muted", text: emptyText });
  const label = (name, uid) => (name || "?") + (uid ? " #" + uid : "");
  return el("table", {},
    el("thead", {}, el("tr", {}, ["Fecha", "Cuenta", "Qué pasó", "Por quién"].map((h) => el("th", { text: h })))),
    el("tbody", {}, rows.map((h) => el("tr", {},
      el("td", { text: fmtDate(h.ts) }),
      el("td", { text: label(h.cuenta, h.uid) }),
      el("td", { text: h.que }),
      el("td", { text: label(h.por, h.por_uid) })))));
}

// openHistory(title, params): the full history ({}) or one account's ({uid}), in a read-only window.
async function openHistory(title, params) {
  const data = await api("/api/admin/password-history" + qs(params));
  await infoDialog({
    title: title,
    body: el("div", {},
      el("p", { class: "muted", text: "Hasta " + data.limite + " cambios. " + HISTORY_NOTE }),
      historyTable(data.historial, "Sin cambios de contraseña registrados.")),
  });
}

// --- Mi contraseña (every account) ---------------------------------------------------------------------

function myPasswordCard() {
  const error = el("div", { class: "kit-error" });
  const change = wire(btn("Cambiar mi contraseña", "primary"), error, async () => {
    const typed = await passwordDialog({
      title: "Cambiar mi contraseña",
      current: true,
      note: "Escribe la actual y luego la nueva dos veces. Después entrarás de nuevo con la nueva.",
      help: AYUDAS.miContrasena,
    });
    if (!typed) return;
    const sure = await confirmDialog({
      action: "cambiar tu contraseña",
      diff: { resumen: "¿Seguro que quieres cambiar tu contraseña? Se cerrarán tus sesiones abiertas y entrarás de nuevo con la nueva." },
      expires_s: 60,
    });
    if (!sure) return;
    await api("/api/account/password", { method: "POST", json: { current: typed.current, password: typed.password } });
    toast("contraseña cambiada: entra con la nueva", "ok");
    setTimeout(() => location.reload(), 1200);
  });
  return card("Mi contraseña",
    el("p", { class: "muted", text: "Cambia tu propia contraseña: primero la actual, luego la nueva dos veces. Nadie más puede verla." }),
    el("div", { class: "kit-edit" }, change),
    error);
}
