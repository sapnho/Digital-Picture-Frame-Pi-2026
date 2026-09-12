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
    if (tab.dataset.view === "removed") loadRemoved();
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

  const FIT_WORDS = {
    cover: "cropped to fill the screen",
    contain: "whole, on a plain background",
    blur: "whole, on a blurred background",
    mat: "whole, in a mat",
  };
  const chips = [];
  if (cur.shown_as) chips.push(`Shown ${FIT_WORDS[cur.shown_as] || cur.shown_as}`);
  if (cur.caption) chips.push(cur.caption);
  // Pixel size, because "why was this cropped?" is nearly always answered by
  // comparing the picture's shape with the panel's.
  if (cur.width && cur.height) chips.push(`${fmt(cur.width)} × ${fmt(cur.height)}`);
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

  renderRestartNotice();

  const filters = state.filters || {};
  if (filterHasLanded(filters)) {
    previewing = false;
    if (gridWaiting) { gridWaiting = false; loadLibrary(); }
  }
  const pill = $("#filter-count");
  if (pill && !previewing) {
    const total = (state.library || {}).files || 0;
    pill.textContent = filters.active
      ? `${fmt(state.playlist_size)} of ${fmt(total)} pictures`
      : `all ${fmt(total)} pictures`;
    pill.className = filters.active ? "pill is-filtered" : "pill";
  }
  showFilter(filters);

  const lib = state.library || {};
  const gone = state.removed || {};
  $("#stats").innerHTML = [
    ["Pictures", fmt(lib.files)],
    ["Videos", fmt(lib.videos)],
    ...(gone.count ? [["Removed", fmt(gone.count)]] : []),
    ["In playlist", fmt(state.playlist_size)],
    ["Round", `${fmt(state.playlist_round)} · ${fmt(state.playlist_remaining)} left`],
    ["Times shown", lib.shown_max === lib.shown_min
      ? fmt(lib.shown_min) : `${fmt(lib.shown_min)}–${fmt(lib.shown_max)}`],
    ["Next in", state.paused ? "paused" : `${Math.round(state.next_change_in || 0)}s`],
    ["Frame rate", `${state.fps ?? 0}/s`],
    ["Uptime", duration(state.uptime)],
    ...healthTiles(state.health || {}),
  ].map(([k, v, cls]) => `<div${cls ? ` class="${cls}"` : ""}><dt>${k}</dt><dd>${v}</dd></div>`)
    .join("");
}

/* How the Pi itself is doing.  Every reading is optional: a machine that
   cannot measure its own temperature shows no temperature tile rather than a
   tile reading "—", and the row simply gets shorter. */
function healthTiles(h) {
  const tiles = [];
  if (h.cpu_temp != null)
    tiles.push(["CPU temperature", `${h.cpu_temp.toFixed(1)} °C`,
                h.cpu_temp >= 80 ? "warn" : ""]);
  if (h.cpu_percent != null) tiles.push(["CPU load", `${h.cpu_percent}%`]);
  if (h.memory_percent != null) tiles.push(["Memory used", `${h.memory_percent}%`]);
  if (h.disk_free != null)
    tiles.push(["Free space", `${(h.disk_free / 1073741824).toFixed(1)} GiB`,
                h.disk_free < 536870912 ? "warn" : ""]);
  // Worth its own tile only when there is something to say: an undervoltage
  // flag the firmware set at 3am is the whole reason for measuring at all.
  if (h.undervoltage || h.undervoltage_since_boot)
    tiles.push(["Power supply", h.undervoltage ? "undervoltage" : "dip recorded",
                "warn"]);
  return tiles;
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

/* --------------------------------------------------------------- filter */
/* Everything in this panel changes what the frame shows, not what the grid
   shows.  Typing counts (a preview, cheap, applies nothing); leaving a box or
   picking from a dropdown applies.  Applying mid-keystroke would send the
   picture on the wall somewhere new on every letter. */

const FILTER_FIELDS = () => document.querySelectorAll("[data-filter]");
let filterTimer = null;
let filterKnown = false;      // the option lists are fetched once
let previewing = false;       // an edit that has not been applied yet
let gridWaiting = false;      // a grid reload waiting for the frame to catch up

function filterPatch() {
  const patch = {};
  FILTER_FIELDS().forEach((el) => {
    patch[el.dataset.filter] = el.type === "checkbox" ? el.checked : el.value.trim();
  });
  return patch;
}

async function applyFilter(patch) {
  clearTimeout(filterTimer);
  previewing = false;
  // Both flags before the request, not after it: the frame usually publishes
  // the new state before the POST has even been read back here, and a flag
  // set after that would never be looked at again.  The grid is then
  // reloaded when the state says the filter has landed, rather than after a
  // guessed delay -- a race the slow case always loses.
  gridWaiting = true;
  // Narrowing the slideshow and then browsing the whole library is not what
  // anyone means; "show everything" puts the grid back too.
  $("#only-selected").checked = !(patch && patch.reset);
  await api("/api/filters", {
    method: "POST",
    body: JSON.stringify(patch || filterPatch()),
  });
}

/* Has the frame caught up with what the panel is asking for? */
function filterHasLanded(f) {
  const panel = filterPatch();
  const folderMatches = f.folder_choice === "(other)"
    ? true : (f.subfolder || "") === panel.folder;
  return folderMatches
    && (f.tags_text || "") === panel.tags
    && !!f.tags_match_all === panel.tags_match_all
    && (f.location_contains || "") === panel.location
    && (f.date_from_text || "") === panel.date_from
    && (f.date_to_text || "") === panel.date_to;
}

async function previewFilter() {
  const pill = $("#filter-count");
  // Nothing to preview when the frame is already showing exactly this: the
  // applied count is the better thing to be looking at.
  if (state && filterHasLanded(state.filters || {})) return;
  try {
    const { matching } = await api("/api/filters/preview", {
      method: "POST", body: JSON.stringify(filterPatch()),
    });
    previewing = true;
    pill.textContent = `${fmt(matching)} would be showing`;
    pill.className = "pill is-preview";
  } catch (_) { /* the applied count comes back on the next state anyway */ }
}

FILTER_FIELDS().forEach((el) => {
  el.addEventListener("change", () => applyFilter());
  if (el.type === "text") {
    el.addEventListener("input", () => {
      clearTimeout(filterTimer);
      filterTimer = setTimeout(previewFilter, 300);
    });
  }
});

document.querySelectorAll(".filter-quick [data-days]").forEach((button) => {
  button.onclick = () => {
    const from = new Date(Date.now() - button.dataset.days * 86400000);
    applyFilter({ date_from: from.toISOString().slice(0, 10), date_to: "" });
  };
});
$("#f-reset").onclick = () => applyFilter({ reset: true });
$("#only-selected").addEventListener("change", loadLibrary);

/* The panel's own contents: the folders, tags and places that exist. */
async function loadFilterOptions() {
  if (filterKnown) return;
  filterKnown = true;
  const body = await api("/api/filters");
  const fill = (selector, rows, asOption) => {
    const target = $(selector);
    rows.forEach((row) => {
      const option = document.createElement("option");
      option.value = row.name;
      if (asOption) option.textContent = `${row.name} (${fmt(row.count)})`;
      target.appendChild(option);
    });
  };
  fill("#f-folder", body.folders, true);
  fill("#f-tag-list", body.tags, false);
  fill("#f-place-list", body.locations, false);
  showFilter(body.filters);
}

/* Filters can also be changed from Home Assistant, a key press or another
   browser, so the panel follows the state document rather than only its own
   last edit.  A box being typed into is left alone. */
function showFilter(f) {
  if (!f) return;
  const set = (selector, value) => {
    const el = $(selector);
    if (el && document.activeElement !== el) el.value = value ?? "";
  };
  set("#f-folder", f.folder_choice === "(other)" ? "" : f.subfolder);
  set("#f-tags", f.tags_text);
  set("#f-place", f.location_contains);
  set("#f-from", f.date_from_text);
  set("#f-to", f.date_to_text);
  const all = $("#f-tags-all");
  if (all && document.activeElement !== all) all.checked = !!f.tags_match_all;
  $("#filter").classList.toggle("is-active", !!f.active);
}

/* -------------------------------------------------------------- library */
let libraryTimer = null;
$("#search").addEventListener("input", () => {
  clearTimeout(libraryTimer);
  libraryTimer = setTimeout(loadLibrary, 250);
});
$("#folder").addEventListener("change", loadLibrary);

async function loadLibrary() {
  loadFilterOptions();
  const q = $("#search").value.trim();
  const folder = $("#folder").value;
  const selected = $("#only-selected").checked ? "&selected=1" : "";
  let photos = await api(
    `/api/library/photos?limit=120&q=${encodeURIComponent(q)}${selected}`);
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

/* -------------------------------------------------------------- removed */
/* Nothing here deletes anything — the frame moves a picture aside and writes
   a line about it.  This page reads those lines back, which is the whole
   point: a folder of loose JPEGs cannot tell you why any of them is there. */
let removedTimer = null;
$("#removed-search").addEventListener("input", () => {
  clearTimeout(removedTimer);
  removedTimer = setTimeout(loadRemoved, 250);
});
$("#removed-restored").addEventListener("change", loadRemoved);

async function loadRemoved() {
  const withRestored = $("#removed-restored").checked;
  const query = $("#removed-search").value.trim().toLowerCase();
  let rows = await api(`/api/removed?include_restored=${withRestored}`);
  if (query) {
    rows = rows.filter((r) =>
      [r.basename, r.title, r.caption, r.location, r.folder, (r.tags || []).join(" ")]
        .join(" ").toLowerCase().includes(query));
  }
  const list = $("#removed-list");
  list.innerHTML = "";
  $("#removed-empty").hidden = rows.length > 0;
  $("#removed-empty").textContent = query
    ? "Nothing removed matches that."
    : "Nothing has been removed.";
  for (const r of rows) list.appendChild(removalRow(r));
}

function removalRow(r) {
  const row = document.createElement("article");
  row.className = "removal" + (r.restored_at ? " is-restored" : "");

  const shot = r.on_disk
    ? `<span class="shot"><img loading="lazy" alt=""
         src="/api/removed/${encodeURIComponent(r.stored_as)}/thumb"></span>`
    : `<span class="shot"><span class="gone">file gone</span></span>`;

  /* What it was, then when it went, then where it belongs. Tags and place are
     the two that make a picture recognisable months later, so they come before
     the technical detail. */
  const said = [r.title, r.location, (r.tags || []).join(", ")].filter(Boolean).join(" · ");
  const when = r.removed_iso ? when_text(r.removed_iso) : "";
  const by = r.source ? ` from the ${sourceName(r.source)}` : "";
  const taken = r.taken_iso ? `taken ${r.taken_iso.slice(0, 10)}` : "date unknown";

  row.innerHTML =
    shot +
    `<div class="what">` +
      `<strong>${escapeHtml(r.title || r.basename)}</strong>` +
      `<div class="line">${escapeHtml(said || taken)}${
        said ? ` · ${escapeHtml(taken)}` : ""}${r.is_video ? " · video" : ""}</div>` +
      `<div class="line">Removed ${escapeHtml(when)}${escapeHtml(by)}</div>` +
      `<div class="line from" title="${escapeHtml(r.original_path)}">${
        escapeHtml(r.original_path)}</div>` +
      (r.restored_at
        ? `<div class="line back">Put back ${escapeHtml(when_text(r.restored_iso))}${
            r.restored_to !== r.original_path && r.restored_to
              ? ` as ${escapeHtml(r.restored_to.split("/").pop())}`
              : ""}</div>`
        : "") +
    `</div>` +
    `<div class="actions"></div>`;

  const img = row.querySelector("img");
  if (img) img.onload = (e) => e.target.classList.add("ready");

  if (!r.restored_at && r.on_disk) {
    const button = document.createElement("button");
    button.className = "btn";
    button.textContent = "Put it back";
    button.title = `Move it back to ${r.original_path}`;
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = "Putting back…";
      try {
        const res = await api(
          `/api/removed/${encodeURIComponent(r.stored_as)}/restore`,
          { method: "POST" });
        /* It can land under a different name if something has taken the old
           one since; saying so beats a silent surprise in the folder. */
        if (res.moved) alert(`The original name was taken, so it went back as\n${res.path}`);
      } catch (err) {
        alert(`Could not put it back: ${err.message}`);
      }
      loadRemoved();
    };
    row.querySelector(".actions").appendChild(button);
  }
  return row;
}

function sourceName(source) {
  return { http: "web page", mqtt: "Home Assistant", keyboard: "keyboard",
           gpio: "button", touch: "screen", internal: "frame" }[source] || source;
}

/* "Yesterday at 14:03" beats an ISO timestamp for the question actually being
   asked, which is "was that me, last week?" */
function when_text(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (isNaN(then)) return iso;
  const days = Math.floor((Date.now() - then) / 86400000);
  const clock = then.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (days <= 0) return `today at ${clock}`;
  if (days === 1) return `yesterday at ${clock}`;
  if (days < 7) return `${days} days ago at ${clock}`;
  return then.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
}

/* ------------------------------------------------------------- settings */
/* The whole page is generated from /api/config/schema, which is generated from
   the config dataclasses.  A hand-written list here is how a settings page ends
   up missing exactly the settings nobody thought about. */

let SCHEMA = null;

async function loadSettings() {
  SCHEMA = await api("/api/config/schema");
  drawSettings();
}

function drawSettings() {
  const form = $("#settings");
  const query = ($("#settings-search")?.value || "").trim().toLowerCase();
  const showAdvanced = !!$("#settings-advanced")?.checked;
  form.innerHTML = "";
  let shown = 0;

  for (const section of SCHEMA.sections) {
    const visible = section.fields.filter((f) => {
      if (f.advanced && !showAdvanced && !query) return false;
      if (!query) return true;
      return (f.label + " " + f.key + " " + f.note).toLowerCase().includes(query);
    });
    if (!visible.length) continue;
    shown += visible.length;

    const block = document.createElement("section");
    block.className = "settings-group";
    block.innerHTML = `<h2>${escapeHtml(section.label)}</h2>` +
                      `<p class="muted">${escapeHtml(section.prose)}</p>` +
                      `<div class="form"></div>`;
    const grid = block.querySelector(".form");
    for (const f of visible) grid.appendChild(fieldControl(f));
    form.appendChild(block);
  }
  if (!shown) {
    form.innerHTML = `<p class="empty">Nothing matches “${escapeHtml(query)}”.</p>`;
  }
  form.querySelectorAll("[data-key]").forEach(wireControl);
  form.querySelectorAll("[data-pick-list]").forEach(wirePickList);
  form.querySelectorAll("[data-tiers]").forEach(wireTiers);
}

function optionList(name) {
  return (SCHEMA.options && SCHEMA.options[name]) || [];
}

function fieldControl(f) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const wide = ["pick", "pick-string", "json", "tiers"].includes(f.kind);
  if (wide) wrap.classList.add("field-wide");

  let control;
  switch (f.kind) {
    case "bool":
      control = `<input type="checkbox" data-key="${f.key}" data-kind="bool"` +
                `${f.value ? " checked" : ""}>`;
      break;
    case "int":
    case "number":
      control = `<input type="number" step="${f.kind === "int" ? 1 : "any"}" ` +
                `data-key="${f.key}" data-kind="${f.kind}" value="${f.value ?? ""}">`;
      break;
    case "secret":
      control = `<input type="password" data-key="${f.key}" data-kind="text" ` +
                `value="" autocomplete="new-password" ` +
                `placeholder="${f.is_set ? "set — type to replace" : "not set"}">`;
      break;
    case "select": {
      const opts = f.choices
        ? f.choices.map((c) => ({ name: String(c), label: String(c) }))
        : optionList(f.options);
      const current = String(f.value ?? "");
      control = `<select data-key="${f.key}" data-kind="select">` +
        opts.map((o) => `<option value="${escapeHtml(o.name)}"` +
          `${o.name === current ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("") +
        `</select>`;
      break;
    }
    case "pick":
      control = pickList(f.key, f.value || [], optionList(f.options), true, f.ordered);
      break;
    case "pick-string":
      control = pickList(f.key, String(f.value || "").toLowerCase().split(/[\s,]+/)
                                 .filter(Boolean), optionList(f.options), false, false);
      break;
    case "datalist": {
      // A real dropdown of what is actually in the library, but still a text
      // box: the setting matches any part of a path, so typing "2024" to catch
      // every folder from that year has to keep working.
      const id = `list-${f.key.replace(/\W/g, "-")}`;
      const opts = optionList(f.options);
      control = `<input type="text" list="${id}" data-key="${f.key}" ` +
                `data-kind="text" value="${escapeHtml(f.value ?? "")}" ` +
                `placeholder="${opts.length ? "any folder" : ""}">` +
                `<datalist id="${id}">` +
                opts.map((o) => `<option value="${escapeHtml(o.name)}">` +
                                `${escapeHtml(o.label)}</option>`).join("") +
                `</datalist>`;
      break;
    }
    case "tiers":
      control = tiersEditor(f);
      break;
    case "csv":
      control = `<input type="text" data-key="${f.key}" data-kind="csv" ` +
                `value="${escapeHtml((f.value || []).join(", "))}" ` +
                `placeholder="comma separated">`;
      break;
    case "numbers":
      control = `<input type="text" data-key="${f.key}" data-kind="numbers" ` +
                `value="${escapeHtml((f.value || []).join(", "))}" ` +
                `placeholder="${f.nullable ? "empty = automatic" : "comma separated"}">`;
      break;
    case "json":
      control = `<textarea rows="${jsonRows(f.value)}" data-key="${f.key}" ` +
                `data-kind="json" spellcheck="false">` +
                `${escapeHtml(JSON.stringify(f.value ?? null, null, 1))}</textarea>`;
      break;
    default:
      control = `<input type="text" data-key="${f.key}" data-kind="text" ` +
                `value="${escapeHtml(f.value ?? "")}">`;
  }

  const badge = f.live ? "" : `<span class="badge-restart" ` +
    `title="Saved now; the frame picks it up when it restarts">restart</span>`;
  const note = `<div class="hint">${markdownish(f.note)}</div>`;
  // A control nobody has met before needs its explanation before it, not
  // under it: by the time you have read the tiers box you have already
  // guessed wrong about what it wants.
  const explainFirst = f.kind === "tiers";
  wrap.innerHTML = `<label>${escapeHtml(f.label)}${badge}</label>` +
                   (explainFirst ? note : "") + control +
                   (explainFirst ? "" : note) +
                   `<div class="hint key">${f.key}</div>`;
  return wrap;
}

/* ---- address tiers -------------------------------------------------------
   geo.key_order is a list of lists, which as raw JSON is unreadable and as a
   row of checkboxes is wrong — the order inside a line is what does the work.
   One line per tier, keys separated by commas, with a live preview of what
   the current picture's own address would come out as. */
function tiersEditor(f) {
  const text = (f.value || []).map((tier) => (tier || []).join(", ")).join("\n");
  const keys = optionList(f.options);
  return `<div data-tiers data-key="${f.key}" data-kind="list">
    <textarea rows="${Math.max(3, (f.value || []).length)}" spellcheck="false"
              class="tiers-text">${escapeHtml(text)}</textarea>
    <div class="tiers-preview">
      <span class="tiers-out">…</span>
      <span class="tiers-source muted"></span>
    </div>
    <details class="tiers-keys">
      <summary>Keys you can use</summary>
      <p class="muted">Click one to add it to the last line. Not every address
        has every key — that is why a line lists alternatives.</p>
      <div class="tiers-chips">${keys.map((k) =>
        `<button type="button" data-add="${k.name}" title="${escapeHtml(k.label)}"
                 class="${k.available ? "have" : ""}">${k.name}</button>`).join("")}</div>
    </details>
  </div>`;
}

function parseTiers(text) {
  return text.split("\n")
    .map((line) => line.split(",").map((k) => k.trim().toLowerCase()).filter(Boolean))
    .filter((tier) => tier.length);
}

function wireTiers(box) {
  const area = box.querySelector(".tiers-text");
  const out = box.querySelector(".tiers-out");
  const source = box.querySelector(".tiers-source");
  let timer = null;

  const preview = async () => {
    try {
      const res = await fetch("/api/geo/preview", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ detail: "custom", key_order: parseTiers(area.value) }),
      });
      if (!res.ok) return;
      const data = await res.json();
      out.textContent = data.text || "(nothing — none of these keys is in the address)";
      source.textContent = `from ${data.source}`;
      // Mark the keys this particular address actually has, so the chips teach
      // rather than just list.
      const have = new Set(data.available || []);
      box.querySelectorAll("[data-add]").forEach((chip) =>
        chip.classList.toggle("have", have.has(chip.dataset.add)));
    } catch (err) { /* the frame is busy or down; the preview is optional */ }
  };

  const commit = () => {
    const value = parseTiers(area.value);
    send("set_config", { key: box.dataset.key, value });
    $("#saved").textContent =
      `${box.dataset.key} = ${value.length} tier${value.length === 1 ? "" : "s"}` +
      "  (not yet written to the config file)";
  };

  area.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(preview, 250);
  };
  area.onchange = commit;
  box.onclick = (e) => {
    const chip = e.target.closest("[data-add]");
    if (!chip) return;
    const lines = area.value.split("\n");
    const last = lines.length - 1;
    lines[last] = lines[last].trim()
      ? `${lines[last].replace(/,\s*$/, "")}, ${chip.dataset.add}`
      : chip.dataset.add;
    area.value = lines.join("\n");
    commit();
    preview();
  };
  preview();
}

function jsonRows(value) {
  return Math.min(14, Math.max(3, JSON.stringify(value ?? null, null, 1).split("\n").length));
}

/* Only backticks and ** ** — the notes are ours, not user input, but they are
   still escaped first so a stray < in a strftime example cannot inject. */
function markdownish(text) {
  return escapeHtml(text || "")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function wireControl(el) {
  if (el.dataset.kind === "list") return;          // handled by wirePickList
  const commit = () => {
    let value;
    switch (el.dataset.kind) {
      case "bool": value = el.checked; break;
      case "int": value = Math.round(Number(el.value)); break;
      case "number": value = Number(el.value); break;
      case "csv":
        value = el.value.split(",").map((s) => s.trim()).filter(Boolean);
        break;
      case "numbers": {
        const parts = el.value.split(",").map((s) => s.trim()).filter(Boolean);
        value = parts.length ? parts.map(Number) : null;
        if (value && value.some((n) => Number.isNaN(n))) return note(el, "not a number");
        break;
      }
      case "json":
        try {
          value = JSON.parse(el.value);
        } catch (err) {
          return note(el, `not valid JSON: ${err.message}`);
        }
        break;
      default: value = el.value;
    }
    if (el.type === "password" && el.value === "") return;   // unchanged
    el.classList.remove("bad");
    send("set_config", { key: el.dataset.key, value });
    $("#saved").textContent =
      `${el.dataset.key} = ${el.type === "password" ? "••••••••" : JSON.stringify(value)}` +
      "  (not yet written to the config file)";
  };
  el.onchange = commit;
}

function note(el, message) {
  el.classList.add("bad");
  $("#saved").textContent = `${el.dataset.key}: ${message} — not saved`;
}

/* ---- tick-and-reorder list ----------------------------------------------
   Used for the caption elements, where order is the whole point ("Place ·
   Date" and "Date · Place" are different captions), and for the mat styles
   and transitions, where it is not: ticking several means "choose between
   these".  `asArray` sends a JSON array; otherwise a space-separated string,
   which is the form picframe used and the config still accepts. */
function pickList(key, chosen, available, asArray, reorder) {
  const known = (available.length ? available : chosen)
    .map((f) => (typeof f === "string" ? { name: f, label: f } : f));
  const byName = Object.fromEntries(known.map((f) => [f.name, f.label]));
  const rows = [
    ...chosen.filter((n) => n in byName).map((n) => ({ name: n, on: true })),
    ...known.filter((f) => !chosen.includes(f.name)).map((f) => ({ name: f.name, on: false })),
  ].map(({ name, on }) => `
    <li data-name="${escapeHtml(name)}" class="${on ? "on" : ""}">
      <label><input type="checkbox" ${on ? "checked" : ""}>
        <span>${escapeHtml(byName[name])}</span></label>
      ${reorder ? `<button type="button" data-move="-1" title="Move up">▲</button>
      <button type="button" data-move="1" title="Move down">▼</button>` : ""}
    </li>`).join("");
  return `<ol class="caption-list" data-pick-list data-key="${key}" data-kind="list"
              data-array="${asArray ? 1 : 0}"${reorder ? " data-ordered" : ""}>${rows}</ol>`;
}

function wirePickList(list) {
  const asArray = list.dataset.array === "1";
  const commit = () => {
    const names = [...list.querySelectorAll("li")]
      .filter((li) => li.querySelector("input").checked)
      .map((li) => li.dataset.name);
    // A mat style is a string, as picframe had it; the rest are real lists.
    const value = asArray ? names : (names.join(" ") || "single");
    send("set_config", { key: list.dataset.key, value });
    $("#saved").textContent =
      `${list.dataset.key} = ${asArray ? `[${names.join(", ")}]` : value}` +
      "  (not yet written to the config file)";
  };
  list.onclick = (e) => {
    const move = e.target.closest("[data-move]");
    if (!move) return;
    const li = move.closest("li");
    const up = Number(move.dataset.move) < 0;
    const sibling = up ? li.previousElementSibling : li.nextElementSibling;
    if (!sibling) return;
    up ? list.insertBefore(li, sibling) : list.insertBefore(sibling, li);
    commit();
  };
  list.onchange = (e) => {
    if (e.target.type !== "checkbox") return;
    e.target.closest("li").classList.toggle("on", e.target.checked);
    commit();
  };
}

/* ------------------------------------------------------------------ theme */
/* The choice lives in this browser, not in the frame's configuration: the
   phone in a dark room and the laptop at the desk are looking at the same
   frame and want different answers. */
const themePicker = $("#theme");
try {
  themePicker.value = localStorage.getItem("picframe3-theme") || "auto";
} catch (err) { themePicker.value = "auto"; }
themePicker.onchange = () => {
  const choice = themePicker.value;
  if (choice === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = choice;
  try { localStorage.setItem("picframe3-theme", choice); } catch (err) { /* private window */ }
};

$("#settings-search").oninput = () => { if (SCHEMA) drawSettings(); };
$("#settings-advanced").onchange = () => { if (SCHEMA) drawSettings(); };

$("#save").onclick = async () => {
  await api("/api/config?persist=true", { method: "PATCH", body: "{}" });
  $("#saved").textContent = "Saved to the config file.";
};
$("#reload").onclick = async () => {
  await send("reload");
  await loadSettings();
  $("#saved").textContent = "Reloaded from the config file.";
};
$("#restart").onclick = () => restartFrame();
$("#restart-now").onclick = () => restartFrame();

/* ------------------------------------------------------------------ restart
   Several settings — the MQTT broker, the HTTP port, which folders are
   indexed — can only be picked up by a fresh process.  Saving first is the
   default, because a restart would otherwise throw away the very changes it
   is being asked to apply. */
let restarting = false;

async function restartFrame() {
  if (restarting) return;
  const unsaved = state && state.unsaved_changes;
  const question = unsaved
    ? "Save the settings and restart the frame?\n\nThe picture goes dark for a few seconds."
    : "Restart the frame?\n\nThe picture goes dark for a few seconds.";
  if (!confirm(question)) return;

  restarting = true;
  for (const id of ["#restart", "#restart-now"]) {
    $(id).disabled = true;
    $(id).textContent = "Restarting…";
  }
  $("#saved").textContent = "Restarting the frame…";
  try {
    // The frame stops answering as soon as it acts on this, so a failed fetch
    // here is the expected case, not an error.
    await fetch("/api/restart?save=true", { method: "POST" });
  } catch (err) { /* it went down mid-reply */ }
  waitForTheFrame();
}

async function waitForTheFrame(attempt = 0) {
  if (attempt > 60) {                     // two minutes
    $("#saved").textContent =
      "The frame has not come back. Check: journalctl -u picframe3@pi -n 40";
    restarting = false;
    $("#restart").disabled = false;
    $("#restart").textContent = "Restart the frame";
    $("#restart-now").disabled = false;
    $("#restart-now").textContent = "Save & restart";
    return;
  }
  await new Promise((done) => setTimeout(done, 2000));
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    if (res.ok) return location.reload();
  } catch (err) { /* still down */ }
  $("#saved").textContent = `Restarting the frame… (${(attempt + 1) * 2}s)`;
  waitForTheFrame(attempt + 1);
}

function renderRestartNotice() {
  const notice = $("#restart-notice");
  if (!state) return;
  const pending = state.restart_required || [];
  const unsaved = !!state.unsaved_changes;
  if (!pending.length && !unsaved) { notice.hidden = true; return; }
  notice.hidden = false;
  if (pending.length) {
    $("#restart-headline").textContent = pending.length === 1
      ? "One setting needs a restart"
      : `${pending.length} settings need a restart`;
    $("#restart-detail").textContent =
      pending.join(", ") +
      (unsaved ? " — these will be saved to the config file first." : "");
    $("#restart-now").hidden = false;
  } else {
    $("#restart-headline").textContent = "Unsaved changes";
    $("#restart-detail").textContent =
      "Everything you changed is live on the frame, but not yet in the config " +
      "file — it would go back to the old values on the next restart.";
    $("#restart-now").hidden = true;
  }
}

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
