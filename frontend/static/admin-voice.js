// LPQ_VISION admin-voice.js: the Voz del bot tab of the mostrador (family admin_voice; SPEC 1.5; owner rulings
// 3.8.1 and 3.10.3). Destino Telegram: each visible site's chat (pasted from /id), its test and its mantenimiento
// switch; the admin also sees the admin chat (machine alerts). A chat field takes ONLY digits (admin-kit's
// chatField) and carries the "Pega solo los números" hint under it. Textos del bot (admin only): every message
// with its fichas as chips (a click inserts one at the cursor), Guardar, Probar (example values) and Restaurar
// original. Every editable field shows its three helps (admin-kit's fieldHelp; the admin chat's come from the
// knob registry). Tests go through the api's own Telegram helper, so they work even with the bot down.

import { api, el, toast } from "/static/app.js";
import { DIGITS_HINT, busy, card, chatField, fieldHelp } from "/static/admin-kit.js";

const FAMILIAS = {
  site: "Van al chat del sitio",
  machine: "Van al chat de admin (o a todos los chats si no hay)",
  all: "Van a todos los chats",
  reply: "Responden al chat que preguntó",
  direct: "Van a un chat elegido",
};
const TEXTAREA_STYLE = "width: 100%; font: inherit; padding: 8px 10px; border: 1px solid var(--line); "
  + "border-radius: var(--radius); background: var(--paper); margin: 6px 0;";

// The three helps of this tab's fields (owner ruling 3.10.3). The admin chat's live in the knob registry.
const AYUDAS = {
  chat: {
    que_es: "El grupo de Telegram del sitio, donde llegan sus alertas. Escribe /id en ese grupo: el bot contesta con el número del chat.",
    ejemplo: "1001234567890",
    recomendacion: "Pega el número, pulsa Guardar y después «Enviar prueba» para confirmar que llega.",
    copiar: false,
  },
  mantenimiento: {
    que_es: "Calla las alertas del sitio mientras lo arreglan, por ejemplo con una cámara en reparación o una remodelación.",
    ejemplo: "Poner en mantenimiento durante una remodelación",
    recomendacion: "Quítalo en cuanto terminen: mientras esté puesto, ese sitio no avisa nada.",
    copiar: false,
  },
  texto: {
    que_es: "Lo que escribe el bot en cada caso. Las fichas entre llaves, como {sitio}, se llenan solas al enviar.",
    ejemplo: "🟢 El sitio {sitio} volvió a mandar imagen ({camara}).",
    recomendacion: "Corto y claro, como lo diría el gerente. Deja las fichas tal cual, con sus llaves, y usa «Probar» antes de dejarlo.",
    copiar: false,
  },
};

export async function render(ctx) {
  const voice = await api("/api/admin/voice");
  let adminHelp = null;
  if (ctx.sess.admin) {
    const knob = (await api("/api/admin/knobs")).knobs.find((k) => k.name === "admin_chat_id");
    // The registry's example carries the minus; this field takes only the digits.
    if (knob) adminHelp = { que_es: knob.que_es, ejemplo: knob.ejemplo.replace("-", ""), recomendacion: knob.recomendacion, copiar: false };
  }
  const box = el("div", {}, destinoCard(voice, ctx, adminHelp));
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

function destinoCard(voice, ctx, adminHelp) {
  const parts = [
    el("p", {
      class: "muted",
      text: "Cada sitio avisa en su propio grupo de Telegram." + (ctx.sess.admin ? " El chat de admin recibe las alertas de máquina." : ""),
    }),
    fieldHelp(AYUDAS.chat, { name: "Chat del sitio" }),
    fieldHelp(AYUDAS.mantenimiento, { name: "Mantenimiento" }),
    ...voice.sites.map((s) => siteRow(s, ctx)),
  ];
  if (ctx.sess.admin) parts.push(el("h3", { text: "Chat de admin (alertas de máquina)" }), adminChatRow(voice, ctx, adminHelp));
  return card("Destino Telegram", ...parts);
}

function siteRow(s, ctx) {
  const field = chatField(s.chat_id);
  const error = el("div", { class: "kit-error" });
  const save = wire(btn("Guardar", "primary"), error, async () => {
    const chat = field.read();
    await api("/api/admin/voice/chat/" + encodeURIComponent(s.site), { method: "POST", json: { chat_id: chat } });
    toast(chat ? "chat guardado" : "chat quitado", "ok");
    ctx.refresh();
  }, null);
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
    el("div", { class: "kit-edit" }, field.node, save, test, maint),
    el("div", { class: "kit-hint", text: DIGITS_HINT }),
    error);
}

function adminChatRow(voice, ctx, adminHelp) {
  const field = chatField(voice.admin_chat_id);
  const error = el("div", { class: "kit-error" });
  const save = wire(btn("Guardar", "primary"), error, async () => {
    const chat = field.read();
    await api("/api/admin/voice/admin_chat", { method: "POST", json: { chat_id: chat } });
    toast(chat ? "chat de admin guardado" : "chat de admin quitado", "ok");
    ctx.refresh();
  }, null);
  const test = wire(btn("Enviar prueba"), error, async () => {
    const answer = await api("/api/admin/voice/test", { method: "POST", json: {} });
    toast("prueba enviada: " + answer.enviado, "ok");
  }, null);
  return el("div", { class: "kit-knob" },
    el("div", { class: "muted", text: "Vacío = las alertas de máquina van a todos los chats de los sitios." }),
    el("div", { class: "kit-edit" }, field.node, save, test),
    el("div", { class: "kit-hint", text: DIGITS_HINT }),
    adminHelp ? fieldHelp(adminHelp) : null,
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
  const parts = [
    el("p", {
      class: "muted",
      text: "Las fichas entre llaves se llenan solas al enviar; pulsa una ficha para insertarla donde está el cursor. "
        + "«Probar» manda el texto con valores de ejemplo " + where + ".",
    }),
    fieldHelp(AYUDAS.texto, { name: "Cada texto" }),
  ];
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
  const area = el("textarea", { rows: "3", maxlength: String(maxChars), style: TEXTAREA_STYLE, placeholder: t.original });
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
