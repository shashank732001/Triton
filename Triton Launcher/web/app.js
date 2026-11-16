"use strict";

// The C# host talks to us through WebView2's message channel. Opened in a normal
// browser (design preview) there is no host, so we fall back to mock data.
const host = window.chrome && window.chrome.webview;

const DEFAULT_CONFIG = {
  name: "",
  city: "",
  country: "",
  temperatureUnit: "celsius",
  clock24h: true,
  wallpaper: "auto",
  livePalette: "aurora",
  ambientAfterSeconds: 120,
  lowPower: false,
  apps: [
    { id: "files", name: "Files" },
    { id: "chrome", name: "Chrome" },
    { id: "brave", name: "Brave" },
    { id: "settings", name: "Settings" },
  ],
};

let config = DEFAULT_CONFIG;
const state = { wallpapers: [], wallpaperKey: null, autostart: true, ambient: false, weatherAt: 0, volumeTouchedAt: 0, voiceOn: false, voiceTouchedAt: 0 };

const $ = sel => document.querySelector(sel);
const pad = n => String(n).padStart(2, "0");
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function send(cmd, data = {}) {
  const msg = Object.assign({ cmd }, data);
  if (host) host.postMessage(msg);
  else console.debug("[host]", msg);
}

async function loadConfig() {
  try {
    const res = await fetch("/config.json", { cache: "no-store" });
    if (res.ok) config = Object.assign({}, DEFAULT_CONFIG, await res.json());
  } catch { /* preview mode or missing file: keep defaults */ }
}

/* ============================== Icons ============================== */

const APP_ICONS = {
  files: { brand: "#f5b400", svg: `<svg viewBox="0 0 64 64"><path d="M6 17a6 6 0 0 1 6-6h12.5l5.5 5.5h22a6 6 0 0 1 6 6V26H6z" fill="#E09A00"/><path d="M6 24h52v24a6 6 0 0 1-6 6H12a6 6 0 0 1-6-6z" fill="#FFC531"/><path d="M6 24h52v5H6z" fill="#FFD966"/></svg>` },
  chrome: { brand: "#4285f4", svg: `<svg viewBox="0 0 64 64"><path d="M32 32 9.48 19A26 26 0 0 1 54.52 19z" fill="#EA4335"/><path d="M32 32 54.52 19A26 26 0 0 1 32 58z" fill="#FBBC04"/><path d="M32 32 32 58A26 26 0 0 1 9.48 19z" fill="#34A853"/><circle cx="32" cy="32" r="12.5" fill="#fff"/><circle cx="32" cy="32" r="10" fill="#4285F4"/></svg>` },
  brave: { brand: "#fb542b", svg: `<svg viewBox="0 0 64 64"><path d="M32 5l13.5 3.6 4.6 5.6-2.6 4.2 1.6 5.2-6.4 20.2L32 59l-10.7-15.2-6.4-20.2 1.6-5.2-2.6-4.2 4.6-5.6z" fill="#FB542B"/><path d="M32 5l13.5 3.6 4.6 5.6-2.6 4.2 1.6 5.2-6.4 20.2L32 59z" fill="#E8431E"/><path d="M32 29.5l6.2 4.1-2.6 8.6L32 45l-3.6-2.8-2.6-8.6z" fill="#fff"/><path d="M23 24l5.5 1.5M41 24l-5.5 1.5" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>` },
  settings: { brand: "#6b8cff", svg: `<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="19.5" fill="none" stroke="#D5DBE8" stroke-width="9" stroke-dasharray="7.66 7.66"/><circle cx="32" cy="32" r="17" fill="#D5DBE8"/><circle cx="32" cy="32" r="17" fill="none" stroke="#B9C2D6" stroke-width="1.5"/><circle cx="32" cy="32" r="8" fill="#3D6BEA"/></svg>` },
  youtube: { brand: "#ff0033", svg: `<svg viewBox="0 0 64 64"><rect x="4" y="13" width="56" height="38" rx="12" fill="#FF0033"/><path d="M27 23.5v17L41.5 32z" fill="#fff"/></svg>` },
};

// Built-in last tile: every open/minimized app window, to bring back what went behind the launcher
const WINDOWS_TILE = {
  brand: "#38d1c4",
  svg: `<svg viewBox="0 0 64 64"><rect x="17" y="8" width="40" height="30" rx="6" fill="#2A9D94"/><rect x="12" y="15" width="40" height="30" rx="6" fill="#38D1C4"/><rect x="7" y="22" width="40" height="32" rx="6" fill="#E9FFFC"/><path d="M7 28a6 6 0 0 1 6-6h28a6 6 0 0 1 6 6v1H7z" fill="#9FEDE5"/><circle cx="12.5" cy="25.5" r="1.4" fill="#2A9D94"/><circle cx="17" cy="25.5" r="1.4" fill="#2A9D94"/></svg>`,
};

const ICON = {
  wifi(level) {
    const o = n => (level >= n ? 1 : 0.28);
    return `<svg viewBox="0 0 24 24"><path d="M2.5 9.2a14 14 0 0 1 19 0" opacity="${o(3)}"/><path d="M5.8 12.7a9.4 9.4 0 0 1 12.4 0" opacity="${o(2)}"/><path d="M9.1 16.2a4.6 4.6 0 0 1 5.8 0" opacity="${o(1)}"/><circle cx="12" cy="19.6" r="1.3" fill="currentColor" stroke="none"/>${level === 0 ? '<path d="M4 4l16 16"/>' : ""}</svg>`;
  },
  ethernet: `<svg viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="12" rx="2"/><path d="M8 16v4h8v-4M9 8v3M12 8v3M15 8v3"/></svg>`,
  mic(on) {
    const slash = on ? "" : '<path d="M4 4l16 16"/>';
    return `<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>${slash}</svg>`;
  },
  volume(level, muted) {
    const waves = muted ? '<path d="M16 9.5l5 5M21 9.5l-5 5"/>'
      : (level > 0 ? '<path d="M16 9a4.5 4.5 0 0 1 0 6"/>' : "") + (level > 50 ? '<path d="M18.6 6.4a8.2 8.2 0 0 1 0 11.2"/>' : "");
    return `<svg viewBox="0 0 24 24"><path d="M3.5 9.5h3.8L12 5.5v13l-4.7-4H3.5z"/>${waves}</svg>`;
  },
};

// Weather icons: solid shapes, no gradients/ids so they can be repeated freely.
const W = {
  sun: `<circle cx="32" cy="32" r="12" fill="#FFC53D"/><g stroke="#FFC53D" stroke-width="4" stroke-linecap="round"><path d="M32 6v6M32 52v6M6 32h6M52 32h6M13.6 13.6l4.2 4.2M46.2 46.2l4.2 4.2M13.6 50.4l4.2-4.2M46.2 17.8l4.2-4.2"/></g>`,
  moon: `<path d="M41 44A18 18 0 0 1 26 13a19 19 0 1 0 25 25 18 18 0 0 1-10 6z" fill="#E6E3FF"/>`,
  cloud: (x = 0, y = 0, c = "#F3F6FB") => `<path transform="translate(${x} ${y})" d="M19 51h27a11 11 0 0 0 1.2-21.9A15 15 0 0 0 18.6 33 9 9 0 0 0 19 51z" fill="${c}"/>`,
  drops: `<g stroke="#6CB8FF" stroke-width="3.4" stroke-linecap="round"><path d="M22 55l-3 6M33 55l-3 6M44 55l-3 6"/></g>`,
  flakes: `<g fill="#fff"><circle cx="21" cy="58" r="2.6"/><circle cx="32" cy="61" r="2.6"/><circle cx="43" cy="58" r="2.6"/></g>`,
  bolt: `<path d="M34 46l-8 11h6l-3 7 10-12h-6l3-6z" fill="#FFD43B"/>`,
  fog: `<g stroke="#DDE3EC" stroke-width="3.6" stroke-linecap="round"><path d="M12 54h40M18 61h28"/></g>`,
};
function weatherSvg(kind, isDay) {
  const body = {
    clear: isDay ? W.sun : W.moon,
    partly: `<g transform="translate(-6 -8) scale(.85)">${isDay ? W.sun : W.moon}</g>${W.cloud(2, 0)}`,
    cloud: `${W.cloud(-8, -6, "#B9C3D3")}${W.cloud(2, 0)}`,
    fog: `${W.cloud(0, -8)}${W.fog}`,
    rain: `${W.cloud(0, -8)}${W.drops}`,
    snow: `${W.cloud(0, -8)}${W.flakes}`,
    storm: `${W.cloud(0, -10, "#C9D1DE")}${W.bolt}`,
  }[kind];
  return `<svg viewBox="0 0 64 64">${body}</svg>`;
}

/* ============================== Clock ============================== */

let lastMinute = -1;
function renderClock() {
  const d = new Date();
  if (d.getMinutes() === lastMinute) return;
  lastMinute = d.getMinutes();

  let h = d.getHours();
  let suffix = "";
  if (!config.clock24h) { suffix = h < 12 ? "AM" : "PM"; h = h % 12 || 12; } else h = pad(h);
  $("#time").innerHTML = `${h}:${pad(d.getMinutes())}${suffix ? `<small>${suffix}</small>` : ""}`;
  $("#date").textContent = d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });

  const hour = d.getHours();
  const part = hour < 5 ? "Good night" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  $("#greet").textContent = config.name ? `${part}, ${config.name}` : part;
}

/* ============================== Apps ============================== */

function renderApps() {
  const dock = $("#apps");
  dock.innerHTML = "";
  for (const [i, app] of config.apps.entries()) {
    const icon = APP_ICONS[app.id];
    const tile = document.createElement("button");
    tile.className = "tile";
    tile.dataset.id = app.id;
    tile.style.setProperty("--brand", icon ? icon.brand : "#7c8cff");
    tile.style.setProperty("--i", i);   // stagger for the reveal animation
    tile.innerHTML = `<span class="tile-face">${icon ? icon.svg : `<span class="letter">${esc((app.name || "?")[0])}</span>`}</span><span class="tile-label">${esc(app.name)}</span>`;
    tile.addEventListener("click", () => launch(app, tile));
    tile.addEventListener("pointerenter", () => tile.focus({ preventScroll: true }));
    dock.append(tile);
  }

  const tile = document.createElement("button");
  tile.className = "tile";
  tile.id = "windowsTile";
  tile.style.setProperty("--brand", WINDOWS_TILE.brand);
  tile.style.setProperty("--i", config.apps.length);
  tile.innerHTML = `<span class="tile-face">${WINDOWS_TILE.svg}<span class="tile-badge" id="windowsBadge" hidden></span></span><span class="tile-label">Open apps</span>`;
  tile.addEventListener("click", () => openSwitcher());
  tile.addEventListener("pointerenter", () => tile.focus({ preventScroll: true }));
  dock.append(tile);
}

/* ============================== Open apps (window switcher) ============================== */

function renderWindowCount(n) {
  const badge = $("#windowsBadge");
  badge.hidden = !n;
  badge.textContent = n > 9 ? "9+" : String(n);
}

function openSwitcher() {
  if (openPopover && openPopover.id === "pop-windows") return;
  $("#winGrid").innerHTML = `<p class="win-empty">Looking for open apps…</p>`;
  togglePopover("pop-windows", $("#windowsTile"));
  send("windows");
  if (!host) renderWindows([   // preview without the host
    { id: "1", app: "Google Chrome", title: "YouTube - Google Chrome", minimized: true },
    { id: "2", app: "File Explorer", title: "Downloads", minimized: false },
  ]);
}

function renderWindows(items) {
  const pop = $("#pop-windows");
  if (pop.hidden) return;
  $("#winCount").textContent = items.length || "";
  renderWindowCount(items.length);
  const grid = $("#winGrid");
  if (!items.length) {
    grid.innerHTML = `<p class="win-empty">Nothing else is open right now.</p>`;
    pop.focus();
    return;
  }
  grid.innerHTML = "";
  for (const w of items) {
    const card = document.createElement("button");
    card.className = "win-card";
    const name = w.app || w.title;
    const icon = w.icon ? `<img src="${w.icon}" alt="">` : `<span class="letter">${esc((name || "?")[0])}</span>`;
    const sub = w.title && w.title !== name ? `<small>${esc(w.title)}</small>` : "";
    card.innerHTML = `<span class="win-icon">${icon}</span><span class="win-text"><b>${esc(name)}</b>${sub}</span>` +
      (w.minimized ? `<span class="win-tag">Minimized</span>` : "");
    card.addEventListener("click", () => {
      send("focusWindow", { id: w.id });
      closePopover();
      if (!host) toast(`Would switch to ${name} (preview)`);
    });
    grid.append(card);
  }
  grid.firstElementChild.focus();
}

function launch(app, tile) {
  send("launch", { id: app.id });
  if (tile) { tile.classList.remove("launching"); void tile.offsetWidth; tile.classList.add("launching"); }
  if (!host) toast(`Opening ${app.name}…`);
}

/* ============================== Status bar ============================== */

function renderWifi(w) {
  let icon, label;
  if (w.wired) { icon = ICON.ethernet; label = "Ethernet"; }
  else if (w.connected) { icon = ICON.wifi(w.signal > 66 ? 3 : w.signal > 33 ? 2 : 1); label = w.ssid || "Wi-Fi"; }
  else { icon = ICON.wifi(0); label = "Offline"; }
  $("#wifiBtn").innerHTML = `${icon}<span class="label">${esc(label)}</span>`;
}

/** v = {running} from the host, or null when the voice assistant isn't installed (button stays hidden). */
function renderVoice(v) {
  const btn = $("#voiceBtn");
  btn.hidden = !v;
  if (!v) return;
  state.voiceOn = v.running;
  btn.classList.toggle("voice-on", v.running);
  btn.innerHTML = v.running
    ? `${ICON.mic(true)}<span>Hey Triton</span>`
    : `${ICON.mic(false)}<span class="dim">Voice off</span>`;
}

function renderVolume(v) {
  if (Date.now() - state.volumeTouchedAt < 1500) return;   // don't fight the slider while it's moving
  const level = v ? v.level : 0;
  const muted = v ? v.muted : false;
  $("#volBtn").innerHTML = v
    ? `${ICON.volume(level, muted)}<span>${muted ? "Muted" : level + "%"}</span>`
    : `${ICON.volume(0, true)}<span class="dim">No audio</span>`;
  const range = $("#volRange");
  range.value = level;
  range.style.setProperty("--val", level + "%");
  $("#volValue").textContent = muted ? "Muted" : level + "%";
  $("#muteLabel").textContent = muted ? "Unmute" : "Mute";
  $("#muteIcon").innerHTML = ICON.volume(muted ? 60 : 0, !muted);
  state.muted = muted;
}

function renderPower() {
  $("#autostartBtn").setAttribute("aria-checked", String(state.autostart));
  $("#exitSub").textContent = state.autostart
    ? "The launcher comes back on next restart"
    : "Start on boot is off — it won't come back by itself";
  // TV mode: Windows starts only Triton (no desktop behind it), so "exit" becomes a restart into Windows
  $("#exitBtn").hidden = !!state.tvSession;
  $("#autostartBtn").hidden = !!state.tvSession;
  const toTv = !state.tvSession;
  $("#restartLabel").textContent = toTv ? "Restart in TV mode" : "Restart to Windows";
  $("#restartSub").textContent = toTv
    ? "Only Triton, no Windows desktop: more memory and CPU for Triton"
    : "Restarts into the normal Windows desktop; Triton opens there too";
}

/* ============================== Popovers ============================== */

let openPopover = null;
let popoverOpener = null;

function togglePopover(id, opener) {
  if (openPopover && openPopover.id === id) { closePopover(); return; }
  closePopover();
  const pop = document.getElementById(id);
  pop.hidden = false;
  openPopover = pop;
  popoverOpener = opener;
  requestAnimationFrame(() => (pop.querySelector("input, button") || pop).focus());
}

function closePopover() {
  if (!openPopover) return;
  openPopover.hidden = true;
  openPopover = null;
  if (popoverOpener) popoverOpener.focus();
  popoverOpener = null;
}

function popoverKeys(e) {
  // Lists move with Up/Down (Left/Right stay native for the slider); the open-apps grid uses all four
  const grid = openPopover.classList.contains("switcher");
  const vertical = e.key === "ArrowUp" || e.key === "ArrowDown";
  if (!vertical && !(grid && (e.key === "ArrowLeft" || e.key === "ArrowRight"))) return;
  e.preventDefault();
  const items = [...openPopover.querySelectorAll("input, button")];
  if (!items.length) return;
  const cols = grid ? items.filter(b => b.offsetTop === items[0].offsetTop).length : 1;
  const step = (vertical ? cols : 1) * (e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : -1);
  const i = items.indexOf(document.activeElement);
  items[Math.max(0, Math.min(i + step, items.length - 1))].focus();
}

function wirePopovers() {
  $("#wifiBtn").addEventListener("click", () => send("launch", { id: "wifi" }));
  $("#voiceBtn").addEventListener("click", () => {
    const enabled = !state.voiceOn;
    renderVoice({ running: enabled });
    state.voiceTouchedAt = Date.now();
    toast(enabled ? "Starting voice… say “Hey Triton”, then a command" : "Voice assistant off until the launcher restarts");
    send("voice", { enabled });
  });
  $("#volBtn").addEventListener("click", e => togglePopover("pop-volume", e.currentTarget));
  $("#powerBtn").addEventListener("click", e => togglePopover("pop-power", e.currentTarget));

  let volTimer = null;
  let pendingLevel = null;
  const flushVolume = () => {
    clearTimeout(volTimer);
    if (pendingLevel !== null) send("volume", { level: pendingLevel });
    pendingLevel = null;
  };
  $("#volRange").addEventListener("input", e => {
    const level = Number(e.target.value);
    state.volumeTouchedAt = Date.now();
    e.target.style.setProperty("--val", level + "%");
    $("#volValue").textContent = level + "%";
    $("#volBtn").innerHTML = `${ICON.volume(level, false)}<span>${level}%</span>`;
    pendingLevel = level;
    clearTimeout(volTimer);
    volTimer = setTimeout(flushVolume, 60);
  });
  $("#muteBtn").addEventListener("click", () => {
    // Send any pending slider change first: setting a level unmutes, so it must not arrive after the mute.
    flushVolume();
    const muted = !state.muted;
    state.volumeTouchedAt = 0;
    renderVolume({ level: Number($("#volRange").value), muted });
    send("mute", { muted });
  });
  $("#soundBtn").addEventListener("click", () => { closePopover(); send("launch", { id: "sound" }); });

  $("#exitBtn").addEventListener("click", () => {
    send("exit");
    if (!host) toast("Would exit to Windows (preview)");
  });
  $("#restartBtn").addEventListener("click", () => {
    const mode = state.tvSession ? "windows" : "tv";
    send("restartTo", { mode });
    if (!host) toast(`Would restart ${mode === "tv" ? "into TV mode" : "to Windows"} (preview)`);
  });
  $("#autostartBtn").addEventListener("click", () => {
    state.autostart = !state.autostart;
    renderPower();
    send("autostart", { enabled: state.autostart });
  });

  document.addEventListener("pointerdown", e => {
    if (openPopover && !openPopover.contains(e.target) && !e.target.closest(".bar")) closePopover();
  });
}

/* ============================== Wallpaper ============================== */

const isVideo = p => /\.(mp4|webm)$/i.test(p);
const isImage = p => /\.(jpe?g|png|webp|gif)$/i.test(p);
const assetUrl = p => (/^https?:/i.test(p) ? p : "/" + p.split("/").map(encodeURIComponent).join("/"));

// Resolved when the first wallpaper frame is on screen; the loading screen waits for it.
let markWallpaperReady;
const wallpaperReady = new Promise(resolve => { markWallpaperReady = resolve; });

// "auto" with several videos in the folder = "playlist": play them all in order (or shuffled).
function pickWallpaper() {
  const w = config.wallpaper || "auto";
  if (w !== "auto") return w;
  const videos = state.wallpapers.filter(isVideo);
  if (videos.length > 1) return "playlist";
  return videos[0] || state.wallpapers.find(isImage) || `live:${config.livePalette || "aurora"}`;
}

function fallbackToLive() {
  state.wallpaperKey = null;
  config.wallpaper = `live:${config.livePalette || "aurora"}`;
  applyWallpaper();
}

function applyWallpaper() {
  const key = pickWallpaper();
  if (key === state.wallpaperKey) return;
  state.wallpaperKey = key;

  const root = $("#wallpaper");
  root.className = "wallpaper";
  root.innerHTML = "";
  playlist.video = null;

  if (key.startsWith("live:")) {
    root.classList.add("live", `palette-${key.slice(5)}`);
    root.innerHTML = "<i></i><i></i><i></i><i></i><i></i>";
    markWallpaperReady();
  } else if (key === "playlist") {
    startPlaylist(state.wallpapers.filter(isVideo));
  } else if (isVideo(key)) {
    startPlaylist([key]);
  } else {
    const img = new Image();
    const layer = document.createElement("div");
    layer.className = "image kenburns";
    img.onload = () => { layer.style.backgroundImage = `url("${img.src}")`; layer.classList.add("ready"); markWallpaperReady(); };
    img.onerror = fallbackToLive;
    img.src = assetUrl(key);
    root.append(layer);
  }
}

/* Video playlist: each clip plays once, then crossfades into the next; a single clip just loops.
   Pausing (another app in front) keeps the position, so nothing restarts or skips while you're away. */
const playlist = { items: [], index: 0, failures: 0, video: null };

function startPlaylist(items) {
  playlist.items = config.shuffleWallpapers ? shuffled(items) : items;
  playlist.failures = 0;
  let saved = 0;
  try { saved = Number(localStorage.getItem("wallpaperIndex")) || 0; } catch { }
  playlist.index = items.length > 1 ? saved % items.length : 0;   // carry on where we left off last time
  playVideo();
}

function playVideo() {
  const root = $("#wallpaper");
  const video = document.createElement("video");
  Object.assign(video, { muted: true, autoplay: true, playsInline: true, preload: "auto",
                         loop: playlist.items.length === 1, src: assetUrl(playlist.items[playlist.index]) });
  playlist.video = video;

  video.addEventListener("canplay", () => {
    playlist.failures = 0;
    video.classList.add("ready");
    markWallpaperReady();
    // Fade in over the previous clip's last frame, then drop the old element.
    root.querySelectorAll("video").forEach(old => { if (old !== video) setTimeout(() => old.remove(), 1300); });
  }, { once: true });
  // "ended" sometimes fires seconds late (seen in testing), so also move on just before the last frame.
  let advanced = false;
  const advance = () => { if (!advanced) { advanced = true; nextVideo(); } };
  video.addEventListener("ended", advance);
  video.addEventListener("timeupdate", () => {
    if (!video.loop && video.duration && video.currentTime >= video.duration - 0.3) advance();
  });
  video.addEventListener("error", () => {
    video.remove();
    console.warn("wallpaper: can't play", playlist.items[playlist.index]);
    if (++playlist.failures >= playlist.items.length) fallbackToLive();
    else nextVideo();
  }, { once: true });

  root.append(video);
  if (!document.body.classList.contains("paused")) video.play().catch(() => { });
  try { localStorage.setItem("wallpaperIndex", String(playlist.index)); } catch { }
}

function nextVideo() {
  if (playlist.items.length < 2) return;
  playlist.index = (playlist.index + 1) % playlist.items.length;
  playVideo();
}

function shuffled(items) {
  const a = items.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/* ============================== Weather (Open-Meteo, no API key) ============================== */

function readCache(key, maxAgeMs) {
  try {
    const item = JSON.parse(localStorage.getItem(key));
    if (item && Date.now() - item.t < maxAgeMs) return item.v;
  } catch { /* storage unavailable */ }
  return null;
}
function writeCache(key, value) {
  try { localStorage.setItem(key, JSON.stringify({ t: Date.now(), v: value })); } catch { }
}
async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

// Order: fixed coordinates > city from config.json > Windows device location > internet (IP) location.
async function resolveLocation() {
  const fixed = config.city || (config.latitude != null && config.longitude != null);
  const key = fixed ? `loc:${config.city}|${config.country}|${config.latitude}|${config.longitude}` : "loc:auto";
  const cached = readCache(key, fixed ? 12 * 3600e3 : 3600e3);   // auto: re-check hourly, the NUC may have moved
  if (cached && !(cached.source === "ip" && readCache(key, 15 * 60e3) === null)) return cached;

  let loc;
  if (config.latitude != null && config.longitude != null) {
    loc = { lat: config.latitude, lon: config.longitude, name: config.city || "" };
  } else if (config.city) {
    const country = config.country ? `&countryCode=${encodeURIComponent(config.country)}` : "";
    const g = await getJSON(`https://geocoding-api.open-meteo.com/v1/search?count=1&name=${encodeURIComponent(config.city)}${country}`);
    const r = g.results && g.results[0];
    if (!r) throw new Error(`City not found: ${config.city}`);
    loc = { lat: r.latitude, lon: r.longitude, name: r.name };
  } else {
    // An IP guess is only kept 15 min, so turning Windows location on takes effect quickly.
    loc = await deviceLocation().catch(err => {
      console.warn("device location unavailable, using internet location:", err.message || err);
      return ipLocation();
    });
  }
  writeCache(key, loc);
  return loc;
}

/** Windows location service (Wi-Fi positioning on a NUC). Needs Settings > Privacy > Location turned on. */
function deviceLocation() {
  return new Promise((resolve, reject) => {
    if (config.useDeviceLocation === false || !navigator.geolocation) return reject(new Error("disabled"));
    navigator.geolocation.getCurrentPosition(async pos => {
      const loc = { lat: pos.coords.latitude, lon: pos.coords.longitude, name: "", source: "device" };
      try {
        const r = await getJSON(`https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${loc.lat}&longitude=${loc.lon}&localityLanguage=en`);
        loc.name = r.city || r.locality || "";
      } catch { /* weather still works without a place name */ }
      resolve(loc);
    }, reject, { timeout: 15000, maximumAge: 30 * 60e3 });
  });
}

/** Rough location from the internet connection (often the provider's city, e.g. Berlin). */
async function ipLocation() {
  let ip;
  try { ip = await getJSON("https://ipapi.co/json/"); }
  catch { ip = await getJSON("https://ipwho.is/"); }
  return { lat: ip.latitude, lon: ip.longitude, name: ip.city || "", source: "ip" };
}

function describe(code) {
  if (code === 0) return ["clear", "Clear"];
  if (code === 1) return ["partly", "Mostly clear"];
  if (code === 2) return ["partly", "Partly cloudy"];
  if (code === 3) return ["cloud", "Cloudy"];
  if (code <= 48) return ["fog", "Foggy"];
  if (code <= 57) return ["rain", "Drizzle"];
  if (code <= 67) return ["rain", "Rain"];
  if (code <= 77) return ["snow", "Snow"];
  if (code <= 82) return ["rain", "Showers"];
  if (code <= 86) return ["snow", "Snow showers"];
  return ["storm", "Thunderstorm"];
}

async function loadWeather() {
  try {
    const loc = await resolveLocation();
    const unit = config.temperatureUnit === "fahrenheit" ? "&temperature_unit=fahrenheit" : "";
    const f = await getJSON(
      `https://api.open-meteo.com/v1/forecast?latitude=${loc.lat}&longitude=${loc.lon}` +
      `&current=temperature_2m,weather_code,is_day&hourly=temperature_2m,weather_code,is_day` +
      `&daily=temperature_2m_max,temperature_2m_min&forecast_days=2&timezone=auto${unit}`);
    const data = { loc, f };
    writeCache("weather", data);
    renderWeather(data);
  } catch (err) {
    console.warn("weather:", err);
    const stale = readCache("weather", 6 * 3600e3);   // offline: show the last forecast we had
    if (stale) renderWeather(stale);
  }
  state.weatherAt = Date.now();
}

function renderWeather({ loc, f }) {
  const c = f.current;
  const [kind, text] = describe(c.weather_code);
  const hi = Math.round(f.daily.temperature_2m_max[0]);
  const lo = Math.round(f.daily.temperature_2m_min[0]);

  // Next five slots, two hours apart, starting after the current time.
  const start = f.hourly.time.findIndex(t => t > c.time);
  const hours = [];
  for (let i = start; i >= 0 && i < f.hourly.time.length && hours.length < 5; i += 2) {
    const [k] = describe(f.hourly.weather_code[i]);
    const hh = new Date(f.hourly.time[i]).getHours();
    const label = config.clock24h ? pad(hh) + ":00" : `${hh % 12 || 12} ${hh < 12 ? "AM" : "PM"}`;
    hours.push(`<div class="w-hour"><small>${label}</small>${weatherSvg(k, f.hourly.is_day[i])}<b>${Math.round(f.hourly.temperature_2m[i])}°</b></div>`);
  }

  const el = $("#weather");
  el.innerHTML =
    `<div class="w-now">${weatherSvg(kind, c.is_day)}<div class="w-temp">${Math.round(c.temperature_2m)}°</div>` +
    `<div class="w-desc"><b>${text}</b><span>${esc(loc.name)}${loc.name ? " · " : ""}H ${hi}°  L ${lo}°</span></div></div>` +
    (hours.length ? `<div class="w-sep"></div><div class="w-hours">${hours.join("")}</div>` : "");
  el.hidden = false;
}

/* ============================== Keyboard / remote navigation ============================== */

let lastAppIndex = 0;
const zoneItems = zone => [...document.querySelectorAll(`[data-zone="${zone}"] > button:not([hidden])`)];

function focusApps(i = lastAppIndex) {
  const tiles = zoneItems("apps");
  const tile = tiles[Math.max(0, Math.min(i, tiles.length - 1))];
  if (tile) tile.focus({ preventScroll: true });
}

function wireKeys() {
  document.getElementById("apps").addEventListener("focusin", e => {
    const i = zoneItems("apps").indexOf(e.target);
    if (i >= 0) lastAppIndex = i;
  });

  document.addEventListener("keydown", e => {
    if (wake()) { e.preventDefault(); return; }   // first key only wakes from ambient mode
    if (e.key === "Escape") { if (openPopover) closePopover(); else focusApps(); return; }
    if (openPopover) { popoverKeys(e); return; }

    const el = document.activeElement;
    const zone = el && el.parentElement && el.parentElement.dataset.zone;
    if (!zone) {
      if (e.key.startsWith("Arrow")) { focusApps(); e.preventDefault(); }
      return;
    }
    const items = zoneItems(zone);
    const i = items.indexOf(el);
    switch (e.key) {
      case "ArrowRight": items[(i + 1) % items.length].focus(); break;
      case "ArrowLeft": items[(i - 1 + items.length) % items.length].focus(); break;
      case "ArrowUp": if (zone === "apps") zoneItems("bar")[0].focus(); break;
      case "ArrowDown": if (zone === "bar") focusApps(); break;
      default: return;
    }
    e.preventDefault();
  });
}

/* ============================== Ambient mode + cursor ============================== */

let idleTimer = null;
let cursorTimer = null;

function resetIdle() {
  clearTimeout(idleTimer);
  const secs = Number(config.ambientAfterSeconds);
  if (secs > 0) idleTimer = setTimeout(() => { if (!openPopover) setAmbient(true); }, secs * 1000);
}
function setAmbient(on) {
  state.ambient = on;
  document.body.classList.toggle("ambient", on);
}
/** Returns true if this input was used up to leave ambient mode. */
function wake() {
  resetIdle();
  if (!state.ambient) return false;
  setAmbient(false);
  focusApps();
  return true;
}
function wirePointer() {
  const onPointer = () => {
    wake();
    document.body.classList.remove("hide-cursor");
    clearTimeout(cursorTimer);
    cursorTimer = setTimeout(() => document.body.classList.add("hide-cursor"), 3000);
  };
  ["pointermove", "pointerdown", "wheel"].forEach(t => document.addEventListener(t, onPointer, { passive: true }));
  cursorTimer = setTimeout(() => document.body.classList.add("hide-cursor"), 3000);
}

/* ============================== Visibility (pause when an app covers us) ============================== */

function setVisible(visible) {
  document.body.classList.toggle("paused", !visible);
  const video = playlist.video;
  if (video) visible ? video.play().catch(() => {}) : video.pause();
  if (!visible) return;
  wake();
  if (!openPopover) focusApps();
  if (Date.now() - state.weatherAt > 15 * 60e3) loadWeather();
}

/* ============================== Toast ============================== */

let toastTimer = null;
function toast(text, isError = false) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.toggle("error", isError);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ============================== Host messages ============================== */

function onHostMessage(m) {
  switch (m.type) {
    case "init":
      state.wallpapers = m.wallpapers || [];
      state.autostart = !!m.autostart;
      state.tvSession = !!m.tvSession;
      applyWallpaper();
      renderPower();
      break;
    case "status":
      if (m.wifi) renderWifi(m.wifi);
      renderVolume(m.volume);
      if (typeof m.windows === "number") renderWindowCount(m.windows);
      if (Date.now() - state.voiceTouchedAt > 6000) renderVoice(m.voice);   // starting takes a moment; don't flicker
      if (typeof m.autostart === "boolean" && m.autostart !== state.autostart) { state.autostart = m.autostart; renderPower(); }
      break;
    case "visibility": setVisible(m.visible); break;
    case "launched": toast(`Opening ${m.name}…`); break;
    case "error": toast(m.text, true); break;
    case "restarting": closePopover(); toast(m.text); break;
    case "home": closePopover(); wake(); focusApps(); break;
    case "windows": renderWindows(m.items || []); break;
    case "showWindows": wake(); openSwitcher(); break;
  }
}

/* ============================== Start ============================== */

const wait = ms => new Promise(r => setTimeout(r, ms));

/** Loading screen out, home screen in. */
function reveal() {
  document.body.classList.add("revealing", "booted");
  document.body.classList.remove("booting");
  setTimeout(() => {
    document.body.classList.remove("revealing");
    const boot = $("#boot");
    if (boot) boot.remove();
  }, 2000);
}

async function init() {
  const bootStart = Date.now();
  await loadConfig();
  document.body.classList.toggle("low-power", !!config.lowPower);

  renderApps();
  renderClock();
  setInterval(renderClock, 1000);
  wirePopovers();
  wireKeys();
  wirePointer();

  // The wallpaper is chosen once the host has listed the wallpapers folder ("init" message).
  if (host) {
    host.addEventListener("message", e => onHostMessage(e.data));
    send("hello");
  } else {
    onHostMessage({ type: "init", wallpapers: [], autostart: true });
    onHostMessage({ type: "status", wifi: { connected: true, ssid: "Home Wi-Fi", signal: 82 }, volume: { level: 45, muted: false }, autostart: true, voice: { running: true }, windows: 2 });
  }

  const chrome = config.apps.findIndex(a => a.id === "chrome");
  focusApps(chrome >= 0 ? chrome : 0);

  // Keep the loading screen up until the first wallpaper frame, the weather and the fonts are ready
  // (at most 6 s, e.g. when offline), and for at least 1.4 s so it never just flashes.
  const weather = loadWeather();
  setInterval(loadWeather, 15 * 60e3);
  await Promise.race([Promise.all([wallpaperReady, weather, document.fonts.ready]), wait(6000)]);
  await wait(Math.max(0, 1400 - (Date.now() - bootStart)));

  reveal();
  send("ready");
  resetIdle();
}

init();
