// LPQ_VISION captura.js: the phone capture page (T-C1, sealed contract of Arch section 3).
// The guard lives HERE, in the browser: a tiny gray copy of the video is sampled ten times a
// second; an abrupt change is an arrival, calm frames confirm the landing, a burst of full
// frames goes to /api/upload_burst and the SERVER keeps the sharpest. Direction comes from the
// entry edge of the first movement, mapped through the calibrated kitchen edge. A function
// that is OFF is discarded right here: no request leaves the phone, nothing is stored or billed.
// Copies for the mirilla go to /api/camera/frame and live in the api's RAM only.
//
// Section 3.13.2 (owner, 11 and 12 Oct): phones only, in the phone's browser (no Home-Screen app),
// out of the tabs. A phone arrives only through its camera's link (/captura#CODE: the code sits after
// the #, which never reaches a server; the page hands it over in the X-Enlace header). It gives a name,
// waits "⏳ Pendiente" for the admin's Aceptar, and then captures THAT camera. Its own pass (an
// httponly cookie the page never reads) opens the capture lane; /api/captura/yo says what to show,
// and the page asks again after any refusal of the lane (the server tells why: Sacar, back to its
// Pi, the camera removed, the site deactivated). The same link takes it straight back every day (the
// pass counts, not the code). "Sí, salir" stops on "⏹ Captura detenida" with "▶ Volver a capturar
// <cámara>", and a stop holds (a reload, a site deactivated and back) until a start button, a request
// or another link. The screen stays awake while capturing (Screen Wake Lock, asked with a tap, since a
// phone's browser wants one). One tab captures: the newest one; the others stop and offer
// "▶ Capturar aquí". The live knobs ride the push reply (Section 3.12): the guard knobs, the push
// cadence, the site's two functions.
// Section 3.13: the guard knobs and the burst size are THIS site's own values, and a burst that is
// all black comes back refused with black: true (counted as discarded, never as an error).
// This file never uses the dollar sign (the build's transport rule): strings are joined with +.
import { el, toast } from "/static/app.js";

// --- named constants -------------------------------------------------------------------
const SAMPLE_W = 64;            // the guard's frame: 64x48 gray pixels is plenty for a landing
const SAMPLE_H = 48;
const SAMPLE_MS = 100;          // ~10 samples per second
const PIXEL_DELTA = 28;         // a pixel "changed" when its gray value moved more than this (0-255)
const BURST_GAP_MS = 150;       // spacing between the frames of a burst
const EDGE_BAND = 0.3;          // the outer 30% of each side counts as that edge
const EDGE_MARGIN = 1.5;        // the winning edge must beat the runner-up by this factor, else dudosa
const MOTION_FRAMES_FOR_EDGE = 2; // the first movement frames decide the entry edge
// The push cadence is the knob capture.frame_poll_s (Perillas): /api/captura/yo and every push reply
// carry it as frame_poll_s, and the page never writes a number of its own. One speed, watched or not.
let pushMs = 0;                 // set from the camera's stage before any capture starts
const PUSH_ALIGN_MS = 2000;     // every 2 s during the alignment mode (live sharpness)
const PUSH_WIDTH = 640;         // the copy's width; the api scores it on the same scale as bursts
const PUSH_WATCHDOG_MS = 45000; // no copy accepted for this long while running = the push loop is stuck: restart it
const WATCHDOG_MS = 5000;       // the watchdog's own tick
const FULL_QUALITY = 0.92;
const PUSH_QUALITY = 0.7;
const LOG_MAX = 20;
const EXIT_REVEAL_AT = 90;      // the slider must pass this to reveal "Salir"; a tap alone does nothing
const EXIT_AUTO_CANCEL_S = 10;  // the revealed button and the confirm popup both give up after this
const PENDING_POLL_MS = 3000;   // "⏳ Pendiente" asks the server this often whether the admin answered
const SLOW_POLL_MS = 15000;     // a stopped capture and a deactivated site ask this often whether something changed
const RETRY_MS = 5000;          // "Sin conexión con la ventanilla" tries again by itself this often
const PLAY_TIMEOUT_MS = 10000;  // a camera whose picture never starts playing is given up after this
const PONG_WAIT_MS = 1500;      // how long a stopped tab waits for the capturing tab to answer
const NOMBRE_MAX = 30;          // the server's NOMBRE_TELEFONO_MAX: the page saves a round trip
const PHONE_UA = /iPhone|iPod|Android.*Mobile/;   // phones only: a computer or a tablet is never a camera
const IPHONE_UA = /iPhone|iPod/;                  // which settings path the camera-permission hint names
const ENLACE_HEADER = "X-Enlace";                 // the link's code travels here, never in an address a server sees
const ASCII_MIN = 32;           // a header carries plain printable ASCII only: anything else is a bad code
const ASCII_MAX = 126;
const TAB_CHANNEL = "lpq-captura";                // the tabs of this browser tell each other who captures
const TAB_ID = Date.now().toString(36) + Math.random().toString(36).slice(2);
const STOP_KEY = "lpq.captura.detenida";          // this tab's "Sí, salir" outlives a reload (sessionStorage: this tab only)
const OPPOSITE = { top: "bottom", bottom: "top", left: "right", right: "left" };
const EDGE_ES = { top: "arriba", bottom: "abajo", left: "izquierda", right: "derecha" };
const ROLE_ES = { return: "📥 regresan (merma)", outgoing: "📤 salen (emplatado)", both: "🔀 ambos sentidos" };

// The code after the # of the address (the link's), as typed: the server reads it, the page only carries it.
function codeInAddress() {
  const raw = location.hash.slice(1);
  try { return decodeURIComponent(raw).trim(); } catch (err) { return raw.trim(); }
}

// Stopped by the person ("Sí, salir", or another tab took the capture): nothing starts by itself until a start
// button, a request or a newly opened link. Kept for this tab across a reload; a link in the address starts fresh.
function stoppedAtLoad() {
  try { return !codeInAddress() && sessionStorage.getItem(STOP_KEY) === "1"; } catch (err) { return false; }
}
function setStopped(on) {
  S.stopped = on;
  try { if (on) sessionStorage.setItem(STOP_KEY, "1"); else sessionStorage.removeItem(STOP_KEY); } catch (err) { /* this tab's RAM only */ }
}

const S = {
  code: codeInAddress(),
  stopped: stoppedAtLoad(),
  site: "", camera: "", nombre: "",
  knobs: null, funciones: null, cam: null,
  stream: null, running: false, paused: false, busy: false, starting: false,
  prev: null, baseline: null, lastCaptured: null, occupied: false,
  inMotion: false, motionFrames: 0, calmFrames: 0, entryCounts: null,
  counts: { captured: 0, discarded: 0 },
  watching: false, align: { active: false }, countdownTimer: null, secondsLeft: 0,
  lastPushAt: 0, pushGen: 0, sampleTimer: null, watchTimer: null,
  waitGen: 0, wake: null, wakeAsking: false,
};
if (S.code) setStopped(false);  // a link opened: a fresh start, whatever this tab did before
let exitReset = () => {};       // the exit control's reset, set once the control is wired (a stop puts it back)
const tabs = "BroadcastChannel" in window ? new BroadcastChannel(TAB_CHANNEL) : null;

const byId = (id) => document.getElementById(id);
const video = byId("video");
const sampleCanvas = byId("sample");
const fullCanvas = byId("full");
const pushCanvas = byId("push");
sampleCanvas.width = SAMPLE_W;
sampleCanvas.height = SAMPLE_H;
const sctx = sampleCanvas.getContext("2d", { willReadFrequently: true });
const EMPTY_SHARP = byId("last-sharp").textContent;   // the tiles' own "nothing yet" mark, for every restart
const EMPTY_PUSH = byId("last-push").textContent;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function log(text) {
  const ul = byId("log");
  ul.prepend(el("li", { text: new Date().toLocaleTimeString("es-MX") + " · " + text }));
  while (ul.children.length > LOG_MAX) ul.lastChild.remove();
}
function status(text) { byId("status").textContent = text; }
function chip(id, text, kind) { const c = byId(id); c.textContent = text; c.className = "chip " + (kind || ""); }
// replaceChildren would print an absent part as the word "null": fill() leaves it out.
function fill(node, ...kids) { node.replaceChildren(...kids.filter((k) => k)); }

// --- the screens: one card before and after capturing, the stage while capturing ------------
function showScreen(...kids) {
  S.waitGen++;                      // a pending poll that is still waiting abandons itself
  byId("stage").classList.add("hidden");
  byId("exit").classList.add("hidden");
  byId("role-chip").classList.add("hidden");   // the role is the stage's (paintRole shows it there)
  fill(byId("screen"), ...kids);
  byId("screen").classList.remove("hidden");
}

function showStage() {
  S.waitGen++;                      // capturing again while a request waits: the lane's answer leads from here
  byId("screen").classList.add("hidden");
  byId("stage").classList.remove("hidden");
  byId("exit").classList.remove("hidden");
}

function camChip() {
  if (!S.camera) { byId("cam-chip").classList.add("hidden"); return; }
  chip("cam-chip", S.camera + (S.nombre ? " · " + S.nombre : ""), "");
  byId("cam-chip").classList.remove("hidden");
}

function bigButton(text, onclick, cls) {
  return el("button", { type: "button", class: "big " + (cls || "primary"), text: text, onclick: onclick });
}

// The code leaves the address once the pass carries the state: a reload never asks again by itself.
function dropCode() {
  S.code = "";
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
}

// --- the server's one question: what is this phone now, and what does its link offer --------
const headerSafe = (text) => [...text].every((ch) => ch.charCodeAt(0) >= ASCII_MIN && ch.charCodeAt(0) <= ASCII_MAX);

async function askYo() {
  const headers = {};
  if (S.code) headers[ENLACE_HEADER] = headerSafe(S.code) ? S.code : "?";
  const res = await fetch("/api/captura/yo", { credentials: "same-origin", cache: "no-store", headers: headers });
  let data = {};
  try { data = await res.json(); } catch (err) { /* the error line below says it */ }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

const sameCamera = (a, b) => Boolean(a && b && a.site === b.site && a.camera === b.camera);

// A slow question in a loop, for a screen that waits: it stops as soon as another screen shows (waitGen),
// and decide(now) answers true to keep waiting.
async function keepAsking(ms, decide) {
  const gen = S.waitGen;
  while (gen === S.waitGen) {
    await sleep(ms);
    if (gen !== S.waitGen) return;
    let now;
    try { now = await askYo(); } catch (err) { continue; }   // a hiccup: the next round asks again
    if (gen !== S.waitGen) return;
    if (!decide(now)) return;
  }
}

// Ask, then show what the answer says. note: the lane's own words when it refused (the server's memo
// says the same when it still has it). With no answer the page keeps trying by itself (a tunnel waking up).
async function reconcile(note) {
  let yo;
  try {
    yo = await askYo();
  } catch (err) {
    showScreen(
      el("h2", { text: "Sin conexión con la ventanilla" }),
      el("p", { class: "muted", text: err.message + ". ¿Tailscale está encendido en este teléfono? La página vuelve a intentar sola." }),
      bigButton("Reintentar", () => reconcile(note)));
    keepAsking(RETRY_MS, (now) => { route(now, note); return false; });
    return;
  }
  route(yo, note);
}

// A start button: the person asks to capture (again), so a stop of theirs no longer holds.
function resume() {
  setStopped(false);
  dropCode();
  reconcile();
}

// The way back to the phone's own camera from another screen (a request waiting elsewhere included): the
// server is asked again first, so the button never starts a camera the phone no longer has.
function backTo(yo) {
  if (!yo.camara || !yo.stage) return null;
  return bigButton("▶ Volver a capturar " + yo.camara.camera, resume);
}

function codeForm() {
  const input = el("input", {
    class: "mono", placeholder: "7KQ2-M9XP", maxlength: "12",
    autocomplete: "off", autocapitalize: "characters", autocorrect: "off", spellcheck: "false",
  });
  const form = el("form", {}, el("label", { text: "¿Tienes el código de la cámara? Escríbelo aquí:" }), input,
    el("button", { type: "submit", class: "primary", text: "Seguir" }));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!input.value.trim()) return;
    setStopped(false);                                 // a code typed is a link opened: a fresh start
    S.code = input.value.trim();
    reconcile();
  });
  return form;
}

// Outside the stage the header names the phone's own camera, if it still has one, its site asleep or not (the role
// chip is the stage's).
function headerFor(yo) {
  const own = yo.camara || yo.dormida || null;
  S.site = own ? own.site : "";
  S.camera = own ? own.camera : "";
  S.nombre = yo.nombre || "";
  camChip();
}

function route(yo, note) {
  const aviso = yo.aviso || note || "";
  const link = yo.enlace;
  if (S.running && !(yo.paso === "capturar" && sameCamera(yo.camara, { site: S.site, camera: S.camera }))) stopCapture();
  if (yo.paso !== "capturar" || S.stopped) headerFor(yo);
  switch (yo.paso) {
    case "capturar":
      dropCode();
      if (S.stopped) showStopped();                   // stopped by the person: never starts by itself
      else beginCapture(yo, aviso);
      return;
    case "pendiente":
      dropCode();
      if (S.stopped && yo.dormida) showStopped();       // stopped, and its camera only sleeps: the stop still holds
      else showPending(yo, yo.pide);
      return;
    case "pedir":
      showPedir(yo, aviso);
      return;
    case "sitio_inactivo":
      if (S.stopped && yo.dormida) showStopped();       // stopped, and its camera only sleeps: the stop still holds
      else showInactive(yo, yo.aviso || "");          // the lane's own words would only repeat the title
      return;
    case "llena":
      showScreen(
        el("h2", { text: link.camera + " ya tiene teléfono" }),
        el("p", { text: "Cada cámara tiene un solo teléfono. Si este teléfono debe ser esa cámara, pide a tu gerente que saque al otro desde el mostrador." }),
        el("p", { class: "muted", text: "Si este teléfono ya es esa cámara, abre el enlace en el mismo navegador donde entraste la primera vez: cada navegador guarda su propio pase." }),
        backTo(yo));
      return;
    case "pi_viva":
      showScreen(
        el("h2", { text: "La Pi de " + link.camera + " está mandando imagen" }),
        el("p", { text: "No hace falta un teléfono. Si la Pi deja de mandar imagen, vuelve a abrir este enlace." }),
        backTo(yo));
      return;
    case "pedidos_llenos":
      showScreen(
        el("h2", { text: link.camera + " ya tiene teléfonos esperando" }),
        el("p", { text: "Espera a que el admin los atienda en el mostrador y vuelve a abrir el enlace." }),
        backTo(yo));
      return;
    case "vencido":
      showScreen(
        el("h2", { text: "Ese enlace ya venció" }),
        el("p", { text: "Pide a tu gerente el enlace nuevo de la cámara." }),
        aviso ? el("p", { class: "warnline", text: aviso }) : null,
        codeForm(),
        backTo(yo));
      return;
    case "desconocido":
      showScreen(
        el("h2", { text: "Ese enlace no sirve" }),
        el("p", { text: "Pide a tu gerente el enlace de tu cámara." }),
        aviso ? el("p", { class: "warnline", text: aviso }) : null,
        codeForm());
      return;
    default:
      dropCode();
      showScreen(
        el("h2", { text: "Abre el enlace de tu cámara" }),
        el("p", { text: "Te lo da tu gerente o el admin desde el mostrador (Sitios y cuentas, «Dame el enlace» o «Ver enlace»)." }),
        aviso ? el("p", { class: "warnline", text: aviso }) : null,
        codeForm());
  }
}

// --- joining: a name, then "⏳ Pendiente" until the admin answers ---------------------------
function showPedir(yo, aviso) {
  const link = yo.enlace;
  const name = el("input", { maxlength: String(NOMBRE_MAX), autocomplete: "off", placeholder: "Juan", value: yo.nombre || "" });
  const error = el("div", { class: "errline" });
  const go = el("button", { type: "submit", class: "primary big", text: "Pedir entrar" });
  const form = el("form", {}, el("label", { text: "Tu nombre (así te ve el admin para aceptarte)" }), name, error, go);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    const nombre = name.value.trim();
    if (!nombre) { error.textContent = "Escribe tu nombre: así te reconoce el admin."; return; }
    go.disabled = true;
    try {
      const res = await fetch("/api/captura/pedir", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enlace: S.code, nombre: nombre }),
      });
      let data = {};
      try { data = await res.json(); } catch (err) { /* below */ }
      if (!res.ok) { error.textContent = data.error || ("HTTP " + res.status); return; }
      setStopped(false);                               // asking to be a camera is asking to capture it
      dropCode();
      if (data.paso === "pendiente") {
        // The request is THIS camera's: the wait is for it, whatever the phone captures meanwhile.
        const asked = { site: link.site, camera: link.camera };
        showPending({ nombre: nombre, camara: yo.camara, stage: yo.stage, pide: asked, aviso: null }, asked);
      } else {
        reconcile();
      }
    } catch (err) {
      error.textContent = "sin conexión con la ventanilla";
    } finally {
      go.disabled = false;
    }
  });
  showScreen(
    el("h2", { text: "Cámara " + link.camera + " · " + link.site }),
    link.pi ? el("p", { class: "warnline", text: "La Pi de esta cámara no manda imagen: este teléfono puede tomar su lugar." }) : null,
    yo.camara ? el("p", { class: "warnline", text: "Este teléfono es la cámara " + yo.camara.camera + ": cuando te acepten aquí, esa cámara queda libre." }) : null,
    yo.pide ? el("p", { class: "warnline", text: "Ya pediste entrar como " + yo.pide.camera + ": esto cambia tu pedido." }) : null,
    aviso ? el("p", { class: "warnline", text: aviso }) : null,
    form,
    backTo(yo));                                       // a link opened by mistake: back to its own camera
  name.focus();
}

// "⏳ Pendiente" waits for ONE camera, the one asked for (asked). The phone may still capture another one
// meanwhile (its own, which the answer names first): that never counts as the admin's answer. Accepted: the
// page captures the asked camera by itself (it says so, so a stop of the person's no longer holds here).
// Refused, or the camera went to another phone: it says so, with the way back to the phone's own camera, and
// starts nothing. A deactivated site is said: the asked camera's (the request waits for it) or the phone's own;
// when the page came here because its own camera's site went to sleep under a capture, it captures again when
// that site wakes (the request still rides along).
function showPending(yo, asked) {
  setStopped(false);
  const resumeOwn = yo.dormida || null;
  const note = el("p", { class: "warnline hidden" });
  const asleep = el("p", { class: "warnline hidden" });
  const still = el("p", { class: "muted hidden" });
  const back = el("div", {});
  let backFor = null;                                  // the camera the button names: rebuilt only when it changes
  const paint = (now) => {
    headerFor(now);
    note.textContent = now.aviso || "";
    note.classList.toggle("hidden", !now.aviso);
    const sleeping = [];
    if (now.pide && now.pide.activo === false) {
      sleeping.push("El sitio " + asked.site + " está desactivado: tu pedido espera a que lo activen.");
    }
    if (now.dormida) {
      sleeping.push("El sitio " + now.dormida.site + " está desactivado: "
        + (sameCamera(now.dormida, resumeOwn) ? "cuando lo activen, esta página vuelve a capturar " + now.dormida.camera + "."
          : "la cámara " + now.dormida.camera + " espera a que lo activen."));
    }
    asleep.textContent = sleeping.join(" ");
    asleep.classList.toggle("hidden", !sleeping.length);
    still.textContent = now.camara ? "Mientras, este teléfono sigue siendo la cámara " + now.camara.camera + "." : "";
    still.classList.toggle("hidden", !now.camara);
    const key = now.camara && now.stage ? now.camara.site + "/" + now.camara.camera : "";
    if (key !== backFor) { backFor = key; fill(back, backTo(now)); }   // a tap under way is never cut by a poll
  };
  showScreen(
    el("div", { class: "waiting", text: "⏳" }),
    el("h2", { text: "Pendiente" }),
    el("p", { text: "Espera a que el admin te acepte como la cámara " + asked.camera + " de " + asked.site + " («" + (yo.nombre || "") + "»). Esta página sigue sola en cuanto te acepte." }),
    note, asleep, still, back);
  paint(yo);
  keepAsking(PENDING_POLL_MS, (now) => {
    if (sameCamera(now.camara, asked)) { route(now); return false; }          // accepted: capture it
    if (resumeOwn && sameCamera(now.camara, resumeOwn)) { route(now); return false; }   // its site woke: capture again
    if (sameCamera(now.pide, asked)) { paint(now); return true; }             // still waiting for it (asleep or not)
    if (now.pide || now.paso === "sitio_inactivo") { route(now); return false; }   // another request, or a site asleep
    // The wait ended without this camera: Rechazar says so in its own words; any other end (the camera went to
    // another phone, it was removed, a restart forgot why) says the request ended, with the reason it still has.
    const refused = (now.aviso || "").startsWith("No te aceptaron");
    headerFor(now);
    showScreen(
      el("h2", { text: refused ? "No te aceptaron en " + asked.camera : "Tu pedido para " + asked.camera + " terminó" }),
      now.aviso && !refused ? el("p", { class: "warnline", text: now.aviso }) : null,
      el("p", { text: refused ? "Si fue un error, abre el enlace otra vez." : "Si este teléfono debe ser esa cámara, habla con tu gerente." }),
      backTo(now));
    return false;
  });
}

// A deactivated site (the link's, or the phone's own): the page waits by itself and goes on when it is active
// again (the link's code stays, so the same question gets the new answer; a capture the person had stopped comes
// back stopped).
function showInactive(yo, aviso) {
  showScreen(
    el("h2", { text: "El sitio " + yo.inactivo + " está desactivado" }),
    el("p", { text: "Cuando lo activen, esta página sigue sola." }),
    aviso ? el("p", { class: "warnline", text: aviso }) : null,
    backTo(yo));
  keepAsking(SLOW_POLL_MS, (now) => {
    if (now.paso === "sitio_inactivo" && now.inactivo === yo.inactivo) return true;
    route(now);
    return false;
  });
}

// --- state from the server ------------------------------------------------------------------
function applyStage(stage) {
  S.knobs = stage.capture;
  S.funciones = stage.funciones;
  S.cam = { role: stage.role, source: stage.source, kitchen_edge: stage.kitchen_edge || null };
  pushMs = stage.frame_poll_s * 1000;                       // the ONE cadence, from its one home (the knob)
}

function knobsText() {
  const k = S.knobs || {};
  return "cambio " + k.diff_threshold + " % · calma " + k.settle_frames + " cuadros · ráfaga de " + k.burst_frames
    + " · copia cada " + Math.round(pushMs / 1000) + " s";
}
function paintKnobs() { byId("knobs").textContent = "perillas en vivo: " + knobsText(); }

function paintRole() {
  const cam = S.cam;
  const arrows = { top: byId("arrow-top"), bottom: byId("arrow-bottom"), left: byId("arrow-left"), right: byId("arrow-right") };
  for (const a of Object.values(arrows)) { a.className = "arrow hidden"; a.textContent = ""; }
  byId("role-chip").classList.remove("hidden");      // the role is the stage's: the screens hide it
  const sign = byId("rolesign");
  if (!cam) { chip("role-chip", "cámara desconocida", "bad"); sign.textContent = "cámara no configurada"; return; }
  const f = S.funciones || {};
  if (cam.role === "return") {
    chip("role-chip", ROLE_ES.return, f.merma ? "ok" : "bad");
    sign.textContent = f.merma ? "📥 PLATOS QUE REGRESAN" : "⚠ función MERMA apagada: todo se descarta";
    return;
  }
  if (cam.role === "outgoing") {
    chip("role-chip", ROLE_ES.outgoing, f.presentacion ? "ok" : "bad");
    sign.textContent = f.presentacion ? "📤 PLATOS QUE SALEN" : "⚠ función PRESENTACIÓN apagada: todo se descarta";
    return;
  }
  // role both: the arrows, always visible once calibrated
  chip("role-chip", ROLE_ES.both, cam.kitchen_edge ? "ok" : "warn");
  if (!cam.kitchen_edge) {
    sign.textContent = "⚠ sin calibrar: el gerente la calibra en Cámara (modo alineación). Mientras, todo va a revisión";
    return;
  }
  const k = cam.kitchen_edge, r = OPPOSITE[k];
  arrows[k].className = "arrow " + k + " " + (f.presentacion ? "kitchen" : "off");
  arrows[k].textContent = f.presentacion ? "🟠 COCINA → salen" : "COCINA (presentación apagada: se descarta)";
  arrows[r].className = "arrow " + r + " " + (f.merma ? "room" : "off");
  arrows[r].textContent = f.merma ? "🟢 RESTAURANTE → regresan" : "RESTAURANTE (merma apagada: se descarta)";
  sign.textContent = "cocina: " + EDGE_ES[k] + " · restaurante: " + EDGE_ES[r];
}

// --- the guard ---------------------------------------------------------------------------
function grayFrame() {
  sctx.drawImage(video, 0, 0, SAMPLE_W, SAMPLE_H);
  const d = sctx.getImageData(0, 0, SAMPLE_W, SAMPLE_H).data;
  const g = new Uint8Array(SAMPLE_W * SAMPLE_H);
  for (let i = 0, j = 0; i < d.length; i += 4, j++) g[j] = (d[i] * 77 + d[i + 1] * 151 + d[i + 2] * 28) >> 8;
  return g;
}

// % of pixels that moved more than PIXEL_DELTA, plus the mask of which ones (for the edge count).
function diff(a, b) {
  const mask = new Uint8Array(a.length);
  let n = 0;
  for (let i = 0; i < a.length; i++) { if (Math.abs(a[i] - b[i]) > PIXEL_DELTA) { mask[i] = 1; n++; } }
  return { pct: (100 * n) / a.length, mask };
}

function edgeCounts(mask) {
  const out = { top: 0, bottom: 0, left: 0, right: 0 };
  const bx = Math.round(SAMPLE_W * EDGE_BAND), by = Math.round(SAMPLE_H * EDGE_BAND);
  for (let y = 0; y < SAMPLE_H; y++) for (let x = 0; x < SAMPLE_W; x++) {
    if (!mask[y * SAMPLE_W + x]) continue;
    if (y < by) out.top++;
    if (y >= SAMPLE_H - by) out.bottom++;
    if (x < bx) out.left++;
    if (x >= SAMPLE_W - bx) out.right++;
  }
  return out;
}

function sample() {
  if (!S.running || S.paused || S.busy || video.readyState < 2 || !trackLive()) return;
  const g = grayFrame();
  if (!S.prev) { S.prev = g; S.baseline = g; return; }   // the first frame is the empty zone
  const { pct, mask } = diff(g, S.prev);
  S.prev = g;
  const T = S.knobs.diff_threshold;
  if (pct >= T) {
    if (!S.inMotion) { S.inMotion = true; S.motionFrames = 0; S.entryCounts = { top: 0, bottom: 0, left: 0, right: 0 }; }
    if (S.motionFrames < MOTION_FRAMES_FOR_EDGE) {
      const c = edgeCounts(mask);
      for (const k of Object.keys(c)) S.entryCounts[k] += c[k];
    }
    S.motionFrames++;
    S.calmFrames = 0;
    status("movimiento… (" + pct.toFixed(0) + " %)");
    return;
  }
  if (!S.inMotion) return;
  S.calmFrames++;
  if (S.calmFrames >= S.knobs.settle_frames) { S.inMotion = false; landed(g); }
}

function landed(g) {
  const T = S.knobs.diff_threshold;
  if (diff(g, S.baseline).pct < T) {                    // the zone is empty again
    S.occupied = false; S.baseline = g; status("zona vacía · esperando plato…"); return;
  }
  if (S.occupied && S.lastCaptured && diff(g, S.lastCaptured).pct < T) {
    status("mismo plato, sin cambios"); return;         // a nudge of the same plate is not a new plate
  }
  S.occupied = true;
  S.lastCaptured = g;
  burst();
}

// --- direction + the gatekeeper ---------------------------------------------------------
function decideDirection() {
  const cam = S.cam, f = S.funciones;
  if (cam.role === "return") return { record_type: "return", dudosa: false, why: "cámara de regreso" };
  if (cam.role === "outgoing") return { record_type: "outgoing", dudosa: false, why: "cámara de salida" };
  const doubt = (why) => ({ record_type: f.merma ? "return" : "outgoing", dudosa: true, why });
  if (!cam.kitchen_edge) return doubt("cámara sin calibrar");
  const c = S.entryCounts || { top: 0, bottom: 0, left: 0, right: 0 };
  const sorted = Object.entries(c).sort((a, b) => b[1] - a[1]);
  const edge = sorted[0][0], best = sorted[0][1], second = sorted[1][1];
  if (best === 0 || best < EDGE_MARGIN * second) return doubt("entrada ambigua");
  if (edge === cam.kitchen_edge) return { record_type: "outgoing", dudosa: false, why: "entró por " + EDGE_ES[edge] + " (cocina)" };
  if (edge === OPPOSITE[cam.kitchen_edge]) return { record_type: "return", dudosa: false, why: "entró por " + EDGE_ES[edge] + " (restaurante)" };
  return doubt("entró por un costado (" + EDGE_ES[edge] + ")");
}

function functionOn(record_type) {
  return record_type === "outgoing" ? Boolean(S.funciones.presentacion) : Boolean(S.funciones.merma);
}

function grab(canvas, width, quality) {
  const w = Math.min(width, video.videoWidth), h = Math.round(video.videoHeight * (w / video.videoWidth));
  canvas.width = w; canvas.height = h;
  canvas.getContext("2d").drawImage(video, 0, 0, w, h);
  // toDataURL is SYNCHRONOUS. toBlob encodes on idle tasks, which a hidden tab can starve forever,
  // and an await that never resolves silently kills the push loop. Same JPEG, no scheduling.
  const bin = atob(canvas.toDataURL("image/jpeg", quality).split(",")[1]);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return Promise.resolve(new Blob([bytes], { type: "image/jpeg" }));
}

// --- the capture lane: the pass rides as a cookie by itself ------------------------------
function lane(path, body) {
  return fetch(path, { method: "POST", body: body, credentials: "same-origin", cache: "no-store" });
}
async function bodyOf(res) {
  try { return await res.json(); } catch (err) { return {}; }
}
function refused(res) { return res.status === 401 || res.status === 403; }

// The lane refused this phone (Sacar, back to its Pi, the camera removed, another camera accepted):
// stop and ask the server what the phone is now; its words are the note.
function lostCamera(message) {
  stopCapture();
  reconcile(message);
}

async function burst() {
  S.busy = true;
  try {
    status("📸 ráfaga…");
    const frames = [];
    for (let i = 0; i < S.knobs.burst_frames; i++) {
      frames.push(await grab(fullCanvas, video.videoWidth, FULL_QUALITY));
      if (i < S.knobs.burst_frames - 1) await sleep(BURST_GAP_MS);
    }
    const d = decideDirection();
    const es = d.record_type === "outgoing" ? "sale" : "regresa";
    if (!functionOn(d.record_type)) {
      S.counts.discarded++;
      byId("n-discarded").textContent = S.counts.discarded;
      status("descartado: " + es + ", función apagada");
      log("descartado (" + es + ", " + d.why + "): función apagada, no se envió nada");
      return;
    }
    const form = new FormData();
    frames.forEach((b, i) => form.append("photos", b, "frame" + i + ".jpg"));
    form.append("site", S.site);
    form.append("camera", S.camera);
    form.append("record_type", d.record_type);
    if (d.dudosa) form.append("direction_dudosa", "true");
    status("subiendo " + frames.length + " cuadros…");
    const res = await lane("/api/upload_burst", form);
    const data = await bodyOf(res);
    if (!S.running) return;                                   // stopped while the burst traveled
    if (refused(res)) { lostCamera(data.error || ("HTTP " + res.status)); return; }
    if (res.status === 400 && data.black) {
      // Section 3.13: the door refuses a burst that is all black (a covered lens): nothing stored, nothing billed.
      S.counts.discarded++;
      byId("n-discarded").textContent = S.counts.discarded;
      status("descartado: imagen negra (¿algo tapa la cámara?)");
      log("descartado (" + es + "): la ventanilla rechazó una ráfaga negra, no se guardó nada");
      return;
    }
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    S.counts.captured++;
    byId("n-captured").textContent = S.counts.captured;
    byId("last-sharp").textContent = data.sharpness;
    status("✔ plato #" + data.plate_id + " · " + es + " · nitidez " + data.sharpness
      + (d.dudosa ? " · dirección dudosa (a revisión)" : "") + (data.duplicate ? " · repetido" : ""));
    log("#" + data.plate_id + " " + es + " · nitidez " + data.sharpness + " · " + d.why + (d.dudosa ? " · DUDOSA" : ""));
  } catch (err) {
    status("✘ error: " + err.message);
    log("error al subir: " + err.message);
    toast(err.message, "bad");
  } finally {
    S.busy = false;
  }
}

// --- the copies for the mirilla + the pause --------------------------------------------
function applyAlign(align) {
  const wasActive = S.align.active;
  S.align = align || { active: false };
  if (S.align.active && !S.paused) {
    S.paused = true;
    byId("banner").classList.remove("hidden");
    chip("conn-chip", "en pausa (alineación)", "warn");
    S.secondsLeft = S.align.seconds_left || 0;
    clearInterval(S.countdownTimer);
    S.countdownTimer = setInterval(() => {
      S.secondsLeft = Math.max(0, S.secondsLeft - 1);
      const m = String(Math.floor(S.secondsLeft / 60)).padStart(2, "0"), s = String(S.secondsLeft % 60).padStart(2, "0");
      byId("countdown").textContent = S.secondsLeft > 0 ? m + ":" + s : "reanudando…";
    }, 1000);
    log("captura en pausa: modo alineación");
  } else if (S.align.active && S.paused) {
    S.secondsLeft = S.align.seconds_left || S.secondsLeft;  // resync the countdown with the server
  } else if (!S.align.active && S.paused) {
    S.paused = false;
    clearInterval(S.countdownTimer);
    byId("banner").classList.add("hidden");
    chip("conn-chip", "capturando", "ok");
    S.prev = null; S.baseline = null; S.occupied = false; S.inMotion = false;   // fresh eyes after a pause
    log("captura reanudada");
    if (wasActive) refreshStage();                          // the alignment may have calibrated the edge
  }
}

async function refreshStage() {
  try {
    const yo = await askYo();
    if (yo.paso === "capturar" && yo.stage && yo.camara.camera === S.camera) { applyStage(yo.stage); paintRole(); }
  } catch (err) { /* the next push reply keeps the knobs live anyway */ }
}

// The page knobs ride the push reply (Section 3.12): obey a change at once and say so in the log.
function applyLive(data) {
  const before = knobsText();
  if (data.capture) S.knobs = data.capture;
  if (data.frame_poll_s) pushMs = data.frame_poll_s * 1000;
  const after = knobsText();
  if (after !== before) { log("perillas nuevas: " + after); paintKnobs(); }
  if (data.funciones && JSON.stringify(data.funciones) !== JSON.stringify(S.funciones)) {
    S.funciones = data.funciones;
    log("funciones nuevas: merma " + (S.funciones.merma ? "encendida" : "apagada")
      + ", presentación " + (S.funciones.presentacion ? "encendida" : "apagada"));
    paintRole();
  }
}

// The loop carries a generation number: the watchdog can abandon a stuck generation and start a
// fresh one without waiting for an await that may never resolve; a stop abandons it the same way.
async function pushLoop(gen) {
  while (S.running && gen === S.pushGen) {
    try {
      if (video.readyState >= 2 && trackLive()) {          // a camera with no picture sends no copy
        const blob = await grab(pushCanvas, PUSH_WIDTH, PUSH_QUALITY);
        const form = new FormData();
        form.append("frame", blob, "frame.jpg");
        form.append("site", S.site);
        form.append("camera", S.camera);
        const res = await lane("/api/camera/frame", form);
        const data = await bodyOf(res);
        if (!S.running || gen !== S.pushGen) return;          // stopped while the copy traveled
        if (res.ok) {
          S.lastPushAt = Date.now();
          S.watching = Boolean(data.watching);
          applyLive(data);
          applyAlign(data.align);
          if (!S.paused && trackLive()) chip("conn-chip", S.watching ? "capturando · alguien mira" : "capturando", "ok");
        } else if (refused(res)) {
          lostCamera(data.error || ("HTTP " + res.status));
          return;
        } else if (res.status === 400 && /^unknown (site|camera)/.test(data.error || "")) {
          lostCamera("La ventanilla ya no conoce " + S.camera + ".");
          return;
        } else {
          chip("conn-chip", "ventanilla rechazó la copia", "bad");
        }
      }
    } catch (err) {
      chip("conn-chip", "sin conexión con la ventanilla", "bad");
    }
    await sleep(S.align.active ? PUSH_ALIGN_MS : pushMs);   // watching only changes the chip text
  }
}

// The watchdog: paints the age of the last accepted copy, and restarts the push loop when it
// falls silent (a hidden tab, a hung encoder, anything). Runs on its own timer, never awaits.
function watchdog() {
  if (!S.running) return;
  const age = S.lastPushAt ? Math.round((Date.now() - S.lastPushAt) / 1000) : null;
  byId("last-push").textContent = age === null ? EMPTY_PUSH : age + " s";
  if (age !== null && age * 1000 > PUSH_WATCHDOG_MS && trackLive()) {   // a muted camera is waited for, not restarted
    log("sin copias aceptadas desde hace " + age + " s: reiniciando el envío");
    chip("conn-chip", "envío reiniciado", "warn");
    S.lastPushAt = Date.now();      // one restart per watchdog window, not one per tick
    S.pushGen++;
    pushLoop(S.pushGen);
  }
}

// --- the screen stays awake while capturing (Screen Wake Lock) -------------------------------
// A phone's browser grants the lock only right after a tap (iPhone above all), and drops it whenever the page
// hides. So while no lock is held, the stage shows a big "☀️ Toca aquí" button; any tap on the stage asks too.
// Without the API, or when it refuses even right after a tap (a battery saver), a note says how to keep the
// screen on by hand.
const WAKE_NOTE_NO_API = "Este navegador no puede mantener la pantalla encendida: en los ajustes del teléfono, pon el "
  + "bloqueo automático de la pantalla en «Nunca» mientras captura.";
const WAKE_NOTE_REFUSED = "El teléfono no dejó mantener la pantalla encendida (¿modo de ahorro de batería?): quítalo y "
  + "toca ☀️ otra vez.";

function wakeUi(state) {
  byId("wake-btn").classList.toggle("hidden", !(S.running && (state === "off" || state === "refused")));
  const note = byId("wake-note");
  note.textContent = state === "refused" ? WAKE_NOTE_REFUSED : WAKE_NOTE_NO_API;
  note.classList.toggle("hidden", !(S.running && (state === "unsupported" || state === "refused")));
}

// tapped: asked from a tap (the ☀️ button, any tap on the page); a refusal then is the browser's own answer.
async function keepAwake(tapped) {
  if (!S.running) return;
  if (!("wakeLock" in navigator)) { wakeUi("unsupported"); return; }
  if (S.wake || S.wakeAsking) return;
  S.wakeAsking = true;
  try {
    const lock = await navigator.wakeLock.request("screen");
    if (!S.running) { lock.release().catch(() => {}); return; }
    S.wake = lock;
    lock.addEventListener("release", () => {
      if (S.wake !== lock) return;                 // our own release (a stop): nothing to show
      S.wake = null;
      wakeUi("off");
      if (S.running && document.visibilityState === "visible") log("la pantalla ya puede apagarse: toca ☀️ para que siga encendida");
    });
    wakeUi("on");
  } catch (err) {
    wakeUi(tapped ? "refused" : "off");
  } finally {
    S.wakeAsking = false;
  }
}

function releaseAwake() {
  const lock = S.wake;
  S.wake = null;
  if (lock) lock.release().catch(() => {});
  wakeUi("on");
}

// --- start and stop ------------------------------------------------------------------------
// note: why the page landed here (the lane's or the server's words), for the stage's log. A request for
// another camera that still waits rides along in the log too: the admin's Aceptar moves the phone there.
async function beginCapture(yo, note) {
  if (S.running || S.starting) {
    if (S.running && sameCamera(yo.camara, { site: S.site, camera: S.camera })) showStage();   // already on it: show it
    return;
  }
  S.site = yo.camara.site;
  S.camera = yo.camara.camera;
  S.nombre = yo.nombre || "";
  if (!yo.stage) { reconcile("La ventanilla ya no conoce " + S.camera + "."); return; }
  applyStage(yo.stage);
  camChip();
  await start();                                       // the role is painted once the stage shows
  if (!S.running) return;
  if (note) log(note);
  if (yo.pide && !sameCamera(yo.pide, yo.camara)) {
    log("⏳ pediste entrar como " + yo.pide.camera + ": cuando el admin te acepte, este teléfono pasa a esa cámara");
  }
}

// What went wrong with the camera, in the person's words: the browser's refusal names the permission (with this
// phone's own settings path), a camera another app holds says so, a page without https says that.
function troubleOf(err) {
  const name = (err && err.name) || "";
  if (!window.isSecureContext || !navigator.mediaDevices) {
    return ["La cámara solo abre con https.", "Abre el enlace con la dirección https del club."];
  }
  if (name === "NotAllowedError" || name === "SecurityError") {
    return ["El navegador no dio permiso para usar la cámara.", IPHONE_UA.test(navigator.userAgent || "")
      ? "En Safari: aA, Configuración del sitio web, Cámara, Permitir. Luego vuelve a intentar."
      : "En Chrome: el candado junto a la dirección, Permisos, Cámara, Permitir. Luego vuelve a intentar."];
  }
  if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError") {
    return ["La cámara está ocupada: otra app la está usando.", "Cierra esa app (o la llamada) y vuelve a intentar."];
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return ["Este teléfono no tiene una cámara que sirva aquí.", "Prueba con otro teléfono."];
  }
  return [(err && err.message) || String(err), "Vuelve a intentar; si sigue, reinicia el navegador."];
}

function cameraTrouble(text, hint) {
  showScreen(
    el("h2", { text: "No se pudo abrir la cámara" }),
    el("p", { class: "errline", text: text }),
    hint ? el("p", { class: "muted", text: hint }) : null,
    bigButton("▶ Abrir la cámara", resume));
}

// The camera's own state: "ended" = the browser closed it for good (another app took it, a permission was
// withdrawn); "mute" = no picture for now (a call, the camera app, the page in the background). The guard and
// the copies wait while it is muted; an ended camera stops the capture and says so.
function trackLive() {
  const track = S.stream ? S.stream.getVideoTracks()[0] : null;
  return Boolean(track && track.readyState === "live" && !track.muted);
}

function watchTrack(stream) {
  const track = stream.getVideoTracks()[0];
  if (!track) return;
  const mine = () => S.running && S.stream === stream;
  track.addEventListener("ended", () => {
    if (!mine()) return;
    stopCapture();
    cameraTrouble("La cámara se cerró: otra app la tomó o el teléfono la apagó.", "Cierra esa app y vuelve a intentar.");
  });
  track.addEventListener("mute", () => {
    if (!mine()) return;
    chip("conn-chip", "cámara sin imagen", "bad");
    status("la cámara no da imagen: ¿otra app la está usando?");
    log("la cámara dejó de dar imagen: la captura espera");
  });
  track.addEventListener("unmute", () => {
    if (!mine()) return;
    S.prev = null; S.baseline = null; S.occupied = false; S.inMotion = false;   // fresh eyes, like after a pause
    if (!S.paused) chip("conn-chip", "capturando", "ok");
    status("esperando plato…");
    log("la cámara volvió a dar imagen");
  });
}

// The picture starts playing on the shown stage (a hidden video may never start on an iPhone); one that never
// starts is given up after PLAY_TIMEOUT_MS with its own words.
function playOrTimeout() {
  let timer = null;
  const late = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error("La imagen de la cámara no arrancó.")), PLAY_TIMEOUT_MS);
  });
  return Promise.race([video.play(), late]).finally(() => clearTimeout(timer));   // the loser never fires
}

// Another screen shown while the camera opens (another link, a refusal) takes over: the start gives up quietly.
const SUPERSEDED = new Error("superseded");

async function start() {
  S.starting = true;
  let gen = S.waitGen;
  try {
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("este navegador no da la cámara aquí");
    }
    S.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
    if (gen !== S.waitGen) throw SUPERSEDED;
    video.srcObject = S.stream;
    showStage();
    gen = S.waitGen;
    await playOrTimeout();
    if (gen !== S.waitGen) throw SUPERSEDED;
  } catch (err) {
    S.starting = false;
    if (S.stream) { for (const t of S.stream.getTracks()) t.stop(); S.stream = null; }
    video.srcObject = null;
    if (err !== SUPERSEDED) cameraTrouble(...troubleOf(err));
    return;
  }
  S.starting = false;
  watchTrack(S.stream);
  paintRole();
  S.running = true;
  S.paused = false;
  S.align = { active: false };
  S.prev = null; S.baseline = null; S.occupied = false; S.inMotion = false; S.lastCaptured = null;
  S.counts = { captured: 0, discarded: 0 };
  byId("n-captured").textContent = "0";
  byId("n-discarded").textContent = "0";
  byId("last-sharp").textContent = EMPTY_SHARP;
  byId("last-push").textContent = EMPTY_PUSH;
  byId("log").replaceChildren();
  S.lastPushAt = 0;
  chip("conn-chip", "capturando", "ok");
  status("esperando plato…");
  paintKnobs();
  log("cámara " + S.camera + " de " + S.site + ": " + video.videoWidth + "x" + video.videoHeight);
  clearInterval(S.sampleTimer);
  clearInterval(S.watchTimer);
  S.sampleTimer = setInterval(sample, SAMPLE_MS);
  S.watchTimer = setInterval(watchdog, WATCHDOG_MS);
  S.pushGen++;
  pushLoop(S.pushGen);
  wakeUi("off");                    // the button shows until a lock is held: the request below may need a tap
  keepAwake(false);
  if (tabs) tabs.postMessage({ captura: TAB_ID });   // the other tabs of this browser stop: the newest one captures
}

function stopCapture() {
  S.running = false;
  S.pushGen++;                      // the push loop in flight abandons itself
  clearInterval(S.sampleTimer); S.sampleTimer = null;
  clearInterval(S.watchTimer); S.watchTimer = null;
  clearInterval(S.countdownTimer);
  S.paused = false;
  S.align = { active: false };
  byId("banner").classList.add("hidden");
  exitReset();                      // a slider or a "¿Seguro?" left open goes back with the capture
  if (S.stream) { for (const t of S.stream.getTracks()) t.stop(); S.stream = null; }
  video.srcObject = null;
  releaseAwake();
  chip("conn-chip", "detenida", "");
}

// "⏹ Captura detenida": the phone keeps its camera and nothing starts by itself (S.stopped: a deactivated site
// that comes back, a reload, or a poll lands here again, never on the stage). The page still asks now and then:
// a phone that lost its camera meanwhile shows why; one the admin moved to another camera offers that one.
// otherTab: another tab of this browser took the capture; once no tab answers for it, this is a plain stop.
function showStopped(otherTab) {
  const mine = { site: S.site, camera: S.camera };
  showScreen(
    el("h2", { text: otherTab ? "Este teléfono ya captura en otra pestaña" : "⏹ Captura detenida" }),
    el("p", { class: "muted", text: "Este teléfono sigue siendo la cámara " + mine.camera + ": nadie más puede tomar su lugar." }),
    bigButton(otherTab ? "▶ Capturar aquí" : "▶ Volver a capturar " + mine.camera, resume));
  keepAsking(SLOW_POLL_MS, (now) => {
    if (sameCamera(now.camara, mine) || sameCamera(now.dormida, mine)) return true;   // still this camera (its site asleep or not)
    if (!now.camara) { route(now); return false; }
    headerFor(now);
    showScreen(
      el("h2", { text: "⏹ Captura detenida" }),
      el("p", { class: "warnline", text: "Ahora este teléfono es la cámara " + now.camara.camera + " (" + mine.camera + " quedó libre)." }),
      bigButton("▶ Capturar " + now.camara.camera, resume));
    return false;
  });
  if (otherTab) watchOtherTab(S.waitGen);
}

// Is a tab of this browser capturing right now? It answers a ping on the channel within PONG_WAIT_MS.
function anotherTabCaptures() {
  return new Promise((resolve) => {
    const nonce = TAB_ID + Math.random().toString(36).slice(2);
    let timer = null;
    const onMsg = (ev) => { if (ev.data && ev.data.pong === nonce) done(true); };
    const done = (answer) => { tabs.removeEventListener("message", onMsg); clearTimeout(timer); resolve(answer); };
    tabs.addEventListener("message", onMsg);
    timer = setTimeout(() => done(false), PONG_WAIT_MS);
    tabs.postMessage({ ping: nonce });
  });
}

async function watchOtherTab(gen) {
  while (tabs && gen === S.waitGen) {
    await sleep(SLOW_POLL_MS);
    if (gen !== S.waitGen) return;
    if (await anotherTabCaptures()) continue;
    if (gen === S.waitGen) showStopped();               // that tab is gone: nothing captures, a plain stop
    return;
  }
}

// --- the accident-proof exit (presentation only: the guard and the push loop never notice) -----
// Slide past EXIT_REVEAL_AT to reveal "Salir"; release below it and the slider springs back. The
// button and the confirm popup each cancel themselves after EXIT_AUTO_CANCEL_S. "Sí, salir" stops
// this phone's capture: "⏹ Captura detenida", with the way back to the same camera.
function exitControl() {
  const slider = byId("exit-slider");
  const btn = byId("exit-btn");
  const popup = byId("exit-confirm");
  const count = byId("exit-count");
  let btnTimer = null;
  let popupTimer = null;
  let secondsLeft = 0;

  function reset() {
    clearTimeout(btnTimer); btnTimer = null;
    clearInterval(popupTimer); popupTimer = null;
    slider.value = 0;
    btn.classList.add("hidden");
    popup.classList.add("hidden");
  }
  function reveal() {
    if (!btn.classList.contains("hidden")) return;
    btn.classList.remove("hidden");
    btnTimer = setTimeout(reset, EXIT_AUTO_CANCEL_S * 1000);   // untapped: spring back, hide
  }
  function released() {
    if (Number(slider.value) >= EXIT_REVEAL_AT) reveal();
    else if (btn.classList.contains("hidden")) slider.value = 0;    // a tap or a short slide: nothing
  }
  function openPopup() {
    clearTimeout(btnTimer); btnTimer = null;
    secondsLeft = EXIT_AUTO_CANCEL_S;
    count.textContent = String(secondsLeft);
    popup.classList.remove("hidden");
    popupTimer = setInterval(() => {
      secondsLeft -= 1;
      count.textContent = String(Math.max(secondsLeft, 1));
      if (secondsLeft <= 0) reset();                                  // no answer: closes itself
    }, 1000);
  }

  slider.addEventListener("input", () => { if (Number(slider.value) >= EXIT_REVEAL_AT) reveal(); });
  slider.addEventListener("change", released);
  slider.addEventListener("pointerup", released);
  slider.addEventListener("touchend", released);
  btn.addEventListener("click", openPopup);
  byId("exit-no").addEventListener("click", reset);
  byId("exit-yes").addEventListener("click", () => {
    reset();
    if (!S.running) return;
    stopCapture();
    setStopped(true);
    showStopped();
  });
  return reset;
}

document.addEventListener("visibilitychange", () => {
  if (!S.running) return;
  log(document.visibilityState === "hidden" ? "pestaña oculta: el navegador puede frenar la captura" : "pestaña visible");
  if (document.visibilityState === "visible") keepAwake(false);   // the browser drops the lock when the page hides
});
// A tap (click, which follows the finger's lift) is what a phone's browser accepts for the lock: never pointerdown.
// An iPhone delivers a tap's click only to an element that listens (or looks clickable): the stage listens itself.
byId("stage").addEventListener("click", () => { if (S.running) keepAwake(true); });
document.addEventListener("click", () => { if (S.running) keepAwake(true); });
byId("wake-btn").addEventListener("click", () => keepAwake(true));
// Another link opened in this same tab (or Back to the plain address): the page starts over with what it says.
window.addEventListener("hashchange", () => {
  const code = codeInAddress();
  if (code === S.code) return;
  if (S.running) stopCapture();
  if (code) setStopped(false);                         // a link opened: a fresh start
  S.code = code;
  reconcile();
});
// One phone, one capture: a tab that starts capturing tells the others, and they stop (and stay stopped). A
// stopped tab asks now and then whether the capturing one is still there (ping, pong).
if (tabs) {
  tabs.addEventListener("message", (ev) => {
    const data = ev.data || {};
    if (data.ping && S.running) { tabs.postMessage({ pong: data.ping }); return; }
    if (!data.captura || data.captura === TAB_ID || !S.running) return;
    stopCapture();
    setStopped(true);
    showStopped(true);
  });
}
exitReset = exitControl();
if (PHONE_UA.test(navigator.userAgent || "")) {
  reconcile();
} else {
  chip("conn-chip", "detenida", "");
  showScreen(
    el("h2", { text: "Captura solo funciona en un teléfono" }),
    el("p", { text: "Abre el enlace de la cámara en el teléfono que va a ser esa cámara." }));
}
