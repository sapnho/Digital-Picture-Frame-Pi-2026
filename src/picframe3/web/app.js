/* picframe3 web UI — no build step, no framework, no external requests. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, options) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.status === 204 ? null : res.json();
};
const send = (action, payload = {}) =>
  api("/api/command", { method: "POST", body: JSON.stringify({ action, ...payload }) });

let state = null;
let currentId = null;
let dragging = false;

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("is-active"));
    tab.classList.add("is-active");
    $(`#view-${tab.dataset.view}`).classList.add("is-active");
    if (tab.dataset.view === "library") loadLibrary();
    if (tab.dataset.view === "settings") loadSettings();
  });
});

/* ------------------------------------------------------------- controls */
$("#prev").onclick = () => send("previous");
$("#next").onclick = () => send("next");
$("#pause").onclick = () => send("toggle_pause");
$("#power").onclick = () => send("display_toggle");
$("#del").onclick = async () => {
  if (confirm("Move this picture out of the library?")) await send("delete");
};
$("#rescan").onclick = () => send("rescan");

/* The frame has no desktop and so no screenshot tool; it reads its own
   framebuffer back instead. Useful when something looks wrong on the wall. */
let liveUrl = null;
$("#shot").onclick = async () => {
  const button = $("#shot");
  if (liveUrl) return showThumbnail();
  button.disabled = true;
  button.textContent = "Capturing…";
  try {
    const res = await fetch("/api/screenshot", { cache: "no-store" });
    if (!res.ok) throw new Error(await res.text());
    liveUrl = URL.createObjectURL(await res.blob());
    const img = $("#preview");
    img.src = liveUrl;
    img.hidden = false;
    $("#preview-empty").hidden = true;
    $("#live-badge").hidden = false;
    button.textContent = "Show thumbnail";
  } catch (err) {
    button.textContent = "Screenshot failed";
    setTimeout(() => (button.textContent = "Screenshot"), 2500);
  } finally {
    button.disabled = false;
  }
};

function showThumbnail() {
  if (liveUrl) URL.revokeObjectURL(liveUrl);
  liveUrl = null;
  currentId = null;              // force render() to reload the thumbnail
  $("#live-badge").hidden = true;
  $("#shot").textContent = "Screenshot";
  if (state) render(state);
}

const brightness = $("#brightness");
brightness.oninput = () => ($("#brightness-out").textContent = `${brightness.value}%`);
brightness.onchange = () => send("brightness", { value: brightness.value / 100 });

const interval = $("#interval");
interval.oninput = () => ($("#interval-out").textContent = interval.value);
interval.onchange = () =>
  send("set_config", { key: "slideshow.interval", value: Number(interval.value) });

document.addEventListener("keydown", (e) => {
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  const map = { ArrowRight: "next", ArrowLeft: "previous", " ": "toggle_pause", p: "toggle_pause" };
  if (map[e.key]) { e.preventDefault(); send(map[e.key]); }
});

/* ---------------------------------------------------------------- state */
function render(next) {
  state = next;
  $("#live").classList.toggle("on", !!state.running);
  const d = state.display || {};
  $("#display-info").textContent = d.width
    ? `${d.width}×${d.height} · ${d.refresh_hz}Hz · ${d.backend}`
    : "";

  const cur = state.current || {};
  if (liveUrl) {
    // a captured frame is on display; leave it alone until dismissed
  } else if (cur.id && cur.id !== currentId) {
    currentId = cur.id;
    const img = $("#preview");
    img.src = `/api/library/photo/${cur.id}/thumb`;
    img.hidden = false;
    $("#preview-empty").hidden = true;
  } else if (!cur.id && !liveUrl) {
    $("#preview").hidden = true;
    $("#preview-empty").hidden = false;
    currentId = null;
  }

  $("#title").textContent = cur.title || cur.basename || "—";
  const bits = [];
  if (cur.taken_at) bits.push(new Date(cur.taken_at * 1000).toLocaleDateString(undefined,
    { day: "numeric", month: "long", year: "numeric" }));
  if (cur.location) bits.push(cur.location);
  if (cur.folder) bits.push(cur.folder.split("/").pop());
  $("#subtitle").textContent = bits.join("  ·  ");

  const chips = [];
  if (cur.caption) chips.push(cur.caption);
  if (cur.model) chips.push([cur.make, cur.model].filter(Boolean).join(" ").replace(/^(\S+) \1/, "$1"));
  if (cur.f_number) chips.push(`f/${cur.f_number}`);
  if (cur.exposure_time) chips.push(cur.exposure_time);
  if (cur.iso) chips.push(`ISO ${cur.iso}`);
  if (cur.focal_length) chips.push(`${cur.focal_length}mm`);
  (cur.tags || []).forEach((t) => chips.push(`#${t}`));
  $("#exif").innerHTML = chips.map((c) => `<li>${escapeHtml(c)}</li>`).join("");

  $("#pause").textContent = state.paused ? "Resume" : "Pause";
  $("#power").textContent = state.display_on ? "Screen off" : "Screen on";

  if (!dragging) {
    brightness.value = Math.round((state.brightness ?? 1) * 100);
    $("#brightness-out").textContent = `${brightness.value}%`;
    interval.value = Math.round(state.interval ?? 180);
    $("#interval-out").textContent = interval.value;
  }

  const pct = state.interval > 0
    ? 100 * (1 - Math.min(1, (state.next_change_in || 0) / state.interval)) : 0;
  $("#progress-bar").style.width = `${pct}%`;

  const lib = state.library || {};
  $("#stats").innerHTML = [
    ["Pictures", fmt(lib.files)],
    ["Videos", fmt(lib.videos)],
    ["In playlist", fmt(state.playlist_size)],
    ["Next in", state.paused ? "paused" : `${Math.round(state.next_change_in || 0)}s`],
    ["Frame rate", `${state.fps ?? 0}/s`],
    ["Uptime", duration(state.uptime)],
  ].map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
}

[brightness, interval].forEach((el) => {
  el.addEventListener("pointerdown", () => (dragging = true));
  el.addEventListener("pointerup", () => setTimeout(() => (dragging = false), 400));
});

/* ------------------------------------------------------------ streaming */
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (_) {} };
  es.onerror = () => {
    $("#live").classList.remove("on");
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
api("/api/state").then(render).catch(() => {});
setInterval(() => { if (state && !state.paused) {
  state.next_change_in = Math.max(0, (state.next_change_in || 0) - 1);
  const pct = state.interval > 0 ? 100 * (1 - state.next_change_in / state.interval) : 0;
  $("#progress-bar").style.width = `${pct}%`;
} }, 1000);

/* -------------------------------------------------------------- library */
let libraryTimer = null;
$("#search").addEventListener("input", () => {
  clearTimeout(libraryTimer);
  libraryTimer = setTimeout(loadLibrary, 250);
});
$("#folder").addEventListener("change", loadLibrary);

async function loadLibrary() {
  const q = $("#search").value.trim();
  const folder = $("#folder").value;
  let photos = await api(`/api/library/photos?limit=120&q=${encodeURIComponent(q)}`);
  if (folder) photos = photos.filter((p) => p.folder === folder);
  const grid = $("#grid");
  grid.innerHTML = "";
  $("#grid-empty").hidden = photos.length > 0;
  for (const p of photos) {
    const card = document.createElement("figure");
    card.className = "card";
    card.title = p.path;
    card.innerHTML =
      `<img loading="lazy" src="/api/library/photo/${p.id}/thumb" alt="">` +
      (p.is_video ? `<span class="badge">video</span>` : "") +
      `<figcaption>${escapeHtml(p.title || p.basename)}</figcaption>`;
    card.querySelector("img").onload = (e) => e.target.classList.add("ready");
    card.onclick = () => send("jump", { id: p.id });
    grid.appendChild(card);
  }
  if ($("#folder").options.length <= 1) {
    const folders = await api("/api/library/folders");
    folders.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.path;
      opt.textContent = `${f.path.split("/").slice(-2).join("/")} (${f.count})`;
      $("#folder").appendChild(opt);
    });
  }
}

/* ------------------------------------------------------------- settings */
const FIELDS = [
  ["slideshow.interval", "Seconds per picture", "number"],
  ["slideshow.transition", "Transition", "transition"],
  ["slideshow.transition_time", "Transition length (s)", "number"],
  ["slideshow.order", "Order", "select", ["shuffle", "random", "date_desc", "date_asc", "name", "folder", "recent", "least_played"]],
  ["slideshow.kenburns", "Ken Burns pan & zoom", "bool"],
  ["slideshow.portrait_pairs", "Pair portrait photos", "bool"],
  ["viewer.fit", "Fit", "select", ["auto", "cover", "contain", "blur", "mat"]],
  ["viewer.mat_style", "Mat style", "select", ["single", "double", "bevel", "float", "float_shadow", "polaroid", "random"]],
  ["viewer.show_clock", "Show clock", "bool"],
  ["viewer.clock_format", "Clock format", "text"],
  ["viewer.text_seconds", "Caption seconds", "number"],
  ["viewer.text_size", "Caption size", "number"],
  ["display.brightness", "Brightness", "number"],
  ["library.subfolder", "Only this subfolder", "text"],
];

async function loadSettings() {
  const cfg = await api("/api/config");
  const transitions = await api("/api/transitions");
  const form = $("#settings");
  form.innerHTML = "";
  for (const [key, label, kind, choices] of FIELDS) {
    const value = key.split(".").reduce((o, k) => (o ?? {})[k], cfg);
    const wrap = document.createElement("div");
    wrap.className = "field";
    let control;
    if (kind === "bool") {
      control = `<input type="checkbox" data-key="${key}" ${value ? "checked" : ""}>`;
    } else if (kind === "select" || kind === "transition") {
      const opts = (kind === "transition" ? ["random", ...transitions] : choices)
        .map((c) => `<option ${c === value ? "selected" : ""}>${c}</option>`).join("");
      control = `<select data-key="${key}">${opts}</select>`;
    } else {
      control = `<input type="${kind}" step="any" data-key="${key}" value="${value ?? ""}">`;
    }
    wrap.innerHTML = `<label>${label}</label>${control}<div class="hint">${key}</div>`;
    form.appendChild(wrap);
  }
  form.querySelectorAll("[data-key]").forEach((el) => {
    el.onchange = () => {
      const value = el.type === "checkbox" ? el.checked
        : el.type === "number" ? Number(el.value) : el.value;
      send("set_config", { key: el.dataset.key, value });
      $("#saved").textContent = `${el.dataset.key} = ${value}  (not yet written to disk)`;
    };
  });
}

$("#save").onclick = async () => {
  await api("/api/config?persist=true", { method: "PATCH", body: "{}" });
  $("#saved").textContent = "Saved to the config file.";
};
$("#reload").onclick = async () => {
  await send("reload");
  await loadSettings();
  $("#saved").textContent = "Reloaded from the config file.";
};

/* --------------------------------------------------------------- utils */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(n) { return (n ?? 0).toLocaleString(); }
function duration(sec) {
  sec = Math.round(sec || 0);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
