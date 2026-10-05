"use strict";

const $ = (id) => document.getElementById(id);
let current = null;
let profiles = [];
let online = false;
let busy = false;
let eventLog = [];
let reconnectDelay = 1000;
let refreshTimer = null;
let profileOptionsSignature = "";
let renderedProfileId = null;
const lampNames = Array.from({length: 24}, (_, index) => `OUTPUT ${String(index).padStart(2, "0")}`);

function text(id, value) { $(id).textContent = value ?? "—"; }
function label(value) { return String(value ?? "").replaceAll("_", " ").replaceAll("-", " "); }
function notice(message) { text("notice", message); $("notice").hidden = !message; }
function connection(connected) {
  online = connected;
  text("connection", connected ? "Live connection" : "Bridge disconnected · retrying");
  $("connection-dot").className = `dot ${connected ? "connected" : "disconnected"}`;
  updateButtons();
}
function updateButtons() {
  const commissioned = current?.mode !== "run" || (current?.commissioning?.hardware_verified && current?.commissioning?.aircraft_verified);
  $("run").disabled = !online || busy || !current?.ready || !commissioned || current?.running || ["bench", "demo", "observe"].includes(current?.mode);
  $("stop").disabled = !online || busy || !current?.running;
  $("profile-select").disabled = !online || busy || !!current?.running || ["bench", "demo"].includes(current?.mode);
  for (const id of ["lamp-test", "lamp-off", "lamp-walk", "lcd-test"]) $(id).disabled = !online || busy || current?.mode !== "bench" || !current?.board?.connected;
}
function formatValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isFinite(value) ? String(Math.round(value * 10000) / 10000) : "invalid";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function time(value) {
  if (value === null || value === undefined) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
}
function renderProfiles(forceSelection = false) {
  const select = $("profile-select");
  const selected = current?.profile?.id ?? "";
  const signature = JSON.stringify(profiles.map((profile) => [profile.id, profile.name, profile.compatible]));
  const rebuild = signature !== profileOptionsSignature;
  if (rebuild) {
    select.replaceChildren();
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "Select profile"; empty.disabled = true; select.append(empty);
    for (const profile of profiles) {
      const option = document.createElement("option");
      option.value = profile.id; option.textContent = `${profile.name}${profile.compatible ? "" : " · incompatible aircraft"}`;
      option.disabled = !profile.compatible;
      select.append(option);
    }
    profileOptionsSignature = signature;
  }
  if (rebuild || forceSelection || selected !== renderedProfileId) {
    select.value = selected;
    renderedProfileId = selected;
  }
  updateButtons();
}
function renderEvents() {
  const list = $("events"); list.replaceChildren();
  if (!eventLog.length) { const item = document.createElement("li"); item.className = "muted"; item.textContent = "No events yet."; list.append(item); return; }
  for (const event of eventLog.slice().reverse().slice(0, 100)) {
    const item = document.createElement("li");
    const clock = document.createElement("time"); clock.textContent = time(event.time);
    const kind = document.createElement("span"); kind.className = "event-kind"; kind.textContent = String(event.kind ?? "event").toUpperCase();
    const message = document.createElement("span"); message.className = "event-message"; message.textContent = event.message ?? formatValue(event.data ?? event);
    item.append(clock, kind, message); list.append(item);
  }
}
function render(state) {
  current = state;
  text("state", label(state.state || (state.running ? "Running" : "Idle")));
  text("mode", state.mode);
  text("run", state.mode === "commission" ? "Start commissioning" : "Start panel");
  const commissioned = state.commissioning?.hardware_verified && state.commissioning?.aircraft_verified;
  const modeReadiness = {
    bench: "Bench ready. Use bounded output checks; simulator flight commands are disabled.",
    observe: "Observe connected. Displaying simulator feedback; physical flight commands are disabled.",
    demo: "Offline sample data. Hardware and simulator connections are not being tested.",
    commission: "Commissioning ready. Start explicitly to enable physical command testing."
  };
  text("readiness", state.error || (state.running ? "Physical command routing is enabled; displays follow simulator feedback." : state.ready ? state.mode === "run" ? commissioned ? "Qualified and ready. Start to enable the physical controls." : "Commissioning is incomplete. Complete hardware and aircraft checks with run --commission before normal Start." : modeReadiness[state.mode] || "Ready." : "Waiting for the board, a compatible aircraft, and current feedback."));
  const board = state.board ?? {}; const identity = board.identity ?? {};
  text("board-state", board.connected ? "Connected" : "Disconnected");
  text("board-name", identity.name || identity.board_type); text("firmware", identity.firmware); text("serial", identity.serial);
  text("configuration", board.configuration_ok ? "Matches pin map" : "Not verified");
  const sim = state.simulator ?? {};
  text("sim-state", sim.connected ? sim.healthy ? "Connected · current data" : "Connected · waiting for data" : "Disconnected");
  text("sim-version", sim.version); text("aircraft", sim.aircraft);
  text("hardware-verified", state.commissioning?.hardware_verified ? "Yes" : "No");
  text("aircraft-verified", state.commissioning?.aircraft_verified ? "Yes" : "No");
  text("profile-name", state.profile?.name || "No profile selected");
  text("profile-verification", state.profile ? `${state.profile.compatible ? "Compatible aircraft" : "Aircraft mismatch"} · ${state.profile.verification === "simulator_tested" ? "Simulator tested" : "Reference verified; simulator commissioning required"}` : "Select a profile after the simulator identifies its aircraft.");
  const banner = $("mode-banner"); banner.hidden = !["commission", "bench", "observe", "demo"].includes(state.mode); banner.className = `banner ${state.mode === "demo" ? "demo" : ""}`;
  const banners = {
    commission: "COMMISSIONING · Unverified aircraft profile. Physical controls can send commands for testing. Complete the checklist before enabling normal operation.",
    bench: "BENCH · Output tests drive the physical lamps and LCD. Simulator flight commands are disabled.",
    observe: "OBSERVE · Simulator feedback is visible. Physical controls do not send flight commands.",
    demo: "DEMO · Offline sample data. No physical hardware or simulator commands."
  };
  banner.textContent = banners[state.mode] || "";
  const panel = state.panel ?? {};
  text("lcd", Array.from({length: 4}, (_, row) => String(panel.rows?.[row] ?? "").slice(0, 20).padEnd(20, " ")).join("\n"));
  const indicators = $("indicators"); indicators.replaceChildren();
  for (let i = 0; i < 24; i++) {
    const supplied = panel.indicators?.find((item) => item.index === i);
    if (supplied?.name) lampNames[i] = label(supplied.name).toUpperCase();
    const option = $("lamp-select").options[i];
    if (option) option.textContent = `${String(i).padStart(2, "0")} · ${lampNames[i]}`;
    const on = supplied?.on ?? !!((panel.lamps || 0) & (1 << i));
    const item = document.createElement("div"); item.className = `indicator${on ? " on" : ""}${i >= 19 ? " spare" : ""}`;
    item.setAttribute("aria-label", `${supplied?.name || lampNames[i]}: ${on ? "on" : "off"}`);
    const dot = document.createElement("span"); dot.className = "lamp";
    const name = document.createElement("span"); name.textContent = supplied?.name || lampNames[i];
    item.append(dot, name); indicators.append(item);
  }
  const inputs = $("inputs"); inputs.replaceChildren();
  for (const [name, pressed] of Object.entries(state.inputs ?? {})) {
    const chip = document.createElement("span"); chip.className = `input-chip${pressed ? " pressed" : ""}`; chip.textContent = label(name).toUpperCase(); chip.title = pressed ? "Closed / pressed" : "Open / released"; inputs.append(chip);
  }
  if (!inputs.childElementCount) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "Input states appear after the board snapshot."; inputs.append(empty); }
  const encoderCounts = $("encoder-counts"); encoderCounts.replaceChildren();
  for (const [name, count] of Object.entries(state.encoder_counts ?? {})) {
    const item = document.createElement("div"); item.className = "encoder-count";
    const nameText = document.createElement("span"); nameText.textContent = name;
    const countText = document.createElement("strong"); countText.textContent = typeof count === "number" ? `${count >= 0 ? "+" : ""}${count}` : "—";
    item.append(nameText, countText); encoderCounts.append(item);
  }
  if (!encoderCounts.childElementCount) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "Raw encoder event totals are not available yet."; encoderCounts.append(empty); }
  const capabilities = $("capabilities"); capabilities.replaceChildren();
  for (const [name, capability] of Object.entries(state.capabilities ?? {})) {
    const item = document.createElement("div"); item.className = `capability${capability.available ? "" : " unavailable"}`;
    const dot = document.createElement("span"); dot.className = "availability";
    const description = document.createElement("span"); description.textContent = `${label(name).toUpperCase()} · ${capability.available ? "Available" : capability.reason || "Unavailable"}`;
    item.append(dot, description); capabilities.append(item);
  }
  $("bench").hidden = state.mode !== "bench";
  const telemetry = $("telemetry"); telemetry.replaceChildren();
  const entries = Object.entries(state.telemetry ?? {}).sort(([a], [b]) => a.localeCompare(b));
  text("telemetry-count", `${entries.length} VALUES`);
  for (const [key, sample] of entries) {
    const row = document.createElement("tr");
    for (const value of [key, formatValue(sample.value), sample.valid ? "Current" : "Invalid / stale", time(sample.updated_at)]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); }
    row.children[2].className = sample.valid ? "valid" : "invalid"; telemetry.append(row);
  }
  if (!entries.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 4; cell.className = "muted"; cell.textContent = "Waiting for simulator telemetry."; row.append(cell); telemetry.append(row); }
  if (Array.isArray(state.events)) { eventLog = state.events.slice(-100); renderEvents(); }
  renderProfiles();
}
async function get(path) {
  const response = await fetch(path, {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function action(path, data = {}) {
  busy = true; notice(""); updateButtons();
  try {
    const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (result.status) render(result.status);
  } catch (error) { notice(error.message || "Request failed"); renderProfiles(true); }
  finally { busy = false; updateButtons(); }
}
async function refresh() {
  try {
    const [state, profileData] = await Promise.all([get("/api/status"), get("/api/profiles")]);
    profiles = profileData.profiles ?? []; render(state);
  } catch (error) { notice(error.message); }
}
function connect() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/events`);
  socket.addEventListener("open", () => { connection(true); notice(""); reconnectDelay = 1000; refresh(); });
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(event.data);
      if (message.type === "status") render(message.data ?? message.status);
      else if (message.type === "event") { const item = message.data ?? message.event; if (item) { eventLog.push(item); eventLog = eventLog.slice(-100); renderEvents(); } }
    } catch (error) { notice("A diagnostic update could not be read; refreshing status."); refresh(); }
  });
  socket.addEventListener("error", () => socket.close());
  socket.addEventListener("close", () => { connection(false); setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(10000, reconnectDelay * 1.7); });
}

$("run").addEventListener("click", () => action("/api/run"));
$("stop").addEventListener("click", () => action("/api/stop"));
$("profile-select").addEventListener("change", () => { if ($("profile-select").value) action("/api/profile", {profile_id: $("profile-select").value}); });
$("lamp-test").addEventListener("click", () => action("/api/bench/lamp", {index: Number($("lamp-select").value), duration: Number($("lamp-duration").value)}));
$("lamp-off").addEventListener("click", () => action("/api/bench/lamp", {index: null}));
$("lamp-walk").addEventListener("click", () => action("/api/bench/walk"));
$("lcd-test").addEventListener("click", () => action("/api/bench/lcd", {text: $("lcd-text").value}));
for (let index = 0; index < 24; index++) { const option = document.createElement("option"); option.value = String(index); option.textContent = `${String(index).padStart(2, "0")} · ${lampNames[index]}`; $("lamp-select").append(option); }
refresh(); connect(); refreshTimer = setInterval(refresh, 5000);
window.addEventListener("pagehide", () => clearInterval(refreshTimer));
