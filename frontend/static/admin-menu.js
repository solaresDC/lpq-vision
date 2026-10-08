// LPQ_VISION admin-menu.js: the Menú tab of the mostrador (family admin_menu; SPEC 1.5; owner ruling 3.10.1).
// The dishes are the DATABASE's menu (menu.yaml is only the seed). Their definitions apply to every site and
// belong to the admin: Crear platillo, edit (a change in the SET of component names bumps the menu version
// behind the double confirm), on/off (off behind the double confirm). Reference photos come in two levels:
// UNIVERSAL (the brand's, for every site current and future; the admin's) and LOCAL (each site's own; they win
// over the universal ones for that site). Every photo shown here is its ORIGINAL at full resolution; the model
// receives a light copy of the same photo, which nobody sees. A manager sees the universal photos and manages
// only its own site's local ones; the server enforces that too.

import { api, el, qs, toast } from "/static/app.js";
import { busy, card, confirmDialog, confirmFlow } from "/static/admin-kit.js";

const INLINE = "width: auto;";
const enc = encodeURIComponent;
const CONDITION_WORDS = { normal: "luz normal", lampara: "con lámpara" };
const USA_WORDS = {
  local: "El modelo usa las fotos locales de este sitio.",
  universal: "Este sitio no tiene fotos locales de este platillo: el modelo usa las universales.",
  ninguna: "Sin fotos: el modelo trabaja solo con la definición.",
};

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

// finish(answer): a family answer is either the result itself or step 1 of the double confirm; this ends both.
async function finish(answer) {
  if (!answer || !answer.pending_id) return answer;
  if (!(await confirmDialog(answer))) return null;
  const done = await api("/api/admin/confirm", { method: "POST", json: { pending_id: answer.pending_id } });
  return done.result;
}

export async function render(ctx) {
  const data = await api("/api/admin/menu" + qs({ site: ctx.site }));
  const where = data.site
    ? "Estás viendo las fotos universales y las locales de " + data.site + "."
    : "Estás viendo las fotos universales; elige un sitio arriba para ver también sus fotos locales.";
  const box = el("div", {}, card("Menú (versión " + data.menu_version + ")",
    el("p", {
      class: "muted",
      text: "Las definiciones y las fotos universales valen para todas las sucursales, actuales y futuras. Si una "
        + "sucursal sube fotos locales de un platillo, el modelo usa esas; si no, usa las universales. " + where,
    }),
    el("p", {
      class: "muted",
      text: "Aquí ves cada foto completa (tócala para abrirla en grande). El modelo recibe una copia ligera de la "
        + "misma foto, que nadie ve. Un cambio aquí cuenta desde el siguiente plato analizado.",
    })));
  if (ctx.sess.admin) box.append(createDishCard(ctx));
  for (const d of data.dishes) box.append(dishCard(d, data, ctx));
  return box;
}

// --- definitions ----------------------------------------------------------------------------

function componentsTable(componentes) {
  return el("table", {},
    el("thead", {}, el("tr", {}, el("th", { text: "Componente" }), el("th", { class: "num", text: "Porción (g)" }))),
    el("tbody", {}, componentes.map((c) => el("tr", {},
      el("td", { text: c.nombre }),
      el("td", { class: "num", text: String(c.porcion_g) })))));
}

function compRow(c) {
  const name = el("input", { type: "text", autocomplete: "off", placeholder: "pan_trigo", style: INLINE });
  name.value = c ? c.nombre : "";
  const grams = el("input", { type: "number", min: "1", step: "1", placeholder: "90", style: "width: 110px;" });
  grams.value = c ? String(c.porcion_g) : "";
  const remove = el("button", { class: "kit-copy", type: "button", text: "quitar" });
  const row = el("div", { class: "kit-edit", style: "margin-bottom: 6px;" }, name, grams, el("span", { class: "muted", text: "g" }), remove);
  remove.addEventListener("click", () => row.remove());
  row.readComp = () => ({ nombre: name.value.trim(), porcion_g: Number(grams.value) });
  return row;
}

function readComps(box) {
  return [...box.children].map((row) => row.readComp()).filter((c) => c.nombre);
}

function createDishCard(ctx) {
  const error = el("div", { class: "kit-error" });
  const id = el("input", { type: "text", autocomplete: "off", placeholder: "tartine_pollo", style: INLINE });
  const nombre = el("input", { type: "text", placeholder: "Tartine de pollo", style: INLINE });
  const plate = el("input", { type: "text", placeholder: "(opcional)", style: INLINE });
  const comps = el("div", {}, compRow(null), compRow(null));
  const add = btn("Otro componente");
  add.addEventListener("click", () => comps.append(compRow(null)));
  const create = wire(btn("Crear platillo", "primary"), error, async () => {
    const answer = await api("/api/admin/menu/dishes", {
      method: "POST",
      json: { dish_id: id.value.trim(), nombre: nombre.value.trim(), plate_type: plate.value.trim() || null, componentes: readComps(comps) },
    });
    toast("platillo " + answer.dish_id + " creado (versión " + answer.menu_version + ")", "ok");
    ctx.refresh();
  });
  return card("Crear platillo",
    el("p", {
      class: "muted",
      text: "Nace prendido y en la versión actual del menú (no la sube). Sus fotos se suben después: universales aquí, "
        + "locales en cada sucursal.",
    }),
    el("div", { class: "kit-edit" },
      el("span", { class: "muted", text: "Clave:" }), id,
      el("span", { class: "muted", text: "Nombre:" }), nombre,
      el("span", { class: "muted", text: "Tipo de plato:" }), plate),
    el("h3", { text: "Componentes (minúsculas_con_guion_bajo y gramos)" }),
    comps,
    el("div", { class: "kit-edit" }, add, create),
    error);
}

function definitionEditor(d, ctx, error) {
  const nombre = el("input", { type: "text", style: INLINE });
  nombre.value = d.nombre;
  const plate = el("input", { type: "text", placeholder: "(opcional)", style: INLINE });
  plate.value = d.plate_type || "";
  const comps = el("div", {}, d.componentes.map((c) => compRow(c)));
  const add = btn("Otro componente");
  add.addEventListener("click", () => comps.append(compRow(null)));
  const save = wire(btn("Guardar definición", "primary"), error, async () => {
    const result = await finish(await api("/api/admin/menu/dishes/" + enc(d.dish_id) + "/edit", {
      method: "POST",
      json: { nombre: nombre.value.trim(), plate_type: plate.value.trim() || null, componentes: readComps(comps) },
    }));
    if (result) {
      toast(result.bump ? "definición guardada: el menú pasó a la versión " + result.menu_version : "definición guardada", "ok");
      ctx.refresh();
    }
  });
  return el("div", {},
    el("div", { class: "kit-edit" },
      el("span", { class: "muted", text: "Nombre:" }), nombre,
      el("span", { class: "muted", text: "Tipo de plato:" }), plate),
    el("h3", { text: "Componentes (minúsculas_con_guion_bajo y gramos)" }),
    comps,
    el("div", { class: "kit-edit" }, add, save),
    el("div", {
      class: "kit-note",
      text: "Cambiar el nombre, el tipo o una porción no cambia la versión. Agregar, quitar o renombrar un "
        + "componente sube la versión del menú (con confirmación).",
    }));
}

function activeRow(d, ctx, error) {
  const toggle = wire(btn(d.activo ? "Apagar platillo" : "Prender platillo", d.activo ? "bad" : "ok"), error, async () => {
    const result = await finish(await api("/api/admin/menu/dishes/" + enc(d.dish_id) + "/active", {
      method: "POST", json: { on: !d.activo },
    }));
    if (result) {
      toast(d.activo ? "platillo apagado" : "platillo prendido", "ok");
      ctx.refresh();
    }
  });
  return el("div", { class: "kit-edit", style: "margin-top: 8px;" }, toggle);
}

// --- reference photos: universal (level = data.brand) and local (level = the site) ----------------

async function sendPhoto(level, d, file, condition, replace) {
  const form = new FormData();
  form.append("photo", file);
  form.append("site", level);
  form.append("dish_id", d.dish_id);
  form.append("condition", condition);
  if (replace) form.append("replace", replace);
  return api("/api/admin/menu/photos", { method: "POST", form: form });
}

function photoTile(d, p, level, editable, ctx, error) {
  const src = "/api/admin/menu/photo" + qs({ path: p.photo_path });
  const img = el("img", { class: "thumb", alt: d.nombre, src: src, loading: "lazy" });
  const parts = [
    el("a", { href: src, target: "_blank", rel: "noopener", title: "Ver completa" }, img),
    el("div", {
      class: "muted",
      text: (CONDITION_WORDS[p.condition] || p.condition) + (p.original ? "" : " · sin original: se ve la copia ligera"),
    }),
  ];
  if (p.compartida_con.length) {
    parts.push(el("div", { class: "kit-note", text: "Compartida con " + p.compartida_con.join(", ") + ": cambiarla aquí no cambia la de ellos." }));
  }
  if (editable) {
    const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: "display: none;" });
    const replaceBtn = el("button", { class: "kit-copy", type: "button", text: "Reemplazar" });
    replaceBtn.addEventListener("click", () => file.click());
    file.addEventListener("change", () => busy(replaceBtn, async () => {
      error.textContent = "";
      try {
        if (!file.files.length) return;
        await sendPhoto(level, d, file.files[0], p.condition, p.photo_path);
        toast("foto reemplazada", "ok");
        ctx.refresh();
      } catch (err) {
        error.textContent = err.message;
      }
    }));
    const remove = wire(el("button", { class: "kit-copy", type: "button", text: "Quitar" }), error, async () => {
      const result = await confirmFlow(() => api("/api/admin/menu/photos/delete", {
        method: "POST",
        json: { site: level, dish_id: d.dish_id, photo_path: p.photo_path, condition: p.condition },
      }));
      if (result) {
        toast("foto quitada", "ok");
        ctx.refresh();
      }
    });
    parts.push(el("div", { class: "kit-edit" }, replaceBtn, remove, file));
  }
  return el("div", {}, ...parts);
}

function uploadRow(level, d, data, ctx, error) {
  const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: INLINE });
  const condition = el("select", { style: INLINE },
    data.conditions.map((c) => el("option", { value: c, text: CONDITION_WORDS[c] || c })));
  const upload = wire(btn("Subir foto", "primary"), error, async () => {
    if (!file.files.length) throw new Error("Elige una foto primero.");
    await sendPhoto(level, d, file.files[0], condition.value, null);
    toast("foto subida", "ok");
    ctx.refresh();
  });
  return el("div", {},
    el("div", { class: "kit-edit", style: "margin-top: 8px;" }, file, condition, upload),
    el("div", {
      class: "muted",
      text: "JPEG o PNG, hasta " + data.upload_max_mb + " MB. Se guarda completa para verla aquí; el modelo recibe "
        + "una copia ligera (lado mayor " + data.diet_max_side + " px).",
    }));
}

function photoBlock(title, rows, level, editable, emptyText, d, data, ctx, error) {
  return el("div", {},
    el("h3", { text: title }),
    rows.length
      ? el("div", { class: "grid" }, rows.map((p) => photoTile(d, p, level, editable, ctx, error)))
      : el("p", { class: "muted", text: emptyText }),
    editable ? uploadRow(level, d, data, ctx, error) : null);
}

// --- one dish -------------------------------------------------------------------------------------

function dishCard(d, data, ctx) {
  const error = el("div", { class: "kit-error" });
  const admin = ctx.sess.admin;
  const parts = [
    el("div", { class: "kit-head" },
      el("span", { class: "muted", text: d.dish_id }),
      el("span", { class: d.activo ? "chip ok" : "chip bad", text: d.activo ? "prendido" : "apagado" }),
      el("span", { class: "chip", text: "versión " + d.menu_version })),
  ];
  if (admin) parts.push(definitionEditor(d, ctx, error), activeRow(d, ctx, error));
  else parts.push(componentsTable(d.componentes));
  parts.push(photoBlock("Fotos universales (todas las sucursales)", d.universales, data.brand, admin,
    admin ? "Sin fotos universales: súbelas aquí." : "Sin fotos universales.", d, data, ctx, error));
  if (data.site) {
    parts.push(photoBlock("Fotos locales de " + data.site, d.locales, data.site, true,
      "Sin fotos locales en " + data.site + ".", d, data, ctx, error));
    parts.push(el("div", { class: d.usa === "ninguna" ? "kit-note" : "kit-reco", text: USA_WORDS[d.usa] }));
  }
  parts.push(error);
  return card(d.nombre, ...parts);
}
