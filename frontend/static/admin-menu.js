// LPQ_VISION admin-menu.js: the Menú tab of the mostrador (family admin_menu; SPEC 1.5).
// The dishes are the DATABASE's menu (menu.yaml is only the seed). Their definitions apply to every site and
// belong to the admin: Crear platillo, edit (a change in the SET of component names bumps the menu version
// behind the double confirm), on/off (off behind the double confirm). The reference photos belong to each site:
// with a site chosen (a manager always has its own), every dish shows its diet copies with Subir, Reemplazar
// and Quitar. The uploader keeps the original untouched and gives the model a light copy; a photo shared with
// another site is never modified (copy-on-diverge). A manager reads the definitions without editing them.

import { api, el, qs, toast } from "/static/app.js";
import { busy, card, confirmDialog, confirmFlow } from "/static/admin-kit.js";

const INLINE = "width: auto;";
const enc = encodeURIComponent;
const CONDITION_WORDS = { normal: "luz normal", lampara: "con lámpara" };

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
    ? "Estás viendo las fotos de " + data.site + "."
    : "Elige un sitio arriba para ver y subir sus fotos.";
  const box = el("div", {}, card("Menú (versión " + data.menu_version + ")",
    el("p", {
      class: "muted",
      text: "Las definiciones valen para todos los sitios; las fotos de referencia son de cada sitio. " + where
        + " Un cambio aquí cuenta desde el siguiente plato analizado.",
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
      text: "Nace prendido y en la versión actual del menú (no la sube). Sus fotos de referencia se suben después, sitio por sitio.",
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

// --- reference photos (the chosen site) ---------------------------------------------------------

async function sendPhoto(d, data, file, condition, replace) {
  const form = new FormData();
  form.append("photo", file);
  form.append("site", data.site);
  form.append("dish_id", d.dish_id);
  form.append("condition", condition);
  if (replace) form.append("replace", replace);
  return api("/api/admin/menu/photos", { method: "POST", form: form });
}

function photoTile(d, p, data, ctx, error) {
  const img = el("img", { class: "thumb", alt: d.nombre, src: "/api/admin/menu/photo" + qs({ path: p.photo_path }) });
  const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: "display: none;" });
  const replaceBtn = el("button", { class: "kit-copy", type: "button", text: "Reemplazar" });
  replaceBtn.addEventListener("click", () => file.click());
  file.addEventListener("change", () => busy(replaceBtn, async () => {
    error.textContent = "";
    try {
      if (!file.files.length) return;
      await sendPhoto(d, data, file.files[0], p.condition, p.photo_path);
      toast("foto reemplazada en " + data.site, "ok");
      ctx.refresh();
    } catch (err) {
      error.textContent = err.message;
    }
  }));
  const remove = wire(el("button", { class: "kit-copy", type: "button", text: "Quitar" }), error, async () => {
    const result = await confirmFlow(() => api("/api/admin/menu/photos/delete", {
      method: "POST",
      json: { site: data.site, dish_id: d.dish_id, photo_path: p.photo_path, condition: p.condition },
    }));
    if (result) {
      toast("foto quitada de " + data.site, "ok");
      ctx.refresh();
    }
  });
  return el("div", {},
    img,
    el("div", { class: "muted", text: (CONDITION_WORDS[p.condition] || p.condition) + (p.subida ? " · subida aquí" : " · colocada a mano") }),
    p.compartida_con.length
      ? el("div", { class: "kit-note", text: "Compartida con " + p.compartida_con.join(", ") + ": reemplazarla aquí no cambia la de ellos." })
      : null,
    el("div", { class: "kit-edit" }, replaceBtn, remove, file));
}

function photosBlock(d, data, ctx, error) {
  const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: INLINE });
  const condition = el("select", { style: INLINE },
    data.conditions.map((c) => el("option", { value: c, text: CONDITION_WORDS[c] || c })));
  const upload = wire(btn("Subir foto", "primary"), error, async () => {
    if (!file.files.length) throw new Error("Elige una foto primero.");
    await sendPhoto(d, data, file.files[0], condition.value, null);
    toast("foto subida a " + data.site, "ok");
    ctx.refresh();
  });
  return el("div", {},
    d.photos.length
      ? el("div", { class: "grid" }, d.photos.map((p) => photoTile(d, p, data, ctx, error)))
      : el("p", { class: "muted", text: "Sin fotos en este sitio: el modelo trabaja solo con la definición." }),
    el("div", { class: "kit-edit", style: "margin-top: 8px;" }, file, condition, upload),
    el("div", {
      class: "muted",
      text: "JPEG o PNG, hasta " + data.upload_max_mb + " MB. Se guarda el original intacto y una copia ligera "
        + "(lado mayor " + data.diet_max_side + " px), que es la que ve el modelo.",
    }));
}

// --- one dish -------------------------------------------------------------------------------------

function dishCard(d, data, ctx) {
  const error = el("div", { class: "kit-error" });
  const parts = [
    el("div", { class: "kit-head" },
      el("span", { class: "muted", text: d.dish_id }),
      el("span", { class: d.activo ? "chip ok" : "chip bad", text: d.activo ? "prendido" : "apagado" }),
      el("span", { class: "chip", text: "versión " + d.menu_version })),
  ];
  if (ctx.sess.admin) parts.push(definitionEditor(d, ctx, error), activeRow(d, ctx, error));
  else parts.push(componentsTable(d.componentes));
  if (data.site) parts.push(el("h3", { text: "Fotos de referencia en " + data.site }), photosBlock(d, data, ctx, error));
  parts.push(error);
  return card(d.nombre, ...parts);
}
