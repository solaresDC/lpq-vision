// LPQ_VISION captura.js: the phone capture page (T-C1, sealed contract of Arch section 3).
// The guard lives HERE, in the browser: a tiny gray copy of the video is sampled ten times a
// second; an abrupt change is an arrival, calm frames confirm the landing, a burst of full
// frames goes to /api/upload_burst and the SERVER keeps the sharpest. Direction comes from the
// entry edge of the first movement, mapped through the calibrated kitchen edge. A function
// that is OFF is discarded right here: no request leaves the phone, nothing is stored or billed.
// Copies for the mirilla go to /api/camera/frame and live in the api's RAM only.
//
// Fase 3 (Section 3.12, SPEC 1.3 and 1.5):
// - The alta, under the ORDER LAW: install the page as a Home-Screen app FIRST, enroll SECOND. An
//   installed app keeps its own storage (what a browser tab enrolls never reaches it), and Safari's
//   7-day purge spares it. The page first asks the server who it is (/api/device/status); a device
//   that is not enrolled presents the site's gafete ONCE (/api/device/enroll sets the long-lived
//   httponly cookie; the page keeps its own copy for the header road).
// - The gafete rides EVERY call of the capture lane in the X-Device-Key header (the cookie travels by
//   itself). Never in a URL, never in the log.
// - The camera picker shows the site's cameras, live. A Pi camera that sends nothing is 🔴: the
//   emergency road (the 8pm protocol) lets this phone take its place, behind a login of that site and
//   the server's double confirm; a session the page opened for it closes right after.
// - The live knobs ride the push reply: the guard knobs, the push cadence, the site's two functions
//   and the camera's source (back to its Pi = this phone stops). Zero extra requests.
// Section 3.13: the server enforces the gafete (401 or 403 = enroll again), the guard knobs and the
// burst size are THIS site's own values, and a burst that is all black comes back refused with
// black: true (counted as discarded, never as an error).
// This file never uses the dollar sign (the build's transport rule): strings are joined with +.
import { el, poll, session, toast } from "/static/app.js";

// --- named constants -------------------------------------------------------------------
const SAMPLE_W = 64;            // the guard's frame: 64x48 gray pixels is plenty for a landing
const SAMPLE_H = 48;
const SAMPLE_MS = 100;          // ~10 samples per second
const PIXEL_DELTA = 28;         // a pixel "changed" when its gray value moved more than this (0-255)
const BURST_GAP_MS = 150;       // spacing between the frames of a burst
const EDGE_BAND = 0.3;          // the outer 30% of each side counts as that edge
const EDGE_MARGIN = 1.5;        // the winning edge must beat the runner-up by this factor, else dudosa
const MOTION_FRAMES_FOR_EDGE = 2; // the first movement frames decide the entry edge
// The push cadence is the knob capture.frame_poll_s (Perillas): the state and every push reply carry it
// as frame_poll_s, and the page never writes a number of its own. One speed, watched or not.
let pushMs = 0;                 // set by loadState() before any capture starts (start needs a loaded state)
const PUSH_ALIGN_MS = 2000;     // every 2 s during the alignment mode (live sharpness)
const PUSH_WIDTH = 640;         // the copy's width; the api scores it on the same scale as bursts
const PUSH_WATCHDOG_MS = 45000; // no copy accepted for this long while running = the push loop is stuck: restart it
const WATCHDOG_MS = 5000;       // the watchdog's own tick
const FULL_QUALITY = 0.92;
const PUSH_QUALITY = 0.7;
const LOG_MAX = 20;
const EXIT_REVEAL_AT = 90;      // the slider must pass this to reveal "Salir"; a tap alone does nothing
const EXIT_AUTO_CANCEL_S = 10;  // the revealed button and the confirm popup both give up after this
const SLOTS_POLL_MS = 5000;     // the camera picker re-reads the site's cameras this often while on screen
const FRESH_PUSHES = 3;         // a copy younger than 3 pushes = someone is sending that camera (camara.js's rule)
const KEY_HEADER = "X-Device-Key";
const STORE = { site: "lpq.site", camera: "lpq.camera", gafete: "lpq.gafete" };
const OPPOSITE = { top: "bottom", bottom: "top", left: "right", right: "left" };
const EDGE_ES = { top: "arriba", bottom: "abajo", left: "izquierda", right: "derecha" };
const ROLE_ES = { return: "📥 regresan (merma)", outgoing: "📤 salen (emplatado)", both: "🔀 ambos sentidos" };
const SOURCE_ES = { phone: "📱 teléfono", pi: "Pi" };
const SLOT_LINES = {
  mine: "▶ la cámara de este teléfono: toca para capturar",
  free: "⚪ libre: toca para capturarla con este teléfono",
  busy: "🟢 otro teléfono la está mandando",
  fallen: "🔴 la Pi no manda imagen: este teléfono puede tomar su lugar",
  alive: "🟢 la Pi está mandando imagen",
};
const INSTALL_STEPS = {
  ios: "En Safari: Compartir (en iOS 26 está dentro del botón ··· de abajo a la derecha), luego «Agregar a pantalla de inicio» (Add to Home Screen), con la opción de abrir como app web encendida. Después abre LPQ Captura desde su ícono y haz ahí el paso 2.",
  android: "En Chrome: menú ⋮, luego «Agregar a la pantalla principal» o «Instalar app». Después abre LPQ Captura desde su ícono y haz ahí el paso 2.",
  computer: "En una computadora no hace falta instalar: inscribe esta pestaña con el paso 2.",
};

const qsParams = new URLSearchParams(location.search);
const S = {
  site: "", camera: "",
  knobs: null, funciones: null, cam: null, cameras: {}, pollS: 0, slotsNote: "", slotsStop: null,
  stream: null, running: false, paused: false, busy: false,
  prev: null, baseline: null, lastCaptured: null, occupied: false,
  inMotion: false, motionFrames: 0, calmFrames: 0, entryCounts: null,
  counts: { captured: 0, discarded: 0 },
  watching: false, align: { active: false }, countdownTimer: null, secondsLeft: 0,
  lastPushAt: 0, pushGen: 0, sampleTimer: null, watchTimer: null,
};

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
const PANES = ["boot", "enroll", "slots", "stage"];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function log(text) {
  const ul = byId("log");
  ul.prepend(el("li", { text: new Date().toLocaleTimeString("es-MX") + " · " + text }));
  while (ul.children.length > LOG_MAX) ul.lastChild.remove();
}
function status(text) { byId("status").textContent = text; }
function chip(id, text, kind) { const c = byId(id); c.textContent = text; c.className = "chip " + (kind || ""); }
function showPane(name) {
  for (const p of PANES) byId(p).classList.toggle("hidden", p !== name);
  if (name !== "slots" && S.slotsStop) { S.slotsStop(); S.slotsStop = null; }
}
// replaceChildren would print an absent part as the word "null": fill() leaves it out.
function fill(node, ...kids) { node.replaceChildren(...kids.filter((k) => k)); }

// --- the device's memory (the header road's copy; the cookie is the server's) ------------
function readStore(key) { try { return localStorage.getItem(key) || ""; } catch (err) { return ""; } }
function writeStore(key, value) { try { localStorage.setItem(key, value); } catch (err) { /* the cookie still carries the gafete */ } }
function dropStore(key) { try { localStorage.removeItem(key); } catch (err) { /* nothing to drop */ } }

// --- the capture lane: the gafete on every call -------------------------------------------
function keyHeaders() {
  const headers = {};
  const key = readStore(STORE.gafete);
  if (key) headers[KEY_HEADER] = key;
  return headers;
}
function lane(path, opts) {
  const o = opts || {};
  return fetch(path, { method: o.method || "GET", body: o.body, headers: keyHeaders(), credentials: "same-origin", cache: "no-store" });
}
async function bodyOf(res) {
  try { return await res.json(); } catch (err) { return {}; }
}
function refusedKey(res) { return res.status === 401 || res.status === 403; }

// The session road, only for the emergency relevo (a login of the site, then the server's double confirm).
async function sessionPost(path, json) {
  const opts = { method: "POST", credentials: "same-origin", headers: {} };
  if (json !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(json); }
  const res = await fetch(path, opts);
  const data = await bodyOf(res);
  if (!res.ok) { const err = new Error(data.error || ("HTTP " + res.status)); err.status = res.status; throw err; }
  return data;
}

// --- state from the api ------------------------------------------------------------------
async function loadState() {
  const res = await lane("/api/camera/state?site=" + encodeURIComponent(S.site));
  const st = await bodyOf(res);
  if (refusedKey(res)) { const err = new Error(st.error || ("HTTP " + res.status)); err.refused = true; throw err; }
  if (!res.ok) throw new Error(st.error || ("HTTP " + res.status));
  S.knobs = st.capture;
  S.funciones = st.funciones;
  S.cameras = st.cameras || {};
  S.pollS = st.frame_poll_s;
  pushMs = st.frame_poll_s * 1000;                          // the ONE cadence, from its one home (the knob)
  S.cam = S.camera ? S.cameras[S.camera] || null : null;
  return st;
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
  const sign = byId("rolesign");
  if (!cam) { chip("role-chip", "cámara desconocida", "bad"); sign.textContent = "cámara no configurada"; return; }
  const f = S.funciones || {};
  if (cam.role === "return") {
    chip("role-chip", "📥 regresan (merma)", f.merma ? "ok" : "bad");
    sign.textContent = f.merma ? "📥 PLATOS QUE REGRESAN" : "⚠ función MERMA apagada: todo se descarta";
    return;
  }
  if (cam.role === "outgoing") {
    chip("role-chip", "📤 salen (emplatado)", f.presentacion ? "ok" : "bad");
    sign.textContent = f.presentacion ? "📤 PLATOS QUE SALEN" : "⚠ función PRESENTACIÓN apagada: todo se descarta";
    return;
  }
  // role both: the arrows, always visible once calibrated
  chip("role-chip", "🔀 ambos sentidos", cam.kitchen_edge ? "ok" : "warn");
  if (!cam.kitchen_edge) {
    sign.textContent = "⚠ sin calibrar: entra a /camara → modo alineación. Mientras, todo va a revisión";
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
  if (!S.running || S.paused || S.busy || video.readyState < 2) return;
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
    const res = await lane("/api/upload_burst", { method: "POST", body: form });
    const data = await bodyOf(res);
    if (refusedKey(res)) { lostEnrollment(data.error || ("HTTP " + res.status)); return; }
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
    if (wasActive) loadState().then(() => paintRole()).catch(() => {});   // the alignment may have calibrated the edge
  }
}

// The page knobs ride the push reply (Section 3.12): obey a change at once and say so in the log. Answers false
// when the camera's source is no longer the phone (the mostrador sent it back to its Pi): this phone stops.
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
  if (data.source && data.source !== "phone") {
    const camera = S.camera;
    stopCapture();
    showSlots(camera + " volvió a su Pi (relevo desde el mostrador): este teléfono dejó de capturarla.");
    return false;
  }
  return true;
}

// The loop carries a generation number: the watchdog can abandon a stuck generation and start a
// fresh one without waiting for an await that may never resolve; a stop abandons it the same way.
async function pushLoop(gen) {
  while (S.running && gen === S.pushGen) {
    try {
      if (video.readyState >= 2) {
        const blob = await grab(pushCanvas, PUSH_WIDTH, PUSH_QUALITY);
        const form = new FormData();
        form.append("frame", blob, "frame.jpg");
        form.append("site", S.site);
        form.append("camera", S.camera);
        const res = await lane("/api/camera/frame", { method: "POST", body: form });
        const data = await bodyOf(res);
        if (!S.running || gen !== S.pushGen) return;          // stopped while the copy traveled
        if (res.ok) {
          S.lastPushAt = Date.now();
          S.watching = Boolean(data.watching);
          if (!applyLive(data)) return;
          applyAlign(data.align);
          if (!S.paused) chip("conn-chip", S.watching ? "capturando · alguien mira" : "capturando", "ok");
        } else if (refusedKey(res)) {
          lostEnrollment(data.error || ("HTTP " + res.status));
          return;
        } else if (res.status === 400 && /^unknown (site|camera)/.test(data.error || "")) {
          const where = S.site + "/" + S.camera;
          stopCapture();
          showSlots("La ventanilla ya no conoce " + where + ": elige otra cámara.");
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
  if (age !== null && age * 1000 > PUSH_WATCHDOG_MS) {
    log("sin copias aceptadas desde hace " + age + " s: reiniciando el envío");
    chip("conn-chip", "envío reiniciado", "warn");
    S.lastPushAt = Date.now();      // one restart per watchdog window, not one per tick
    S.pushGen++;
    pushLoop(S.pushGen);
  }
}

// --- start and stop ------------------------------------------------------------------------
async function start() {
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (err) {
    toast("no se pudo abrir la cámara: " + err.message, "bad");
    S.slotsNote = "La cámara solo abre en la app instalada sobre HTTPS, en un origen HTTPS o en localhost: revisa el permiso de cámara.";
    paintSlots(null);
    return;
  }
  video.srcObject = S.stream;
  await video.play();
  showPane("stage");
  S.running = true;
  S.paused = false;
  S.align = { active: false };
  S.prev = null; S.baseline = null; S.occupied = false; S.inMotion = false; S.lastCaptured = null;
  S.counts = { captured: 0, discarded: 0 };
  byId("n-captured").textContent = "0";
  byId("n-discarded").textContent = "0";
  byId("last-sharp").textContent = EMPTY_SHARP;
  byId("last-push").textContent = EMPTY_PUSH;
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
  if (S.stream) { for (const t of S.stream.getTracks()) t.stop(); S.stream = null; }
  video.srcObject = null;
  chip("conn-chip", "detenida", "");
  chip("role-chip", "sin cámara", "");
}

async function beginCapture(name) {
  S.camera = name;
  S.cam = S.cameras[name] || null;
  if (!S.cam) { toast("esa cámara ya no existe en el sitio", "bad"); return; }
  writeStore(STORE.camera, name);
  paintRole();
  await start();
}

// --- one dialog for the page's questions (the exit keeps its own); never a browser dialog -----
function closeDialog() {
  byId("dialog").classList.add("hidden");
  byId("dialog-box").replaceChildren();
}

function ask(opts) {
  return new Promise((resolve) => {
    const answer = (value) => { closeDialog(); resolve(value); };
    fill(byId("dialog-box"),
      el("div", { class: "q", text: opts.title }),
      opts.body ? el("div", { class: "body", text: opts.body }) : null,
      opts.items && opts.items.length ? el("ul", {}, opts.items.map((t) => el("li", { text: t }))) : null,
      el("div", { class: "buttons" },
        el("button", { type: "button", class: opts.yesClass || "primary", text: opts.yes || "Sí", onclick: () => answer(true) }),
        opts.no ? el("button", { type: "button", text: opts.no, onclick: () => answer(false) }) : null));
    byId("dialog").classList.remove("hidden");
  });
}

// The login of the emergency road: the site's manager or the admin. Answers the session, or null on cancel.
function loginDialog(site) {
  return new Promise((resolve) => {
    const user = el("input", { autocomplete: "username", autocapitalize: "none", spellcheck: "false", placeholder: "usuario" });
    const pass = el("input", { type: "password", autocomplete: "current-password", placeholder: "contraseña" });
    const error = el("div", { class: "errline" });
    const enter = el("button", { type: "submit", class: "primary", text: "Entrar" });
    const cancel = el("button", { type: "button", text: "Cancelar", onclick: () => { closeDialog(); resolve(null); } });
    const form = el("form", {}, user, pass, error, el("div", { class: "buttons" }, enter, cancel));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      enter.disabled = true;
      error.textContent = "";
      try {
        const res = await fetch("/api/login", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user: user.value.trim(), password: pass.value }),
        });
        const data = await bodyOf(res);
        if (!res.ok) { error.textContent = data.error || "no se pudo entrar"; pass.value = ""; return; }
        closeDialog();
        resolve(data);
      } catch (err) {
        error.textContent = "sin conexión con la ventanilla";
      } finally {
        enter.disabled = false;
      }
    });
    byId("dialog-box").replaceChildren(
      el("div", { class: "q", text: "Entra con una cuenta de " + site }),
      el("div", { class: "body", text: "El gerente del sitio o el admin. La sesión se cierra sola al terminar el relevo." }),
      form);
    byId("dialog").classList.remove("hidden");
    user.focus();
  });
}

// --- the boot: who is this device? -----------------------------------------------------------
async function boot() {
  showPane("boot");
  byId("boot-note").textContent = "conectando con la ventanilla…";
  let st;
  try {
    const res = await lane("/api/device/status");
    st = await bodyOf(res);
    if (!res.ok) throw new Error(st.error || ("HTTP " + res.status));
  } catch (err) {
    byId("boot-note").replaceChildren(
      el("span", { text: "Sin conexión con la ventanilla (" + err.message + "). " }),
      el("button", { type: "button", text: "Reintentar", onclick: boot }));
    return;
  }
  if (!st.enrolled && st.stale && readStore(STORE.gafete)) {
    dropStore(STORE.gafete);        // the page's copy is the stale one: ask once more with the cookie alone
    boot();
    return;
  }
  if (!st.enrolled) {
    showEnroll(st.stale ? "El gafete de este teléfono ya no sirve (¿se generó uno nuevo en el mostrador?): inscríbelo otra vez." : "");
    return;
  }
  S.site = st.site;
  writeStore(STORE.site, st.site);
  showSlots("");
}

// --- the alta: install first, enroll second ---------------------------------------------------
function standalone() {
  const media = window.matchMedia && window.matchMedia("(display-mode: standalone)").matches;
  return Boolean(media || window.navigator.standalone === true);
}

function platform() {
  const ua = navigator.userAgent || "";
  if (/iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "ios";
  if (/Android/.test(ua)) return "android";
  return "computer";
}

function showEnroll(note) {
  const kind = platform();
  const inApp = standalone();
  const phoneInBrowser = kind !== "computer" && !inApp;
  const site = el("input", {
    value: qsParams.get("site") || readStore(STORE.site) || "", placeholder: "demo",
    autocomplete: "off", autocapitalize: "none", spellcheck: "false",
  });
  const key = el("input", {
    class: "mono", placeholder: "pega aquí el gafete",
    autocomplete: "off", autocapitalize: "none", autocorrect: "off", spellcheck: "false",
  });
  const error = el("div", { class: "errline" });
  const go = el("button", {
    type: "submit", class: phoneInBrowser ? "warn" : "primary",
    text: phoneInBrowser ? "Inscribir en el navegador (no recomendado)" : "Inscribir este teléfono",
  });
  const form = el("form", {},
    el("label", { text: "Sitio" }), site,
    el("label", { text: "Gafete" }), key,
    error, go);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    error.textContent = "";
    if (phoneInBrowser && !(await ask({
      title: "¿Inscribir en el navegador?",
      body: "Lo que se inscribe aquí no pasa a la app instalada. Lo correcto: primero instalar (paso 1) y hacer este paso dentro de la app.",
      yes: "Inscribir aquí de todos modos", yesClass: "warn", no: "Volver",
    }))) return;
    go.disabled = true;
    try {
      const res = await fetch("/api/device/enroll", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site: site.value.trim(), key: key.value.trim() }),
      });
      const data = await bodyOf(res);
      if (!res.ok) { error.textContent = data.error || ("HTTP " + res.status); return; }
      writeStore(STORE.gafete, key.value.trim());
      writeStore(STORE.site, data.site);
      S.site = data.site;
      toast("teléfono inscrito en " + data.site, "ok");
      showSlots("");
    } catch (err) {
      error.textContent = "sin conexión con la ventanilla";
    } finally {
      go.disabled = false;
    }
  });
  fill(byId("enroll"),
    el("h2", { text: "Alta de este teléfono" }),
    note ? el("p", { class: "warnline", text: note }) : null,
    el("div", { class: "steps" },
      el("div", { class: "step" + (inApp || kind === "computer" ? " done" : "") },
        el("h3", { text: inApp ? "1 · Instalar: listo" : kind === "computer" ? "1 · Instalar: aquí no hace falta" : "1 · Instalar primero" }),
        el("p", { class: "muted", text: inApp ? "Estás dentro de la app instalada (LPQ Captura)." : INSTALL_STEPS[kind] })),
      el("div", { class: "step" },
        el("h3", { text: "2 · Inscribir después" }),
        el("p", {
          class: "muted",
          text: "El gafete lo muestra el admin en el mostrador: Sitios y cuentas, el sitio, Gafete y estado, «Mostrar gafete», copiar. Pégalo aquí una sola vez: el teléfono lo recuerda.",
        }),
        phoneInBrowser ? el("p", { class: "warnline", text: "Estás en el navegador, no en la app instalada: haz primero el paso 1." }) : null,
        form)));
  showPane("enroll");
}

async function forgetDevice() {
  const sure = await ask({
    title: "¿Olvidar este teléfono?",
    body: "Deja de estar inscrito en " + S.site + ": para volver a capturar hay que pegar el gafete otra vez.",
    yes: "Olvidar", yesClass: "bad", no: "Cancelar",
  });
  if (!sure) return;
  try { await fetch("/api/device/forget", { method: "POST", credentials: "same-origin" }); } catch (err) { /* the next enrollment replaces the cookie anyway */ }
  dropStore(STORE.gafete);
  dropStore(STORE.camera);
  showEnroll("");
}

function lostEnrollment(message) {
  stopCapture();
  dropStore(STORE.gafete);
  showEnroll("La ventanilla rechazó el gafete de este teléfono (" + message + "): inscríbelo otra vez.");
}

// --- the camera picker and the emergency road ---------------------------------------------------
function freshAge(cam) {
  const age = cam.last_frame_age_s;
  return typeof age === "number" && age <= FRESH_PUSHES * S.pollS ? age : null;
}

function slotKind(name, cam) {
  const age = freshAge(cam);
  if (cam.source === "pi") return age === null ? "fallen" : "alive";
  if (name === readStore(STORE.camera)) return "mine";
  return age === null ? "free" : "busy";
}

function showSlots(note) {
  showPane("slots");
  S.slotsNote = note || "";
  paintSlots(null);
  if (S.slotsStop) S.slotsStop();
  S.slotsStop = poll(refreshSlots, SLOTS_POLL_MS);
}

async function refreshSlots() {
  try {
    await loadState();
    paintSlots(null);
  } catch (err) {
    if (err.refused) { lostEnrollment(err.message); return; }
    paintSlots(err.message);
  }
}

function paintSlots(error) {
  const names = Object.keys(S.cameras);
  const list = el("div", { class: "slots" }, names.map((name) => {
    const cam = S.cameras[name];
    const kind = slotKind(name, cam);
    const age = freshAge(cam);
    const seen = age !== null && kind !== "mine" ? " (copia hace " + Math.round(age) + " s)" : "";
    return el("button", { type: "button", class: "slot " + kind, onclick: () => pickSlot(name) },
      el("div", { class: "name", text: name }),
      el("div", { class: "line", text: (ROLE_ES[cam.role] || cam.role) + " · " + (SOURCE_ES[cam.source] || cam.source) }),
      el("div", { class: "line", text: SLOT_LINES[kind] + seen }));
  }));
  fill(byId("slots"),
    el("h2", { text: "¿Qué cámara es este teléfono?" }),
    el("p", { class: "muted" },
      "Sitio ", el("b", { text: S.site }), " · teléfono inscrito · ",
      el("button", { type: "button", class: "linkish", text: "olvidar este teléfono", onclick: forgetDevice })),
    S.slotsNote ? el("p", { class: "warnline", text: S.slotsNote }) : null,
    error ? el("p", { class: "errline", text: "No se pudo leer el sitio: " + error }) : null,
    names.length ? list : el("p", { class: "muted", text: "Este sitio aún no tiene cámaras: se agregan en el mostrador." }),
    S.knobs ? el("p", { class: "muted", text: "perillas: " + knobsText() }) : null);
}

async function pickSlot(name) {
  const cam = S.cameras[name];
  if (!cam) return;
  const kind = slotKind(name, cam);
  if (kind === "fallen") { await takeover(name); return; }
  if (kind === "alive") {
    await ask({
      title: "La Pi de " + name + " está mandando imagen",
      body: "No hace falta el relevo. Si de verdad hay que cambiarla, se hace en el mostrador (Sitios y cuentas, Relevo).",
      yes: "Entendido",
    });
    return;
  }
  if (kind === "busy") {
    const sure = await ask({
      title: "Otro teléfono ya manda " + name,
      body: "Su última copia llegó hace " + Math.round(freshAge(cam)) + " s. Si los dos capturan, los platos salen dobles. ¿Usar este teléfono de todos modos?",
      yes: "Usar este", yesClass: "warn", no: "Cancelar",
    });
    if (!sure) return;
  }
  await beginCapture(name);
}

// The emergency road (SPEC 1.5, the 8pm protocol): this phone takes the place of a Pi that sends nothing.
// A login of the site (the page reuses a session of that site when the browser already has one), then the
// server's double confirm with its visible diff. A session the page opened closes right after, done or not.
async function takeover(name) {
  const cam = S.cameras[name];
  const lost = cam.kitchen_edge ? " y su calibración se borra (luego calíbrala en /camara, modo alineación)" : "";
  const go = await ask({
    title: "🔴 La Pi de " + name + " no manda imagen",
    body: "Este teléfono puede tomar su lugar: " + name + " pasa a «teléfono»" + lost + ". Para confirmarlo se pide una cuenta de " + S.site + ".",
    yes: "Tomar su lugar", yesClass: "warn", no: "Cancelar",
  });
  if (!go) return;
  let sess = await session();
  let opened = false;
  if (!sess || !(sess.admin || sess.site === S.site)) {
    sess = await loginDialog(S.site);
    if (!sess) return;
    opened = true;
  }
  let done = false;
  try {
    if (!(sess.admin || sess.site === S.site)) throw new Error("esa cuenta no es de " + S.site);
    const step1 = await sessionPost("/api/admin/sites/" + encodeURIComponent(S.site) + "/cameras/" + encodeURIComponent(name) + "/takeover");
    const items = (step1.diff.cambios || []).map((c) => c.que + ": " + c.antes + " → " + c.despues);
    const sure = await ask({
      title: "¿Hacer el relevo?", body: step1.diff.resumen, items: items,
      yes: "Sí, hacer el relevo", yesClass: "warn", no: "Cancelar",
    });
    if (!sure) return;
    await sessionPost("/api/admin/confirm", { pending_id: step1.pending_id });
    done = true;
    toast("relevo hecho: " + name + " ahora es de este teléfono", "ok");
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    if (opened) { try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); } catch (err) { /* the session dies with its lifetime */ } }
  }
  if (!done) return;
  try { await loadState(); } catch (err) { toast(err.message, "bad"); return; }
  await beginCapture(name);
}

// --- the accident-proof exit (presentation only: the guard and the push loop never notice) -----
// Slide past EXIT_REVEAL_AT to reveal "Salir"; release below it and the slider springs back. The
// button and the confirm popup each cancel themselves after EXIT_AUTO_CANCEL_S. "Sí, salir" stops
// this phone's capture and goes back to the camera picker (a kiosk app has no back button).
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
    showSlots("");
  });
}

document.addEventListener("visibilitychange", () => {
  if (!S.running) return;
  log(document.visibilityState === "hidden" ? "pestaña oculta: el navegador puede frenar la captura" : "pestaña visible");
});
if (!readStore(STORE.camera) && qsParams.get("camera")) writeStore(STORE.camera, qsParams.get("camera"));
exitControl();
boot();
