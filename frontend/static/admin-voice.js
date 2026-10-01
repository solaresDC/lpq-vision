// LPQ_VISION admin-voice.js: the Voz del bot tab of the mostrador (family admin_voice; SPEC 1.5).
// Destino Telegram: each visible site's chat (pasted from /id), its test and its mantenimiento switch; the
// admin also sees the admin chat (machine alerts). Textos del bot (admin only): every message with its
// fichas as chips (a click inserts one at the cursor), Guardar, Probar (example values) and Restaurar
// original. Tests go through the api's own Telegram helper, so they work even when the bot is down.

import { api, el, toast } from "/static/app.js";
import { busy, card } from "/static/admin-kit.js";

const FAMILIAS = {
  site: "Van al chat del sitio",
  machine: "Van al chat de admin (o a todos los chats si no hay)",
  all: "Van a todos los chats",
  reply: "Responden al chat que preguntó",
  direct: "Van a un chat elegido",
};
const TEXTAREA_STYLE = "width: 100%; font: inherit; padding: 8px 10px; border: 1px solid var(--line); "
  + "border-radius: var(--radius); background: var(--paper); margin: 6px 0;";

export async function render(ctx) {
  const voice = await api("/api/admin/voice");
  const box = el("div", {}, destinoCard(voice, ctx));
  if (ctx.sess.admin) box.append(await textosCard(ctx));
  else box.append(card("Textos del bot", el("p", { class: "muted", text: "Los textos del bot los cambia el admin." })));
  return box;
}

function btn(text, cls) {
  return el("button", { class: cls || "", type: "button", text: text });
}

// wire(button, error, fn, okText): fn runs on click with the button busy; an error lands under the row.
function wire(button, error, fn, okText) {
  button.addEventListener("click", () => busy(button, async () => {
    error.textContent = "";
    try {
      await fn();
      if (okText) toast(okText, "ok");
    } catch (err) {
      error.textContent = err.message;
    }
  }));
  return button;
}

// --- Destino Telegram ----------------------------------------------------------------------

function destinoCard(voice, ctx) {
  const parts = [
    el("p", {
      class: "muted",
      text: "Escribe /id en el grupo de Telegram del sitio: el bot contesta con el número del chat. "
        + "Pégalo aquí, pulsa Guardar y luego «Enviar prueba».",
    }),
    ...voice.sites.map((s) => siteRow(s, ctx)),
  ];
  if (ctx.sess.admin) parts.push(el("h3", { text: "Chat de admin (alertas de máquina)" }), adminChatRow(voice, ctx));
  return card("Destino Telegram", ...parts);
}

function siteRow(s, ctx) {
  const input = el("input", { type: "text", inputmode: "numeric", placeholder: "-1001234567890" });
  input.value = s.chat_id || "";
  const error = el("div", { class: "kit-error" });
  const save = wire(btn("Guardar", "primary"), error, async () => {
    await api("/api/admin/voice/chat/" + encodeURIComponent(s.site), {
      method: "POST", json: { chat_id: input.value.trim() || null },
    });
    ctx.refresh();
  }, input.value.trim() ? "chat guardado" : null);
  const test = wire(btn("Enviar prueba"), error, async () => {
    const answer = await api("/api/admin/voice/test", { method: "POST", json: { site: s.site } });
    toast("prueba enviada: " + answer.enviado, "ok");
  }, null);
  const maint = wire(btn(s.mantenimiento ? "Quitar mantenimiento" : "Poner en mantenimiento", s.mantenimiento ? "ok" : ""),
    error, async () => {
      await api("/api/admin/voice/mantenimiento/" + encodeURIComponent(s.site), {
        method: "POST", json: { on: !s.mantenimiento },
      });
      ctx.refresh();
    }, s.mantenimiento ? "las alertas del sitio vuelven" : "sitio en mantenimiento: sus alertas callan");
  return el("div", { class: "kit-knob" },
    el("div", { class: "kit-head" },
      el("span", { class: "kit-name", text: s.site }),
      el("span", { class: s.chat_id ? "chip ok" : "chip", text: s.chat_id ? "chat asignado" : "sin chat" }),
      s.mantenimiento ? el("span", { class: "chip warn", text: "en mantenimiento: sus alertas callan" }) : null),
    el("div", { class: "kit-edit" }, input, save, test, maint),
    error);
}

function adminChatRow(voice, ctx) {
  const input = el("input", { type: "text", inputmode: "numeric", placeholder: "-1001234567890" });
  input.value = voice.admin_chat_id || "";
  const error = el("div", { class: "kit-error" });
  const save = wire(btn("Guardar", "primary"), error, async () => {
    await api("/api/admin/voice/admin_chat", { method: "POST", json: { chat_id: input.value.trim() || null } });
    ctx.refresh();
  }, "chat de admin guardado");
  const test = wire(btn("Enviar prueba"), error, async () => {
    const answer = await api("/api/admin/voice/test", { method: "POST", json: {} });
    toast("prueba enviada: " + answer.enviado, "ok");
  }, null);
  return el("div", { class: "kit-knob" },
    el("div", { class: "muted", text: "Vacío = las alertas de máquina van a todos los chats de los sitios." }),
    el("div", { class: "kit-edit" }, input, save, test),
    voice.env_chat
      ? el("div", { class: "kit-note", text: "Mientras exista, el chat temporal de .env (TELEGRAM_CHAT_ID) es el último recurso: recibe lo que no tenga otro destino." })
      : null,
    error);
}

// --- Textos del bot (admin) ------------------------------------------------------------------

async function textosCard(ctx) {
  const data = await api("/api/admin/textos");
  const groups = new Map();
  for (const t of data.textos) {
    if (!groups.has(t.familia)) groups.set(t.familia, []);
    groups.get(t.familia).push(t);
  }
  const where = ctx.site ? "al chat de " + ctx.site : "al chat de admin";
  const parts = [el("p", {
    class: "muted",
    text: "Las fichas entre llaves se llenan solas al enviar; pulsa una ficha para insertarla donde está el cursor. "
      + "«Probar» manda el texto con valores de ejemplo " + where + ".",
  })];
  for (const [familia, list] of groups) {
    parts.push(el("h3", { text: FAMILIAS[familia] || familia }));
    for (const t of list) parts.push(textoRow(t, data.max_chars, ctx));
  }
  return card("Textos del bot", ...parts);
}

function insertAt(area, token) {
  const start = area.selectionStart ?? area.value.length;
  const end = area.selectionEnd ?? area.value.length;
  area.value = area.value.slice(0, start) + token + area.value.slice(end);
  area.focus();
  area.selectionStart = area.selectionEnd = start + token.length;
}

function textoRow(t, maxChars, ctx) {
  const area = el("textarea", { rows: "3", maxlength: String(maxChars), style: TEXTAREA_STYLE });
  area.value = t.actual;
  const error = el("div", { class: "kit-error" });
  const chips = Object.keys(t.fichas).map((name) =>
    el("button", { type: "button", class: "kit-copy", text: "{" + name + "}", onclick: () => insertAt(area, "{" + name + "}") }));
  const save = wire(btn("Guardar", "primary"), error, async () => {
    await api("/api/admin/textos/save", { method: "POST", json: { key: t.key, text: area.value } });
    ctx.refresh();
  }, "texto guardado");
  const test = wire(btn("Probar"), error, async () => {
    const answer = await api("/api/admin/textos/test", { method: "POST", json: { key: t.key, text: area.value, site: ctx.site } });
    toast("prueba enviada: " + answer.enviado, "ok");
  }, null);
  const restore = t.propio
    ? wire(btn("Restaurar original"), error, async () => {
      await api("/api/admin/textos/restore", { method: "POST", json: { key: t.key } });
      ctx.refresh();
    }, "original restaurado")
    : null;
  return el("div", { class: "kit-knob" },
    el("div", { class: "kit-head" },
      el("span", { class: "kit-name", text: t.key }),
      el("span", { class: t.propio ? "chip warn" : "chip", text: t.propio ? "propio" : "de fábrica" })),
    el("div", { class: "muted", text: t.cuando }),
    chips.length ? el("div", { class: "kit-edit" }, el("span", { class: "muted", text: "Fichas:" }), ...chips) : null,
    area,
    t.propio ? el("div", { class: "muted", text: "Original: " + t.original }) : null,
    el("div", { class: "kit-edit" }, save, test, restore),
    error);
}
