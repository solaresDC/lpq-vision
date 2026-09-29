// LPQ_VISION camara.js: the mirilla (foto-al-toque + vista viva) and the alignment mode (T-C6).
// Copies are fetched from the api's RAM store and shown; nothing here stores anything. Polling only
// while the switch is on and the tab is visible: switch off or tab closed = zero requests.
import { api, session, requireLogin, mountHeader, poll, el, $, toast } from "/static/app.js";

const ALIGN_TICK_MS = 2000;                       // live sharpness + countdown cadence during alignment
const EDGES = ["top", "right", "bottom", "left"];  // the 90-degree rotate walks this ring
const OPPOSITE = { top: "bottom", bottom: "top", left: "right", right: "left" };
const EDGE_ES = { top: "arriba", bottom: "abajo", left: "izquierda", right: "derecha" };

let sess = await session();
if (!sess) sess = await requireLogin();
mountHeader("Cámara", sess);

const S = { site: sess.site || "demo", camera: "", st: null, cam: null, edge: "top", frameUrl: null, liveStop: null, alignStop: null, secondsLeft: 0, lastAge: null, lastFetchAt: 0 };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// "otra" as a self-completing interaction: when the copy on screen is the same one aging, wait for
// the next push (polling the frame every OTRA_POLL_MS, at most OTRA_CAP_MS) and show it when it lands.
const OTRA_POLL_MS = 1000;
const OTRA_CAP_MS = 25000;
const OTRA_TICK_MS = 250;      // the countdown repaints this often; the frame poll stays at OTRA_POLL_MS
const OTRA_CUSHION_S = 2;      // added to the estimate so the copy almost always lands before zero
// The push cadence lives in api/routes.py FRAME_POLL_S, a mostrador knob in FASE 3; captura and
// camara read it from /api/camera/state (frame_poll_s), never hardcode it.

// --- picker ---------------------------------------------------------------------------
const siteInput = el("input", { value: S.site, placeholder: "sitio" });
const camSelect = el("select");
const loadBtn = el("button", { class: "primary", text: "Cargar" });
if (!sess.admin) siteInput.disabled = true;
$("#picker").append(el("div", {}, el("label", { text: "Sitio" }), siteInput), el("div", {}, el("label", { text: "Cámara" }), camSelect), el("div", {}, loadBtn));

async function loadState() {
  S.site = siteInput.value.trim();
  S.st = await api(`/api/camera/state?site=${encodeURIComponent(S.site)}`);
  const names = Object.keys(S.st.cameras);
  const keep = names.includes(camSelect.value) ? camSelect.value : names[0] || "";
  camSelect.replaceChildren(...names.map((n) => el("option", { value: n, text: `${n} (${S.st.cameras[n].role}, ${S.st.cameras[n].source})` })));
  camSelect.value = keep;
  S.camera = keep;
  S.cam = S.st.cameras[keep] || null;
  $("#poll-s").textContent = S.st.frame_poll_s;
  paintCamInfo();
  paintArrows();
  return S.st;
}

function paintCamInfo() {
  const c = S.cam;
  if (!c) { $("#cam-info").textContent = "este sitio no tiene cámaras"; return; }
  const f = S.st.funciones;
  const parts = [`rol ${c.role}`, `fuente ${c.source}`, `merma ${f.merma ? "ON" : "off"}`, `presentación ${f.presentacion ? "ON" : "off"}`];
  parts.push(c.kitchen_edge ? `borde-cocina: ${EDGE_ES[c.kitchen_edge]}` : "sin calibrar");
  if (c.last_frame_age_s !== undefined) parts.push(`última copia hace ${c.last_frame_age_s} s`);
  if (c.align.active) parts.push(`⏸ alineación activa (${c.align.seconds_left} s)`);
  $("#cam-info").textContent = parts.join(" · ");
}

// The arrows over the picture: the edge being edited (alignment) or the saved one (mirilla).
function paintArrows() {
  const arrows = { top: $("#arrow-top"), bottom: $("#arrow-bottom"), left: $("#arrow-left"), right: $("#arrow-right") };
  for (const a of Object.values(arrows)) { a.className = "arrow hidden"; a.textContent = ""; }
  if (!S.cam || S.cam.role !== "both") return;
  const edge = S.alignStop ? S.edge : S.cam.kitchen_edge;
  if (!edge) return;
  const f = S.st.funciones, k = edge, r = OPPOSITE[edge];
  arrows[k].className = `arrow ${k} ${f.presentacion ? "kitchen" : "off"}`;
  arrows[k].textContent = f.presentacion ? "🟠 COCINA" : "COCINA · se descarta";
  arrows[r].className = `arrow ${r} ${f.merma ? "room" : "off"}`;
  arrows[r].textContent = f.merma ? "🟢 RESTAURANTE" : "RESTAURANTE · se descarta";
}

loadBtn.addEventListener("click", async () => { try { await loadState(); toast("cámara cargada", "ok"); } catch (err) { toast(err.message, "bad"); } });
camSelect.addEventListener("change", () => { S.camera = camSelect.value; S.cam = S.st ? S.st.cameras[S.camera] : null; paintCamInfo(); paintArrows(); });

// --- the mirilla ----------------------------------------------------------------------
async function fetchFrame() {
  if (!S.camera) { toast("elige una cámara", "bad"); return; }
  const res = await fetch(`/api/camera/frame?site=${encodeURIComponent(S.site)}&camera=${encodeURIComponent(S.camera)}&_=${Date.now()}`, { cache: "no-store" });
  const img = $("#frame"), badge = $("#badge"), empty = $("#empty");
  if (res.status === 404) {
    $("#frame-info").textContent = "sin copia fresca: nadie está capturando ahora";
    badge.classList.add("hidden");
    empty.classList.remove("hidden");
    empty.textContent = "sin copia fresca: nadie está capturando ahora";
    S.lastAge = null;
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const sharp = parseFloat(res.headers.get("X-Sharpness"));
  const age = res.headers.get("X-Frame-Age-S");
  S.lastAge = parseFloat(age);   // the age of the copy now on screen: "otra" compares against it
  S.lastFetchAt = Date.now();    // when that age was read, so "same copy" can be recognised later
  const blob = await res.blob();
  if (S.frameUrl) URL.revokeObjectURL(S.frameUrl);
  S.frameUrl = URL.createObjectURL(blob);
  img.src = S.frameUrl;
  img.classList.remove("hidden");
  empty.classList.add("hidden");
  const ok = Number.isFinite(sharp) && sharp >= S.st.sharpness_min;
  // A copy older than 3 polls means captura stopped pushing: say so instead of grading a stale frame.
  const stale = Number.isFinite(parseFloat(age)) && parseFloat(age) > 3 * S.st.frame_poll_s;
  if (stale) {
    badge.textContent = "copia vieja: la captura no está enviando";
    badge.className = "badge bad";
    $("#frame-info").textContent = `copia de hace ${age} s · nitidez ${Number.isFinite(sharp) ? sharp : "—"} · piso ${S.st.sharpness_min}`;
    return sharp;
  }
  badge.textContent = Number.isFinite(sharp) ? `nitidez ${sharp}` : "nitidez —";
  badge.className = `badge ${ok ? "ok" : "bad"}`;
  $("#frame-info").textContent = `copia de hace ${age} s · piso ${S.st.sharpness_min}`;
  return sharp;
}

async function cadenceS() {
  try {
    const st = await api(`/api/camera/state?site=${encodeURIComponent(S.site)}`);
    return st.frame_poll_s;
  } catch { return S.st.frame_poll_s; }                       // the state the page already loaded
}

// "otra", sealed (HQ): vista viva ON = the button is grey and idle (the view refreshes itself).
// Vista viva OFF + one click = grey instantly, a countdown with the REAL time left to the next
// copy, the copy newer than the one on screen at click time shows itself, the button revives.
// No silent case: every click ends in a new picture, a message, or the 25 s cap.
async function otra() {
  const btn = $("#otra"), note = $("#otra-note");
  if (btn.disabled) return;
  btn.disabled = true;                                        // (1) grey at once
  const onScreenAge = S.lastAge, onScreenAt = S.lastFetchAt;  // the copy on screen at click time
  let ticker = null;
  try {
    // (2) the overlay starts AT THE CLICK, before any network: ONE overlay over the picture, the
    // countdown alone first ("siguiente foto en N…", estimated from what the page already knows,
    // plus a cushion, spinner hidden); only past the estimate the spinner appears with
    // "esperando la copia… (N s)" counting the seconds since the click. The network refines below.
    const waitText = $("#wait-text"), waitSpin = $("#wait .spin");
    const t0 = Date.now();
    const guessAge = S.lastAge === null ? 0 : S.lastAge + (t0 - S.lastFetchAt) / 1000;
    const guessCadence = S.st.frame_poll_s;
    let eta = t0 + (Math.max(0, guessCadence - guessAge) + OTRA_CUSHION_S) * 1000;
    const paint = () => {
      const now = Date.now();
      const left = Math.ceil((eta - now) / 1000);
      if (left > 0) { waitText.textContent = `siguiente foto en ${left}…`; waitSpin.classList.add("hidden"); }
      else { waitText.textContent = `esperando la copia… (${Math.floor((now - t0) / 1000)} s)`; waitSpin.classList.remove("hidden"); }
    };
    paint();
    $("#wait").classList.remove("hidden");
    ticker = setInterval(paint, OTRA_TICK_MS);
    const sharp = await fetchFrame();
    if (sharp === null || sharp === undefined) { note.textContent = ""; return; }   // no copy: fetchFrame said so
    // Newer than the copy that was on screen? Same copy = its age grew by the elapsed time.
    const sameAge = onScreenAge === null ? null : onScreenAge + (Date.now() - onScreenAt) / 1000;
    if (sameAge === null || S.lastAge < sameAge - 0.5) { note.textContent = ""; return; }   // a new copy landed already
    // The same copy: the network only refines the estimate (the real cadence and the real age).
    eta = t0 + (Math.max(0, (await cadenceS()) - S.lastAge) + OTRA_CUSHION_S) * 1000;
    // (3) poll until the age resets (a newer copy) or the cap; the fresh copy paints itself.
    while (Date.now() - t0 < OTRA_CAP_MS) {
      await sleep(OTRA_POLL_MS);
      const prev = S.lastAge;
      await fetchFrame();
      if (S.lastAge === null || S.lastAge < prev) return;
    }
    note.textContent = `sin copia nueva en ${OTRA_CAP_MS / 1000} s: la captura no está enviando`;
  } finally {
    if (ticker) clearInterval(ticker);
    $("#wait").classList.add("hidden");
    btn.disabled = $("#live").checked;                        // revive, unless vista viva took over
  }
}

$("#otra").addEventListener("click", () => otra().catch((err) => { $("#otra-note").textContent = ""; toast(err.message, "bad"); }));
$("#live").addEventListener("change", (ev) => {
  $("#otra").disabled = ev.target.checked;                    // vista viva ON: the button has no job
  $("#otra-note").textContent = "";
  if (ev.target.checked) { S.liveStop = poll(fetchFrame, (S.st ? S.st.frame_poll_s : 5) * 1000); }
  else if (S.liveStop) { S.liveStop(); S.liveStop = null; }
});

// --- the alignment mode ---------------------------------------------------------------
const minutesInput = el("input", { type: "number", min: 1, value: 1 });
const startBtn = el("button", { class: "warn", text: "▶ Iniciar alineación" });
$("#align-setup").append(el("div", {}, el("label", { text: "Cuerda (minutos)" }), minutesInput), el("div", {}, startBtn));

function fmtCount(s) { return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; }

function paintEdgeControls() {
  const box = $("#edge-controls");
  box.replaceChildren();
  if (!S.cam || S.cam.role !== "both") {
    box.append(el("span", { class: "muted", text: `cámara de rol ${S.cam ? S.cam.role : "?"}: sin flechas, solo nitidez` }));
    return;
  }
  const saved = S.cam.kitchen_edge;
  const dirty = saved !== S.edge;
  box.append(
    el("span", { class: `chip ${dirty ? "warn" : "ok"}`, text: `cocina: ${EDGE_ES[S.edge]}${dirty ? " (sin guardar)" : " (guardado)"}` }),
    el("button", { text: "⟳ rotar 90°", onclick: () => { S.edge = EDGES[(EDGES.indexOf(S.edge) + 1) % 4]; paintArrows(); paintEdgeControls(); } }),
    el("button", { class: "ok", text: "💾 Guardar borde-cocina", disabled: !dirty, onclick: saveEdge }),
  );
}

async function saveEdge() {
  try {
    const r = await api("/api/camera/calibration", { method: "POST", json: { site: S.site, camera: S.camera, kitchen_edge: S.edge } });
    toast(`borde-cocina guardado: ${EDGE_ES[r.kitchen_edge]} (${r.action === "inserted" ? "nuevo" : "reemplazado"})`, "ok");
    await loadState();
    paintEdgeControls();
  } catch (err) { toast(err.message, "bad"); }
}

async function startAlign() {
  if (!S.camera) { toast("elige una cámara", "bad"); return; }
  const minutes = Math.max(1, parseInt(minutesInput.value, 10) || 1);
  try {
    const r = await api("/api/camera/align/start", { method: "POST", json: { site: S.site, camera: S.camera, minutes } });
    S.secondsLeft = r.seconds;
    if (r.capped) toast(`cuerda recortada al tope: ${r.seconds} s`, "warn");
    S.edge = (S.cam && S.cam.kitchen_edge) || "top";
    $("#align-active").classList.remove("hidden");
    $("#align-setup").classList.add("hidden");
    $("#countdown").textContent = fmtCount(S.secondsLeft);
    paintEdgeControls();
    S.alignStop = poll(alignTick, ALIGN_TICK_MS);
    paintArrows();
  } catch (err) { toast(err.message, "bad"); }
}

async function alignTick() {
  const sharp = await fetchFrame();
  $("#live-sharp").textContent = sharp === null || sharp === undefined ? "—" : sharp;
  $("#live-sharp").style.color = sharp !== null && sharp >= S.st.sharpness_min ? "var(--ok)" : "var(--bad)";
  const st = await api(`/api/camera/state?site=${encodeURIComponent(S.site)}`);
  const cam = st.cameras[S.camera];
  if (!cam || !cam.align.active) { endAlign("la cuerda venció: la captura despertó sola"); return; }
  S.secondsLeft = cam.align.seconds_left;
  $("#countdown").textContent = fmtCount(S.secondsLeft);
}

function endAlign(reason) {
  if (S.alignStop) { S.alignStop(); S.alignStop = null; }
  $("#align-active").classList.add("hidden");
  $("#align-setup").classList.remove("hidden");
  toast(`alineación terminada: ${reason}`, "ok");
  loadState().catch(() => {});
}

startBtn.addEventListener("click", startAlign);
$("#stop").addEventListener("click", async () => {
  try { await api("/api/camera/align/stop", { method: "POST", json: { site: S.site, camera: S.camera } }); endAlign("terminada a mano"); }
  catch (err) { toast(err.message, "bad"); }
});

try { await loadState(); } catch (err) { toast(err.message, "bad"); }
