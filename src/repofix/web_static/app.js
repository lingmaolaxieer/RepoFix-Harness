const $ = (selector) => document.querySelector(selector);
const state = { sessions: [], selected: null, detail: null, config: null, timer: null };
const statusNames = {
  created: "已创建", running: "运行中", paused: "已暂停", cancel_requested: "取消中",
  cancelled: "已取消", completed: "已完成", failed: "失败", cleaned: "已清理"
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.remove("hidden");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add("hidden"), 3800);
}

async function loadConfig() {
  state.config = await api("/api/config");
  $("#config-chip").textContent = `${state.config.model} · ${state.config.base_url}`;
  $("#config-warning").classList.toggle("hidden", state.config.has_api_key);
  const form = $("#settings-form");
  form.elements.base_url.value = state.config.base_url;
  form.elements.model.value = state.config.model;
  form.elements.timeout_seconds.value = state.config.timeout_seconds;
  form.elements.max_retries.value = state.config.max_retries;
  $("#api-key-hint").textContent = state.config.has_api_key ? `已保存 ${state.config.api_key_hint}` : "尚未保存";
  $("#config-path").textContent = state.config.config_path;
}

async function loadSessions(keepSelection = true) {
  state.sessions = await api("/api/sessions");
  renderSessions();
  if (keepSelection && state.selected) await selectSession(state.selected, false);
}

function renderSessions() {
  const list = $("#session-list");
  list.replaceChildren();
  if (!state.sessions.length) {
    const empty = document.createElement("p");
    empty.textContent = "还没有任务";
    empty.style.color = "#789f98";
    empty.style.padding = "8px";
    list.append(empty);
    return;
  }
  for (const session of state.sessions) {
    const button = document.createElement("button");
    button.className = `session-card${state.selected === session.id ? " active" : ""}`;
    button.innerHTML = `<strong>${escapeHtml(session.project_name)}</strong><small><span>${escapeHtml(statusNames[session.status] || session.status)}</span><span>${escapeHtml(session.id)}</span></small>`;
    button.addEventListener("click", () => selectSession(session.id));
    list.append(button);
  }
}

async function selectSession(id, updateList = true) {
  state.selected = id;
  state.detail = await api(`/api/sessions/${encodeURIComponent(id)}`);
  if (updateList) renderSessions();
  renderDetail();
}

function renderDetail() {
  const session = state.detail.session;
  $("#empty-view").classList.add("hidden");
  $("#detail-view").classList.remove("hidden");
  $("#page-title").textContent = session.id;
  const status = $("#detail-status");
  status.textContent = statusNames[session.status] || session.status;
  status.className = `status-pill ${session.status}`;
  $("#detail-project").textContent = session.source_repo.split(/[\\/]/).filter(Boolean).pop() || session.source_repo;
  $("#detail-issue").textContent = session.task.issue;
  $("#metric-model").textContent = session.model;
  $("#metric-executor").textContent = `${session.executor} · ${session.source_kind === "folder" ? "文件夹快照" : "Git worktree"}`;
  $("#metric-turns").textContent = session.turns;
  $("#metric-tools").textContent = session.tool_calls;
  $("#resume-button").disabled = state.detail.worker_active || ["completed", "cancelled", "cleaned"].includes(session.status);
  $("#cancel-button").disabled = !state.detail.worker_active;
  $("#cleanup-button").disabled = state.detail.worker_active || session.status === "cleaned";
  const error = $("#error-panel");
  error.textContent = session.last_error || "";
  error.classList.toggle("hidden", !session.last_error);
  renderEvents(state.detail.events);
  $("#patch-content").textContent = state.detail.patch || "等待 Agent 产生代码改动…";
  const passed = session.verification?.passed;
  $("#verification-label").textContent = passed === true ? "独立验证通过" : passed === false ? "独立验证失败" : "尚未验证";
}

function renderEvents(events) {
  $("#event-count").textContent = `${events.length} 条事件`;
  const list = $("#event-list");
  list.replaceChildren();
  for (const event of [...events].reverse()) {
    const item = document.createElement("article");
    item.className = "event";
    const summary = event.payload.summary || event.payload.reason || event.payload.status ||
      (event.payload.message?.content && String(event.payload.message.content).slice(0, 180)) || "";
    item.innerHTML = `<time>${escapeHtml(new Date(event.created_at).toLocaleTimeString())}</time><strong>${escapeHtml(event.kind)}</strong><p>${escapeHtml(summary)}</p>`;
    list.append(item);
  }
}

function openDialog(id) { document.getElementById(id).showModal(); }
function closeDialog(id) { document.getElementById(id).close(); }

async function inspectProject() {
  const box = $("#project-inspection");
  try {
    const result = await api("/api/project/inspect", { method: "POST", body: { path: $("#project-path").value } });
    box.textContent = `${result.source_kind === "git" ? "Git 项目" : "普通文件夹"} · ${result.message}`;
    box.className = `inspection${result.ready ? "" : " error"}`;
  } catch (error) {
    box.textContent = error.message;
    box.className = "inspection error";
  }
}

$("#new-task-button").addEventListener("click", () => openDialog("task-dialog"));
$("#empty-new-button").addEventListener("click", () => openDialog("task-dialog"));
$("#settings-button").addEventListener("click", () => openDialog("settings-dialog"));
$("#warning-config-button").addEventListener("click", () => openDialog("settings-dialog"));
$("#refresh-button").addEventListener("click", () => loadSessions().catch(error => toast(error.message, true)));
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => closeDialog(button.dataset.close)));
$("#project-path").addEventListener("change", inspectProject);
$("#pick-folder").addEventListener("click", async () => {
  try {
    const result = await api("/api/project/pick", { method: "POST", body: {} });
    if (result.path) { $("#project-path").value = result.path; await inspectProject(); }
  } catch (error) { toast(error.message, true); }
});

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/config", { method: "POST", body: Object.fromEntries(form.entries()) });
    event.currentTarget.elements.api_key.value = "";
    await loadConfig();
    closeDialog("settings-dialog");
    toast("模型配置已持久保存");
  } catch (error) { toast(error.message, true); }
});

$("#task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = $("#create-task-submit");
  submit.disabled = true;
  submit.textContent = "正在创建…";
  const raw = Object.fromEntries(new FormData(event.currentTarget).entries());
  raw.max_turns = Number(raw.max_turns);
  raw.max_tool_calls = Number(raw.max_tool_calls);
  raw.max_minutes = Number(raw.max_minutes);
  raw.acceptance_commands = raw.acceptance_commands.split("\n").map(line => line.trim()).filter(Boolean);
  try {
    const session = await api("/api/sessions", { method: "POST", body: raw });
    closeDialog("task-dialog");
    event.currentTarget.reset();
    event.currentTarget.elements.acceptance_commands.value = "python -m pytest -q";
    await loadSessions(false);
    await selectSession(session.id);
    toast("任务已创建，Agent 开始运行");
  } catch (error) { toast(error.message, true); }
  finally { submit.disabled = false; submit.textContent = "创建并运行"; }
});

$("#resume-button").addEventListener("click", async () => {
  try { await api(`/api/sessions/${state.selected}/resume`, { method: "POST", body: {} }); await selectSession(state.selected); toast("任务已继续运行"); }
  catch (error) { toast(error.message, true); }
});
$("#cancel-button").addEventListener("click", async () => {
  try { await api(`/api/sessions/${state.selected}/cancel`, { method: "POST", body: {} }); await selectSession(state.selected); toast("已请求取消"); }
  catch (error) { toast(error.message, true); }
});
$("#cleanup-button").addEventListener("click", async () => {
  if (!confirm("清理隔离工作区？执行轨迹和补丁会保留。")) return;
  try { await api(`/api/sessions/${state.selected}/cleanup`, { method: "POST", body: {} }); await loadSessions(); toast("工作区已清理"); }
  catch (error) { toast(error.message, true); }
});

async function poll() {
  try {
    await loadSessions(true);
  } catch (error) {
    $("#system-state span").textContent = "控制器连接异常";
  } finally {
    state.timer = setTimeout(poll, 2500);
  }
}

Promise.all([loadConfig(), loadSessions(false)])
  .then(() => { if (state.sessions.length) selectSession(state.sessions[0].id); state.timer = setTimeout(poll, 2500); })
  .catch(error => toast(error.message, true));
