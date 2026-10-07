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
.kit-dialog .card { max-width: 560px; }
.kit-dialog table { margin: 10px 0; }
.kit-countdown { font-size: 13px; color: var(--muted); }
.kit-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.kit-placeholder { color: var(--muted); }
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

export function helps(knob) {
  const copy = el("button", {
    class: "kit-copy",
    type: "button",
    text: "copiar",
    onclick: async () => {
      try {
        await navigator.clipboard.writeText(knob.ejemplo);
        toast("ejemplo copiado", "ok");
      } catch {
        toast("no se pudo copiar: selecciónalo a mano", "bad");
      }
    },
  });
  return el("div", { class: "kit-helps" },
    el("div", { text: knob.que_es }),
    el("div", {}, el("span", { class: "muted", text: "Ejemplo: " }), el("code", { text: knob.ejemplo }), copy),
    el("div", { class: "kit-reco", text: "Recomendación: " + knob.recomendacion }));
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

// newPassword(minChars): a new password typed TWICE by a human. Each field has a "ver" button that shows
// what was typed; copy, cut, paste and drop are blocked in both, so the password can be read but never
// copied, and the second field is really typed again. read() returns the password or throws an Error to
// show (too short, or the two fields differ); clear() empties both and hides them again. The server keeps
// its own rule (PASSWORD_MIN_CHARS): this widget only saves a round trip.
export function newPassword(minChars = 10) {
  const fields = [];
  const make = (placeholder) => {
    const input = el("input", { type: "password", autocomplete: "new-password", placeholder: placeholder, style: "width: auto;" });
    for (const name of ["copy", "cut", "paste", "drop"]) input.addEventListener(name, (ev) => ev.preventDefault());
    const eye = el("button", { type: "button", class: "kit-copy", text: "ver" });
    eye.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.textContent = show ? "ocultar" : "ver";
    });
    fields.push({ input: input, eye: eye });
    return el("span", { class: "kit-edit" }, input, eye);
  };
  const node = el("span", { class: "kit-edit" }, make("contraseña (mínimo " + minChars + ")"), make("repítela"));
  return {
    node: node,
    read: () => {
      const first = fields[0].input.value;
      if (first.length < minChars) throw new Error("La contraseña necesita al menos " + minChars + " caracteres.");
      if (first !== fields[1].input.value) throw new Error("Las dos contraseñas no coinciden: escríbela igual en los dos campos.");
      return first;
    },
    clear: () => {
      for (const f of fields) {
        f.input.value = "";
        f.input.type = "password";
        f.eye.textContent = "ver";
      }
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
