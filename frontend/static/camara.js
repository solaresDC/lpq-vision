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

const S = { site: sess.site || "demo", camera: "", st: null, cam: null, edge: "top", frameUrl: null, liveStop: null, alignStop: null, secondsLeft: 0 };

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
    return null;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const sharp = parseFloat(res.headers.get("X-Sharpness"));
  const age = res.headers.get("X-Frame-Age-S");
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

$("#otra").addEventListener("click", () => fetchFrame().catch((err) => toast(err.message, "bad")));
$("#live").addEventListener("change", (ev) => {
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
