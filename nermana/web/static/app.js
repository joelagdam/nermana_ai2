let config = {};
let statusCache = {};
let dashboardCache = {};
let chatInitiated = false;

const pages = Array.from(document.querySelectorAll(".page"));
const navButtons = Array.from(document.querySelectorAll("#nav button"));
const pageTitle = document.getElementById("pageTitle");
const statusLine = document.getElementById("statusLine");
const statusPills = document.getElementById("statusPills");
const toastStack = document.getElementById("toastStack");
const rail = document.querySelector(".rail");
const drawerButton = document.getElementById("drawerButton");
const drawerBackdrop = document.getElementById("drawerBackdrop");

navButtons.forEach((button) => {
  button.addEventListener("click", () => {
    showPage(button.dataset.page);
    closeDrawer();
  });
});

drawerButton.addEventListener("click", openDrawer);
drawerBackdrop.addEventListener("click", closeDrawer);
document.getElementById("refreshButton").addEventListener("click", () => runAction("Refresh", refreshAll));
document.querySelectorAll("[data-jump]").forEach((button) => {
  button.addEventListener("click", () => showPage(button.dataset.jump));
});

function showPage(id) {
  pages.forEach((page) => page.classList.toggle("active", page.id === id));
  navButtons.forEach((button) => {
    const active = button.dataset.page === id;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  pageTitle.textContent = navButtons.find((button) => button.dataset.page === id)?.textContent || "Nermana";
}

function openDrawer() {
  rail.classList.add("open");
  drawerBackdrop.classList.add("open");
}

function closeDrawer() {
  rail.classList.remove("open");
  drawerBackdrop.classList.remove("open");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { ok: false, error: text };
    }
  }
  if (!response.ok) {
    throw new Error(data.detail || data.error || response.statusText);
  }
  return data;
}

async function runAction(title, task, successMessage = "Done") {
  showToast(title, "Working", "pending", 1800);
  try {
    const result = await task();
    if (result && result.ok === false) {
      showToast(title, result.error || result.message || "Action failed.", "error");
    } else {
      showToast(title, successMessage, "success");
    }
    return result;
  } catch (error) {
    showToast(title, String(error.message || error), "error");
    return { ok: false, error: String(error.message || error) };
  }
}

function showToast(title, message, type = "success", timeout = 7000) {
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const text = document.createElement("p");
  text.textContent = message;
  toast.append(strong, text);
  toastStack.appendChild(toast);
  setTimeout(() => toast.remove(), timeout);
}

function renderResult(nodeId, value) {
  const node = document.getElementById(nodeId);
  if (!node) return;
  node.innerHTML = "";
  if (typeof value === "string") {
    node.appendChild(activityItem(value || "No output", ""));
    return;
  }
  if (!value || typeof value !== "object") {
    node.appendChild(activityItem("No output", ""));
    return;
  }
  if (value.ok === false) {
    node.appendChild(activityItem("Action failed", value.error || value.message || "Unknown error"));
    return;
  }
  if (value.summary) {
    node.appendChild(activityItem("Summary", value.summary));
  }
  if (value.reply) {
    node.appendChild(activityItem("Reply", value.reply));
  }
  if (value.results) {
    renderSearchCards(node, value);
    return;
  }
  if (value.weather) {
    node.appendChild(activityItem("Weather", weatherText(value)));
    return;
  }
  if (value.content) {
    node.appendChild(activityItem("Content", value.content));
    return;
  }
  Object.entries(value)
    .filter(([key]) => !["ok", "raw", "data"].includes(key))
    .slice(0, 12)
    .forEach(([key, item]) => {
      node.appendChild(activityItem(labelize(key), simpleValue(item)));
    });
}

function renderSearchCards(node, value) {
  const results = value.results || [];
  node.appendChild(activityItem("Search", `${results.length} result(s) from ${value.provider || "provider"} for ${value.query || "query"}`));
  if (value.fallback_error) {
    node.appendChild(activityItem("Fallback used", value.fallback_error));
  }
  if (!results.length) {
    node.appendChild(activityItem("No results", value.error || "Try Auto provider or configure SearXNG."));
    return;
  }
  results.forEach((item, index) => {
    const title = `${index + 1}. ${item.title || "Untitled"}`;
    const detail = [item.content, item.url ? `Source: ${item.url}` : ""].filter(Boolean).join("\n");
    node.appendChild(activityItem(title, detail));
  });
}

function weatherText(value) {
  const current = value.weather?.current || {};
  const units = value.weather?.current_units || {};
  const daily = value.weather?.daily || {};
  const parts = [`${value.location || "Weather"} is available.`];
  if (current.temperature_2m !== undefined) {
    parts.push(`Now ${current.temperature_2m}${units.temperature_2m || ""}`);
  }
  if (current.apparent_temperature !== undefined) {
    parts.push(`feels like ${current.apparent_temperature}${units.temperature_2m || ""}`);
  }
  if (current.relative_humidity_2m !== undefined) {
    parts.push(`humidity ${current.relative_humidity_2m}%`);
  }
  if ((daily.time || []).length) {
    parts.push(`Forecast starts ${daily.time[0]}.`);
  }
  return parts.join(", ");
}

function simpleValue(value) {
  if (Array.isArray(value)) return value.map(simpleValue).join(", ");
  if (value && typeof value === "object") return Object.entries(value).map(([key, item]) => `${labelize(key)}: ${simpleValue(item)}`).join(" | ");
  return String(value ?? "");
}

function labelize(key) {
  return String(key).replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function refreshAll() {
  config = await api("/api/settings");
  dashboardCache = await api("/api/dashboard");
  statusCache = { agent: dashboardCache.agent, capabilities: dashboardCache.capabilities };
  statusLine.textContent = summarizeStatus(statusCache);
  renderStatusPills(statusCache);
  fillForms();
  renderDashboard(dashboardCache);
  await renderPresets();
  await renderModels();
  await renderTools();
  await renderMemory();
  await renderLogs();
  await renderUpdateStatus(false);
  await maybeInitiateChat();
}

function summarizeStatus(status) {
  const caps = status.capabilities || [];
  const online = caps.find((cap) => cap.name === "internet")?.available ? "online" : "offline";
  const model = status.agent?.config?.model || "no model";
  const modelOk = status.agent?.model_health?.ok ? "model ready" : "model unavailable";
  return `${online} | ${model} | ${modelOk}`;
}

function renderStatusPills(status) {
  const caps = status.capabilities || [];
  const internet = caps.find((cap) => cap.name === "internet");
  const llama = caps.find((cap) => cap.name === "llama_server_binary");
  const localModel = status.agent?.model_health;
  statusPills.innerHTML = "";
  statusPills.append(
    pill(internet?.available ? "Online" : "Offline", internet?.available ? "good" : "off"),
    pill(localModel?.ok ? "Model ready" : "Model off", localModel?.ok ? "good" : "off"),
    pill(llama?.available ? "llama found" : "llama missing", llama?.available ? "good" : "off")
  );
}

function pill(text, kind = "") {
  const item = document.createElement("span");
  item.className = `status-pill ${kind}`;
  item.textContent = text;
  return item;
}

function fillForms() {
  setFormValues("modelSettings", {
    models_dir: config.model.models_dir,
    base_url: config.model.base_url,
    llama_server_path: config.model.llama_server_path,
    context_size: config.model.context_size,
    threads: config.model.threads,
    batch_size: config.model.batch_size,
    ubatch_size: config.model.ubatch_size,
    parallel_slots: config.model.parallel_slots,
    request_timeout_seconds: config.model.request_timeout_seconds,
    mlock: config.model.mlock,
    no_mmap: config.model.no_mmap,
    temperature: config.model.temperature,
    top_p: config.model.top_p,
    thinking_mode: config.model.thinking_mode,
  });
  document.querySelector("#fileSettings [name=allowed_dirs]").value = (config.files.allowed_dirs || []).join("\n");
  document.querySelector("#fileSettings [name=max_read_mb]").value = config.files.max_read_mb;
  setDottedForm("providersForm", config);
  setDottedForm("toolDecisionSettings", config);
  setDottedForm("phoneSettings", config);
  setDottedForm("telegramSettings", config);
  setDottedForm("settingsForm", config);
  document.querySelector("#telegramSettings [name='telegram.allowed_user_ids']").value = (config.telegram.allowed_user_ids || []).join(",");
  renderSettingsCards();
}

function setFormValues(formId, values) {
  const form = document.getElementById(formId);
  for (const [key, value] of Object.entries(values)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  }
}

function setDottedForm(formId, source) {
  const form = document.getElementById(formId);
  Array.from(form.elements).forEach((input) => {
    if (!input.name) return;
    const value = getDotted(source, input.name);
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  });
}

function getDotted(source, path) {
  return path.split(".").reduce((obj, key) => (obj ? obj[key] : undefined), source);
}

function patchFromDottedForm(formId) {
  const patch = {};
  const form = document.getElementById(formId);
  Array.from(form.elements).forEach((input) => {
    if (!input.name) return;
    const value = input.type === "checkbox" ? input.checked : coerce(input.value);
    setDotted(patch, input.name, value);
  });
  return patch;
}

function setDotted(target, path, value) {
  const parts = path.split(".");
  let current = target;
  while (parts.length > 1) {
    const part = parts.shift();
    current[part] = current[part] || {};
    current = current[part];
  }
  current[parts[0]] = value;
}

function coerce(value) {
  if (value === "") return "";
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
  return value;
}

async function savePatch(patch, message = "Saved") {
  config = await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
  await refreshAll();
  return { ok: true, message };
}

function renderDashboard(data) {
  const stats = data.stats || {};
  const workers = data.workers || [];
  const working = Number(stats.workers_working || 0);
  const total = Number(stats.workers_total || workers.length || 0);
  const model = stats.model || "no model selected";
  const city = stats.weather_city || "no city";
  document.getElementById("dashboardSummary").textContent = `${model} | ${city} | ${stats.tools_working || 0}/${stats.tools_total || 0} tools active`;
  document.getElementById("dashboardWorkingCount").textContent = `${working}/${total}`;
  document.getElementById("workerHealthLabel").textContent = `${working} working`;
  document.getElementById("dashboardGeneratedAt").textContent = formatTime(data.generated_at);
  document.getElementById("downloadHealthLabel").textContent = stats.downloads_active ? `${stats.downloads_active} active` : "Idle";
  renderDashboardStats(stats);
  renderWorkers(workers);
  renderActivity(data);
  renderDownloads(data.downloads || []);
}

function renderDashboardStats(stats) {
  const grid = document.getElementById("dashboardStats");
  grid.innerHTML = "";
  grid.append(
    metric("Workers", `${stats.workers_working || 0}/${stats.workers_total || 0}`, "active capabilities", stats.workers_working ? "good" : "warn"),
    metric("Tools", `${stats.tools_working || 0}/${stats.tools_total || 0}`, `${stats.tools_enabled || 0} enabled`, stats.tools_working ? "good" : "warn"),
    metric("Memory", stats.memories || 0, "saved facts", stats.memories ? "good" : ""),
    metric("Sessions", stats.sessions || 0, "chat histories", stats.sessions ? "good" : ""),
    metric("Downloads", stats.downloads_active || 0, `${stats.downloads_total || 0} tracked`, stats.downloads_active ? "busy" : ""),
    metric("Folders", stats.allowed_folders || 0, "file allowlist", stats.allowed_folders ? "good" : "warn")
  );
}

function metric(label, value, detail, kind = "") {
  const item = document.createElement("div");
  item.className = `metric ${kind}`;
  const labelNode = document.createElement("span");
  labelNode.textContent = label;
  const valueNode = document.createElement("strong");
  valueNode.textContent = value;
  const detailNode = document.createElement("small");
  detailNode.textContent = detail;
  item.append(labelNode, valueNode, detailNode);
  return item;
}

function renderWorkers(workers) {
  const list = document.getElementById("workerList");
  list.innerHTML = "";
  if (!workers.length) {
    list.appendChild(activityItem("No workers reported", "Dashboard data unavailable."));
    return;
  }
  workers.forEach((worker) => {
    const item = document.createElement("div");
    item.className = `worker ${worker.state || "offline"}`;
    const dot = document.createElement("span");
    dot.className = "state-dot";
    const content = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = worker.name || "Worker";
    const details = document.createElement("small");
    details.textContent = worker.details || "";
    content.append(title, details);
    const state = document.createElement("span");
    state.className = "state-label";
    state.textContent = stateLabel(worker.state);
    item.append(dot, content, state);
    list.appendChild(item);
  });
}

function renderActivity(data) {
  const list = document.getElementById("activityList");
  list.innerHTML = "";
  const sessions = data.recent_sessions || [];
  const memories = data.recent_memories || [];
  if (!sessions.length && !memories.length) {
    list.appendChild(activityItem("No recent activity", "Chat and memory activity will appear here."));
    return;
  }
  sessions.slice(0, 4).forEach((session) => {
    list.appendChild(activityItem(session.title || session.id || "Session", `updated ${formatTime(session.updated_at)}`));
  });
  memories.slice(0, 3).forEach((memory) => {
    list.appendChild(activityItem(memory.source || "memory", (memory.content || "").slice(0, 120)));
  });
}

function renderDownloads(downloads) {
  const list = document.getElementById("downloadList");
  list.innerHTML = "";
  if (!downloads.length) {
    list.appendChild(activityItem("No model downloads", "Start one from Models when you need a new GGUF."));
    return;
  }
  downloads.forEach((job) => {
    const title = job.filename || job.id || "model download";
    const detail = `${job.state || "unknown"} | ${job.percent ? `${Number(job.percent).toFixed(1)}%` : formatTime(job.updated_at)}`;
    list.appendChild(activityItem(title, detail));
  });
}

function activityItem(title, detail) {
  const item = document.createElement("div");
  item.className = "activity-item";
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail || "";
  item.append(strong, small);
  return item;
}

function stateLabel(state) {
  if (state === "working") return "Working";
  if (state === "ready") return "Ready";
  if (state === "busy") return "Busy";
  if (state === "disabled") return "Disabled";
  return "Offline";
}

function formatTime(value) {
  const number = Number(value || 0);
  if (!number) return "n/a";
  return new Date(number * 1000).toLocaleString();
}

function makeRow(title, meta, controls = [], body = "") {
  const row = document.createElement("div");
  row.className = "row";
  const content = document.createElement("div");
  const heading = document.createElement("strong");
  heading.textContent = title;
  const small = document.createElement("small");
  small.textContent = meta || "";
  content.append(heading, small);
  if (body) {
    const bodyNode = document.createElement("div");
    bodyNode.className = "subline";
    bodyNode.textContent = body;
    content.appendChild(bodyNode);
  }
  const actions = document.createElement("div");
  controls.forEach((control) => actions.appendChild(control));
  row.append(content, actions);
  return row;
}

function badge(text, off = false) {
  const item = document.createElement("span");
  item.className = `badge ${off ? "off" : ""}`;
  item.textContent = text;
  return item;
}

function button(text, handler, primary = false) {
  const item = document.createElement("button");
  item.type = "button";
  item.textContent = text;
  if (primary) item.classList.add("primary-action");
  item.addEventListener("click", handler);
  return item;
}

document.getElementById("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("chatInput");
  const message = input.value.trim();
  if (!message) return;
  addMessage("user", message);
  input.value = "";
  const result = await runAction(
    "Chat",
    () => api("/api/chat", { method: "POST", body: JSON.stringify({ message, session_id: "web" }) }),
    "Reply ready"
  );
  addMessage("assistant", result.reply || result.error || "");
});

function addMessage(role, text) {
  const log = document.getElementById("chatLog");
  const item = document.createElement("div");
  item.className = `message ${role}`;
  item.textContent = text;
  log.appendChild(item);
  log.scrollTop = log.scrollHeight;
}

async function maybeInitiateChat() {
  if (chatInitiated || document.getElementById("chatLog").children.length) return;
  chatInitiated = true;
  try {
    const result = await api("/api/proactive?session_id=web");
    if (result.message) addMessage("assistant", result.message);
  } catch (error) {
    showToast("Chat", String(error.message || error), "error");
  }
}

function renderSettingsCards() {
  const node = document.getElementById("settingsCards");
  if (!node) return;
  node.innerHTML = "";
  node.append(
    metric("Web", `${config.server.host}:${config.server.port}`, config.server.public_url || "local only", "good"),
    metric("Model", config.model.auto_start_server ? "auto-start" : "manual", config.model.active_model || "no active model", config.model.auto_start_server ? "good" : ""),
    metric("Memory", config.memory.auto_remember ? "learning" : "manual", `${config.memory.retain_messages} retained`, config.memory.auto_remember ? "good" : "warn"),
    metric("Consolidation", `${config.memory.min_consolidate_items}+`, `every ${config.memory.consolidate_every_seconds}s`, "good"),
    metric("Autonomy", config.safety.autonomy, config.safety.require_confirmation_for_power ? "power confirms" : "power allowed", config.safety.require_confirmation_for_power ? "warn" : "good")
  );
}

async function renderModels() {
  const data = await api("/api/models");
  renderLlamaStatus(data.llama_server);
  const list = document.getElementById("modelsList");
  list.innerHTML = "";
  if (!data.models.length) {
    list.appendChild(makeRow("Model vault empty", "no GGUF files"));
  }
  data.models.forEach((model) => {
    const select = button("Select", async () => {
      await runAction("Model", () => api("/api/models/select", { method: "POST", body: JSON.stringify({ model_name: model.name }) }), "Model selected");
      await refreshAll();
    });
    select.disabled = !model.loadable;
    list.appendChild(makeRow(model.name, `${model.size_mb} MB ${model.loadable ? "" : "(.guff typo)"}`, [badge(model.active ? "active" : "idle", !model.active), select]));
  });
  renderResult("modelOutput", data.health);
}

async function renderPresets() {
  const select = document.querySelector("#modelDownload [name=preset_id]");
  const list = document.getElementById("presetsList");
  const current = select.value;
  select.innerHTML = "";
  list.innerHTML = "";
  const custom = document.createElement("option");
  custom.value = "";
  custom.textContent = "Direct link";
  select.appendChild(custom);
  let data = {};
  try {
    data = await api("/api/models/presets");
  } catch (error) {
    const message = String(error.message || error);
    list.appendChild(makeRow("Preset models unavailable", message));
    renderResult("downloadOutput", { ok: false, error: message });
    return;
  }
  const presets = Array.isArray(data.presets) ? data.presets : [];
  if (!presets.length) {
    list.appendChild(makeRow("No preset models", "empty preset catalog"));
  }
  presets.forEach((preset) => {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = `${preset.name} - ${preset.size_hint}`;
    option.title = preset.notes;
    select.appendChild(option);
    const download = button("Download", () => downloadModel({ preset_id: preset.id, select: true }), true);
    list.appendChild(makeRow(preset.name, preset.size_hint, [download], preset.notes));
  });
  select.value = current;
}

async function downloadModel(payload) {
  renderDownloadProgress({ state: "queued", message: "Queued", bytes_read: 0, total_bytes: 0, percent: 0 });
  renderResult("downloadOutput", "Download queued.");
  const started = await runAction(
    "Model download",
    () => api("/api/models/download/start", { method: "POST", body: JSON.stringify(payload) }),
    "Download started"
  );
  if (!started.ok) {
    renderResult("downloadOutput", started);
    return started;
  }
  let job = started.job;
  renderDownloadProgress(job);
  renderResult("downloadOutput", job);
  while (job && !["complete", "error"].includes(job.state)) {
    await delay(1000);
    job = await api(`/api/models/downloads/${started.job_id}`);
    renderDownloadProgress(job);
    renderResult("downloadOutput", job);
  }
  if (job.state === "complete") {
    showToast("Model download", "Download complete", "success");
    await refreshAll();
  } else {
    showToast("Model download", job.error || "Download failed", "error");
  }
  return job.result || job;
}

function renderDownloadProgress(job) {
  const panel = document.getElementById("downloadProgress");
  const title = document.getElementById("downloadProgressTitle");
  const text = document.getElementById("downloadProgressText");
  const bar = document.getElementById("downloadProgressBar");
  panel.hidden = false;
  const filename = job.filename || "model";
  const bytes = `${formatBytes(job.bytes_read || 0)}${job.total_bytes ? ` / ${formatBytes(job.total_bytes)}` : ""}`;
  const percent = Number(job.percent || 0);
  title.textContent = `${filename} ${job.state || ""}`.trim();
  text.textContent = job.total_bytes ? `${percent.toFixed(1)}% | ${bytes}` : `${job.message || "Working"} | ${bytes}`;
  if (job.total_bytes) {
    bar.value = Math.max(0, Math.min(100, percent));
  } else {
    bar.removeAttribute("value");
  }
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function renderLlamaStatus(status) {
  const node = document.getElementById("llamaStatus");
  if (!status) {
    node.textContent = "llama-server status unavailable";
    return;
  }
  const found = status.available ? `found: ${status.resolved}` : "not found";
  node.textContent = `llama-server ${found}. Current setting: ${status.configured}.`;
}

document.getElementById("scanModels").addEventListener("click", () => runAction("Models", renderModels, "Models scanned"));
document.getElementById("restartModel").addEventListener("click", async () => {
  const result = await runAction("Model server", () => api("/api/models/restart", { method: "POST" }), "Restart requested");
  renderResult("modelOutput", result);
});
document.getElementById("detectLlama").addEventListener("click", async () => {
  const result = await runAction("llama-server", () => api("/api/models/llama/use-detected", { method: "POST" }), "Path saved");
  renderResult("modelOutput", result);
  await refreshAll();
});
document.getElementById("modelDownload").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    preset_id: form.preset_id.value,
    url: form.url.value,
    filename: form.filename.value,
    select: form.select.checked,
  };
  await downloadModel(payload);
});
document.getElementById("modelSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await runAction("Model settings", () =>
    savePatch({
      model: {
        models_dir: form.models_dir.value,
        base_url: form.base_url.value,
        llama_server_path: form.llama_server_path.value,
        context_size: Number(form.context_size.value),
        threads: Number(form.threads.value),
        batch_size: Number(form.batch_size.value),
        ubatch_size: Number(form.ubatch_size.value),
        parallel_slots: Number(form.parallel_slots.value),
        request_timeout_seconds: Number(form.request_timeout_seconds.value),
        mlock: form.mlock.checked,
        no_mmap: form.no_mmap.checked,
        temperature: Number(form.temperature.value),
        top_p: Number(form.top_p.value),
        thinking_mode: form.thinking_mode.value,
      },
    })
  );
});
document.getElementById("modelTest").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = await runAction("Model test", () => api("/api/models/test", { method: "POST", body: JSON.stringify({ message: event.currentTarget.message.value }) }), "Test complete");
  renderResult("modelOutput", result);
});

async function renderTools() {
  const data = await api("/api/tools");
  const list = document.getElementById("toolsList");
  const select = document.querySelector("#toolRun [name=tool]");
  list.innerHTML = "";
  select.innerHTML = "";
  data.tools.forEach((tool) => {
    const toggle = button(tool.enabled ? "Disable" : "Enable", async () => {
      await runAction("Tool", () => api(`/api/tools/${tool.name}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !tool.enabled }) }), "Tool updated");
      await refreshAll();
    });
    list.appendChild(makeRow(tool.name, `${tool.provider} | ${tool.risk} | ${tool.details}`, [badge(tool.available ? "available" : "off", !tool.available), toggle]));
    const option = document.createElement("option");
    option.value = tool.name;
    option.textContent = tool.name;
    select.appendChild(option);
  });
}

document.getElementById("toolRun").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  let payload = {};
  try {
    payload = JSON.parse(form.payload.value || "{}");
  } catch {
    showToast("Tool", "Payload must be valid JSON.", "error");
    return;
  }
  const result = await runAction("Tool", () => api(`/api/tools/${form.tool.value}/run`, { method: "POST", body: JSON.stringify({ payload }) }), "Tool finished");
  renderResult("toolOutput", result);
});

document.getElementById("toolDecisionSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runAction("Tool decisions", () => savePatch(patchFromDottedForm("toolDecisionSettings")));
});

async function renderMemory() {
  const data = await api("/api/memory");
  const list = document.getElementById("memoryList");
  list.innerHTML = "";
  if (!data.memories.length) {
    list.appendChild(makeRow("No memories yet", "empty store"));
  }
  data.memories.forEach((memory) => {
    const forget = button("Forget", async () => {
      await runAction("Memory", () => api(`/api/memory/${memory.id}`, { method: "DELETE" }), "Memory removed");
      await renderMemory();
    });
    list.appendChild(makeRow(memory.source || "memory", memory.tags || "", [forget], memory.content.slice(0, 320)));
  });
}

document.getElementById("memorySearch").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = encodeURIComponent(event.currentTarget.q.value);
  const data = await runAction("Memory search", () => api(`/api/memory/search?q=${q}`), "Search complete");
  const list = document.getElementById("memoryList");
  list.innerHTML = "";
  (data.results || []).forEach((memory) => {
    list.appendChild(makeRow(memory.source, memory.tags, [], memory.content));
  });
});
document.getElementById("memoryAdd").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await runAction("Memory", () => api("/api/memory", { method: "POST", body: JSON.stringify({ content: form.content.value, tags: form.tags.value }) }), "Memory added");
  form.reset();
  await renderMemory();
});

document.getElementById("fileSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await runAction("Files", () =>
    savePatch({
      files: {
        allowed_dirs: form.allowed_dirs.value.split(/\n+/).map((x) => x.trim()).filter(Boolean),
        max_read_mb: Number(form.max_read_mb.value),
      },
    })
  );
});
document.getElementById("fileRead").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitter = event.submitter;
  const path = event.currentTarget.path.value;
  const tool = submitter?.value === "list" ? "list_files" : submitter?.value === "index" ? "index_file" : "read_file";
  const result = await runAction("Files", () => api(`/api/tools/${tool}/run`, { method: "POST", body: JSON.stringify({ payload: { path } }) }), "File action complete");
  renderResult("fileOutput", result);
});

document.getElementById("providersForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runAction("Providers", () => savePatch(patchFromDottedForm("providersForm")));
});
document.getElementById("searchTest").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = event.currentTarget.query.value.trim();
  if (!query) return;
  const result = await runAction(
    "Search",
    () => api("/api/tools/web_search/run", { method: "POST", body: JSON.stringify({ payload: { query } }) }),
    "Search complete"
  );
  renderResult("searchOutput", result);
});
document.getElementById("phoneSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runAction("Phone settings", () => savePatch(patchFromDottedForm("phoneSettings")));
});
document.querySelectorAll("[data-phone-tool]").forEach((item) => {
  item.addEventListener("click", async () => {
    const result = await runAction("Phone", () => api(`/api/tools/${item.dataset.phoneTool}/run`, { method: "POST", body: JSON.stringify({ payload: {} }) }), "Phone action complete");
    renderResult("phoneOutput", result);
  });
});
document.getElementById("phoneAction").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    package: form.package.value,
    permission: form.permission.value,
    namespace: form.namespace.value,
    key: form.key.value,
    value: form.value.value,
    enabled: form.enabled.checked,
    granted: form.granted.checked,
    op: form.op.value,
    mode: form.mode.value,
  };
  const result = await runAction("Phone", () => api(`/api/tools/${form.tool.value}/run`, { method: "POST", body: JSON.stringify({ payload }) }), "Phone action complete");
  renderResult("phoneOutput", result);
});

document.getElementById("telegramSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = patchFromDottedForm("telegramSettings");
  patch.telegram.allowed_user_ids = String(document.querySelector("#telegramSettings [name='telegram.allowed_user_ids']").value)
    .split(",")
    .map((x) => Number(x.trim()))
    .filter((x) => Number.isFinite(x) && x > 0);
  await runAction("Telegram", () => savePatch(patch));
});
document.getElementById("telegramPoll").addEventListener("click", async () => {
  const result = await runAction("Telegram", () => api("/api/telegram/poll_once", { method: "POST" }), "Poll complete");
  renderResult("telegramOutput", result);
});

async function renderLogs() {
  renderLogsView(await api("/api/logs"));
}

function renderLogsView(data) {
  const node = document.getElementById("logsOutput");
  node.innerHTML = "";
  const modelPanel = document.createElement("section");
  modelPanel.className = "dashboard-panel";
  modelPanel.appendChild(activityItem("Model health", data.model_health?.ok ? "Ready" : data.model_health?.error || "Unavailable"));
  const sessionPanel = document.createElement("section");
  sessionPanel.className = "dashboard-panel";
  sessionPanel.appendChild(activityItem("Recent sessions", `${(data.recent_sessions || []).length} session(s)`));
  (data.recent_sessions || []).slice(0, 6).forEach((session) => {
    sessionPanel.appendChild(activityItem(session.title || session.id || "Session", `Updated ${formatTime(session.updated_at)}`));
  });
  const toolsPanel = document.createElement("section");
  toolsPanel.className = "dashboard-panel";
  const tools = data.tools || [];
  const active = tools.filter((tool) => tool.enabled && tool.available).length;
  toolsPanel.appendChild(activityItem("Tools", `${active}/${tools.length} available`));
  tools.slice(0, 10).forEach((tool) => {
    toolsPanel.appendChild(activityItem(tool.name, `${tool.available ? "Available" : "Unavailable"} | ${tool.details || tool.provider}`));
  });
  node.append(modelPanel, sessionPanel, toolsPanel);
}

async function renderUpdateStatus(refreshRemote = false) {
  const suffix = refreshRemote ? "?refresh=1" : "";
  const result = await api(`/api/update/status${suffix}`);
  renderUpdateStatusResult(result);
  return result;
}

function renderUpdateStatusResult(result) {
  const node = document.getElementById("updateStatus");
  if (!result.ok) {
    node.textContent = result.error || result.message || "Update status unavailable.";
    return;
  }
  const parts = [result.message || "Update status ready."];
  if (result.branch) parts.push(`branch ${result.branch}`);
  if (result.current) parts.push(`current ${result.current}`);
  if (result.remote) parts.push(`upstream ${result.remote}`);
  node.textContent = parts.join(" | ");
}

document.getElementById("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = patchFromDottedForm("settingsForm");
  const result = await runAction("Settings", () => savePatch(patch));
  renderResult("settingsOutput", { ok: true, message: "Settings saved", ...result });
});

document.getElementById("checkUpdateButton").addEventListener("click", async () => {
  const result = await runAction("Update check", () => renderUpdateStatus(true), "Check complete");
  renderUpdateStatusResult(result);
  renderResult("updateOutput", result);
});

document.getElementById("updateButton").addEventListener("click", async () => {
  const result = await runAction("Update", () => api("/api/update", { method: "POST", body: JSON.stringify({}) }), "Update finished");
  renderUpdateStatusResult(result.status || result);
  renderResult("updateOutput", result);
});

refreshAll().catch((error) => {
  statusLine.textContent = String(error.message || error);
  showToast("Startup", String(error.message || error), "error");
});
