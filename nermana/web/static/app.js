let config = {};
let statusCache = {};

const pages = Array.from(document.querySelectorAll(".page"));
const navButtons = Array.from(document.querySelectorAll("#nav button"));
const pageTitle = document.getElementById("pageTitle");
const statusLine = document.getElementById("statusLine");

navButtons.forEach((button) => {
  button.addEventListener("click", () => showPage(button.dataset.page));
});
document.getElementById("refreshButton").addEventListener("click", refreshAll);

function showPage(id) {
  pages.forEach((page) => page.classList.toggle("active", page.id === id));
  navButtons.forEach((button) => button.classList.toggle("active", button.dataset.page === id));
  pageTitle.textContent = navButtons.find((button) => button.dataset.page === id)?.textContent || "Nermana";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function output(nodeId, value) {
  document.getElementById(nodeId).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function refreshAll() {
  config = await api("/api/settings");
  statusCache = await api("/api/status");
  statusLine.textContent = summarizeStatus(statusCache);
  fillForms();
  renderModels();
  renderTools();
  renderMemory();
  renderLogs();
  document.getElementById("settingsJson").value = JSON.stringify(config, null, 2);
}

function summarizeStatus(status) {
  const caps = status.capabilities || [];
  const online = caps.find((cap) => cap.name === "internet")?.available ? "online" : "offline";
  const model = status.agent?.config?.model || "no model";
  const modelOk = status.agent?.model_health?.ok ? "model ready" : "model unavailable";
  return `${online} | ${model} | ${modelOk}`;
}

function fillForms() {
  setFormValues("modelSettings", {
    base_url: config.model.base_url,
    llama_server_path: config.model.llama_server_path,
    context_size: config.model.context_size,
    threads: config.model.threads,
    temperature: config.model.temperature,
    top_p: config.model.top_p,
    thinking_mode: config.model.thinking_mode,
  });
  document.querySelector("#fileSettings [name=allowed_dirs]").value = (config.files.allowed_dirs || []).join("\n");
  document.querySelector("#fileSettings [name=max_read_mb]").value = config.files.max_read_mb;
  setDottedForm("providersForm", config);
  setDottedForm("phoneSettings", config);
  setDottedForm("telegramSettings", config);
  const allowed = document.querySelector("#telegramSettings [name='telegram.allowed_user_ids']");
  allowed.value = (config.telegram.allowed_user_ids || []).join(",");
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
  if (value === "") return null;
  if (/^-?\d+(\.\d+)?$/.test(value)) return Number(value);
  return value;
}

async function savePatch(patch) {
  config = await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
  document.getElementById("settingsJson").value = JSON.stringify(config, null, 2);
  await refreshAll();
}

document.getElementById("chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("chatInput");
  const message = input.value.trim();
  if (!message) return;
  addMessage("user", message);
  input.value = "";
  const result = await api("/api/chat", { method: "POST", body: JSON.stringify({ message, session_id: "web" }) });
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

async function renderModels() {
  const data = await api("/api/models");
  const list = document.getElementById("modelsList");
  list.innerHTML = "";
  data.models.forEach((model) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<div><strong>${model.name}</strong><small>${model.size_mb} MB ${model.loadable ? "" : "(.guff typo)"}</small></div>`;
    const controls = document.createElement("div");
    const badge = document.createElement("span");
    badge.className = `badge ${model.active ? "" : "off"}`;
    badge.textContent = model.active ? "active" : "idle";
    const button = document.createElement("button");
    button.textContent = "Select";
    button.disabled = !model.loadable;
    button.addEventListener("click", async () => {
      await api("/api/models/select", { method: "POST", body: JSON.stringify({ model_name: model.name }) });
      await refreshAll();
    });
    controls.append(badge, button);
    row.appendChild(controls);
    list.appendChild(row);
  });
  output("modelOutput", data.health);
}

document.getElementById("scanModels").addEventListener("click", renderModels);
document.getElementById("restartModel").addEventListener("click", async () => output("modelOutput", await api("/api/models/restart", { method: "POST" })));
document.getElementById("modelSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await savePatch({
    model: {
      base_url: form.base_url.value,
      llama_server_path: form.llama_server_path.value,
      context_size: Number(form.context_size.value),
      threads: Number(form.threads.value),
      temperature: Number(form.temperature.value),
      top_p: Number(form.top_p.value),
      thinking_mode: form.thinking_mode.value,
    },
  });
});
document.getElementById("modelTest").addEventListener("submit", async (event) => {
  event.preventDefault();
  output("modelOutput", await api("/api/models/test", { method: "POST", body: JSON.stringify({ message: event.currentTarget.message.value }) }));
});

async function renderTools() {
  const data = await api("/api/tools");
  const list = document.getElementById("toolsList");
  const select = document.querySelector("#toolRun [name=tool]");
  list.innerHTML = "";
  select.innerHTML = "";
  data.tools.forEach((tool) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<div><strong>${tool.name}</strong><small>${tool.provider} | ${tool.risk} | ${tool.details}</small></div>`;
    const controls = document.createElement("div");
    const badge = document.createElement("span");
    badge.className = `badge ${tool.available ? "" : "off"}`;
    badge.textContent = tool.available ? "available" : "off";
    const toggle = document.createElement("button");
    toggle.textContent = tool.enabled ? "Disable" : "Enable";
    toggle.addEventListener("click", async () => {
      await api(`/api/tools/${tool.name}/enabled`, { method: "POST", body: JSON.stringify({ enabled: !tool.enabled }) });
      await refreshAll();
    });
    controls.append(badge, toggle);
    row.appendChild(controls);
    list.appendChild(row);
    const option = document.createElement("option");
    option.value = tool.name;
    option.textContent = tool.name;
    select.appendChild(option);
  });
}

document.getElementById("toolRun").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = JSON.parse(form.payload.value || "{}");
  output("toolOutput", await api(`/api/tools/${form.tool.value}/run`, { method: "POST", body: JSON.stringify({ payload }) }));
});

async function renderMemory() {
  const data = await api("/api/memory");
  const list = document.getElementById("memoryList");
  list.innerHTML = "";
  data.memories.forEach((memory) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<div><strong>${memory.source || "memory"}</strong><small>${memory.tags || ""}</small><div>${memory.content.slice(0, 320)}</div></div>`;
    const button = document.createElement("button");
    button.textContent = "Forget";
    button.addEventListener("click", async () => {
      await api(`/api/memory/${memory.id}`, { method: "DELETE" });
      await renderMemory();
    });
    row.appendChild(button);
    list.appendChild(row);
  });
}

document.getElementById("memorySearch").addEventListener("submit", async (event) => {
  event.preventDefault();
  const q = encodeURIComponent(event.currentTarget.q.value);
  const data = await api(`/api/memory/search?q=${q}`);
  const list = document.getElementById("memoryList");
  list.innerHTML = "";
  data.results.forEach((memory) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<div><strong>${memory.source}</strong><small>${memory.tags}</small><div>${memory.content}</div></div>`;
    list.appendChild(row);
  });
});
document.getElementById("memoryAdd").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await api("/api/memory", { method: "POST", body: JSON.stringify({ content: form.content.value, tags: form.tags.value }) });
  form.reset();
  await renderMemory();
});

document.getElementById("fileSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  await savePatch({ files: { allowed_dirs: form.allowed_dirs.value.split(/\n+/).map((x) => x.trim()).filter(Boolean), max_read_mb: Number(form.max_read_mb.value) } });
});
document.getElementById("fileRead").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitter = event.submitter;
  const path = event.currentTarget.path.value;
  const tool = submitter.value === "list" ? "list_files" : submitter.value === "index" ? "index_file" : "read_file";
  output("fileOutput", await api(`/api/tools/${tool}/run`, { method: "POST", body: JSON.stringify({ payload: { path } }) }));
});

document.getElementById("providersForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await savePatch(patchFromDottedForm("providersForm"));
});
document.getElementById("phoneSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await savePatch(patchFromDottedForm("phoneSettings"));
});
document.querySelectorAll("[data-phone-tool]").forEach((button) => {
  button.addEventListener("click", async () => output("phoneOutput", await api(`/api/tools/${button.dataset.phoneTool}/run`, { method: "POST", body: JSON.stringify({ payload: {} }) })));
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
  output("phoneOutput", await api(`/api/tools/${form.tool.value}/run`, { method: "POST", body: JSON.stringify({ payload }) }));
});

document.getElementById("telegramSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const patch = patchFromDottedForm("telegramSettings");
  patch.telegram.allowed_user_ids = String(document.querySelector("#telegramSettings [name='telegram.allowed_user_ids']").value)
    .split(",")
    .map((x) => Number(x.trim()))
    .filter((x) => Number.isFinite(x) && x > 0);
  await savePatch(patch);
});
document.getElementById("telegramPoll").addEventListener("click", async () => output("telegramOutput", await api("/api/telegram/poll_once", { method: "POST" })));

async function renderLogs() {
  output("logsOutput", await api("/api/logs"));
}

document.getElementById("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await savePatch(JSON.parse(document.getElementById("settingsJson").value));
    output("settingsOutput", "Saved");
  } catch (error) {
    output("settingsOutput", String(error));
  }
});

refreshAll().catch((error) => {
  statusLine.textContent = String(error);
});
