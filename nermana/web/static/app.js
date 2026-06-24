let config = {};
let statusCache = {};

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
      showToast(title, result.error || "Action failed.", "error");
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

function output(nodeId, value) {
  document.getElementById(nodeId).textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function refreshAll() {
  config = await api("/api/settings");
  statusCache = await api("/api/status");
  statusLine.textContent = summarizeStatus(statusCache);
  renderStatusPills(statusCache);
  fillForms();
  await renderPresets();
  await renderModels();
  await renderTools();
  await renderMemory();
  await renderLogs();
  document.getElementById("settingsJson").value = JSON.stringify(config, null, 2);
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
  document.querySelector("#telegramSettings [name='telegram.allowed_user_ids']").value = (config.telegram.allowed_user_ids || []).join(",");
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
  document.getElementById("settingsJson").value = JSON.stringify(config, null, 2);
  await refreshAll();
  return { ok: true, message };
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
  output("modelOutput", data.health);
}

async function renderPresets() {
  const data = await api("/api/models/presets");
  const select = document.querySelector("#modelDownload [name=preset_id]");
  const current = select.value;
  select.innerHTML = "";
  const custom = document.createElement("option");
  custom.value = "";
  custom.textContent = "Direct link";
  select.appendChild(custom);
  data.presets.forEach((preset) => {
    const option = document.createElement("option");
    option.value = preset.id;
    option.textContent = `${preset.name} - ${preset.size_hint}`;
    option.title = preset.notes;
    select.appendChild(option);
  });
  select.value = current;
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
  output("modelOutput", result);
});
document.getElementById("detectLlama").addEventListener("click", async () => {
  const result = await runAction("llama-server", () => api("/api/models/llama/use-detected", { method: "POST" }), "Path saved");
  output("modelOutput", result);
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
  output("downloadOutput", "Download in progress.");
  const result = await runAction("Model download", () => api("/api/models/download", { method: "POST", body: JSON.stringify(payload) }), "Download complete");
  output("downloadOutput", result);
  await refreshAll();
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
  output("modelOutput", result);
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
  output("toolOutput", result);
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
  output("fileOutput", result);
});

document.getElementById("providersForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runAction("Providers", () => savePatch(patchFromDottedForm("providersForm")));
});
document.getElementById("phoneSettings").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runAction("Phone settings", () => savePatch(patchFromDottedForm("phoneSettings")));
});
document.querySelectorAll("[data-phone-tool]").forEach((item) => {
  item.addEventListener("click", async () => {
    const result = await runAction("Phone", () => api(`/api/tools/${item.dataset.phoneTool}/run`, { method: "POST", body: JSON.stringify({ payload: {} }) }), "Phone action complete");
    output("phoneOutput", result);
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
  output("phoneOutput", result);
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
  output("telegramOutput", result);
});

async function renderLogs() {
  output("logsOutput", await api("/api/logs"));
}

document.getElementById("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  let patch = {};
  try {
    patch = JSON.parse(document.getElementById("settingsJson").value);
  } catch {
    showToast("Settings", "JSON is invalid.", "error");
    return;
  }
  const result = await runAction("Settings", () => savePatch(patch));
  output("settingsOutput", result);
});

document.getElementById("updateButton").addEventListener("click", async () => {
  const result = await runAction("Update", () => api("/api/update", { method: "POST", body: JSON.stringify({}) }), "Update finished");
  output("updateOutput", result);
});

refreshAll().catch((error) => {
  statusLine.textContent = String(error.message || error);
  showToast("Startup", String(error.message || error), "error");
});
