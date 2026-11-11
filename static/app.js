"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_CHART_ROWS = 10;
const WARN_BAND = 20;
const POLL_MS = 500;
const CALLS_PER_SECOND_ESTIMATE = 5;
const INTERFACE_COLORS = ["#34d399", "#fbbf24", "#4f8cff", "#f472b6", "#38bdf8", "#a78bfa"];

const METRICS = {
  util: { label: "Utilization", unit: "%", peak: "util_peak", mean: "util_mean", percent: true },
  perf: { label: "Performance score", unit: "", peak: "perf_peak", mean: "perf_mean", percent: true },
  uplink: { label: "Uplink", unit: " Mbps", peak: "uplink_peak_mbps", mean: "uplink_mean_mbps", percent: false },
};

const state = {
  models: [],
  jobId: null,
  pollTimer: null,
  result: null,
  parsed: null,
  threshold: 50,
  metric: "util",
  selected: null,
  filter: "",
  onlyHigh: false,
  countFilter: "all",
  sort: { key: "util_peak", dir: "desc" },
};

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    node.setAttribute(key, String(value));
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function tooltip(node, text) {
  node.appendChild(svg("title", {}, text));
  return node;
}

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (options.body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }
  if (!response.ok) throw new Error((data && data.error) || `Request failed (${response.status}).`);
  return data;
}

const isNumber = (v) => typeof v === "number" && Number.isFinite(v);
const isHigh = (v) => isNumber(v) && v >= state.threshold;
const round1 = (v) => Math.round(v * 10) / 10;

function fmt(value, metric) {
  if (!isNumber(value)) return "n/a";
  const info = METRICS[metric];
  if (metric === "uplink") return `${value >= 100 ? Math.round(value) : round1(value)} Mbps`;
  return `${round1(value)}${info.unit}`;
}

function durationLabel(seconds) {
  for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (seconds >= size && seconds % size === 0) {
      const count = seconds / size;
      return `${count} ${unit}${count === 1 ? "" : "s"}`;
    }
  }
  return `${seconds} seconds`;
}

function formatTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function shorten(text, max) {
  const value = String(text || "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function severity(value) {
  if (!isNumber(value)) return "sev-none";
  if (value >= state.threshold) return "sev-high";
  if (value >= state.threshold - WARN_BAND) return "sev-warn";
  return "sev-ok";
}

function niceMax(value) {
  if (!isNumber(value) || value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * power >= value) return step * power;
  return 10 * power;
}

function showBanner(message, kind = "error") {
  const node = $("banner");
  node.textContent = message || "";
  node.className = kind === "info" ? "banner info" : "banner";
  node.hidden = !message;
}

/* ---------- Heat scale ---------- */

const HEAT_STOPS = [
  [0, [27, 42, 68]],
  [40, [47, 111, 143]],
  [60, [251, 191, 36]],
  [80, [249, 115, 22]],
  [100, [248, 113, 113]],
];

function heatColor(value) {
  if (!isNumber(value)) return "#121826";
  const v = Math.max(0, Math.min(100, value));
  for (let i = 1; i < HEAT_STOPS.length; i += 1) {
    const [x1, c1] = HEAT_STOPS[i - 1];
    const [x2, c2] = HEAT_STOPS[i];
    if (v <= x2) {
      const t = (v - x1) / (x2 - x1 || 1);
      const rgb = c1.map((c, idx) => Math.round(c + (c2[idx] - c) * t));
      return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    }
  }
  return "rgb(248, 113, 113)";
}

/* ---------- Controls ---------- */

function selectedModels() {
  return Array.from(document.querySelectorAll("#models input[type=checkbox]:checked")).map((i) => i.value);
}

function selection() {
  const all = $("all-models").checked;
  const chosen = new Set(selectedModels());
  const models = state.models.filter((m) => all || chosen.has(m.model));
  const networks = new Set();
  let devices = 0;
  for (const m of models) {
    devices += m.devices;
    for (const id of m.networks || []) networks.add(id);
  }
  return { all, models, devices, networks: networks.size };
}

function fillResolution(select, options, fallback) {
  const previous = Number(select.value);
  select.replaceChildren();
  for (const option of options) {
    const parts = [option.label, `${option.points} point${option.points === 1 ? "" : "s"}`];
    let text = parts.join(" · ");
    if (!option.allowed) text += ` (${option.reason})`;
    select.appendChild(el("option", { value: option.seconds, disabled: !option.allowed }, text));
  }
  const keep = options.find((o) => o.seconds === previous && o.allowed);
  select.value = String(keep ? previous : fallback ?? "");
}

function resolutionHint(options, steady) {
  const allowed = options.filter((o) => o.allowed);
  if (!allowed.length) return "None fit this window.";
  if (allowed.length < options.length) return `${steady} Greyed steps exceed the limit.`;
  return steady;
}

async function loadOptions() {
  const windowSeconds = Number($("window").value);
  if (!windowSeconds) return;
  const sel = selection();
  try {
    const options = await api(`/api/options?window=${windowSeconds}&devices=${sel.devices}`);
    fillResolution($("perf-res"), options.performance, options.defaultPerformance);
    fillResolution($("uplink-res"), options.uplink, options.defaultUplink);
    $("perf-hint").textContent = resolutionHint(options.performance, "1 call per appliance per point");
    $("uplink-hint").textContent = resolutionHint(options.uplink, "1 call per network");
  } catch (error) {
    showBanner(error.message);
  }
  updateControls();
}

function updateControls() {
  const sel = selection();
  $("models").classList.toggle("disabled", sel.all);
  $("selection-hint").textContent = sel.devices
    ? `${sel.devices} appliance${sel.devices === 1 ? "" : "s"} in ${sel.networks} network${sel.networks === 1 ? "" : "s"}.`
    : "";
  const perf = Number($("perf-res").value);
  const uplink = Number($("uplink-res").value);
  const windowSeconds = Number($("window").value);
  const running = state.jobId !== null;
  const ready = sel.devices > 0 && perf > 0 && uplink > 0 && windowSeconds > 0;
  $("fetch").disabled = !ready || running;
  const plan = $("plan");
  if (!ready) {
    plan.hidden = true;
    $("call-count").textContent = "";
    $("call-split").textContent = "";
    return;
  }
  const perfCalls = sel.devices * Math.floor(windowSeconds / perf);
  const total = 1 + sel.networks + perfCalls;
  const seconds = Math.ceil(total / CALLS_PER_SECOND_ESTIMATE);
  const eta = seconds < 60 ? "under a minute" : `about ${Math.ceil(seconds / 60)} minute${seconds < 120 ? "" : "s"}`;
  plan.hidden = false;
  $("call-count").textContent = `${total} Dashboard calls · expect ${eta}`;
  $("call-split").textContent =
    `1 utilization · ${sel.networks} uplink · ${perfCalls} performance score`;
}

function renderModels() {
  const box = $("models");
  box.replaceChildren();
  if (!state.models.length) {
    box.appendChild(el("div", { className: "none" }, "No appliance devices with a model and a serial."));
  }
  for (const item of state.models) {
    const label = el("label");
    const input = el("input", { type: "checkbox", value: item.model });
    input.addEventListener("change", loadOptions);
    label.append(input, el("span", {}, item.model), el("span", { className: "count-tag" }, item.devices));
    box.appendChild(label);
  }
}

async function loadModels() {
  const orgId = $("org").value;
  state.models = [];
  renderModels();
  updateControls();
  if (!orgId) return;
  showStep("Reading appliances");
  $("selection-hint").textContent = "Loading appliance inventory…";
  try {
    const data = await api(`/api/models?org=${encodeURIComponent(orgId)}`);
    state.models = data.models || [];
    const windowSelect = $("window");
    const current = Number(windowSelect.value) || data.defaultWindow;
    windowSelect.replaceChildren();
    for (const option of data.windows || []) {
      windowSelect.appendChild(el("option", { value: option.seconds, selected: option.seconds === current }, option.label));
    }
    $("util-resolution").textContent = durationLabel(data.interval);
    renderModels();
    await loadOptions();
    clearStep();
  } catch (error) {
    showLoadError(error.message);
  }
}

async function loadOrganizations() {
  showStep("Reading organizations");
  try {
    const data = await api("/api/organizations");
    const select = $("org");
    select.replaceChildren();
    for (const org of data.organizations || []) {
      select.appendChild(el("option", { value: org.id }, `${org.name} (${org.id})`));
    }
    select.disabled = !data.organizations.length;
    if (!data.organizations.length) {
      showLoadError("This API key cannot see any organizations.");
      return;
    }
    await loadModels();
  } catch (error) {
    showLoadError(error.message);
  }
}

/* ---------- Fetch job ---------- */

function showLoadError(message) {
  $("status").textContent = message;
  showBanner(message);
}

function showStep(line) {
  if (state.jobId) return;
  $("progress-card").hidden = false;
  $("progress").hidden = true;
  $("progress-count").textContent = "";
  $("progress-title").textContent = line;
  $("status").textContent = "";
}

function clearStep() {
  if (state.jobId) return;
  $("progress-card").hidden = true;
}

function setProgress(done, total, detail, title) {
  $("progress-card").hidden = false;
  $("progress").hidden = !total;
  $("progress-title").textContent = title;
  if (total) {
    $("progress").max = total;
    $("progress").value = done;
    $("progress-count").textContent = `${done} / ${total}`;
  } else {
    $("progress").removeAttribute("value");
    $("progress-count").textContent = "";
  }
  $("status").textContent = detail || "";
}

function endJob() {
  clearTimeout(state.pollTimer);
  state.jobId = null;
  $("cancel").hidden = true;
  updateControls();
}

async function poll() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(state.jobId)}`);
    if (job.state === "running") {
      setProgress(job.done, job.total, job.phase, "Fetching");
      state.pollTimer = setTimeout(poll, POLL_MS);
      return;
    }
    endJob();
    if (job.state === "done") {
      setProgress(job.total, job.total, job.result.summary || "", "Fetch finished");
      loadResult(job.result);
    } else {
      const wasCancelled = job.state === "cancelled";
      setProgress(job.done, job.total, job.error || "Fetch did not finish.", wasCancelled ? "Fetch cancelled" : "Fetch failed");
      showBanner(job.error || "Fetch did not finish.", wasCancelled ? "info" : "error");
    }
  } catch (error) {
    endJob();
    showBanner(error.message);
  }
}

async function runFetch() {
  const body = {
    organizationId: $("org").value,
    allModels: $("all-models").checked,
    models: selectedModels(),
    timespan: Number($("window").value),
    performanceResolution: Number($("perf-res").value),
    uplinkResolution: Number($("uplink-res").value),
  };
  showBanner("");
  try {
    const data = await api("/api/fetch", { body });
    state.jobId = data.jobId;
    $("cancel").hidden = false;
    updateControls();
    setProgress(0, data.total, "Utilization", "Fetching");
    state.pollTimer = setTimeout(poll, POLL_MS);
  } catch (error) {
    showBanner(error.message);
  }
}

async function cancelFetch() {
  if (!state.jobId) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(state.jobId)}/cancel`, { body: {} });
    $("status").textContent = "Cancelling after the calls in flight…";
  } catch (error) {
    showBanner(error.message);
  }
}

/* ---------- Parsed series ---------- */

// utilization [midpointMs, percent, startIso]
// performance [t0Ms, t1Ms, score, t0Iso, t1Iso]
// uplink [midpointMs, sent, received, startIso]
// totals [midpointMs, mbps]
function parseResult(result) {
  const halfIntervalMs = (seconds) => seconds * 500;
  const utilization = {};
  const performance = {};
  for (const [serial, series] of Object.entries(result.series)) {
    utilization[serial] = series.utilization.map(([start, v]) => [Date.parse(start) + halfIntervalMs(result.interval), v, start]);
    performance[serial] = series.performance.map(([t0, t1, v]) => [Date.parse(t0), Date.parse(t1), v, t0, t1]);
  }
  const uplinks = {};
  const totals = {};
  for (const [networkId, network] of Object.entries(result.networks)) {
    uplinks[networkId] = {};
    const sum = new Map();
    for (const [iface, points] of Object.entries(network.uplinks)) {
      uplinks[networkId][iface] = points.map(([start, sent, received]) => {
        const t = Date.parse(start) + halfIntervalMs(result.uplinkResolution);
        if (isNumber(sent) || isNumber(received)) sum.set(t, (sum.get(t) || 0) + (sent || 0) + (received || 0));
        return [t, sent, received, start];
      });
    }
    totals[networkId] = Array.from(sum.entries()).sort((a, b) => a[0] - b[0]);
  }
  return { utilization, performance, uplinks, totals, start: Date.parse(result.start), end: Date.parse(result.end) };
}

function deviceBySerial(serial) {
  return state.result.devices.find((d) => d.serial === serial);
}

function metricPoints(metric, device) {
  const p = state.parsed;
  if (metric === "util") return (p.utilization[device.serial] || []).map(([t, v]) => [t, v]);
  if (metric === "perf") return (p.performance[device.serial] || []).map(([t0, , v]) => [t0, v]);
  return p.totals[device.network_id] || [];
}

/* ---------- Results ---------- */

function loadResult(result) {
  state.result = result;
  state.parsed = parseResult(result);
  state.selected = result.devices.length ? result.devices[0].serial : null;
  state.countFilter = "all";
  $("empty").hidden = true;
  $("results").hidden = false;
  $("meta").textContent =
    `${result.window}, ${formatTime(result.start)} to ${formatTime(result.end)}. ` +
    `Utilization every ${durationLabel(result.interval)}, performance score every ${durationLabel(result.performanceResolution)}, ` +
    `uplink usage every ${durationLabel(result.uplinkResolution)}. ${result.calls} Dashboard calls, fetched ${formatTime(result.fetchedAt)}.`;
  const withHistory = result.devices.filter((d) => d.util_intervals > 0).length;
  if (result.devices.length && !withHistory) {
    showBanner("No utilization history came back. Only primary MX devices return utilization history.", "info");
  }
  renderDetailSelect();
  renderAll();
  renderErrors();
}

function renderAll() {
  if (!state.result) return;
  renderCounts();
  renderRanking();
  renderHeatmap();
  renderDetail();
  renderTable();
}

function renderCounts() {
  const devices = state.result.devices;
  const counts = {
    all: devices.length,
    util: devices.filter((d) => isHigh(d.util_peak)).length,
    perf: devices.filter((d) => isHigh(d.perf_peak)).length,
    nodata: devices.filter((d) => !d.util_intervals).length,
  };
  for (const [key, value] of Object.entries(counts)) {
    const node = $(`count-${key}`);
    node.textContent = value;
    node.classList.toggle("nonzero", value > 0);
  }
  document.querySelectorAll(".count").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.filter === state.countFilter));
  });
}

function selectDevice(serial) {
  state.selected = serial;
  $("detail-device").value = serial;
  renderRanking();
  renderHeatmap();
  renderDetail();
  renderTable();
}

/* ---------- Ranking ---------- */

function topDevices(info) {
  return state.result.devices
    .filter((d) => isNumber(d[info.peak]))
    .sort((a, b) => b[info.peak] - a[info.peak])
    .slice(0, MAX_CHART_ROWS);
}

function renderRanking() {
  const host = $("ranking");
  host.replaceChildren();
  const info = METRICS[state.metric];
  const shown = topDevices(info);
  $("ranking-caption").textContent = info.percent
    ? `Top ${shown.length} by peak ${info.label.toLowerCase()} (bar = mean, tick = peak).`
    : `Top ${shown.length} by network peak uplink throughput (sent + received).`;
  if (!shown.length) {
    host.appendChild(el("p", { className: "hint" }, `No ${info.label.toLowerCase()} data was returned.`));
    return;
  }
  const scaleMax = info.percent ? Math.max(100, ...shown.map((d) => d[info.peak])) : niceMax(shown[0][info.peak]);
  const width = 960;
  const labelWidth = 190;
  const valueWidth = 145;
  const plotWidth = width - labelWidth - valueWidth;
  const rowHeight = 24;
  const top = 18;
  const height = top + shown.length * rowHeight + 18;
  const x = (v) => labelWidth + (Math.max(0, Math.min(scaleMax, v)) / scaleMax) * plotWidth;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `Appliances ranked by peak ${info.label}` });

  for (let i = 0; i <= 4; i += 1) {
    const tick = (scaleMax * i) / 4;
    root.appendChild(svg("line", { x1: x(tick), x2: x(tick), y1: top - 4, y2: height - 16, class: "grid" }));
    root.appendChild(svg("text", { x: x(tick), y: height - 4, "text-anchor": "middle", class: "axis-label" },
      info.percent ? `${round1(tick)}` : `${round1(tick)} Mbps`));
  }

  shown.forEach((device, index) => {
    const y = top + index * rowHeight;
    const sev = info.percent ? severity(device[info.peak]) : "sev-scale";
    const selected = device.serial === state.selected;
    const group = svg("g");
    group.appendChild(svg("rect", { x: 0, y, width, height: rowHeight, class: `row-hit${selected ? " selected" : ""}` }));
    group.appendChild(tooltip(svg("text", {
      x: labelWidth - 8, y: y + rowHeight / 2 + 3, "text-anchor": "end", class: `row-label${selected ? " selected" : ""}`,
    }, shorten(`${device.device_name} · ${device.model}`, 28)), `${device.device_name} (${device.model}) ${device.network_name}`));
    if (isNumber(device[info.mean])) {
      group.appendChild(svg("rect", {
        x: labelWidth, y: y + 4, height: rowHeight - 8, rx: 2,
        width: Math.max(1, x(device[info.mean]) - labelWidth), class: `bar ${sev}`,
      }));
    }
    group.appendChild(svg("rect", { x: x(device[info.peak]) - 1.5, y: y + 2, width: 3, height: rowHeight - 4, rx: 1, class: `peak ${sev}` }));
    group.appendChild(svg("text", { x: width - valueWidth + 8, y: y + rowHeight / 2 + 3, class: `value-label ${sev}` },
      `pk ${fmt(device[info.peak], state.metric)} · avg ${fmt(device[info.mean], state.metric)}`));
    group.addEventListener("click", () => selectDevice(device.serial));
    root.appendChild(group);
  });

  if (info.percent && state.threshold <= scaleMax) {
    const tx = x(state.threshold);
    root.appendChild(svg("line", { x1: tx, x2: tx, y1: top - 8, y2: height - 16, class: "threshold" }));
    root.appendChild(svg("text", { x: tx, y: 10, "text-anchor": "middle", class: "threshold-label" }, `thr ${state.threshold}`));
  }
  host.appendChild(root);
}

/* ---------- Heatmap ---------- */

function renderLegend(maxValue) {
  const host = $("heat-legend");
  host.replaceChildren();
  const info = METRICS[state.metric];
  const root = svg("svg", { width: 170, height: 22, viewBox: "0 0 170 22", role: "img", "aria-label": "Color scale" });
  for (let i = 0; i <= 100; i += 2) root.appendChild(svg("rect", { x: 10 + i * 1.4, y: 2, width: 3, height: 7, fill: heatColor(i) }));
  const labels = info.percent ? ["0", "50", "100"] : ["0", `${round1(maxValue / 2)}`, `${round1(maxValue)}`];
  [0, 50, 100].forEach((pos, i) => {
    root.appendChild(svg("text", { x: 10 + pos * 1.4, y: 18, "text-anchor": "middle", class: "axis-label" }, labels[i]));
  });
  const wrapper = el("span", { className: "legend-svg" });
  wrapper.appendChild(root);
  host.appendChild(wrapper);
}

function renderHeatmap() {
  const host = $("heatmap");
  host.replaceChildren();
  const info = METRICS[state.metric];
  const resolution = state.metric === "util" ? state.result.interval
    : state.metric === "perf" ? state.result.performanceResolution : state.result.uplinkResolution;
  $("heat-title").textContent = `${info.label} (${durationLabel(resolution)} intervals)`;
  const devices = topDevices(info);
  const rows = devices.map((d) => ({ device: d, points: new Map(metricPoints(state.metric, d)) }));
  const times = Array.from(new Set(rows.flatMap((r) => Array.from(r.points.keys())))).sort((a, b) => a - b);
  const maxValue = info.percent ? 100 : niceMax(Math.max(0, ...devices.map((d) => d[info.peak])));
  renderLegend(maxValue);
  if (!rows.length || !times.length) {
    host.appendChild(el("p", { className: "hint" }, `No ${info.label.toLowerCase()} data was returned.`));
    return;
  }
  const width = 960;
  const labelWidth = 180;
  const plotWidth = width - labelWidth - 8;
  const rowHeight = 20;
  const cellWidth = plotWidth / times.length;
  const height = 4 + rows.length * rowHeight + 20;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${info.label} heatmap` });
  rows.forEach(({ device, points }, rowIndex) => {
    const y = 4 + rowIndex * rowHeight;
    const selected = device.serial === state.selected;
    const label = tooltip(svg("text", {
      x: labelWidth - 8, y: y + rowHeight / 2 + 3, "text-anchor": "end", class: `row-label${selected ? " selected" : ""}`,
    }, shorten(device.device_name, 26)), `${device.device_name} (${device.model})`);
    label.addEventListener("click", () => selectDevice(device.serial));
    root.appendChild(label);
    times.forEach((t, col) => {
      const value = points.get(t);
      const scaled = info.percent ? value : isNumber(value) ? (value / maxValue) * 100 : value;
      const hot = info.percent && isHigh(value);
      const cell = tooltip(svg("rect", {
        x: labelWidth + col * cellWidth, y: y + 1,
        width: Math.max(cellWidth - (cellWidth > 3 ? 0.5 : 0), 0.4), height: rowHeight - 2,
        fill: heatColor(scaled), class: `cell${hot ? " hot" : ""}`,
      }), `${device.device_name} · ${formatTime(new Date(t))} · ${fmt(value, state.metric)}`);
      cell.addEventListener("click", () => selectDevice(device.serial));
      root.appendChild(cell);
    });
  });
  const ticks = Math.min(5, times.length);
  for (let i = 0; i < ticks; i += 1) {
    const index = ticks === 1 ? 0 : Math.round((i * (times.length - 1)) / (ticks - 1));
    root.appendChild(svg("text", {
      x: labelWidth + index * cellWidth + cellWidth / 2, y: height - 5,
      "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle", class: "axis-label",
    }, formatTime(new Date(times[index]))));
  }
  host.appendChild(root);
}

/* ---------- Detail chart ---------- */

function renderDetailSelect() {
  const select = $("detail-device");
  select.replaceChildren();
  for (const d of state.result.devices) {
    select.appendChild(el("option", { value: d.serial }, `${d.device_name} (${d.model})`));
  }
  if (state.selected) select.value = state.selected;
}

function linePath(points, x, y) {
  let path = "";
  let drawing = false;
  for (const [t, v] of points) {
    if (!isNumber(v)) {
      drawing = false;
      continue;
    }
    path += `${drawing ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
    drawing = true;
  }
  return path;
}

function stepPath(slices, x, y) {
  let path = "";
  let previousEnd = null;
  for (const [t0, t1, v] of slices) {
    if (!isNumber(v)) {
      previousEnd = null;
      continue;
    }
    path += previousEnd === t0 ? `V${y(v).toFixed(1)}` : `M${x(t0).toFixed(1)},${y(v).toFixed(1)}`;
    path += `H${x(t1).toFixed(1)}`;
    previousEnd = t1;
  }
  return path;
}

function nearest(points, t) {
  let lo = 0;
  let hi = points.length - 1;
  if (hi < 0) return null;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] < t) lo = mid + 1;
    else hi = mid;
  }
  const candidate = points[lo];
  const before = points[lo - 1];
  return before && Math.abs(before[0] - t) < Math.abs(candidate[0] - t) ? before : candidate;
}

function renderDetail() {
  const host = $("detail");
  host.replaceChildren();
  const device = state.selected && deviceBySerial(state.selected);
  if (!device) {
    $("detail-title").textContent = "Appliance detail";
    $("detail-facts").textContent = "";
    return;
  }
  const p = state.parsed;
  const util = p.utilization[device.serial] || [];
  const perf = p.performance[device.serial] || [];
  const uplinks = p.uplinks[device.network_id] || {};
  const interfaces = Object.keys(uplinks);
  $("detail-title").textContent = `${device.device_name} (${device.model})`;
  let facts = `Serial ${device.serial} · network ${device.network_name || device.network_id}`;
  if (device.shared_network) facts += " · uplink usage covers the whole network, which other selected appliances share";
  const spareNote = (state.result.errors || []).find((error) => error.serial === device.serial && /spare appliance/i.test(error.message || ""));
  if (spareNote) facts += ` · ${spareNote.message}`;
  $("detail-facts").textContent = facts;

  const width = 960;
  const left = 50;
  const right = 14;
  const topY = 20;
  const topH = 140;
  const bottomY = topY + topH + 40;
  const bottomH = 110;
  const height = bottomY + bottomH + 24;
  const plotW = width - left - right;
  const x = (t) => left + ((t - p.start) / (p.end - p.start || 1)) * plotW;
  const tFromX = (px) => p.start + ((px - left) / plotW) * (p.end - p.start);

  const pctMax = Math.max(100, ...util.map((u) => u[1]).filter(isNumber), ...perf.map((s) => s[2]).filter(isNumber));
  const yTop = (v) => topY + topH - (v / pctMax) * topH;
  let mbpsMax = 0;
  for (const points of Object.values(uplinks)) {
    for (const [, s, r] of points) mbpsMax = Math.max(mbpsMax, isNumber(s) ? s : 0, isNumber(r) ? r : 0);
  }
  mbpsMax = niceMax(mbpsMax);
  const yBottom = (v) => bottomY + bottomH - (v / mbpsMax) * bottomH;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `Utilization, score, and uplink for ${device.device_name}` });

  // Top panel: percent metrics.
  root.appendChild(svg("text", { x: left, y: topY - 7, class: "panel-title" }, "Utilization & score (0–100)"));
  for (let tick = 0; tick <= pctMax; tick += 25) {
    root.appendChild(svg("line", { x1: left, x2: width - right, y1: yTop(tick), y2: yTop(tick), class: "grid" }));
    root.appendChild(svg("text", { x: left - 6, y: yTop(tick) + 3, "text-anchor": "end", class: "axis-label" }, tick));
  }
  root.appendChild(svg("line", { x1: left, x2: width - right, y1: yTop(state.threshold), y2: yTop(state.threshold), class: "threshold" }));
  root.appendChild(svg("path", { d: linePath(util, x, yTop), class: "util-line" }));
  root.appendChild(svg("path", { d: stepPath(perf, x, yTop), class: "score-line" }));
  if (!util.some((u) => isNumber(u[1]))) {
    root.appendChild(svg("text", { x: left + plotW / 2, y: topY + topH / 2, "text-anchor": "middle", class: "note" }, "No utilization history for this appliance"));
  }
  const legendTop = svg("g", { transform: `translate(${width - right - 320}, ${topY - 11})` });
  legendTop.appendChild(svg("line", { x1: 0, x2: 16, y1: 0, y2: 0, class: "util-line" }));
  legendTop.appendChild(svg("text", { x: 20, y: 3, class: "legend-text" }, "Util %"));
  legendTop.appendChild(svg("line", { x1: 80, x2: 96, y1: 0, y2: 0, class: "score-line" }));
  legendTop.appendChild(svg("text", { x: 100, y: 3, class: "legend-text" }, "Score"));
  legendTop.appendChild(svg("line", { x1: 160, x2: 176, y1: 0, y2: 0, class: "threshold" }));
  legendTop.appendChild(svg("text", { x: 180, y: 3, class: "legend-text" }, `Thr ${state.threshold}`));
  root.appendChild(legendTop);

  // Bottom panel: uplink throughput.
  root.appendChild(svg("text", { x: left, y: bottomY - 7, class: "panel-title" }, "Uplink usage (Mbps, whole network)"));
  for (let i = 0; i <= 3; i += 1) {
    const tick = (mbpsMax * i) / 3;
    root.appendChild(svg("line", { x1: left, x2: width - right, y1: yBottom(tick), y2: yBottom(tick), class: "grid" }));
    root.appendChild(svg("text", { x: left - 6, y: yBottom(tick) + 3, "text-anchor": "end", class: "axis-label" }, round1(tick)));
  }
  const legendBottom = svg("g", { transform: `translate(${left + 220}, ${bottomY - 11})` });
  let legendX = 0;
  interfaces.forEach((iface, i) => {
    const color = INTERFACE_COLORS[i % INTERFACE_COLORS.length];
    root.appendChild(svg("path", { d: linePath(uplinks[iface].map(([t, , r]) => [t, r]), x, yBottom), class: "series-line", stroke: color }));
    root.appendChild(svg("path", { d: linePath(uplinks[iface].map(([t, s]) => [t, s]), x, yBottom), class: "series-line sent", stroke: color }));
    legendBottom.appendChild(svg("line", { x1: legendX, x2: legendX + 16, y1: 0, y2: 0, class: "series-line", stroke: color }));
    legendBottom.appendChild(svg("text", { x: legendX + 20, y: 3, class: "legend-text" }, iface));
    legendX += 34 + iface.length * 6;
  });
  if (interfaces.length) {
    legendBottom.appendChild(svg("text", { x: legendX + 6, y: 3, class: "axis-label" }, "(solid rx, dash tx)"));
  } else {
    root.appendChild(svg("text", { x: left + plotW / 2, y: bottomY + bottomH / 2, "text-anchor": "middle", class: "note" }, "No uplink usage for this network"));
  }
  root.appendChild(legendBottom);

  // Shared time axis.
  const ticks = 5;
  for (let i = 0; i <= ticks; i += 1) {
    const t = p.start + ((p.end - p.start) * i) / ticks;
    root.appendChild(svg("text", {
      x: x(t), y: height - 5, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", class: "axis-label",
    }, formatTime(new Date(t))));
  }

  // Hover readout.
  const crosshair = svg("line", { x1: 0, x2: 0, y1: topY, y2: bottomY + bottomH, class: "crosshair", visibility: "hidden" });
  const dots = svg("g", { visibility: "hidden" });
  root.append(crosshair, dots);
  const overlay = svg("rect", { x: left, y: topY, width: plotW, height: bottomY + bottomH - topY, class: "overlay" });
  root.appendChild(overlay);
  const readout = $("readout");
  readout.textContent = "Point at the chart to read values.";

  overlay.addEventListener("pointermove", (event) => {
    const box = root.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * width;
    const t = tFromX(Math.max(left, Math.min(width - right, px)));
    crosshair.setAttribute("x1", x(t));
    crosshair.setAttribute("x2", x(t));
    crosshair.setAttribute("visibility", "visible");
    dots.replaceChildren();
    dots.setAttribute("visibility", "visible");
    const parts = [formatTime(new Date(t))];
    const u = nearest(util, t);
    if (u && isNumber(u[1])) {
      parts.push(`utilization ${round1(u[1])}%`);
      dots.appendChild(svg("circle", { cx: x(u[0]), cy: yTop(u[1]), r: 3.5, fill: "#38bdf8", class: "hover-dot" }));
    }
    const slice = perf.find(([t0, t1]) => t >= t0 && t < t1);
    if (slice && isNumber(slice[2])) {
      parts.push(`score ${round1(slice[2])} (${formatTime(new Date(slice[0]))} to ${formatTime(new Date(slice[1]))})`);
      dots.appendChild(svg("circle", { cx: x(t), cy: yTop(slice[2]), r: 3.5, fill: "#7c5cff", class: "hover-dot" }));
    }
    interfaces.forEach((iface, i) => {
      const point = nearest(uplinks[iface], t);
      if (!point) return;
      const color = INTERFACE_COLORS[i % INTERFACE_COLORS.length];
      parts.push(`${iface} rx ${fmt(point[2], "uplink")}, tx ${fmt(point[1], "uplink")}`);
      if (isNumber(point[2])) dots.appendChild(svg("circle", { cx: x(point[0]), cy: yBottom(point[2]), r: 3, fill: color, class: "hover-dot" }));
    });
    readout.textContent = parts.join("  ·  ");
  });
  overlay.addEventListener("pointerleave", () => {
    crosshair.setAttribute("visibility", "hidden");
    dots.setAttribute("visibility", "hidden");
  });
  host.appendChild(root);
}

/* ---------- Table ---------- */

const COLUMNS = [
  { key: "device_name", label: "Appliance", type: "text" },
  { key: "model", label: "Model", type: "text" },
  { key: "network_name", label: "Network", type: "text" },
  { key: "util_peak", label: "Utilization peak", type: "num", badge: "util" },
  { key: "util_mean", label: "Utilization mean", type: "num", badge: "util" },
  { key: "perf_peak", label: "Score peak", type: "num", badge: "perf" },
  { key: "perf_mean", label: "Score mean", type: "num", badge: "perf" },
  { key: "uplink_peak_mbps", label: "Uplink peak Mbps", type: "num" },
  { key: "uplink_total_gb", label: "Uplink total GB", type: "num" },
];

function compareValues(a, b, type) {
  if (type === "num") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

function sortedRows(rows) {
  const column = COLUMNS.find((c) => c.key === state.sort.key) || COLUMNS[0];
  const factor = state.sort.dir === "asc" ? 1 : -1;
  const missing = (v) => v === null || v === undefined || v === "";
  return rows
    .map((row, index) => ({ row, index }))
    .sort((l, r) => {
      const a = l.row[column.key];
      const b = r.row[column.key];
      if (missing(a) || missing(b)) {
        if (missing(a) !== missing(b)) return missing(a) ? 1 : -1;
        return l.index - r.index;
      }
      return factor * compareValues(a, b, column.type) || l.index - r.index;
    })
    .map((item) => item.row);
}

function visibleRows() {
  const needle = state.filter.trim().toLowerCase();
  return state.result.devices.filter((d) => {
    if (state.countFilter === "util" && !isHigh(d.util_peak)) return false;
    if (state.countFilter === "perf" && !isHigh(d.perf_peak)) return false;
    if (state.countFilter === "nodata" && d.util_intervals) return false;
    if (state.onlyHigh && !(isHigh(d.util_peak) || isHigh(d.perf_peak))) return false;
    if (needle) {
      const haystack = [d.device_name, d.model, d.network_name, d.serial].join(" ").toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

function renderTable() {
  const table = $("table");
  table.replaceChildren();
  const rows = sortedRows(visibleRows());

  const head = el("thead");
  const headRow = el("tr");
  for (const column of COLUMNS) {
    const active = state.sort.key === column.key;
    const th = el("th", {
      className: column.type === "num" ? "num" : undefined,
      scope: "col",
      "aria-sort": active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none",
    });
    const button = el("button", { type: "button" }, column.label);
    if (active) button.appendChild(el("span", { className: "arrow" }, state.sort.dir === "asc" ? "▲" : "▼"));
    button.addEventListener("click", () => {
      if (state.sort.key === column.key) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      else state.sort = { key: column.key, dir: column.type === "text" ? "asc" : "desc" };
      renderTable();
    });
    th.appendChild(button);
    headRow.appendChild(th);
  }
  head.appendChild(headRow);

  const body = el("tbody");
  if (!rows.length) {
    const tr = el("tr");
    tr.appendChild(el("td", { colspan: COLUMNS.length, className: "table-empty" }, "No appliances match."));
    body.appendChild(tr);
  }
  for (const row of rows) {
    const tr = el("tr", { className: row.serial === state.selected ? "selected" : undefined });
    for (const column of COLUMNS) {
      const td = el("td", { className: column.type === "num" ? "num" : undefined });
      const value = row[column.key];
      if (column.badge) td.appendChild(el("span", { className: `badge ${severity(value)}` }, fmt(value, column.badge)));
      else if (column.type === "num") td.textContent = isNumber(value) ? String(value) : "n/a";
      else td.textContent = value || "";
      if (column.key === "network_name" && row.shared_network) td.appendChild(el("span", { className: "sub" }, " (shared)"));
      tr.appendChild(td);
    }
    tr.addEventListener("click", () => {
      selectDevice(row.serial);
      if (window.innerWidth < 1080) {
        $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });
    body.appendChild(tr);
  }
  table.append(head, body);
  $("table-caption").textContent =
    `${rows.length} of ${state.result.devices.length} appliances. Click a header to sort, click it again to reverse. Click a row to open its detail chart.`;
}

/* ---------- Errors and CSV ---------- */

function renderErrors() {
  const errors = state.result.errors || [];
  $("errors-panel").hidden = !errors.length;
  const table = $("errors");
  table.replaceChildren();
  if (!errors.length) return;
  const head = el("thead");
  const hr = el("tr");
  for (const label of ["Call", "Appliance or network", "Serial", "Message"]) hr.appendChild(el("th", { scope: "col" }, label));
  head.appendChild(hr);
  const body = el("tbody");
  for (const error of errors) {
    const tr = el("tr");
    tr.append(el("td", {}, error.source), el("td", {}, error.device_name), el("td", { className: "mono" }, error.serial), el("td", {}, error.message));
    body.appendChild(tr);
  }
  table.append(head, body);
}

function csvCell(value) {
  let text = value === null || value === undefined ? "" : String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function download(name, lines) {
  const blob = new Blob([lines.map((cells) => cells.map(csvCell).join(",")).join("\r\n")], { type: "text/csv" });
  const link = el("a", { href: URL.createObjectURL(blob), download: name });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function downloadTable() {
  if (!state.result) return;
  const keys = ["serial", ...COLUMNS.map((c) => c.key), "util_latest", "perf_latest", "uplink_mean_mbps", "shared_network"];
  const lines = [keys];
  for (const row of sortedRows(visibleRows())) lines.push(keys.map((k) => row[k]));
  download("appliance_summary.csv", lines);
}

function downloadSeries() {
  const device = state.selected && deviceBySerial(state.selected);
  if (!device) return;
  const r = state.result;
  const plus = (iso, seconds) => new Date(Date.parse(iso) + seconds * 1000).toISOString().replace(".000Z", "Z");
  const lines = [["metric", "interface", "direction", "start", "end", "value"]];
  for (const [start, v] of r.series[device.serial].utilization) lines.push(["utilization_percent", "", "", start, plus(start, r.interval), v]);
  for (const [t0, t1, v] of r.series[device.serial].performance) lines.push(["performance_score", "", "", t0, t1, v]);
  const network = r.networks[device.network_id];
  if (network) {
    for (const [iface, points] of Object.entries(network.uplinks)) {
      for (const [start, sent, received] of points) {
        const end = plus(start, r.uplinkResolution);
        lines.push(["uplink_mbps", iface, "received", start, end, received]);
        lines.push(["uplink_mbps", iface, "sent", start, end, sent]);
      }
    }
  }
  download(`${device.serial}_series.csv`, lines);
}

/* ---------- Startup ---------- */

async function init() {
  $("org").addEventListener("change", loadModels);
  $("all-models").addEventListener("change", loadOptions);
  $("clear-models").addEventListener("click", () => {
    document.querySelectorAll("#models input[type=checkbox]").forEach((i) => { i.checked = false; });
    loadOptions();
  });
  $("window").addEventListener("change", loadOptions);
  $("perf-res").addEventListener("change", updateControls);
  $("uplink-res").addEventListener("change", updateControls);
  $("threshold").addEventListener("input", (event) => {
    state.threshold = Number(event.target.value);
    $("threshold-value").textContent = `${state.threshold}%`;
    renderAll();
  });
  $("fetch").addEventListener("click", runFetch);
  $("cancel").addEventListener("click", cancelFetch);
  $("metric").addEventListener("change", (event) => {
    state.metric = event.target.value;
    renderRanking();
    renderHeatmap();
  });
  const setHotspotView = (view) => {
    $("view-ranking").setAttribute("aria-selected", String(view === "ranking"));
    $("view-heatmap").setAttribute("aria-selected", String(view === "heatmap"));
    $("ranking-wrap").hidden = view !== "ranking";
    $("heatmap-wrap").hidden = view !== "heatmap";
  };
  $("view-ranking").addEventListener("click", () => setHotspotView("ranking"));
  $("view-heatmap").addEventListener("click", () => setHotspotView("heatmap"));
  $("detail-device").addEventListener("change", (event) => selectDevice(event.target.value));
  $("series-csv").addEventListener("click", downloadSeries);
  $("filter").addEventListener("input", (event) => { state.filter = event.target.value; if (state.result) renderTable(); });
  $("only-high").addEventListener("change", (event) => { state.onlyHigh = event.target.checked; if (state.result) renderTable(); });
  $("csv").addEventListener("click", downloadTable);
  document.querySelectorAll(".count").forEach((button) => {
    button.addEventListener("click", () => {
      state.countFilter = button.dataset.filter;
      renderCounts();
      renderTable();
    });
  });

  try {
    const status = await api("/api/status");
    if (status.error) {
      $("key-pill").textContent = "No API key";
      $("key-pill").classList.add("bad");
      showBanner(`${status.error} Set MK_CSM_KEY to a read-only Dashboard key and restart server.py.`);
      return;
    }
  } catch (error) {
    showBanner(error.message);
    return;
  }
  await loadOrganizations();
}

document.addEventListener("DOMContentLoaded", init);
