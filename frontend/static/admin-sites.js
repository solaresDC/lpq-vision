// LPQ_VISION admin-sites.js: the Sitios y cuentas tab (family admin_sites; SPEC 1.3, 1.5; owner ruling 3.10.3).
// Each visible site: the auditor (do the cameras' roles add up to the functions?), its two functions, its
// cameras (add, change role, relevo, remove) and, for the admin, its gafete and its active switch. The admin
// also gets Crear restaurante (site, cameras, functions, gafete, Telegram group, menu photos and manager: all
// or nothing; the group's chat can be tested before creating, and the knobs the new restaurant inherits are
// shown read-only) and Cuentas (create; password change through a window and a "¿seguro?"; activate; role and
// site; delete with the typed name; recover). Every editable field shows its three helps (admin-kit's
// fieldHelp). New passwords go through admin-kit's newPassword() or passwordDialog(): viewable with "ver" while
// typed, never copyable. Nobody can SEE a stored password; the page never keeps one. A gafete appears only
// after "Mostrar gafete".
// Live (Section 3.13.1): every LIVE_MS the tab re-reads its sites (and, for the admin, the accounts) and rebuilds
// in place each card whose data changed, so a relevo made from a phone or another window shows up by itself.
// A card someone is using waits, with a note, until it is free: a field with the cursor in it, a value typed or
// chosen and not saved, a button still working, a gafete on screen (until "ocultar"), or an open window. A site
// added or removed elsewhere rebuilds the whole tab on the same terms, checked again right before it happens. The
// re-read never opens the login (an expired session waits for the next click) and dies with the tab.

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
const FIELD_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA"]);
const LIVE_NOTE_CARD = "Hay cambios nuevos en esta tarjeta: aparecen en cuanto termines lo que estás editando.";
const LIVE_NOTE_HOLD = "Hay cambios nuevos en esta tarjeta: aparecen en cuanto ocultes el gafete.";
const LIVE_NOTE_TAB = "Se agregó o se quitó un sitio en otra ventana: la pestaña se actualiza en cuanto termines lo que estás editando.";

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
    que_es: "Qué aparato toma las fotos: el teléfono con la página de captura, o una Raspberry Pi.",
    ejemplo: "teléfono",
    recomendacion: "teléfono. «Relevo» cambia la fuente de una cámara que ya existe.",
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

export async function render(ctx) {
  const data = await api("/api/admin/sites" + qs({ site: ctx.site }));
  const view = { names: siteNames(data), note: el("div", { class: "kit-note hidden" }), cards: new Map(), parts: [], users: null };
  const box = el("div", {}, view.note);
  if (!data.sites.length) box.append(card("Sitios", el("p", { class: "muted", text: "No hay sitios que mostrar." })));
  for (const s of data.sites) {
    const node = remember(siteCard(s, ctx));
    view.cards.set(s.site, { node: node, sig: JSON.stringify(s) });
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
  return node;
}

// In use: a window is open, the cursor is in one of its fields, a value was typed or chosen and not saved, a
// button is still working (busy() disables it until its answer lands, error line included), or a gafete is on
// screen. A field added after the card was built (another camera row) counts once it holds anything. A card in
// use is never rebuilt under someone's hands.
function inUse(node) {
  if (document.querySelector(".overlay")) return true;
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
        if (!entry) continue;
        if (entry.sig === sig) { unflag(entry.node); continue; }
        if (inUse(entry.node)) { flag(entry.node); continue; }
        const fresh = remember(siteCard(s, ctx));
        entry.node.replaceWith(fresh);
        entry.node = fresh;
        entry.sig = sig;
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

// --- one site --------------------------------------------------------------------------------

function siteCard(s, ctx) {
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
    parts.push(el("h3", { text: "Funciones" }), funcionesRow(s, ctx, error), fieldHelp(AYUDAS.funciones));
    parts.push(el("h3", { text: "Cámaras" }), camerasTable(s, ctx, error), addCameraRow(s, ctx, error), cameraHelps());
  }
  if (ctx.sess.admin) parts.push(el("h3", { text: "Gafete y estado" }), adminRow(s, ctx, error));
  parts.push(error);
  return card("Sitio " + s.site, ...parts);
}

function funcionesRow(s, ctx, error) {
  const merma = checkbox("Merma (platos que regresan)", s.funciones.merma);
  const pres = checkbox("Emplatado (platos que salen)", s.funciones.presentacion);
  const save = wire(btn("Guardar funciones", "primary"), error, async () => {
    await api(sitePath(s.site) + "/funciones", {
      method: "POST", json: { merma: merma.box.checked, presentacion: pres.box.checked },
    });
    toast("funciones guardadas", "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit" }, merma.node, pres.node, save);
}

function camerasTable(s, ctx, error) {
  if (!s.cameras.length) return el("p", { class: "muted", text: "Sin cámaras." });
  return el("table", {},
    el("thead", {}, el("tr", {}, ["Cámara", "Fuente", "Rol", "Borde-cocina", ""].map((h) => el("th", { text: h })))),
    el("tbody", {}, s.cameras.map((c) => cameraRow(s, c, ctx, error))));
}

function cameraRow(s, c, ctx, error) {
  const role = choice(ROLES, c.role);
  const saveRole = wire(btn("Guardar rol"), error, async () => {
    await api(sitePath(s.site) + "/cameras", { method: "POST", json: { name: c.name, source: c.source, role: role.value } });
    toast("cámara guardada", "ok");
    ctx.refresh();
  });
  const other = c.source === "phone" ? "pi" : "phone";
  const relevo = wire(btn("Relevo a " + word(SOURCES, other)), error, async () => {
    const lost = c.kitchen_edge ? " y pierde su calibración (borde-cocina)" : "";
    if (!window.confirm("La cámara " + c.name + " pasa a fuente «" + word(SOURCES, other) + "»" + lost + ". ¿Seguir?")) return;
    await api(sitePath(s.site) + "/cameras/" + enc(c.name) + "/source", { method: "POST", json: { source: other } });
    toast("relevo hecho: " + c.name + " ahora es " + word(SOURCES, other), "ok");
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
  const edge = c.role === "both" ? (c.kitchen_edge ? EDGES[c.kitchen_edge] : "sin calibrar") : "no aplica";
  return el("tr", {},
    el("td", { class: "kit-name", text: c.name }),
    el("td", { text: word(SOURCES, c.source) }),
    el("td", {}, el("span", { class: "kit-edit" }, role, saveRole)),
    el("td", { text: edge }),
    el("td", {}, el("span", { class: "kit-edit" }, relevo, remove)));
}

function addCameraRow(s, ctx, error) {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: AYUDAS.camara.ejemplo, style: INLINE });
  const source = choice(SOURCES, "phone");
  const role = choice(ROLES, "both");
  const add = wire(btn("Agregar cámara"), error, async () => {
    await api(sitePath(s.site) + "/cameras", {
      method: "POST", json: { name: name.value.trim(), source: source.value, role: role.value },
    });
    toast("cámara agregada", "ok");
    ctx.refresh();
  });
  return el("div", { class: "kit-edit", style: "margin-top: 8px;" }, name, source, role, add);
}

function showKey(box, key) {
  box.dataset.liveHold = "1";        // a gafete on screen holds its card still until "ocultar" (the live re-read waits)
  const copy = el("button", { class: "kit-copy", type: "button", text: "copiar" });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(key);
      toast("gafete copiado", "ok");
    } catch {
      toast("no se pudo copiar: selecciónalo a mano", "bad");
    }
  });
  const hide = el("button", { class: "kit-copy", type: "button", text: "ocultar" });
  hide.addEventListener("click", () => {
    box.replaceChildren();
    delete box.dataset.liveHold;
  });
  box.replaceChildren(el("div", { class: "kit-helps" },
    el("div", {}, el("span", { class: "muted", text: "Gafete: " }), el("code", { text: key }), copy, hide),
    el("div", {
      class: "kit-note",
      text: "Para inscribir un teléfono: instala /captura en su pantalla de inicio, ábrela desde ese ícono y pega "
        + "este gafete (instalar primero, inscribir después). Puedes abrir el mostrador en ese mismo teléfono para "
        + "copiarlo. No lo compartas por chat.",
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
      toast("gafete nuevo creado: inscribe otra vez cada dispositivo", "ok");
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
  const source = choice(SOURCES, "phone");
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
