// LPQ_VISION captura.js: the phone capture page (T-C1, sealed contract of Arch section 3).
// The guard lives HERE, in the browser: a tiny gray copy of the video is sampled ten times a
// second; an abrupt change is an arrival, calm frames confirm the landing, a burst of full
// frames goes to /api/upload_burst and the SERVER keeps the sharpest. Direction comes from the
// entry edge of the first movement, mapped through the calibrated kitchen edge. A function
// that is OFF is discarded right here: no request leaves the phone, nothing is stored or billed.
// Copies for the mirilla go to /api/camera/frame and live in the api's RAM only.
import { el, $, toast } from "/static/app.js";

// --- named constants -------------------------------------------------------------------
const SAMPLE_W = 64;            // the guard's frame: 64x48 gray pixels is plenty for a landing
const SAMPLE_H = 48;
const SAMPLE_MS = 100;          // ~10 samples per second
const PIXEL_DELTA = 28;         // a pixel "changed" when its gray value moved more than this (0-255)
const BURST_GAP_MS = 150;       // spacing between the frames of a burst
const EDGE_BAND = 0.3;          // the outer 30% of each side counts as that edge
const EDGE_MARGIN = 1.5;        // the winning edge must beat the runner-up by this factor, else dudosa
const MOTION_FRAMES_FOR_EDGE = 2; // the first movement frames decide the entry edge
const PUSH_IDLE_MS = 15000;     // a copy every 15 s when nobody watches (so the api can tell us when they do)
const PUSH_WATCHED_MS = 5000;   // every 5 s while the mirilla is watching
const PUSH_ALIGN_MS = 2000;     // every 2 s during the alignment mode (live sharpness)
const PUSH_WIDTH = 640;         // the copy's width; the api scores it on the same scale as bursts
const FULL_QUALITY = 0.92;
const PUSH_QUALITY = 0.7;
const LOG_MAX = 20;
const OPPOSITE = { top: "bottom", bottom: "top", left: "right", right: "left" };
const EDGE_ES = { top: "arriba", bottom: "abajo", left: "izquierda", right: "derecha" };

const qsParams = new URLSearchParams(location.search);
const S = {
  site: qsParams.get("site") || localStorage.getItem("lpq.site") || "demo",
  camera: qsParams.get("camera") || localStorage.getItem("lpq.camera") || "",
  knobs: null, funciones: null, cam: null, cameras: {},
  stream: null, running: false, paused: false, busy: false,
  prev: null, baseline: null, lastCaptured: null, occupied: false,
  inMotion: false, motionFrames: 0, calmFrames: 0, entryCounts: null,
  counts: { captured: 0, discarded: 0 },
  watching: false, align: { active: false }, countdownTimer: null, secondsLeft: 0,
};

const video = $("#video");
const sampleCanvas = $("#sample");
const fullCanvas = $("#full");
const pushCanvas = $("#push");
sampleCanvas.width = SAMPLE_W;
sampleCanvas.height = SAMPLE_H;
const sctx = sampleCanvas.getContext("2d", { willReadFrequently: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function log(text) {
  const ul = $("#log");
  ul.prepend(el("li", { text: `${new Date().toLocaleTimeString("es-MX")} · ${text}` }));
  while (ul.children.length > LOG_MAX) ul.lastChild.remove();
}
function status(text) { $("#status").textContent = text; }
function chip(id, text, kind = "") { const c = $(id); c.textContent = text; c.className = `chip ${kind}`; }

// --- state from the api ------------------------------------------------------------------
async function loadState() {
  const res = await fetch(`/api/camera/state?site=${encodeURIComponent(S.site)}`);
  if (!res.ok) throw new Error((await res.json()).error || `HTTP ${res.status}`);
  const st = await res.json();
  S.knobs = st.capture;
  S.funciones = st.funciones;
  S.cameras = st.cameras;
  if (!S.camera || !st.cameras[S.camera]) S.camera = Object.keys(st.cameras)[0] || "";
  S.cam = st.cameras[S.camera] || null;
  paintRole();
  return st;
}

function paintRole() {
  const cam = S.cam;
  const arrows = { top: $("#arrow-top"), bottom: $("#arrow-bottom"), left: $("#arrow-left"), right: $("#arrow-right") };
  for (const a of Object.values(arrows)) { a.className = "arrow hidden"; a.textContent = ""; }
  if (!cam) { chip("#role-chip", "cámara desconocida", "bad"); $("#rolesign").textContent = "cámara no configurada"; return; }
  const f = S.funciones || {};
  if (cam.role === "return") {
    chip("#role-chip", "📥 regresan (merma)", f.merma ? "ok" : "bad");
    $("#rolesign").textContent = f.merma ? "📥 PLATOS QUE REGRESAN" : "⚠ función MERMA apagada: todo se descarta";
    return;
  }
  if (cam.role === "outgoing") {
    chip("#role-chip", "📤 salen (emplatado)", f.presentacion ? "ok" : "bad");
    $("#rolesign").textContent = f.presentacion ? "📤 PLATOS QUE SALEN" : "⚠ función PRESENTACIÓN apagada: todo se descarta";
    return;
  }
  // role both: the arrows, always visible once calibrated
  chip("#role-chip", "🔀 ambos sentidos", cam.kitchen_edge ? "ok" : "warn");
  if (!cam.kitchen_edge) {
    $("#rolesign").textContent = "⚠ sin calibrar: entra a /camara → modo alineación. Mientras, todo va a revisión";
    return;
  }
  const k = cam.kitchen_edge, r = OPPOSITE[k];
  arrows[k].className = `arrow ${k} ${f.presentacion ? "kitchen" : "off"}`;
  arrows[k].textContent = f.presentacion ? "🟠 COCINA → salen" : "COCINA (presentación apagada: se descarta)";
  arrows[r].className = `arrow ${r} ${f.merma ? "room" : "off"}`;
  arrows[r].textContent = f.merma ? "🟢 RESTAURANTE → regresan" : "RESTAURANTE (merma apagada: se descarta)";
  $("#rolesign").textContent = `cocina: ${EDGE_ES[k]} · restaurante: ${EDGE_ES[r]}`;
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
    status(`movimiento… (${pct.toFixed(0)} %)`);
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
  const [edge, best] = sorted[0], second = sorted[1][1];
  if (best === 0 || best < EDGE_MARGIN * second) return doubt("entrada ambigua");
  if (edge === cam.kitchen_edge) return { record_type: "outgoing", dudosa: false, why: `entró por ${EDGE_ES[edge]} (cocina)` };
  if (edge === OPPOSITE[cam.kitchen_edge]) return { record_type: "return", dudosa: false, why: `entró por ${EDGE_ES[edge]} (restaurante)` };
  return doubt(`entró por un costado (${EDGE_ES[edge]})`);
}

function functionOn(record_type) {
  return record_type === "outgoing" ? Boolean(S.funciones.presentacion) : Boolean(S.funciones.merma);
}

function grab(canvas, width, quality) {
  const w = Math.min(width, video.videoWidth), h = Math.round(video.videoHeight * (w / video.videoWidth));
  canvas.width = w; canvas.height = h;
  canvas.getContext("2d").drawImage(video, 0, 0, w, h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
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
      $("#n-discarded").textContent = S.counts.discarded;
      status(`descartado: ${es}, función apagada`);
      log(`descartado (${es}, ${d.why}): función apagada, no se envió nada`);
      return;
    }
    const form = new FormData();
    frames.forEach((b, i) => form.append("photos", b, `frame${i}.jpg`));
    form.append("site", S.site);
    form.append("camera", S.camera);
    form.append("record_type", d.record_type);
    if (d.dudosa) form.append("direction_dudosa", "true");
    status(`subiendo ${frames.length} cuadros…`);
    const res = await fetch("/api/upload_burst", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    S.counts.captured++;
    $("#n-captured").textContent = S.counts.captured;
    $("#last-sharp").textContent = data.sharpness;
    status(`✔ plato #${data.plate_id} · ${es} · nitidez ${data.sharpness}${d.dudosa ? " · dirección dudosa (a revisión)" : ""}${data.duplicate ? " · repetido" : ""}`);
    log(`#${data.plate_id} ${es} · nitidez ${data.sharpness} · ${d.why}${d.dudosa ? " · DUDOSA" : ""}`);
  } catch (err) {
    status(`✘ error: ${err.message}`);
    log(`error al subir: ${err.message}`);
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
    $("#banner").classList.remove("hidden");
    chip("#conn-chip", "en pausa (alineación)", "warn");
    S.secondsLeft = S.align.seconds_left || 0;
    clearInterval(S.countdownTimer);
    S.countdownTimer = setInterval(() => {
      S.secondsLeft = Math.max(0, S.secondsLeft - 1);
      const m = String(Math.floor(S.secondsLeft / 60)).padStart(2, "0"), s = String(S.secondsLeft % 60).padStart(2, "0");
      $("#countdown").textContent = S.secondsLeft > 0 ? `${m}:${s}` : "reanudando…";
    }, 1000);
    log("captura en pausa: modo alineación");
  } else if (S.align.active && S.paused) {
    S.secondsLeft = S.align.seconds_left || S.secondsLeft;  // resync the countdown with the server
  } else if (!S.align.active && S.paused) {
    S.paused = false;
    clearInterval(S.countdownTimer);
    $("#banner").classList.add("hidden");
    chip("#conn-chip", "capturando", "ok");
    S.prev = null; S.baseline = null; S.occupied = false; S.inMotion = false;   // fresh eyes after a pause
    log("captura reanudada");
    if (wasActive) loadState().catch(() => {});          // the alignment may have calibrated the edge
  }
}

async function pushLoop() {
  while (S.running) {
    try {
      if (video.readyState >= 2) {
        const blob = await grab(pushCanvas, PUSH_WIDTH, PUSH_QUALITY);
        const form = new FormData();
        form.append("frame", blob, "frame.jpg");
        form.append("site", S.site);
        form.append("camera", S.camera);
        const res = await fetch("/api/camera/frame", { method: "POST", body: form });
        if (res.ok) {
          const data = await res.json();
          S.watching = Boolean(data.watching);
          applyAlign(data.align);
          if (!S.paused) chip("#conn-chip", S.watching ? "capturando · alguien mira" : "capturando", "ok");
        } else {
          chip("#conn-chip", "ventanilla rechazó la copia", "bad");
        }
      }
    } catch {
      chip("#conn-chip", "sin conexión con la ventanilla", "bad");
    }
    await sleep(S.align.active ? PUSH_ALIGN_MS : S.watching ? PUSH_WATCHED_MS : PUSH_IDLE_MS);
  }
}

// --- start ---------------------------------------------------------------------------------
async function start() {
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
  } catch (err) {
    toast(`no se pudo abrir la cámara: ${err.message}`, "bad");
    $("#setup-note").textContent = "La cámara solo abre en localhost o en un origen HTTPS. Revisa permisos del navegador.";
    return;
  }
  video.srcObject = S.stream;
  await video.play();
  $("#setup").classList.add("hidden");
  $("#stage").classList.remove("hidden");
  S.running = true;
  S.prev = null; S.baseline = null; S.occupied = false;
  chip("#conn-chip", "capturando", "ok");
  status("esperando plato…");
  log(`cámara ${S.camera} de ${S.site}: ${video.videoWidth}x${video.videoHeight}`);
  setInterval(sample, SAMPLE_MS);
  pushLoop();
}

async function setup() {
  let st;
  try { st = await loadState(); } catch (err) { toast(err.message, "bad"); $("#setup-note").textContent = `no se pudo leer el sitio ${S.site}: ${err.message}`; }
  const siteInput = el("input", { value: S.site });
  const camSelect = el("select", {}, Object.keys(S.cameras).map((c) => el("option", { value: c, text: `${c} (${S.cameras[c].role})` })));
  if (S.camera) camSelect.value = S.camera;
  const startBtn = el("button", { class: "primary", text: "▶ Iniciar cámara" });
  $("#setup-row").replaceChildren(
    el("div", {}, el("label", { text: "Sitio" }), siteInput),
    el("div", {}, el("label", { text: "Cámara" }), camSelect),
    el("div", {}, startBtn),
  );
  siteInput.addEventListener("change", async () => {
    S.site = siteInput.value.trim(); S.camera = "";
    try { await loadState(); camSelect.replaceChildren(...Object.keys(S.cameras).map((c) => el("option", { value: c, text: `${c} (${S.cameras[c].role})` }))); }
    catch (err) { toast(err.message, "bad"); }
  });
  startBtn.addEventListener("click", async () => {
    S.site = siteInput.value.trim(); S.camera = camSelect.value;
    localStorage.setItem("lpq.site", S.site); localStorage.setItem("lpq.camera", S.camera);
    try { await loadState(); } catch (err) { toast(err.message, "bad"); return; }
    if (!S.cam) { toast("esa cámara no existe en el sitio", "bad"); return; }
    await start();
  });
  if (st) $("#setup-note").textContent = `umbrales: ${st.capture.diff_threshold} % de cambio, ${st.capture.settle_frames} cuadros de calma, ráfaga de ${st.capture.burst_frames}`;
}

setup();
