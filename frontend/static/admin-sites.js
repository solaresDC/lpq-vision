// LPQ_VISION admin-sites.js: the Sitios y cuentas tab (family admin_sites; SPEC 1.3, 1.5).
// Each visible site: the auditor (do the cameras' roles add up to the functions?), its two functions, its
// cameras (add, change role, relevo, remove) and, for the admin, its gafete and its active switch. The admin
// also gets Crear restaurante (site, cameras, functions, gafete, menu photos and manager: all or nothing)
// and Cuentas (create; password change through a window and a "¿seguro?"; activate; role and site; delete
// with the typed name; recover). New passwords go through admin-kit's newPassword() or passwordDialog():
// viewable with "ver" while typed, never copyable. Nobody can SEE a stored password; the page never keeps one.
// A gafete appears only after "Mostrar gafete".

import { api, el, fmtDate, qs, toast } from "/static/app.js";
import { busy, card, confirmDialog, confirmFlow, infoDialog, newPassword, passwordDialog, typeToConfirm } from "/static/admin-kit.js";

const SOURCES = [["phone", "teléfono"], ["pi", "Pi"]];
const ROLES = [["return", "regreso"], ["outgoing", "salida"], ["both", "ambas"]];
const ACCOUNT_ROLES = [["manager", "manager (un sitio)"], ["admin", "admin (todos los sitios)"]];
const EDGES = { top: "arriba", right: "derecha", bottom: "abajo", left: "izquierda" };
const INLINE = "width: auto;";
const CHECK = "width: auto; min-height: 0; margin: 0;";
const CHECK_LABEL = "display: inline-flex; align-items: center; gap: 6px; margin: 0; color: var(--ink); font-size: 15px;";
const MAX_CAMERAS = 8;

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

function field(label, input) {
  return el("div", { style: "margin: 8px 0;" }, el("label", { text: label }), input);
}

function sitePath(site) {
  return "/api/admin/sites/" + enc(site);
}

export async function render(ctx) {
  const data = await api("/api/admin/sites" + qs({ site: ctx.site }));
  const box = el("div", {});
  if (!data.sites.length) box.append(card("Sitios", el("p", { class: "muted", text: "No hay sitios que mostrar." })));
  for (const s of data.sites) box.append(siteCard(s, ctx));
  if (ctx.sess.admin) {
    box.append(createCard(ctx));
    box.append(await usersCard(ctx));
  }
  box.append(myPasswordCard());
  return box;
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
    parts.push(el("h3", { text: "Funciones" }), funcionesRow(s, ctx, error));
    parts.push(el("h3", { text: "Cámaras" }), camerasTable(s, ctx, error), addCameraRow(s, ctx, error));
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
  const name = el("input", { type: "text", autocomplete: "off", placeholder: "barra-2", style: INLINE });
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
  const copy = el("button", { class: "kit-copy", type: "button", text: "copiar" });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(key);
      toast("gafete copiado", "ok");
    } catch {
      toast("no se pudo copiar: selecciónalo a mano", "bad");
    }
  });
  box.replaceChildren(el("div", { class: "kit-helps" },
    el("div", {}, el("span", { class: "muted", text: "Gafete: " }), el("code", { text: key }), copy),
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

function createCard(ctx) {
  const error = el("div", { class: "kit-error" });
  const slug = el("input", { type: "text", autocomplete: "off", placeholder: "roma" });
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
  const clone = el("select", {},
    el("option", { value: "", text: "No clonar (empieza sin fotos de referencia)" }),
    ctx.overview.sites.map((s) => el("option", { value: s.site, text: "Clonar las fotos de referencia de " + s.site })));
  const user = el("input", { type: "text", autocomplete: "off", placeholder: "gerente.roma" });
  const pass = newPassword();
  const create = wire(btn("Crear restaurante", "primary"), error, async () => {
    const password = pass.read();
    const answer = await api("/api/admin/sites", {
      method: "POST",
      json: {
        site: slug.value.trim(),
        funciones: { merma: merma.box.checked, presentacion: pres.box.checked },
        cameras: cams.map((c) => c.read()).filter((c) => c.name),
        clone_menu_from: clone.value || null,
        manager_usuario: user.value.trim(),
        manager_password: password,
      },
    });
    pass.clear();
    toast("restaurante " + answer.site + " creado (" + answer.fotos_clonadas + " fotos clonadas)", "ok");
    await ctx.refreshOverview();
    ctx.refresh();
  });
  return card("Crear restaurante",
    el("p", {
      class: "muted",
      text: "Nace todo junto o nada: el sitio, sus cámaras, sus funciones, su gafete, las fotos de referencia "
        + "clonadas y la cuenta de su gerente.",
    }),
    field("Nombre del sitio (minúsculas, sin espacios, como roma o polanco-2)", slug),
    el("div", { class: "kit-edit" }, merma.node, pres.node),
    el("h3", { text: "Cámaras" }), camBox, el("div", { class: "kit-edit" }, more),
    field("Menú", clone),
    field("Usuario del gerente", user),
    field("Contraseña del gerente (escríbela dos veces)", pass.node),
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
    el("td", {}, el("div", { class: "muted", text: changed }), setPass));
}

function newUserForm(siteNames, ctx, error) {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: "gerente.roma", style: INLINE });
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
  return el("div", {}, el("h3", { text: "Nueva cuenta" }), el("div", { class: "kit-edit" }, name, pass.node, editor.node, create));
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

async function usersCard(ctx) {
  const data = await api("/api/admin/users");
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
    el("table", {},
      el("thead", {}, el("tr", {}, ["Usuario", "Rol y sitio", "Estado", "Contraseña"].map((h) => el("th", { text: h })))),
      el("tbody", {}, data.users.map((u) => userRow(u, siteNames, ctx, error, data.recovery_days, activeAdmins)))),
    newUserForm(siteNames, ctx, error),
  ];
  if (data.eliminadas.length) {
    parts.push(el("h3", { text: "Eliminadas: se recuperan en " + data.recovery_days + " días" }), deletedTable(data.eliminadas, ctx, error));
  }
  parts.push(el("div", { class: "kit-edit", style: "margin-top: 10px;" }, historyButton(error)));
  parts.push(error);
  return card("Cuentas", ...parts);
}

// --- Historial de contraseñas (admin) ---------------------------------------------------------------

function historyButton(error) {
  return wire(btn("Historial de contraseñas"), error, async () => {
    const data = await api("/api/admin/password-history");
    const label = (name, uid) => (name || "?") + (uid ? " #" + uid : "");
    const body = data.historial.length
      ? el("table", {},
        el("thead", {}, el("tr", {}, ["Fecha", "Cuenta", "Qué pasó", "Por quién"].map((h) => el("th", { text: h })))),
        el("tbody", {}, data.historial.map((h) => el("tr", {},
          el("td", { text: fmtDate(h.ts) }),
          el("td", { text: label(h.cuenta, h.uid) }),
          el("td", { text: h.que }),
          el("td", { text: label(h.por, h.por_uid) })))))
      : el("p", { class: "muted", text: "Todavía no hay cambios de contraseña registrados." });
    await infoDialog({
      title: "Historial de contraseñas",
      body: el("div", {},
        el("p", {
          class: "muted",
          text: "Los últimos " + data.limite + " cambios, del más nuevo al más viejo. Nadie puede ver una contraseña: "
            + "aquí solo queda quién la puso y cuándo. Los cambios hechos por el servidor (set_password por ssh) solo "
            + "aparecen en la columna Contraseña de cada cuenta.",
        }),
        body),
    });
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
