// LPQ_VISION admin-knobs.js: the Perillas tab of the mostrador (family admin_knobs; SPEC 1.4, 1.5).
// Global view (admin): every first-floor knob is editable, and a per-site knob that some sites override
// offers "Borrar overrides (N)" behind the double confirm. One site's view: a per-site knob takes the
// site's own value or goes back to the global; a global knob is read-only there. A manager edits only its
// own site's per-site knobs. The server enforces every one of these rules too: the page only mirrors them.

import { api, el, fmtDate, qs, toast } from "/static/app.js";
import { SECCIONES, busy, card, confirmFlow, groupBySeccion, helps, knobRow } from "/static/admin-kit.js";

export async function render(ctx) {
  const data = await api("/api/admin/knobs" + qs({ site: ctx.site }));
  const site = data.site;                 // the server's scope: a manager always gets its own site
  const box = el("div", {}, card(site ? "Perillas de " + site : "Perillas globales",
    el("p", {
      class: "muted",
      text: site
        ? "Un valor propio solo afecta a " + site + ". «Volver al global» lo borra y el sitio vuelve a heredar."
        : "Estás cambiando el valor GLOBAL: lo heredan todos los sitios que no tienen valor propio.",
    })));
  if (ctx.sess.admin) box.append(rawLoggingCard(data, ctx));
  for (const [seccion, list] of groupBySeccion(data.knobs)) {
    const plain = list.filter((knob) => !knob.special);   // special knobs live in their own sections
    if (plain.length) box.append(card(SECCIONES[seccion] || seccion, ...plain.map((knob) => knobBlock(knob, site, ctx))));
  }
  return box;
}

function knobBlock(knob, site, ctx) {
  const admin = ctx.sess.admin;
  // A global knob is edited only by the admin, and only from the Global view.
  const editable = knob.per_site || (admin && !site);
  const row = knobRow(knob, {
    site,
    readOnly: !editable,
    onSave: async (value) => {
      await api("/api/admin/knobs/set", { method: "POST", json: { knob: knob.name, value: value, site: site } });
      ctx.refresh();
    },
    onReset: async () => {
      await api("/api/admin/knobs/reset", { method: "POST", json: { knob: knob.name, site: site } });
      ctx.refresh();
    },
  });
  if (!editable) {
    row.append(el("div", { class: "kit-note", text: admin ? "Se cambia en la vista Global." : "Solo el admin la cambia." }));
  }
  if (admin && !site && knob.per_site && knob.overrides > 0) row.append(clearButton(knob, ctx));
  return row;
}

function clearButton(knob, ctx) {
  const button = el("button", { class: "warn", type: "button", text: "Borrar overrides (" + knob.overrides + ")" });
  button.addEventListener("click", () => busy(button, async () => {
    try {
      const result = await confirmFlow(() =>
        api("/api/admin/knobs/clear", { method: "POST", json: { knob: knob.name } }));
      if (result) {
        toast(result.borrados + " valor(es) propio(s) borrado(s)", "ok");
        ctx.refresh();
      }
    } catch (err) {
      toast(err.message, "bad");
    }
  }));
  return el("div", { class: "kit-edit" }, button);
}

function rawLoggingCard(data, ctx) {
  const on = data.raw_logging.on;
  const knob = data.knobs.find((k) => k.name === "raw_logging");
  const error = el("div", { class: "kit-error" });
  const hours = el("input", { type: "number", min: "1", max: "720", step: "1", placeholder: String(data.auto_off_hours) });
  hours.value = String(data.auto_off_hours);
  const button = el("button", { class: on ? "" : "warn", type: "button", text: on ? "Apagar ahora" : "Prender" });
  button.addEventListener("click", () => busy(button, async () => {
    error.textContent = "";
    try {
      const json = on ? { on: false } : { on: true, hours: Number(hours.value) || data.auto_off_hours };
      await api("/api/admin/raw_logging", { method: "POST", json: json });
      toast(on ? "registro forense apagado" : "registro forense prendido", "ok");
      ctx.refresh();
    } catch (err) {
      error.textContent = err.message;
    }
  }));
  const status = el("p", {},
    el("span", { class: on ? "chip warn" : "chip", text: on ? "PRENDIDO" : "apagado" }),
    on && data.raw_logging.off_at
      ? el("span", { class: "muted", text: " se apaga solo el " + fmtDate(data.raw_logging.off_at) })
      : null);
  const controls = on
    ? el("div", { class: "kit-edit" }, button)
    : el("div", { class: "kit-edit" }, el("span", { class: "muted", text: "Horas:" }), hours, button);
  return card("Registro forense", knob ? helps(knob) : null, status, controls, error);
}
