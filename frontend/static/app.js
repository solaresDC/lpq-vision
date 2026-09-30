// LPQ_VISION app.js: the shared helpers of the five pages. Vanilla ES module, no framework,
// no build step (SPEC 1.4). Every page talks ONLY to /api/*; this file is the one door.

export const POLL_MS = 5000;

// --- DOM helpers ---------------------------------------------------------------------

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function $(sel, root = document) { return root.querySelector(sel); }

let toastNode = null;
let toastTimer = null;
export function toast(msg, kind = "") {
  if (!toastNode) { toastNode = el("div", { class: "toast" }); document.body.append(toastNode); }
  toastNode.textContent = msg;
  toastNode.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastNode.className = "toast"; }, 2600);
}

// --- formatting ----------------------------------------------------------------------

export function fmtAgo(seconds) {
  if (seconds === null || seconds === undefined) return "nunca";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `hace ${s} s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("es-MX", { dateStyle: "short", timeStyle: "short" });
}

// Build a query string from an object, skipping empty values (absent-when-off, for URLs).
export function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === "" || v === null || v === undefined) continue;
    p.set(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

// --- the one door to /api/* ----------------------------------------------------------

// api(path, {method, json, form}): fetch with the session cookie. A 401 opens the login
// overlay ONCE (shared across concurrent calls) and, after a successful login, retries the
// same request. Any other error becomes an Error carrying the API's {"error": ...} reason.
export async function api(path, { method = "GET", json, form } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  }
  if (form) opts.body = form;
  const res = await fetch(path, opts);
  if (res.status === 401) {
    await requireLogin();
    return api(path, { method, json, form });
  }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    // The admin floors read err.status: 403 (grant gone), 409 (already exists), 410 (confirm expired).
    const err = new Error((data && data.error) || ("HTTP " + res.status));
    err.status = res.status;
    throw err;
  }
  return data;
}

// The current session or null (never opens the overlay: pages decide what to do).
export async function session() {
  try {
    const res = await fetch("/api/session", { credentials: "same-origin" });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export async function logout() {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
  location.reload();
}

// --- login overlay -------------------------------------------------------------------

let loginPromise = null;
export function requireLogin() {
  if (loginPromise) return loginPromise;
  loginPromise = new Promise((resolve) => {
    const error = el("div", { class: "error" });
    const user = el("input", { name: "user", autocomplete: "username", placeholder: "admin o demo", required: true });
    const pass = el("input", { name: "password", type: "password", autocomplete: "current-password", required: true });
    const submit = el("button", { class: "primary", type: "submit", text: "Entrar" });
    const form = el("form", {},
      el("h2", { text: "Entrar a LPQ_VISION" }),
      el("label", { text: "Usuario" }), user,
      el("label", { text: "Contraseña" }), pass,
      error,
      submit,
    );
    const overlay = el("div", { class: "overlay" }, el("div", { class: "card" }, form));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      submit.disabled = true;
      error.textContent = "";
      try {
        // Direct fetch here on purpose: api() would loop back into this overlay on a 401.
        const res = await fetch("/api/login", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user: user.value.trim(), password: pass.value }),
        });
        const data = await res.json();
        if (!res.ok) { error.textContent = data.error || "no se pudo entrar"; pass.value = ""; return; }
        overlay.remove();
        loginPromise = null;
        resolve(data);
      } catch {
        error.textContent = "sin conexión con la ventanilla";
      } finally {
        submit.disabled = false;
      }
    });
    document.body.append(overlay);
    user.focus();
  });
  return loginPromise;
}

// --- header --------------------------------------------------------------------------

const NAV = [["panel", "Panel"], ["review", "Review"], ["galeria", "Galería"], ["camara", "Cámara"], ["captura", "Captura"]];

// mountHeader(title, sess): the top bar with nav, the locked site chip for a site account,
// and Salir. sess may be null (no session yet): the chip is simply absent.
export function mountHeader(title, sess) {
  const current = location.pathname.replace(/^\//, "");
  // Fase 3: a logged-in account also sees the mostrador; only the admin sees the machine room.
  const items = NAV.concat(sess ? [["admin", "Mostrador"]] : [], sess && sess.admin ? [["maquinas", "Máquinas"]] : []);
  const nav = el("nav", {}, items.map(([path, label]) =>
    el("a", { href: "/" + path, class: current === path ? "current" : "", text: label })));
  const bar = el("header", { class: "topbar" }, el("h1", { text: title }), nav, el("div", { class: "spacer" }));
  if (sess) {
    bar.append(sess.admin
      ? el("span", { class: "chip", text: "admin · todos los sitios" })
      : el("span", { class: "chip locked", text: `🔒 sitio: ${sess.site}` }));
    bar.append(el("button", { text: "Salir", onclick: logout }));
  }
  document.body.prepend(bar);
  return bar;
}

// --- polling -------------------------------------------------------------------------

// poll(fn, ms): run fn now and every ms while the tab is visible; pause when hidden, resume
// (with an immediate run) when visible again. Returns stop(). A closed tab = zero requests.
export function poll(fn, ms = POLL_MS) {
  let timer = null;
  let stopped = false;
  const tick = async () => {
    if (stopped || document.visibilityState !== "visible") return;
    try { await fn(); } catch (err) { console.warn("poll:", err.message); }
  };
  const start = () => { if (timer === null && !stopped) { tick(); timer = setInterval(tick, ms); } };
  const pause = () => { if (timer !== null) { clearInterval(timer); timer = null; } };
  const onVis = () => { if (document.visibilityState === "visible") start(); else pause(); };
  document.addEventListener("visibilitychange", onVis);
  start();
  return () => { stopped = true; pause(); document.removeEventListener("visibilitychange", onVis); };
}
