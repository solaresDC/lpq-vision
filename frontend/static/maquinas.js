// LPQ_VISION maquinas.js: the MACHINE ROOM, the second floor (SPEC 1.6; Section 3.15).
// Only the admin enters, and only with the cuarto password: it opens the room for this session for 15 minutes
// (a countdown on top; "Cerrar el cuarto" closes it at once). When the permission ends, or any read answers 403,
// the page goes back to the door. Inside, every change is the server's double confirm with its visible diff.
// The cards: the post-reboot checklist (re-read every 5 s), the model and its answer length (only models whose
// key exists on the server are listed), the analysts (wanted against running, and Aplicar), the queue constants
// (the machine block's assert answers in its own words), the sessions, restarting one service, the reboot (its
// notice to every chat first), the live log viewer (SSE: the last lines, then every new one) and the cuarto's
// password. The butler (a service of the server's host, built in 3.16; never one on the laptop) does the restarts,
// the reboot and the scale: while its mailbox does not answer, those buttons change nothing and say so.
// One view at a time (loading, door, birth, room, error): show() switches it, stops every timer, poll and stream of
// the view before, and bumps the generation, so an answer that arrives for an older view is dropped.

import { api, el, mountHeader, poll, requireLogin, session, toast } from "/static/app.js";
import { busy, card, confirmDialog, fieldHelp, helps, newPassword } from "/static/admin-kit.js";

const POLL_STATUS_MS = 5000;
const LOG_KEEP = 1000;              // lines the viewer keeps on screen
const RESTART_POLL_MS = 2000;       // while the ventanilla restarts, the page asks /api/session this often
const RESTART_GIVE_UP_MS = 300000;  // and stops asking after this long
const SAME_VENTANILLA_MS = 60000;   // a ventanilla restart that still answers as the same one after this did not happen

const DOOR_HELP = {
  que_es: "La contraseña del cuarto de máquinas: una segunda llave, distinta de la de tu cuenta.",
  ejemplo: "la que guardaste al crearla",
  recomendacion: "El cuarto se cierra solo a los 15 minutos; entonces se pide otra vez.",
  copiar: false,
};
const NEW_PASS_HELP = {
  que_es: "La llave del cuarto de máquinas. La pide esta página cada vez que alguien entra, y cada 15 minutos.",
  ejemplo: "una frase larga que solo tú sepas",
  recomendacion: "Mínimo 10 caracteres, distinta de la de tu cuenta; guárdala donde guardas la del servidor.",
  copiar: false,
};
const LOG_HELP = {
  que_es: "Lo que cada servicio va anotando: la ventanilla (fastapi.log), cada analista (worker-...log) y el bot (bot.log).",
  ejemplo: "fastapi.log",
  recomendacion: "fastapi.log para lo que pasa en las páginas; un worker para los análisis. Es una ventana, no el archivo completo.",
  copiar: false,
};

const host = document.getElementById("room");
let sess = await session();
if (!sess) sess = await requireLogin();
mountHeader("Máquinas", sess);

let cleanups = [];
let gen = 0;                        // bumped by every show(): async answers of an older view are dropped
let view = "loading";

function leave() {
  for (const fn of cleanups.splice(0)) {
    try {
      fn();
    } catch (err) {
      console.warn("leave:", err.message);
    }
  }
}

// Switch the view: everything the old one started stops; returns the new generation.
function show(next, ...nodes) {
  leave();
  gen += 1;
  view = next;
  host.replaceChildren(...nodes.filter(Boolean));
  return gen;
}

// A timer or stream of the current view; one started for a view that is already gone stops at once.
function keep(my, stop) {
  if (my !== gen) {
    stop();
    return false;
  }
  cleanups.push(stop);
  return true;
}

function clock() {
  return new Date().toLocaleTimeString("es-MX");
}

function mmss(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m + ":" + (r < 10 ? "0" + r : String(r));
}

// The person's sentence for an error: the server's own words; plain Spanish when nothing answered; the page's own
// check (a password typed twice differently) as it is; and a slip of this page's code said as such, never as a
// connection problem.
function say(err) {
  if (noAnswer(err)) return "sin conexión con la ventanilla";
  if (err && err.status) return err.message;
  if (err instanceof TypeError || err instanceof ReferenceError || err instanceof RangeError) {
    return "esta página no pudo seguir (" + err.message + "): recárgala";
  }
  return (err && err.message) || "algo falló";
}

// Nothing of the ventanilla's own came back: the connection was cut (marked by call()), or a gateway in between (the
// phone's https road) answered a bare 5xx with no words of the api.
function noAnswer(err) {
  if (err && err.network) return true;
  return Boolean(err && err.status >= 500 && err.message === "HTTP " + err.status);   // api()'s words when no JSON came
}

// api() with its own failures to connect marked (fetch rejects with a TypeError), so that a TypeError of this page's
// code is never read as "sin conexión".
async function call(path, opts) {
  try {
    return await api(path, opts);
  } catch (err) {
    if (err instanceof TypeError) err.network = true;
    throw err;
  }
}

// Every read and action of the room: a 403 means the permission is gone (it ended, or the cuarto password changed in
// another session): back to the door, with the server's own words.
async function roomApi(path, opts) {
  try {
    return await call(path, opts);
  } catch (err) {
    if (err.status === 403) {
      closed(err.message);
      err.closed = true;
    }
    throw err;
  }
}

function closed(message) {
  if (view === "door" || view === "birth" || view === "admin-only") return;
  show("door", doorCard(message));
}

// /api/session without the login overlay: 200 = the same ventanilla, 401 = a NEW one (sessions live in its memory),
// 0 = no answer (it is away).
async function sessionCode() {
  try {
    const res = await fetch("/api/session", { credentials: "same-origin", cache: "no-store" });
    return res.status;
  } catch {
    return 0;
  }
}

// --- the door ---------------------------------------------------------------------------

// Password managers file a password under the user name beside it: "cuarto", never the admin's own account.
function cuartoName() {
  return el("input", { type: "text", autocomplete: "username", value: "cuarto", hidden: true, tabindex: "-1", "aria-hidden": "true" });
}

function doorCard(note) {
  const input = el("input", { type: "password", autocomplete: "current-password", placeholder: "contraseña del cuarto",
    "aria-label": "contraseña del cuarto", style: "width: auto;" });
  const error = el("div", { class: "kit-error" });
  const open = el("button", { class: "primary", type: "submit", text: "Abrir el cuarto" });
  const form = el("form", {}, cuartoName(), fieldHelp(DOOR_HELP), el("div", { class: "kit-edit" }, input, open), error);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!input.value) {
      error.textContent = "Escribe la contraseña del cuarto.";
      input.focus();
      return;
    }
    busy(open, async () => {
      error.textContent = "";
      try {
        await call("/api/admin/machine/unlock", { method: "POST", json: { password: input.value } });
        input.value = "";
        await start();
      } catch (err) {
        error.textContent = say(err);
        input.value = "";
        input.focus();
      }
    });
  });
  setTimeout(() => input.focus(), 0);
  return card("🔒 Cuarto de máquinas cerrado",
    note ? el("p", { class: "kit-note", text: note }) : null,
    el("p", { class: "muted", text: "Aquí se cambia el modelo, los analistas, las constantes de la cola, y se reinicia el servidor. Para entrar se pide su propia contraseña." }),
    form);
}

function birthCard() {
  const pw = newPassword({ repeat: true });
  const error = el("div", { class: "kit-error" });
  const create = el("button", { class: "primary", type: "button", text: "Crear la contraseña del cuarto" });
  create.addEventListener("click", () => busy(create, async () => {
    error.textContent = "";
    try {
      const password = pw.read();
      await call("/api/admin/machine/birth", { method: "POST", json: { password: password } });
      pw.clear();
      toast("contraseña del cuarto creada", "ok");
      await start();
    } catch (err) {
      if (err.status === 409) {
        pw.clear();
        await start(err.message);          // someone created it meanwhile: the door, with the server's words
        return;
      }
      error.textContent = say(err);
    }
  }));
  return card("Cuarto de máquinas: falta su contraseña",
    el("p", { class: "muted", text: "El cuarto todavía no tiene contraseña. Créala ahora (se escribe dos veces): desde aquí solo se puede crear; cambiarla se hace adentro." }),
    fieldHelp(NEW_PASS_HELP), cuartoName(), pw.node, el("div", { class: "kit-edit", style: "margin-top: 8px;" }, create), error);
}

function errorCard(err) {
  const retry = el("button", { class: "primary", type: "button", text: "Reintentar" });
  retry.addEventListener("click", () => start());
  return card("No se pudo abrir el cuarto de máquinas",
    el("p", { class: "kit-note", text: say(err) }),
    el("div", { class: "kit-edit" }, retry));
}

// --- the room -----------------------------------------------------------------------------

async function start(note = null) {
  const my = show("loading", el("p", { class: "muted", text: "Cargando…" }));
  try {
    if (!sess.admin) {
      show("admin-only", card("Cuarto de máquinas", el("p", { class: "muted", text: "Solo el admin entra al cuarto de máquinas." })));
      return;
    }
    const overview = await call("/api/admin/overview");
    if (my !== gen) return;
    if (!overview.cuarto_exists) {
      show("birth", birthCard());
      return;
    }
    if (overview.machine_grant_s <= 0) {
      show("door", doorCard(note));
      return;
    }
    const data = await call("/api/admin/machine");
    if (my !== gen) return;
    if (!(data.grant_s > 0)) {
      show("door", doorCard("El permiso del cuarto venció: escribe la contraseña otra vez."));
      return;
    }
    try {
      renderRoom(data);
    } catch (err) {
      console.error(err);
      show("error", errorCard(err));
    }
  } catch (err) {
    if (my !== gen) return;
    if (err.status === 403) {
      show("door", doorCard(err.message));
      return;
    }
    show("error", errorCard(err));
  }
}

const parts = {};                   // the room's cards, by name

function renderRoom(data) {
  show("room");                     // first: the cards' timers belong to the room
  parts.grant = grantBar(data.grant_s);
  parts.status = statusCard();
  parts.model = modelCard(data);
  parts.workers = workersCard(data);
  parts.consts = constantsCard(data);
  parts.sessions = sessionsCard(data);
  parts.restart = restartCard(data);
  parts.reboot = rebootCard(data);
  parts.logs = logsCard();
  parts.password = passwordCard();
  host.replaceChildren(parts.grant, parts.status, parts.model, parts.workers, parts.consts, parts.sessions,
    parts.restart, parts.reboot, parts.logs, parts.password);
}

// After a change, from a fresh read: the analysts' numbers and the butler's line are repainted in place, and after a
// knob's save (knobs: true) the three knob cards are drawn again. Cards that may hold an action still running (Aplicar,
// the restarts, the reboot's countdown, the log viewer, what is typed for a new password) are never replaced.
async function refreshRoom({ knobs = false } = {}) {
  const my = gen;
  let data;
  try {
    data = await roomApi("/api/admin/machine");
  } catch (err) {
    return;                         // the next status round (or the door) tells the rest
  }
  if (my !== gen) return;
  if (knobs) {
    for (const [name, build] of [["model", modelCard], ["consts", constantsCard], ["sessions", sessionsCard]]) {
      const fresh = build(data);
      parts[name].replaceWith(fresh);
      parts[name] = fresh;
    }
  }
  paintWorkers(data.analistas.deseados, data.analistas.corriendo);
  paintButler(data.mayordomo.responde);
}

function grantBar(grantS) {
  const my = gen;
  const left = el("span", { class: "mq-left" });
  const lock = el("button", { type: "button", text: "Cerrar el cuarto" });
  let deadline = Date.now() + grantS * 1000;
  let asking = false;
  const tick = async () => {
    const s = (deadline - Date.now()) / 1000;
    if (s > 0) {
      left.textContent = "🔓 Cuarto abierto: se cierra solo en " + mmss(s);
      return;
    }
    if (asking) return;
    asking = true;
    // The server owns the permission: another tab of this session may have opened the room again meanwhile.
    const who = await session();
    asking = false;
    if (my !== gen) return;
    if (who && who.machine_grant_s > 0) {
      deadline = Date.now() + who.machine_grant_s * 1000;
      return;
    }
    closed("El permiso del cuarto venció: escribe la contraseña otra vez.");
  };
  tick();
  const timer = setInterval(tick, 1000);
  keep(my, () => clearInterval(timer));
  lock.addEventListener("click", () => busy(lock, async () => {
    try {
      await call("/api/admin/machine/lock", { method: "POST" });
    } catch (err) {
      toast("No se pudo cerrar el cuarto (" + say(err) + "): sigue abierto, vuelve a intentarlo.", "bad");
      return;
    }
    closed("Cerraste el cuarto.");
  }));
  return card(null, el("div", { class: "mq-grant" }, left, el("span", { class: "spacer" }), lock));
}

// --- the checklist ------------------------------------------------------------------------

const DOT = { true: "🟢", false: "🔴", null: "⚪" };

function statusCard() {
  const my = gen;
  const list = el("ul", { class: "mq-status" });
  const when = el("div", { class: "mq-live" });
  const pending = el("p", { class: "kit-note" });
  let inFlight = false;
  const paint = (data) => {
    list.replaceChildren(...data.items.map((item) => el("li", {},
      el("span", { class: "mq-dot", text: DOT[String(item.ok)] || "⚪" }),
      el("strong", { text: item.titulo + ": " }),
      el("span", { text: item.texto }),
      item.detalle ? el("ul", {}, item.detalle.map((line) => el("li", { class: "muted", text: line }))) : null)));
    pending.textContent = rebootLine(data.reinicio_pendiente_s, data.reinicio_ultimo);
    paintWorkers(data.analistas.deseados, data.analistas.corriendo);
    paintButler(data.mayordomo.responde);
  };
  const stop = poll(async () => {
    if (my !== gen || inFlight) return;
    inFlight = true;
    try {
      const data = await roomApi("/api/admin/status");
      if (my !== gen) return;
      paint(data);
      when.textContent = "actualizado " + clock();
    } catch (err) {
      if (!err.closed && my === gen) when.textContent = "No se pudo leer el estado (" + clock() + "): " + say(err);
    } finally {
      inFlight = false;
    }
  }, POLL_STATUS_MS);
  keep(my, stop);
  return card("Estado: la lista de arranque",
    el("p", { class: "muted", text: "Lo primero que se mira después de un reinicio: todo en verde es el cerebro completo. Gris = sin dato o fuera de lo normal sin ser falla." }),
    pending, list, when);
}

// What the checklist says about a reboot: one on its way, or the last one that did not happen (for an hour).
function rebootLine(pendiente, ultimo) {
  if (pendiente !== null && pendiente !== undefined) return "🔄 Hay un reinicio del servidor en camino.";
  if (ultimo && ultimo.estado === "fallido" && ultimo.hace_s < 3600) {
    return "⚠️ El último reinicio pedido no se hizo (hace " + Math.max(1, Math.round(ultimo.hace_s / 60)) + " min): " + ultimo.motivo + ".";
  }
  return "";
}

// The sentence about the chats after a reboot that did not happen: only what the server reports it told them.
function cancelLine(ultimo) {
  if (ultimo.de === null || ultimo.de === undefined || ultimo.de === 0) return "";
  if (ultimo.avisados === ultimo.de) return " Los chats que recibieron el aviso ya saben que el cerebro sigue en línea.";
  if (!ultimo.avisados) return " No se pudo avisar a los chats que no habrá reinicio: avísales tú.";
  return " Se avisó a " + ultimo.avisados + " de " + ultimo.de + " chat(s) que el cerebro sigue en línea; a los demás no se pudo.";
}

// --- one second-floor knob --------------------------------------------------------------------

function knobsByName(data) {
  return Object.fromEntries(data.knobs.map((k) => [k.name, k]));
}

function machineKnob(knob, { models = null } = {}) {
  const value = knob.global;
  let input;
  if (knob.kind === "model") {
    input = el("select", { "aria-label": knob.name }, models.map((m) => el("option", { value: m.model, text: m.model + " (" + m.label + ")" })));
    if (models.some((m) => m.model === value)) {
      input.value = value;
    } else {
      input.prepend(el("option", { value: "", text: "elige un modelo" }));
      input.value = "";
    }
  } else {
    input = el("input", { type: "number", inputmode: "numeric", min: knob.min, max: knob.max, step: "1",
      placeholder: knob.ejemplo, "aria-label": knob.name });
    input.value = value === null || value === undefined ? "" : String(value);
  }
  const save = el("button", { class: "primary", type: "button", text: "Guardar" });
  const error = el("div", { class: "kit-error" });
  save.addEventListener("click", () => busy(save, async () => {
    const my = gen;
    error.textContent = "";
    try {
      const first = await roomApi("/api/admin/machine/knob", { method: "POST", json: { knob: knob.name, value: input.value.trim() } });
      if (my !== gen) return;
      if (!first.pending_id) {
        toast(knob.name + " ya vale eso: nada cambió", "ok");
        return;
      }
      if (!(await confirmDialog(first)) || my !== gen) return;
      const answer = await roomApi("/api/admin/confirm", { method: "POST", json: { pending_id: first.pending_id } });
      if (my !== gen) return;
      const result = answer.result || {};
      toast(result.cambio === false ? knob.name + " ya valía eso: nada cambió" : "guardado: " + knob.name, "ok");
      await refreshRoom({ knobs: true });
    } catch (err) {
      if (!err.closed && my === gen) error.textContent = say(err);
    }
  }));
  return el("div", { class: "kit-knob" },
    el("div", { class: "kit-head" }, el("span", { class: "kit-name", text: knob.name }), el("span", { class: "chip", text: "toda la cadena" })),
    helps(knob),
    el("div", { class: "kit-edit" }, input, save),
    error);
}

function modelCard(data) {
  const byName = knobsByName(data);
  const inUse = data.modelo_en_uso;
  const warning = inUse.con_llave ? null : el("p", { class: "kit-note",
    text: "En uso: " + inUse.model + ", que no tiene llave en este servidor. Elige uno de la lista: solo aparecen los modelos con llave." });
  return card("El modelo",
    el("p", { class: "muted", text: "Quién analiza los platos y cuánto puede escribir. Solo aparecen los modelos cuya llave existe en este servidor." }),
    warning,
    machineKnob(byName["active_model"], { models: data.modelos }),
    machineKnob(byName["machine.max_tokens"]));
}

function constantsCard(data) {
  const byName = knobsByName(data);
  return card("Constantes de la cola",
    el("p", { class: "muted", text: "El tiempo de una llamada siempre debe ser menor que la ventana de rescate: si no, el cambio rebota aquí mismo con la razón." }),
    machineKnob(byName["machine.llm_timeout_s"]),
    machineKnob(byName["machine.orphan_timeout_min"]),
    machineKnob(byName["machine.max_attempts"]));
}

function sessionsCard(data) {
  return card("Sesiones", machineKnob(knobsByName(data)["machine.session_hours"]));
}

// --- the analysts ---------------------------------------------------------------------------

function butlerLine(responde) {
  return el("p", { id: "mq-butler", class: responde ? "muted" : "kit-note",
    text: responde
      ? "🟢 El mayordomo responde: Aplicar, Reiniciar y el reinicio del servidor se le piden a él."
      : "🔴 El mayordomo no responde: Aplicar, Reiniciar, el reinicio del servidor y el ajuste de analistas al arrancar no cambian nada mientras tanto." });
}

function paintButler(responde) {
  const line = document.getElementById("mq-butler");
  if (line && line.dataset.responde !== String(responde)) {
    const fresh = butlerLine(responde);
    fresh.dataset.responde = String(responde);
    line.replaceWith(fresh);
  }
}

function paintWorkers(deseados, corriendo) {
  const wanted = document.getElementById("mq-wanted");
  if (wanted) wanted.textContent = String(deseados);
  const running = document.getElementById("mq-running");
  if (running) running.textContent = corriendo === null || corriendo === undefined ? "?" : String(corriendo);
}

function workersCard(data) {
  const knob = knobsByName(data)["machine.workers"];
  const a = data.analistas;
  const input = el("input", { type: "number", inputmode: "numeric", min: "1", max: String(a.max), step: "1",
    placeholder: knob.ejemplo, "aria-label": "analistas deseados" });
  input.value = String(a.deseados);
  const apply = el("button", { class: "primary", type: "button", text: "Aplicar" });
  const error = el("div", { class: "kit-error" });
  apply.addEventListener("click", () => busy(apply, async () => {
    const my = gen;
    error.textContent = "";
    const count = Number(input.value);
    if (!Number.isInteger(count) || count < 1 || count > a.max) {
      error.textContent = "Escribe un número entero de 1 a " + a.max + ".";
      return;
    }
    try {
      const first = await roomApi("/api/admin/machine/workers", { method: "POST", json: { count: count } });
      if (my !== gen) return;
      if (!first.pending_id) {
        toast("ya corren " + count + (count === 1 ? " analista" : " analistas") + ": nada cambió", "ok");
        return;
      }
      if (!(await confirmDialog(first)) || my !== gen) return;
      const answer = await roomApi("/api/admin/confirm", { method: "POST", json: { pending_id: first.pending_id } });
      if (my !== gen) return;
      const result = answer.result;
      toast("el mayordomo puso " + result.deseados + (result.deseados === 1 ? " analista" : " analistas"), "ok");
    } catch (err) {
      if (err.closed || my !== gen) return;
      error.textContent = say(err);
    }
    await refreshRoom();            // also after an error: a late answer may have kept the new number
  }));
  const butler = butlerLine(data.mayordomo.responde);
  butler.dataset.responde = String(data.mayordomo.responde);
  return card("Los analistas",
    el("p", { class: "mq-line" },
      el("span", { text: "Deseados: " }), el("span", { id: "mq-wanted", class: "mq-big", text: String(a.deseados) }),
      el("span", { text: "   ·   Corriendo ahora: " }),
      el("span", { id: "mq-running", class: "mq-big", text: a.corriendo === null ? "?" : String(a.corriendo) })),
    el("p", { class: "muted", text: "Corriendo se cuenta en vivo («?» = no se pudo contar). El número deseado se recuerda: al arrancar, la ventanilla lo compara con los que corren y le pide al mayordomo el número guardado." }),
    helps(knob),
    el("div", { class: "kit-edit" }, input, apply),
    error,
    butler);
}

// --- restarts ------------------------------------------------------------------------------

// While the ventanilla restarts (fastapi, or the whole server): a NEW ventanilla answers /api/session with 401
// (sessions live in its memory): reload, it asks to log in. The same one still answering after SAME_VENTANILLA_MS
// means it did not restart. `my` is the view of the click that started it.
function waitForNewVentanilla(note, onSame, my) {
  if (my !== gen) return;
  const started = Date.now();
  const timer = setInterval(async () => {
    const code = await sessionCode();
    if (my !== gen) return;
    if (code === 401) {
      clearInterval(timer);
      note.textContent = "La ventanilla volvió: recargando…";
      location.reload();
      return;
    }
    if (code === 200 && Date.now() - started > SAME_VENTANILLA_MS) {
      clearInterval(timer);
      onSame();
    } else if (Date.now() - started > RESTART_GIVE_UP_MS) {
      clearInterval(timer);
      note.textContent = "La ventanilla no ha vuelto en 5 minutos: recarga esta página en un rato.";
    }
  }, RESTART_POLL_MS);
  keep(my, () => clearInterval(timer));
}

function restartCard(data) {
  const rows = data.servicios.map((s) => {
    const go = el("button", { class: s.servicio === "fastapi" ? "bad" : "warn", type: "button", text: "Reiniciar" });
    const note = el("div", { class: "kit-note" });
    const ventanillaGone = (text, my) => {
      note.textContent = text;
      waitForNewVentanilla(note, () => {
        note.textContent = "La ventanilla no se reinició: sigue la misma de antes. Mira la lista de arranque.";
      }, my);
    };
    go.addEventListener("click", () => busy(go, async () => {
      const my = gen;
      note.textContent = "";
      let first;
      try {
        first = await roomApi("/api/admin/machine/restart", { method: "POST", json: { service: s.servicio } });
      } catch (err) {
        if (!err.closed && my === gen) note.textContent = say(err);
        return;
      }
      if (my !== gen || !(await confirmDialog(first)) || my !== gen) return;
      try {
        await roomApi("/api/admin/confirm", { method: "POST", json: { pending_id: first.pending_id } });
      } catch (err) {
        if (err.closed || my !== gen) return;
        if (s.servicio === "fastapi" && noAnswer(err)) {
          ventanillaGone("La ventanilla no contestó: puede estar reiniciándose. Esta página espera a que vuelva.", my);
          return;
        }
        if (s.servicio === "fastapi" && err.status === 504) {
          ventanillaGone(err.message + " Esta página espera a ver si la ventanilla vuelve.", my);
          return;
        }
        note.textContent = say(err);
        return;
      }
      if (my !== gen) return;
      if (s.servicio === "fastapi") {
        ventanillaGone("La ventanilla se reinicia: esta página vuelve sola cuando responda (pedirá entrar otra vez).", my);
      } else {
        toast("el mayordomo reinició " + s.nombre, "ok");
      }
    }));
    return el("div", { class: "mq-service" },
      el("div", { class: "kit-head" }, el("span", { class: "kit-name", text: s.nombre }), go),
      el("div", { class: s.servicio === "fastapi" ? "kit-note" : "muted", text: s.aviso }),
      note);
  });
  return card("Reiniciar un servicio",
    el("p", { class: "muted", text: "Uno solo, y los demás siguen trabajando. La base de datos no tiene botón." }),
    ...rows);
}

// The reboot after its notice: a countdown to the butler's call, then its outcome. A reboot that happens takes the
// ventanilla away and a new one answers (reload); one that does not happen is told by the same ventanilla, which keeps
// "en camino" while it still waits for the butler (its answer, then a grace). `go` stays off while this runs.
function followReboot(note, seconds, lead, my, go) {
  if (my !== gen) return;
  let left = seconds;
  let watching = false;
  go.disabled = true;
  const done = (text) => {
    note.textContent = text;
    go.disabled = false;
  };
  const watch = () => {
    watching = true;
    note.textContent = "Reiniciando: la ventanilla se va unos minutos. Esta página espera y se recarga sola; después entra otra vez, abre el cuarto y revisa la lista de arranque.";
    let since = Date.now();         // the give-up counts from when the ventanilla stopped waiting for the butler
    const timer = setInterval(async () => {
      const code = await sessionCode();
      if (my !== gen) return;
      if (code === 401) {
        clearInterval(timer);
        note.textContent = "La ventanilla volvió: recargando…";
        location.reload();
        return;
      }
      if (code !== 200) return;                // away: keep waiting
      let st;
      try {
        const res = await fetch("/api/admin/status", { credentials: "same-origin", cache: "no-store" });
        if (!res.ok) return;
        st = await res.json();
      } catch {
        return;
      }
      if (my !== gen) return;
      const last = st.reinicio_ultimo;
      if (st.reinicio_pendiente_s !== null && st.reinicio_pendiente_s !== undefined) {
        since = Date.now();
        if (last && last.estado === "sin_respuesta") {
          note.textContent = "El mayordomo tomó la orden pero " + last.motivo + ": esta página espera a ver si el servidor se reinicia.";
        }
        return;
      }
      if (last && last.estado === "fallido") {
        clearInterval(timer);
        done("El reinicio no se hizo: " + last.motivo + "." + cancelLine(last));
        return;
      }
      if (Date.now() - since > RESTART_GIVE_UP_MS) {
        clearInterval(timer);
        done("Pasaron 5 minutos y la ventanilla sigue igual: mira la lista de arranque.");
      }
    }, RESTART_POLL_MS);
    keep(my, () => clearInterval(timer));
  };
  const tick = () => {
    if (watching) return;
    if (left > 0) {
      note.textContent = lead + "El servidor se reinicia en " + left + " s.";
      left -= 1;
      return;
    }
    clearInterval(countdown);
    watch();
  };
  const countdown = setInterval(tick, 1000);
  keep(my, () => clearInterval(countdown));
  tick();
}

function rebootCard(data) {
  const r = data.reinicio;
  const chats = r.chats === null || r.chats === undefined ? "?" : String(r.chats);
  const go = el("button", { class: "bad", type: "button", text: "🔄 Reiniciar el servidor" });
  const note = el("div", { class: "kit-note" });
  go.addEventListener("click", async () => {
    const my = gen;
    const result = await busy(go, async () => {
      note.textContent = "";
      let first;
      try {
        first = await roomApi("/api/admin/machine/reboot", { method: "POST" });
      } catch (err) {
        if (!err.closed && my === gen) note.textContent = say(err);
        return null;
      }
      if (my !== gen || !(await confirmDialog(first)) || my !== gen) return null;
      note.textContent = "Avisando a los chats…";
      try {
        return (await roomApi("/api/admin/confirm", { method: "POST", json: { pending_id: first.pending_id } })).result;
      } catch (err) {
        if (!err.closed && my === gen) note.textContent = say(err);
        return null;
      }
    });
    // after busy() gave the button back: the follow keeps it off until the outcome is known
    if (result) followReboot(note, result.reinicio_en_s, "Aviso enviado a " + result.enviados + " de " + result.chats + " chat(s). ", my, go);
  });
  if (r.pendiente_s !== null && r.pendiente_s !== undefined) {
    followReboot(note, r.pendiente_s, "Hay un reinicio en camino. ", gen, go);
  } else if (r.ultimo && r.ultimo.estado === "fallido" && r.ultimo.hace_s < 3600) {
    note.textContent = "El último reinicio pedido no se hizo: " + r.ultimo.motivo + "." + cancelLine(r.ultimo);
  }
  return card("Reiniciar el servidor",
    el("p", { class: "muted", text: "El servidor completo se apaga y vuelve solo. Antes, la ventanilla avisa a los " + chats + " chat(s) y espera " + r.aviso_s + " s." }),
    el("p", {}, el("span", { class: "muted", text: "El aviso: " }), el("span", { text: r.texto })),
    el("p", { class: "kit-hint", text: "El texto se cambia en el mostrador: Voz del bot, aviso_reinicio." }),
    el("div", { class: "kit-edit" }, go),
    note);
}

// --- the live log viewer (SSE) -------------------------------------------------------------

function sizeText(bytes) {
  return bytes < 1024 ? bytes + " B" : Math.round(bytes / 1024) + " KB";
}

function logsCard() {
  const my = gen;
  const select = el("select", { "aria-label": "Registro" });
  const refresh = el("button", { type: "button", text: "↻", title: "Volver a leer la lista", "aria-label": "Volver a leer la lista" });
  const openBtn = el("button", { class: "primary", type: "button", text: "Ver en vivo" });
  const stopBtn = el("button", { type: "button", text: "Detener" });
  const clearBtn = el("button", { type: "button", text: "Limpiar" });
  const note = el("div", { class: "kit-hint" });
  const view = el("pre", { class: "mq-log" });
  let source = null;
  let opened = "";
  let queue = [];                   // lines waiting for the next frame: one layout per frame, never one per line
  let frame = 0;
  const sizes = [];                 // lines held by each text node of the view
  let total = 0;
  stopBtn.disabled = true;

  const flush = () => {
    frame = 0;
    if (!queue.length) return;
    const lines = queue.length > LOG_KEEP ? queue.slice(-LOG_KEEP) : queue;
    queue = [];
    const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 8;
    view.append(document.createTextNode(lines.join("\n") + "\n"));
    sizes.push(lines.length);
    total += lines.length;
    while (sizes.length > 1 && total - sizes[0] >= LOG_KEEP) {
      view.removeChild(view.firstChild);
      total -= sizes.shift();
    }
    if (atBottom) view.scrollTop = view.scrollHeight;
  };
  const append = (line) => {
    queue.push(line);
    if (queue.length > 2 * LOG_KEEP) queue = queue.slice(-LOG_KEEP);   // a hidden tab draws no frames
    if (!frame) frame = requestAnimationFrame(flush);
  };
  const clear = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    queue = [];
    sizes.length = 0;
    total = 0;
    view.replaceChildren();
  };
  const stop = (message) => {
    if (source) {
      source.close();
      source = null;
    }
    if (frame) {
      cancelAnimationFrame(frame);
      flush();
    }
    stopBtn.disabled = true;
    if (message) note.textContent = message;
  };
  const fill = (data) => {
    const before = select.value;
    select.replaceChildren(...data.archivos.map((f) => el("option", { value: f.archivo, text: f.archivo + " (" + sizeText(f.bytes) + ")" })));
    if (!data.archivos.length) select.append(el("option", { value: "", text: "ningún registro todavía" }));
    if (before && data.archivos.some((f) => f.archivo === before)) select.value = before;
  };
  const loadList = async () => {
    try {
      const data = await roomApi("/api/admin/logs");
      if (my === gen) fill(data);
      return data;
    } catch (err) {
      if (!err.closed && my === gen) note.textContent = "No se pudo leer la lista: " + say(err);
      return null;
    }
  };
  openBtn.addEventListener("click", () => {
    if (!select.value) {
      note.textContent = "No hay ningún registro todavía.";
      return;
    }
    stop(null);
    clear();
    opened = select.value;
    note.textContent = "Conectando con " + opened + "…";
    const name = opened;
    source = new EventSource("/api/admin/logs/stream?archivo=" + encodeURIComponent(name));
    stopBtn.disabled = false;
    source.onopen = () => { note.textContent = "En vivo: " + name; };
    source.onmessage = (ev) => append(ev.data);
    source.addEventListener("aviso", (ev) => append("· " + ev.data));
    source.addEventListener("cerrado", (ev) => stop(ev.data));
    source.onerror = async () => {
      stop("Se cortó la conexión con el registro.");
      const data = await loadList();
      if (my !== gen || !data) return;
      note.textContent = data.archivos.some((f) => f.archivo === name)
        ? "Se cortó la conexión con el registro: pulsa Ver en vivo para seguir."
        : "Ese registro ya no existe: elige otro de la lista.";
    };
  });
  stopBtn.addEventListener("click", () => stop("Detenido."));
  clearBtn.addEventListener("click", () => clear());
  refresh.addEventListener("click", () => loadList());
  keep(my, () => stop(null));
  loadList();
  return card("Registro en vivo",
    fieldHelp(LOG_HELP),
    el("div", { class: "kit-edit" }, select, refresh, openBtn, stopBtn, clearBtn),
    note,
    view);
}

// --- the cuarto's password -------------------------------------------------------------------

function passwordCard() {
  const pw = newPassword({ repeat: true });
  const change = el("button", { class: "primary", type: "button", text: "Cambiar la contraseña del cuarto" });
  const error = el("div", { class: "kit-error" });
  change.addEventListener("click", () => busy(change, async () => {
    const my = gen;
    error.textContent = "";
    try {
      const password = pw.read();
      const first = await roomApi("/api/admin/machine/password", { method: "POST", json: { password: password } });
      pw.clear();
      if (my !== gen || !(await confirmDialog(first)) || my !== gen) return;
      const result = (await roomApi("/api/admin/confirm", { method: "POST", json: { pending_id: first.pending_id } })).result;
      toast(result.otras_sesiones
        ? "contraseña del cuarto cambiada: se cerró en " + result.otras_sesiones + " sesión(es) más"
        : "contraseña del cuarto cambiada", "ok");
    } catch (err) {
      if (err.closed || my !== gen) return;
      error.textContent = say(err);
    }
  }));
  return card("La contraseña del cuarto",
    el("p", { class: "muted", text: "Cambiarla cierra el cuarto en cualquier otra sesión que lo tenga abierto; esta lo conserva." }),
    fieldHelp(NEW_PASS_HELP),
    cuartoName(),
    pw.node,
    el("div", { class: "kit-edit", style: "margin-top: 8px;" }, change),
    error);
}

await start();
