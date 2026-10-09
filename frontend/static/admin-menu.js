// LPQ_VISION admin-menu.js: the Menú tab of the mostrador, the recetario book (family admin_menu; SPEC 1.5;
// owner rulings 3.10.1 and 3.10.2). One page per dish, turned sideways like the restaurant's printed recetario:
// a swipe on the phone, ◀ ▶ or the arrow keys on the computer, a "Página N de M" counter and an index with each
// dish's small photo. A page is read-only until Editar. The admin also orders the pages (one order for the
// brand, the same in every site), reads a photo of one recetario page into a new page (Leer ficha) or writes one
// by hand (Crear a mano): nothing is saved until Guardar. Nobody types or sees an internal key: the server makes
// the clave from the name and each component key from its label. "Tipo de plato" stays out until a báscula
// exists. Every photo shown here is its ORIGINAL; the model receives a light copy nobody sees. A photo opens in a
// viewer inside the page (admin-kit's photoViewer), never a new tab. A manager reads the book and manages only
// its own site's local photos; the server enforces that too.

import { api, el, qs, toast } from "/static/app.js";
import { busy, card, confirmDialog, confirmFlow, photoViewer } from "/static/admin-kit.js";

const enc = encodeURIComponent;
const CONDITION_WORDS = { normal: "luz normal", lampara: "con lámpara" };
const USA_WORDS = {
  local: "El modelo usa las fotos locales de este sitio.",
  universal: "Este sitio no tiene fotos locales de este platillo: el modelo usa las universales.",
  ninguna: "Sin fotos: el modelo trabaja solo con la definición.",
};
const EDIT_NOTE = "Cambiar el nombre, el nombre de un componente, una porción o las notas no cambia la versión del "
  + "menú. Agregar o quitar un componente la sube (te pide confirmación).";

const STYLE = `
.mb-book { display: flex; overflow-x: auto; scroll-snap-type: x mandatory; align-items: flex-start; overscroll-behavior-x: contain; }
.mb-book.mb-editing { overflow-x: hidden; }
.mb-page { flex: 0 0 100%; min-width: 0; scroll-snap-align: start; scroll-snap-stop: always; padding: 0 1px; }
.mb-nav { display: flex; align-items: center; justify-content: center; gap: 12px; margin: 4px 0 10px; }
.mb-counter { font-weight: 600; min-width: 140px; text-align: center; }
.mb-title { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 6px; }
.mb-title h2 { font-size: 22px; margin: 0; }
.mb-index { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); margin-top: 10px; }
.mb-item { display: flex; flex-direction: column; align-items: stretch; gap: 6px; padding: 6px; min-height: 0; text-align: left; }
.mb-item.mb-off { opacity: .6; }
.mb-thumb { width: 100%; aspect-ratio: 4 / 3; object-fit: cover; border-radius: 6px; background: var(--bg); display: flex; align-items: center; justify-content: center; font-weight: 700; color: var(--muted); }
.mb-item-name { font-size: 14px; }
.mb-open { padding: 0; border: 0; background: none; min-height: 0; width: 100%; display: block; cursor: zoom-in; }
.mb-comp { margin-bottom: 6px; }
.mb-comp .mb-label { width: auto; min-width: 180px; flex: 1 1 200px; }
.mb-comp .mb-grams { width: 110px; min-width: 90px; }
.mb-doubt { border-color: var(--warn); background: #fdf3e2; }
.mb-name { max-width: 520px; }
.mb-notes { width: 100%; min-height: 80px; padding: 8px 12px; border: 1px solid var(--line); border-radius: var(--radius); font: inherit; resize: vertical; background: var(--paper); }
.mb-notes-text { white-space: pre-wrap; }
.mb-dudas { background: #fdf3e2; border: 1px solid #f4dcae; border-radius: var(--radius); padding: 10px 12px; margin: 10px 0; }
.mb-dudas ul { margin: 6px 0 0; }
.mb-order { padding-left: 22px; }
.mb-order li { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.mb-order-name { flex: 1; }
.mb-preview { max-width: 220px; margin: 10px 0; }
`;

function injectStyle() {
  if (document.getElementById("admin-menu-style")) return;
  document.head.append(el("style", { id: "admin-menu-style", text: STYLE }));
}

// What survives a re-render (a save, a site change): the page being read, the mode, and an unsaved new page.
const view = { at: null, mode: "book", draft: null };

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

// Two labels that differ only in accents or case are the same label (the server checks this too).
function fold(text) {
  return text.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
}

function oneLine(text) {
  return text.replace(/\s+/g, " ").trim();
}

function photoSrc(photo) {
  return "/api/admin/menu/photo" + qs({ path: photo.photo_path });
}

function fmtDay(iso) {
  return new Date(iso).toLocaleDateString("es-MX", { day: "numeric", month: "short", year: "numeric" });
}

export async function render(ctx) {
  injectStyle();
  const data = await api("/api/admin/menu" + qs({ site: ctx.site }));
  if (!ctx.sess.admin && view.mode !== "book") {
    view.mode = "book";
    view.draft = null;
  }
  if (view.mode === "order") return orderView(data, ctx);
  if (view.mode === "draft") return draftView(data, ctx);
  return bookView(data, ctx);
}

// --- the book --------------------------------------------------------------------------------------------

function introText(data) {
  const where = data.site
    ? "Si " + data.site + " sube fotos locales de un platillo, el modelo usa esas; si no, usa las universales."
    : "Elige un sitio arriba para ver también sus fotos locales.";
  return "Cada página es un platillo. Las definiciones y las fotos universales valen para todas las sucursales, "
    + "actuales y futuras. " + where + " Las fotos se ven completas (tócalas para verlas en grande); el modelo recibe "
    + "una copia ligera que nadie ve. Un cambio aquí cuenta desde el siguiente plato analizado.";
}

function toolbar(ctx, status, error) {
  const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: "display: none;" });
  const leer = btn("Leer ficha", "primary");
  leer.addEventListener("click", () => file.click());
  file.addEventListener("change", () => busy(leer, async () => {
    const chosen = file.files[0];
    file.value = "";
    if (!chosen) return;
    error.textContent = "";
    status.textContent = "Leyendo la ficha… tarda de 5 a 15 segundos (la primera vez después de reiniciar, un poco más).";
    try {
      const form = new FormData();
      form.append("photo", chosen);
      const answer = await api("/api/admin/menu/ficha", { method: "POST", form: form });
      view.mode = "draft";
      view.draft = {
        origen: "ficha",
        lectura: answer.lectura,
        ya_existe: answer.ya_existe,
        segundos: answer.segundos,
        preview: URL.createObjectURL(chosen),
      };
      toast("Ficha leída: revisa la página nueva y guárdala", "ok");
      ctx.refresh();
    } catch (err) {
      error.textContent = err.message;
    } finally {
      status.textContent = "";
    }
  }));
  const mano = btn("Crear a mano");
  mano.addEventListener("click", () => {
    view.mode = "draft";
    view.draft = { origen: "mano" };
    ctx.refresh();
  });
  const ordenar = btn("Ordenar páginas");
  ordenar.addEventListener("click", () => {
    view.mode = "order";
    ctx.refresh();
  });
  return el("div", { class: "kit-edit" }, leer, mano, ordenar, file);
}

function coverOf(d, data) {
  if (data.site && d.locales && d.locales.length) return d.locales[0];
  return d.universales.length ? d.universales[0] : null;
}

function indexCard(dishes, data, jump) {
  const items = dishes.map((d, i) => {
    const cover = coverOf(d, data);
    const pic = cover
      ? el("img", { class: "mb-thumb", alt: "", loading: "lazy", src: photoSrc(cover) })
      : el("div", { class: "mb-thumb", text: d.nombre.slice(0, 1).toUpperCase() });
    const item = el("button", { type: "button", class: "mb-item" + (d.activo ? "" : " mb-off") },
      pic, el("span", { class: "mb-item-name", text: (i + 1) + ". " + d.nombre + (d.activo ? "" : " (apagado)") }));
    item.addEventListener("click", () => jump(i));
    return item;
  });
  return el("section", { class: "card" },
    el("details", { open: window.innerWidth >= 720 },              // on a phone the index starts folded
      el("summary", { text: "Índice (" + dishes.length + (dishes.length === 1 ? " página)" : " páginas)") }),
      el("div", { class: "mb-index" }, items)));
}

function bookView(data, ctx) {
  const admin = ctx.sess.admin;
  const dishes = data.dishes;
  const error = el("div", { class: "kit-error" });
  const status = el("div", { class: "muted" });
  const head = card("Recetario · menú versión " + data.menu_version,
    el("p", { class: "muted", text: introText(data) }),
    admin ? toolbar(ctx, status, error) : null,
    status,
    error);
  if (!dishes.length) {
    return el("div", {}, head, card(null, el("p", {
      class: "muted",
      text: admin ? "El recetario no tiene páginas: usa Leer ficha o Crear a mano." : "El recetario no tiene páginas todavía.",
    })));
  }

  const counter = el("span", { class: "mb-counter" });
  const prev = el("button", { type: "button", text: "◀", "aria-label": "Página anterior" });
  const next = el("button", { type: "button", text: "▶", "aria-label": "Página siguiente" });
  const book = el("div", { class: "mb-book" });
  let current = Math.max(0, dishes.findIndex((d) => d.dish_id === view.at));

  const paint = () => {
    const editing = book.classList.contains("mb-editing");
    counter.textContent = "Página " + (current + 1) + " de " + dishes.length;
    prev.disabled = editing || current === 0;
    next.disabled = editing || current === dishes.length - 1;
  };
  const goTo = (i, smooth) => {
    current = Math.min(Math.max(0, i), dishes.length - 1);
    view.at = dishes[current].dish_id;
    book.scrollTo({ left: current * book.clientWidth, behavior: smooth ? "smooth" : "auto" });
    paint();
  };
  const hooks = {
    setEditing: (on) => {
      book.classList.toggle("mb-editing", on);
      paint();
    },
  };
  book.append(...dishes.map((d) => el("div", { class: "mb-page" }, pageCard(d, data, ctx, hooks))));

  let ticking = false;
  book.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      if (!book.clientWidth) return;
      const i = Math.round(book.scrollLeft / book.clientWidth);
      if (i !== current && i >= 0 && i < dishes.length) {
        current = i;
        view.at = dishes[i].dish_id;
        paint();
      }
    });
  });
  prev.addEventListener("click", () => goTo(current - 1, true));
  next.addEventListener("click", () => goTo(current + 1, true));

  // The arrow keys turn pages, except while typing, editing a page, or with a window open on top.
  const onKey = (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    if (!book.isConnected || book.classList.contains("mb-editing")) return;
    if (document.querySelector(".overlay, .kit-viewer")) return;
    const focus = document.activeElement;
    if (focus && (["INPUT", "TEXTAREA", "SELECT"].includes(focus.tagName) || focus.isContentEditable)) return;
    ev.preventDefault();
    goTo(current + (ev.key === "ArrowRight" ? 1 : -1), true);
  };
  document.addEventListener("keydown", onKey);
  ctx.onLeave(() => document.removeEventListener("keydown", onKey));
  const onResize = () => goTo(current, false);
  window.addEventListener("resize", onResize);
  ctx.onLeave(() => window.removeEventListener("resize", onResize));

  const nav = el("div", { class: "mb-nav" }, prev, counter, next);
  const index = indexCard(dishes, data, (i) => {
    goTo(i, true);
    nav.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  paint();
  requestAnimationFrame(() => goTo(current, false));    // after the shell puts the book on the screen
  return el("div", {}, head, index, nav, book);
}

// --- one page ---------------------------------------------------------------------------------------------

function componentsTable(componentes) {
  return el("table", {},
    el("thead", {}, el("tr", {}, el("th", { text: "Componente" }), el("th", { class: "num", text: "Porción (g)" }))),
    el("tbody", {}, componentes.map((c) => el("tr", {},
      el("td", { text: c.etiqueta }),
      el("td", { class: "num", text: String(c.porcion_g) })))));
}

function pageCard(d, data, ctx, hooks) {
  const error = el("div", { class: "kit-error" });
  const body = el("div", {});
  const showRead = () => {
    hooks.setEditing(false);
    body.replaceChildren(readView(d, ctx, error, showEdit));
  };
  const showEdit = () => {
    hooks.setEditing(true);
    body.replaceChildren(pageEditor({ d, data, ctx, error, onCancel: showRead }));
  };
  showRead();
  return el("section", { class: "card" }, body, photosOf(d, data, ctx, error), error);
}

function readView(d, ctx, error, onEdit) {
  const parts = [
    el("div", { class: "mb-title" },
      el("h2", { text: d.nombre }),
      el("span", { class: d.activo ? "chip ok" : "chip bad", text: d.activo ? "prendido" : "apagado" }),
      el("span", { class: "chip", text: "versión " + d.menu_version })),
  ];
  if (d.antes.length) {
    parts.push(el("div", {
      class: "muted",
      text: "Antes se llamó: " + d.antes.map((a) => "«" + a.nombre + "» (hasta el " + fmtDay(a.hasta) + ")").join(", "),
    }));
  }
  parts.push(componentsTable(d.componentes),
    el("h3", { text: "Notas para el modelo" }),
    el("p", { class: d.notas ? "mb-notes-text" : "muted", text: d.notas || "Sin notas." }));
  if (ctx.sess.admin) {
    const edit = btn("Editar", "primary");
    edit.addEventListener("click", () => {
      error.textContent = "";
      onEdit();
    });
    const toggle = wire(btn(d.activo ? "Apagar platillo" : "Prender platillo", d.activo ? "bad" : "ok"), error, async () => {
      const result = await finish(await api("/api/admin/menu/dishes/" + enc(d.dish_id) + "/active", {
        method: "POST", json: { on: !d.activo },
      }));
      if (result) {
        toast(d.activo ? "platillo apagado" : "platillo prendido", "ok");
        view.at = d.dish_id;
        ctx.refresh();
      }
    });
    parts.push(el("div", { class: "kit-edit", style: "margin-top: 10px;" }, edit, toggle));
  }
  return el("div", {}, ...parts);
}

// --- the page editor: an existing page (d) or a new one (d = null, maybe pre-filled by Leer ficha) ----------

function compRow(c, limits) {
  const label = el("input", {
    type: "text", class: "mb-label", maxlength: String(limits.etiqueta), placeholder: "Aderezo chipotle", "aria-label": "Componente",
  });
  label.value = c ? c.etiqueta || "" : "";
  const grams = el("input", {
    type: "number", class: "mb-grams", inputmode: "numeric", min: "1", max: String(limits.porcion_g), step: "1",
    placeholder: "30", "aria-label": "Gramos",
  });
  grams.value = c && c.porcion_g ? String(c.porcion_g) : "";
  const remove = el("button", { class: "kit-copy", type: "button", text: "quitar" });
  const doubtText = c && c.etiqueta && (c.duda || !c.porcion_g) ? "Revisa: " + (c.duda || "faltan los gramos") : "";
  if (doubtText) grams.classList.add("mb-doubt");
  const row = el("div", { class: "mb-comp" },
    el("div", { class: "kit-edit" }, label, grams, el("span", { class: "muted", text: "g" }), remove),
    doubtText ? el("div", { class: "kit-note", text: doubtText }) : null);
  remove.addEventListener("click", () => row.remove());
  row.read = () => ({ key: c && c.nombre ? c.nombre : null, etiqueta: oneLine(label.value), porcion: grams.value.trim() });
  return row;
}

function readForm(nombreInput, rows, notasInput, limits) {
  const nombre = oneLine(nombreInput.value);
  if (!nombre) throw new Error("Escribe el nombre del platillo.");
  if (nombre.length > limits.nombre) throw new Error("El nombre tiene más de " + limits.nombre + " caracteres.");
  const componentes = [];
  const seen = new Set();
  for (const row of rows) {
    const r = row.read();
    if (!r.etiqueta && !r.porcion) continue;
    if (!r.etiqueta) throw new Error("Falta el nombre del componente de " + r.porcion + " g.");
    const grams = Number(r.porcion);
    if (!Number.isInteger(grams) || grams < 1 || grams > limits.porcion_g) {
      throw new Error("«" + r.etiqueta + "»: escribe sus gramos (un número entero de 1 a " + limits.porcion_g + ").");
    }
    const folded = fold(r.etiqueta);
    if (seen.has(folded)) throw new Error("El componente «" + r.etiqueta + "» aparece dos veces.");
    seen.add(folded);
    const item = { etiqueta: r.etiqueta, porcion_g: grams };
    if (r.key) item.nombre = r.key;
    componentes.push(item);
  }
  if (!componentes.length) throw new Error("Un platillo necesita al menos un componente.");
  if (componentes.length > limits.componentes) throw new Error("Máximo " + limits.componentes + " componentes.");
  const notas = notasInput.value.trim();
  if (notas.length > limits.notas) throw new Error("Las notas pasan de " + limits.notas + " caracteres.");
  return { nombre, componentes, notas };
}

function pageEditor({ d, data, ctx, error, onCancel, draft = null }) {
  const limits = data.limites;
  const source = d
    ? { nombre: d.nombre, componentes: d.componentes, notas: d.notas }
    : (draft && draft.lectura) || { nombre: "", componentes: [], notas: "" };
  const nombre = el("input", {
    type: "text", class: "mb-name", maxlength: String(limits.nombre), placeholder: "Tartine de pollo", "aria-label": "Nombre del platillo",
  });
  nombre.value = source.nombre || "";
  const rows = el("div", {}, (source.componentes.length ? source.componentes : [null, null]).map((c) => compRow(c, limits)));
  const add = btn("Otro componente");
  add.addEventListener("click", () => rows.append(compRow(null, limits)));
  const notas = el("textarea", {
    class: "mb-notes", maxlength: String(limits.notas), "aria-label": "Notas para el modelo",
    placeholder: "Lo que el modelo debe saber al ver este plato (por ejemplo: la vinagreta va aparte, en ramekin).",
  });
  notas.value = source.notas || "";
  const count = el("div", { class: "muted" });
  const recount = () => { count.textContent = notas.value.length + " de " + limits.notas + " caracteres"; };
  notas.addEventListener("input", recount);
  recount();

  const save = wire(btn("Guardar", "primary"), error, async () => {
    const payload = readForm(nombre, [...rows.children], notas, limits);
    if (d) {
      const result = await finish(await api("/api/admin/menu/dishes/" + enc(d.dish_id) + "/edit", { method: "POST", json: payload }));
      if (!result) return;
      toast(result.bump ? "Guardado: el menú pasó a la versión " + result.menu_version : "Guardado", "ok");
      view.at = d.dish_id;
      ctx.refresh();
      return;
    }
    const result = await api("/api/admin/menu/dishes", { method: "POST", json: payload });
    if (view.draft && view.draft.preview) URL.revokeObjectURL(view.draft.preview);
    view.mode = "book";
    view.draft = null;
    view.at = result.dish_id;
    toast("Página nueva: «" + result.nombre + "», página " + result.pagina, "ok");
    ctx.refresh();
  });
  const cancel = btn("Cancelar");
  cancel.addEventListener("click", () => {
    error.textContent = "";
    onCancel();
  });
  return el("div", {},
    el("label", { text: "Nombre del platillo" }), nombre,
    el("h3", { text: "Componentes y su porción estándar" }),
    el("p", { class: "muted", text: "Escríbelos como se dicen en la cocina, con acentos. El sistema les pone su clave interna." }),
    rows,
    el("div", { class: "kit-edit" }, add),
    el("h3", { text: "Notas para el modelo" }), notas, count,
    d ? el("div", { class: "kit-note", text: EDIT_NOTE }) : null,
    el("div", { class: "kit-edit", style: "margin-top: 10px;" }, save, cancel));
}

// --- a new page: Leer ficha (pre-filled) or Crear a mano (empty) -------------------------------------------

function draftView(data, ctx) {
  const draft = view.draft || { origen: "mano" };
  const lectura = draft.lectura || null;
  const error = el("div", { class: "kit-error" });
  const leave = () => {
    if (draft.preview) URL.revokeObjectURL(draft.preview);
    view.mode = "book";
    view.draft = null;
    ctx.refresh();
  };
  const parts = [el("p", {
    class: "muted",
    text: draft.origen === "ficha"
      ? "El modelo leyó la ficha en " + draft.segundos + " s. Revisa todo contra la foto: lo marcado en naranja necesita "
        + "tu atención. Nada se guarda hasta que presiones Guardar."
      : "Escribe el platillo como viene en el recetario. Nada se guarda hasta que presiones Guardar.",
  })];
  if (draft.ya_existe) {
    parts.push(el("div", { class: "mb-dudas", text: "Ya hay un platillo que se llama «" + draft.ya_existe + "»: cambia el nombre o cancela." }));
  }
  if (lectura && lectura.dudas.length) {
    parts.push(el("div", { class: "mb-dudas" },
      el("strong", { text: "Revisa esto antes de guardar:" }),
      el("ul", {}, lectura.dudas.map((t) => el("li", { text: t })))));
  }
  if (draft.preview) {
    const preview = el("button", { type: "button", class: "mb-open mb-preview", title: "Ver la ficha en grande" },
      el("img", { class: "thumb", alt: "La ficha leída", src: draft.preview }));
    preview.addEventListener("click", () => photoViewer([{ src: draft.preview, caption: "La ficha que leyó el modelo" }], 0));
    parts.push(preview);
  }
  parts.push(pageEditor({ d: null, data, ctx, error, onCancel: leave, draft }),
    el("p", { class: "muted", text: "Las fotos del platillo se suben después, en su página." }),
    error);
  return card("Página nueva", ...parts);
}

// --- Ordenar páginas (admin): one order for the brand, the same in every site ---------------------------------

function orderView(data, ctx) {
  const order = data.dishes.slice();
  const error = el("div", { class: "kit-error" });
  const list = el("ol", { class: "mb-order" });
  const draw = () => {
    list.replaceChildren(...order.map((d, i) => {
      const up = el("button", { class: "kit-copy", type: "button", text: "▲", "aria-label": "Subir" });
      const down = el("button", { class: "kit-copy", type: "button", text: "▼", "aria-label": "Bajar" });
      up.disabled = i === 0;
      down.disabled = i === order.length - 1;
      up.addEventListener("click", () => {
        [order[i - 1], order[i]] = [order[i], order[i - 1]];
        draw();
      });
      down.addEventListener("click", () => {
        [order[i + 1], order[i]] = [order[i], order[i + 1]];
        draw();
      });
      return el("li", {}, el("span", { class: "mb-order-name", text: d.nombre + (d.activo ? "" : " (apagado)") }), up, down);
    }));
  };
  draw();
  const save = wire(btn("Guardar orden", "primary"), error, async () => {
    await api("/api/admin/menu/order", { method: "POST", json: { orden: order.map((d) => d.dish_id) } });
    toast("Orden guardado: todas las sucursales lo ven igual", "ok");
    view.mode = "book";
    ctx.refresh();
  });
  const cancel = btn("Cancelar");
  cancel.addEventListener("click", () => {
    view.mode = "book";
    ctx.refresh();
  });
  return card("Ordenar páginas",
    el("p", { class: "muted", text: "Sube o baja cada platillo. El orden es uno para la marca: todas las sucursales ven las páginas así." }),
    list,
    el("div", { class: "kit-edit", style: "margin-top: 10px;" }, save, cancel),
    error);
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

function photoTile(d, rows, i, level, editable, ctx, error) {
  const p = rows[i];
  const open = el("button", { type: "button", class: "mb-open", title: "Ver en grande" },
    el("img", { class: "thumb", alt: d.nombre, src: photoSrc(p), loading: "lazy" }));
  open.addEventListener("click", () => photoViewer(rows.map((r) => ({
    src: photoSrc(r),
    caption: d.nombre + " · " + (CONDITION_WORDS[r.condition] || r.condition),
  })), i));
  const parts = [
    open,
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
        view.at = d.dish_id;
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
        view.at = d.dish_id;
        ctx.refresh();
      }
    });
    parts.push(el("div", { class: "kit-edit" }, replaceBtn, remove, file));
  }
  return el("div", {}, ...parts);
}

function uploadRow(level, d, data, ctx, error) {
  const file = el("input", { type: "file", accept: "image/jpeg,image/png", style: "width: auto;" });
  const condition = el("select", { style: "width: auto;" },
    data.conditions.map((c) => el("option", { value: c, text: CONDITION_WORDS[c] || c })));
  const upload = wire(btn("Subir foto", "primary"), error, async () => {
    if (!file.files.length) throw new Error("Elige una foto primero.");
    await sendPhoto(level, d, file.files[0], condition.value, null);
    toast("foto subida", "ok");
    view.at = d.dish_id;
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
      ? el("div", { class: "grid" }, rows.map((p, i) => photoTile(d, rows, i, level, editable, ctx, error)))
      : el("p", { class: "muted", text: emptyText }),
    editable ? uploadRow(level, d, data, ctx, error) : null);
}

function photosOf(d, data, ctx, error) {
  const admin = ctx.sess.admin;
  const parts = [photoBlock("Fotos universales (todas las sucursales)", d.universales, data.brand, admin,
    admin ? "Sin fotos universales: súbelas aquí." : "Sin fotos universales.", d, data, ctx, error)];
  if (data.site) {
    parts.push(photoBlock("Fotos locales de " + data.site, d.locales, data.site, true,
      "Sin fotos locales en " + data.site + ".", d, data, ctx, error));
    parts.push(el("div", { class: d.usa === "ninguna" ? "kit-note" : "kit-reco", text: USA_WORDS[d.usa] }));
  }
  return el("div", {}, ...parts);
}
