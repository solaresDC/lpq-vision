// LPQ_VISION admin-kit.js: the widgets both floors share (SPEC 1.4, 1.5; sizing ruling v3.8).
// Vanilla ES module, no framework, no build step. The mostrador (admin.js and its section modules) and
// the machine room (maquinas.js) render every knob, help and confirmation through these pieces, so the
// two floors look and behave the same. Everything the server sends is rendered as TEXT, never as HTML.
//
// The three helps (SPEC 1.4): every knob shows what it is, a copyable example (also the field's gray
// placeholder) and the factory recommendation, always visible.
// The double confirm (SPEC 1.5): step 1 is the family's endpoint answering {pending_id, action, diff,
// expires_s}; this dialog shows the diff with a countdown; step 2 (POST /api/admin/confirm) runs only
// when the person presses Confirmar. The diff convention every family follows:
//   {resumen: "one sentence", cambios: [{que, antes, despues}], lista: ["one line per item"]}
// Any other key is shown as "clave: valor".

import { api, el, toast } from "/static/app.js";

export const SECCIONES = {
  analisis: "Análisis",
  captura: "Captura",
  alertas: "Alertas",
  horario: "Horario",
  papelera: "Papelera",
  forense: "Registro forense",
  destino: "Destino Telegram",
  cola: "Cola",
  gasto: "Gasto",
  modelo: "Modelo",
  constantes: "Constantes de máquina",
  analistas: "Analistas",
  sesiones: "Sesiones",
};

// The client shows 2 s less than the server's TTL, so Confirmar never lands on an expired pending id.
const CONFIRM_MARGIN_S = 2;

const STYLE = `
.kit-knob { border-top: 1px solid var(--line); padding: 12px 0; }
.kit-knob:first-of-type { border-top: 0; }
.kit-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 6px; }
.kit-name { font-weight: 600; }
.kit-helps { font-size: 14px; display: grid; gap: 4px; margin-bottom: 8px; }
.kit-helps code { background: var(--bg); padding: 1px 6px; border-radius: 4px; }
.kit-copy { min-height: 28px; padding: 0 10px; margin-left: 6px; font-size: 13px; }
.kit-reco { color: var(--ok); }
.kit-edit { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.kit-edit input, .kit-edit select { width: auto; min-width: 140px; }
.kit-value { font-weight: 600; font-variant-numeric: tabular-nums; }
.kit-error { color: var(--bad); font-size: 14px; min-height: 18px; margin-top: 4px; }
.kit-note { font-size: 13px; color: var(--warn); margin-top: 4px; }
.kit-dialog .card { max-width: 560px; max-height: 90vh; overflow-y: auto; }
.kit-dialog table { margin: 10px 0; }
.kit-countdown { font-size: 13px; color: var(--muted); }
.kit-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.kit-placeholder { color: var(--muted); }
.kit-viewer { position: fixed; inset: 0; z-index: 70; background: rgba(15, 14, 12, .92); display: flex; align-items: center; justify-content: center; padding: 56px 12px 64px; }
.kit-viewer img { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 8px; background: #111; }
.kit-viewer button { position: absolute; min-width: var(--tap); background: rgba(255, 255, 255, .92); border-color: transparent; }
.kit-viewer .kit-x { top: 10px; right: 10px; }
.kit-viewer .kit-prev { left: 10px; top: 50%; transform: translateY(-50%); }
.kit-viewer .kit-next { right: 10px; top: 50%; transform: translateY(-50%); }
.kit-viewer .kit-caption { position: absolute; left: 12px; right: 12px; bottom: 18px; text-align: center; color: #fff; font-size: 14px; }
.kit-field-help { font-size: 14px; margin: 4px 0 10px; display: grid; gap: 6px; }
.kit-field-help code { background: var(--bg); padding: 1px 6px; border-radius: 4px; }
.kit-ask { min-height: 28px; min-width: 28px; padding: 0 9px; margin-left: 6px; font-size: 13px; font-weight: 700; border-radius: 999px; vertical-align: middle; }
.kit-more { background: var(--bg); border-radius: 8px; padding: 8px 10px; display: grid; gap: 4px; }
.kit-hint { font-size: 13px; color: var(--muted); margin-top: 2px; }
.kit-edit input[type="checkbox"] { min-width: 0; }
`;

function injectStyle() {
  if (document.getElementById("admin-kit-style")) return;
  document.head.append(el("style", { id: "admin-kit-style", text: STYLE }));
}
injectStyle();

// --- small pieces ----------------------------------------------------------------------

export function card(title, ...children) {
  return el("section", { class: "card" }, title ? el("h2", { text: title }) : null, ...children);
}

export function placeholder(label, family) {
  return card(label, el("p", {
    class: "kit-placeholder",
    text: "Esta sección aún no está construida (" + family + "). Llega en una sección próxima del kit.",
  }));
}

export function siteLabel(site) {
  return site ? site : "Global (todos los sitios)";
}

export function fmtValue(value) {
  if (value === null || value === undefined || value === "") return "(vacío)";
  if (typeof value === "boolean") return value ? "prendido" : "apagado";
  if (typeof value === "object" && "open" in value && "close" in value) return value.open + " a " + value.close;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// busy(button, fn): the button stays disabled while fn runs; fn's error propagates to the caller.
export async function busy(button, fn) {
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = false;
  }
}

// The registry's knobs grouped by page section, in registry order.
export function groupBySeccion(knobs) {
  const groups = new Map();
  for (const knob of knobs) {
    if (!groups.has(knob.seccion)) groups.set(knob.seccion, []);
    groups.get(knob.seccion).push(knob);
  }
  return groups;
}

// --- the three helps and the badge -------------------------------------------------------

function copyButton(text) {
  return el("button", {
    class: "kit-copy",
    type: "button",
    text: "copiar",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast("ejemplo copiado", "ok");
      } catch {
        toast("no se pudo copiar: selecciónalo a mano", "bad");
      }
    },
  });
}

export function helps(knob) {
  return el("div", { class: "kit-helps" },
    el("div", { text: knob.que_es }),
    el("div", {}, el("span", { class: "muted", text: "Ejemplo: " }), el("code", { text: knob.ejemplo }), copyButton(knob.ejemplo)),
    el("div", { class: "kit-reco", text: "Recomendación: " + knob.recomendacion }));
}

// fieldHelp(help, {name}): the three helps of a field that is not a knob (SPEC 1.4; owner ruling 3.10.3). The
// example is also the field's gray placeholder; the recommendation is always visible; "?" opens what the field
// is and its example, with copiar when the example is something a person would type (help.copiar !== false).
// `name` starts the line when one help serves a whole column of fields (every component's grams, say).
export function fieldHelp(help, { name = "" } = {}) {
  const more = el("div", { class: "kit-more hidden" },
    el("div", { text: help.que_es }),
    el("div", {}, el("span", { class: "muted", text: "Ejemplo: " }), el("code", { text: help.ejemplo }),
      help.copiar === false ? null : copyButton(help.ejemplo)));
  const ask = el("button", {
    class: "kit-ask", type: "button", text: "?", title: "¿Qué es?", "aria-expanded": "false",
    "aria-label": "Qué es" + (name ? ": " + name : ""),
  });
  ask.addEventListener("click", () => {
    const open = !more.classList.toggle("hidden");
    ask.setAttribute("aria-expanded", open ? "true" : "false");
  });
  return el("div", { class: "kit-field-help" },
    el("div", { class: "kit-reco" }, name ? el("strong", { text: name + ". " }) : null, "Recomendación: " + help.recomendacion, ask),
    more);
}

// A Telegram chat field (owner ruling 3.8.1): group ids are negative, so the page shows a fixed "-" and keeps
// ONLY digits while typing or pasting ("-1001234" keeps "1001234"). read() answers "-" plus the digits, or null
// when empty (an empty field removes the chat: absent-when-off). DIGITS_HINT goes under EVERY Telegram field
// (owner ruling 3.10.3).
export const DIGITS_HINT = "Pega solo los números: el signo menos lo pone el sistema.";

export function chatField(current) {
  const input = el("input", { type: "text", inputmode: "numeric", autocomplete: "off", placeholder: "1001234567890" });
  input.value = String(current || "").replace(/[^0-9]/g, "");
  input.addEventListener("input", () => {
    const digits = input.value.replace(/[^0-9]/g, "");
    if (digits !== input.value) input.value = digits;
  });
  const node = el("span", { class: "kit-edit" }, el("span", { class: "kit-value", text: "-" }), input);
  return { node: node, read: () => (input.value ? "-" + input.value : null) };
}

// How the value is painted: a site's own value, inherited from the global, or the global itself.
export function sourceBadge(knob, site) {
  if (!knob.per_site || !site) {
    const extra = knob.per_site && knob.overrides ? " · " + knob.overrides + " sitio(s) con valor propio" : "";
    return el("span", { class: "chip", text: "global" + extra });
  }
  return knob.source === "propio"
    ? el("span", { class: "chip warn", text: "propio de " + site })
    : el("span", { class: "chip", text: "heredado del global" });
}

// --- the editor --------------------------------------------------------------------------

function inputFor(knob, value) {
  const kind = knob.kind;
  const empty = value === null || value === undefined;
  if (kind === "int" || kind === "float") {
    const input = el("input", {
      type: "number",
      inputmode: "decimal",
      min: knob.min,
      max: knob.max,
      step: kind === "int" ? "1" : "any",
      placeholder: knob.ejemplo,
    });
    input.value = empty ? "" : String(value);
    return { node: input, read: () => input.value.trim() };
  }
  if (kind === "choice" || kind === "prompt_merma" || kind === "prompt_presentacion" || kind === "model") {
    const choices = knob.choices.slice();
    if (!empty && !choices.includes(value)) choices.unshift(value);
    const options = choices.map((c) => el("option", { value: c, text: c }));
    if (knob.optional) options.unshift(el("option", { value: "", text: "(de fábrica)" }));
    const select = el("select", {}, options);
    select.value = empty ? "" : value;
    return { node: select, read: () => select.value };
  }
  if (kind === "switch") {
    const select = el("select", {},
      el("option", { value: "true", text: "prendido" }),
      el("option", { value: "false", text: "apagado" }));
    select.value = value ? "true" : "false";
    return { node: select, read: () => select.value === "true" };
  }
  if (kind === "horario") {
    const open = el("input", { type: "time", step: "60" });
    const close = el("input", { type: "time", step: "60" });
    open.value = (value && value.open) || "";
    close.value = (value && value.close) || "";
    const node = el("span", { class: "kit-edit" },
      el("span", { class: "muted", text: "abre" }), open,
      el("span", { class: "muted", text: "cierra" }), close);
    return { node, read: () => ({ open: open.value, close: close.value }) };
  }
  const input = el("input", { type: "text", placeholder: knob.ejemplo });
  input.value = empty ? "" : String(value);
  return { node: input, read: () => input.value.trim() };
}

// knobRow(knob, {site, readOnly, onSave, onReset}): one knob with its badge and its three helps, then
// either its value (read-only) or an editor with Guardar and, for a site's own value, Volver al global.
// onSave(value) and onReset() are async and throw an Error whose message is shown under the row.
export function knobRow(knob, { site = null, readOnly = false, onSave = null, onReset = null } = {}) {
  const value = site && knob.per_site && "effective" in knob ? knob.effective : knob.global;
  const row = el("div", { class: "kit-knob" },
    el("div", { class: "kit-head" }, el("span", { class: "kit-name", text: knob.name }), sourceBadge(knob, site)),
    helps(knob));
  const globalNote = !knob.per_site && site ? el("div", { class: "kit-note", text: "Afecta a TODOS los sitios." }) : null;
  if (readOnly || !onSave) {
    row.append(el("div", {}, el("span", { class: "muted", text: "Valor: " }),
      el("span", { class: "kit-value", text: fmtValue(value) })));
    if (globalNote) row.append(globalNote);
    return row;
  }
  const error = el("div", { class: "kit-error" });
  const editor = inputFor(knob, value);
  const save = el("button", { class: "primary", type: "button", text: "Guardar" });
  save.addEventListener("click", () => busy(save, async () => {
    error.textContent = "";
    try {
      await onSave(editor.read());
      toast("guardado", "ok");
    } catch (err) {
      error.textContent = err.message;
    }
  }));
  const edit = el("div", { class: "kit-edit" }, editor.node, save);
  if (onReset && site && knob.per_site && knob.source === "propio") {
    const reset = el("button", { type: "button", text: "Volver al global" });
    reset.addEventListener("click", () => busy(reset, async () => {
      error.textContent = "";
      try {
        await onReset();
        toast("volvió al global", "ok");
      } catch (err) {
        error.textContent = err.message;
      }
    }));
    edit.append(reset);
  }
  row.append(edit, error);
  if (globalNote) row.append(globalNote);
  return row;
}

// --- the double confirm ------------------------------------------------------------------

export function renderDiff(diff) {
  const box = el("div", {});
  if (!diff || typeof diff !== "object") return box;
  if (diff.resumen) box.append(el("p", { text: diff.resumen }));
  if (Array.isArray(diff.cambios) && diff.cambios.length) {
    box.append(el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Qué" }), el("th", { text: "Antes" }), el("th", { text: "Después" }))),
      el("tbody", {}, diff.cambios.map((c) => el("tr", {},
        el("td", { text: String(c.que) }),
        el("td", { text: fmtValue(c.antes) }),
        el("td", { text: fmtValue(c.despues) }))))));
  }
  if (Array.isArray(diff.lista) && diff.lista.length) {
    box.append(el("ul", {}, diff.lista.map((item) => el("li", { text: String(item) }))));
  }
  for (const [key, value] of Object.entries(diff)) {
    if (key === "resumen" || key === "cambios" || key === "lista") continue;
    box.append(el("div", { class: "muted", text: key + ": " + fmtValue(value) }));
  }
  return box;
}

// confirmDialog({action, diff, expires_s}) resolves true (Confirmar) or false (Cancelar, Escape).
// When the countdown ends, Confirmar is disabled: the person asks again.
export function confirmDialog({ action, diff, expires_s = 60 }) {
  return new Promise((resolve) => {
    let left = Math.max(1, Math.floor(expires_s) - CONFIRM_MARGIN_S);
    const countdown = el("div", { class: "kit-countdown" });
    const ok = el("button", { class: "bad", type: "button", text: "Confirmar" });
    const cancel = el("button", { type: "button", text: "Cancelar" });
    const overlay = el("div", { class: "overlay kit-dialog", role: "dialog", "aria-modal": "true" },
      el("div", { class: "card" },
        el("h2", { text: "Confirmar: " + action }),
        renderDiff(diff),
        countdown,
        el("div", { class: "kit-actions" }, cancel, ok)));
    const tick = () => {
      if (left > 0) {
        countdown.textContent = "Esta confirmación vence en " + left + " s.";
      } else {
        countdown.textContent = "Venció: cierra y vuelve a pedirla.";
        ok.disabled = true;
      }
      left -= 1;
    };
    const onKey = (ev) => { if (ev.key === "Escape") done(false); };
    const timer = setInterval(tick, 1000);
    function done(answer) {
      clearInterval(timer);
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(answer);
    }
    tick();
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
    cancel.focus();
  });
}

// confirmFlow(stepOne): the whole double confirm. stepOne() calls the family's step-1 endpoint and
// returns its answer. Returns the action's result, or null when the person cancelled. A refusal (410
// expired, 403 grant gone, anything else) propagates as an Error carrying .status for the caller.
export async function confirmFlow(stepOne) {
  const pending = await stepOne();
  const yes = await confirmDialog(pending);
  if (!yes) return null;
  const answer = await api("/api/admin/confirm", { method: "POST", json: { pending_id: pending.pending_id } });
  return answer.result;
}

// --- a NEW password --------------------------------------------------------------------------

// newPassword({repeat, minChars}): a password typed by a human. repeat (default true) is for a NEW account:
// a second field that must match. A password change on an existing account passes {repeat: false}: one
// field. ONE "ver" button shows or hides every field together. Copy, cut, paste and drop are blocked, so
// the password can be read but never copied, and the second field is really typed again. read() returns
// the password or throws an Error to show; clear() empties and hides. The server keeps its own rule
// (PASSWORD_MIN_CHARS): this widget only saves a round trip.
export function newPassword({ repeat = true, minChars = 10 } = {}) {
  const make = (placeholder) => {
    const input = el("input", { type: "password", autocomplete: "new-password", placeholder: placeholder, style: "width: auto;" });
    for (const name of ["copy", "cut", "paste", "drop"]) input.addEventListener(name, (ev) => ev.preventDefault());
    return input;
  };
  const inputs = [make("contraseña (mínimo " + minChars + ")")];
  if (repeat) inputs.push(make("repítela"));
  const eye = el("button", { type: "button", class: "kit-copy", text: "ver" });
  const setShown = (show) => {
    for (const input of inputs) input.type = show ? "text" : "password";
    eye.textContent = show ? "ocultar" : "ver";
  };
  eye.addEventListener("click", () => setShown(inputs[0].type === "password"));
  return {
    node: el("span", { class: "kit-edit" }, ...inputs, eye),
    read: () => {
      const first = inputs[0].value;
      if (first.length < minChars) throw new Error("La contraseña necesita al menos " + minChars + " caracteres.");
      if (repeat && first !== inputs[1].value) throw new Error("Las dos contraseñas no coinciden: escríbela igual en los dos campos.");
      return first;
    },
    clear: () => {
      for (const input of inputs) input.value = "";
      setShown(false);
    },
  };
}

// --- type the name to confirm --------------------------------------------------------------------

// typeToConfirm({title, name, warning}): the person must TYPE `name` exactly (paste and drop blocked)
// before "Continuar" enables. Resolves true (Continuar) or false (Cancelar, Escape). It is the FIRST gate:
// the server's double confirm (the diff dialog) still follows, and the server checks the name too.
export function typeToConfirm({ title, name, warning }) {
  return new Promise((resolve) => {
    const input = el("input", { type: "text", autocomplete: "off", placeholder: "escribe el nombre aquí" });
    for (const ev of ["paste", "drop"]) input.addEventListener(ev, (e) => e.preventDefault());
    const ok = el("button", { class: "bad", type: "button", text: "Continuar" });
    const cancel = el("button", { type: "button", text: "Cancelar" });
    ok.disabled = true;
    input.addEventListener("input", () => { ok.disabled = input.value !== name; });
    const overlay = el("div", { class: "overlay kit-dialog", role: "dialog", "aria-modal": "true" },
      el("div", { class: "card" },
        el("h2", { text: title }),
        warning ? el("p", { text: warning }) : null,
        el("p", {}, el("span", { text: "Para seguir, escribe exactamente: " }), el("code", { text: name })),
        input,
        el("div", { class: "kit-actions" }, cancel, ok)));
    const onKey = (ev) => { if (ev.key === "Escape") done(false); };
    function done(answer) {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(answer);
    }
    ok.addEventListener("click", () => { if (input.value === name) done(true); });
    cancel.addEventListener("click", () => done(false));
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
    input.focus();
  });
}

// --- the password window -----------------------------------------------------------------------

// passwordDialog({title, current, note, minChars, help}): a small window for a NEW password, typed twice. With
// current: true (Mi cuenta) it first asks for the CURRENT one. ONE "ver" shows or hides every field; copy,
// cut, paste and drop are blocked in the new-password fields. help: the field's three helps (fieldHelp).
// Resolves {current, password} after a local check (length, match), or null on Cancelar or Escape. The
// caller's "¿seguro?" step follows.
export function passwordDialog({ title, current = false, note = "", minChars = 10, help = null }) {
  return new Promise((resolve) => {
    const make = (placeholder, guarded) => {
      const input = el("input", {
        type: "password",
        autocomplete: guarded ? "new-password" : "current-password",
        placeholder: placeholder,
      });
      if (guarded) for (const name of ["copy", "cut", "paste", "drop"]) input.addEventListener(name, (ev) => ev.preventDefault());
      return input;
    };
    const now = current ? make("contraseña actual", false) : null;
    const first = make("contraseña nueva (mínimo " + minChars + ")", true);
    const second = make("repítela", true);
    const fields = [now, first, second].filter(Boolean);
    const eye = el("button", { type: "button", class: "kit-copy", text: "ver" });
    eye.addEventListener("click", () => {
      const show = first.type === "password";
      for (const f of fields) f.type = show ? "text" : "password";
      eye.textContent = show ? "ocultar" : "ver";
    });
    const error = el("div", { class: "kit-error" });
    const ok = el("button", { class: "primary", type: "button", text: "Continuar" });
    const cancel = el("button", { type: "button", text: "Cancelar" });
    const overlay = el("div", { class: "overlay kit-dialog", role: "dialog", "aria-modal": "true" },
      el("div", { class: "card" },
        el("h2", { text: title }),
        note ? el("p", { class: "muted", text: note }) : null,
        help ? fieldHelp(help) : null,
        ...fields.map((f) => el("div", { style: "margin: 6px 0;" }, f)),
        el("div", { class: "kit-edit" }, eye),
        error,
        el("div", { class: "kit-actions" }, cancel, ok)));
    const onKey = (ev) => { if (ev.key === "Escape") done(null); };
    function done(answer) {
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(answer);
    }
    ok.addEventListener("click", () => {
      if (now && !now.value) {
        error.textContent = "Escribe tu contraseña actual.";
        return;
      }
      if (first.value.length < minChars) {
        error.textContent = "La contraseña nueva necesita al menos " + minChars + " caracteres.";
        return;
      }
      if (first.value !== second.value) {
        error.textContent = "Las dos contraseñas nuevas no coinciden: escríbela igual en los dos campos.";
        return;
      }
      done({ current: now ? now.value : null, password: first.value });
    });
    cancel.addEventListener("click", () => done(null));
    document.addEventListener("keydown", onKey);
    document.body.append(overlay);
    (now || first).focus();
  });
}

// --- a read-only window ------------------------------------------------------------------------------

// infoDialog({title, body}): a window that only shows something (a table, a list) and closes with Cerrar or
// Escape. Resolves when closed.
export function infoDialog({ title, body }) {
  return new Promise((resolve) => {
    const close = el("button", { class: "primary", type: "button", text: "Cerrar" });
    const overlay = el("div", { class: "overlay kit-dialog", role: "dialog", "aria-modal": "true" },
      el("div", { class: "card", style: "max-height: 85vh; overflow: auto;" },
        el("h2", { text: title }),
        body,
        el("div", { class: "kit-actions" }, close)));
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


// --- a photo, big, inside the page -----------------------------------------------------------------------

// photoViewer(photos, start): a photo seen big INSIDE the page, never in a new tab (owner ruling 3.10.2). A dark
// background, the photo whole; ✕, a click outside the photo or Escape closes it; ◀ ▶, the arrow keys or a swipe
// on the phone move between `photos`, each {src, caption}. Any photo in the mostrador opens here.
export function photoViewer(photos, start = 0) {
  if (!photos || !photos.length) return;
  let at = Math.min(Math.max(0, start), photos.length - 1);
  const img = el("img", { alt: "" });
  const caption = el("div", { class: "kit-caption" });
  const close = el("button", { class: "kit-x", type: "button", text: "✕", "aria-label": "Cerrar" });
  const prev = el("button", { class: "kit-prev", type: "button", text: "◀", "aria-label": "Foto anterior" });
  const next = el("button", { class: "kit-next", type: "button", text: "▶", "aria-label": "Foto siguiente" });
  const overlay = el("div", { class: "kit-viewer", role: "dialog", "aria-modal": "true" }, img, caption, close, prev, next);
  const show = () => {
    const photo = photos[at];
    img.src = photo.src;
    img.alt = photo.caption || "";
    caption.textContent = (photo.caption ? photo.caption + " · " : "") + (at + 1) + " de " + photos.length;
    prev.classList.toggle("hidden", photos.length < 2);
    next.classList.toggle("hidden", photos.length < 2);
  };
  const step = (delta) => {
    at = (at + delta + photos.length) % photos.length;
    show();
  };
  const onKey = (ev) => {
    if (ev.key === "Escape") done();
    else if (ev.key === "ArrowLeft") step(-1);
    else if (ev.key === "ArrowRight") step(1);
  };
  function done() {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  }
  let touchX = null;
  overlay.addEventListener("touchstart", (ev) => { touchX = ev.touches[0].clientX; }, { passive: true });
  overlay.addEventListener("touchend", (ev) => {
    if (touchX === null) return;
    const dx = ev.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) > 50 && photos.length > 1) step(dx < 0 ? 1 : -1);
  });
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) done(); });
  close.addEventListener("click", done);
  prev.addEventListener("click", () => step(-1));
  next.addEventListener("click", () => step(1));
  document.addEventListener("keydown", onKey);
  document.body.append(overlay);
  show();
  close.focus();
}
