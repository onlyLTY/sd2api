const $ = (id) => document.getElementById(id);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));

const state = {
  key: sessionStorage.getItem("sd2api_admin_key") || "",
  route: "generate",
  accounts: [],
  pool: null,
  config: null,
  tasks: [],
  logs: [],
  taskSummary: {},
  editId: null,
  focusTask: null,
  refreshing: false,
  timer: null,
};

const routes = {
  generate: { eyebrow: "CREATE", title: "生视频", subtitle: "创建并跟踪 Seedance 视频任务" },
  accounts: { eyebrow: "ACCOUNT POOL", title: "号池管理", subtitle: "管理登录账号、子账号权限与 Credits" },
  logs: { eyebrow: "ACTIVITY", title: "日志", subtitle: "查看账号、登录、视频与系统事件" },
  videos: { eyebrow: "VIDEO LIBRARY", title: "视频管理", subtitle: "自动同步状态并管理所有生成任务" },
};

const stateMap = {
  logged_in: ["已登录", "ok"], captcha_required: ["等待图形验证", "wait"],
  waiting_email_code: ["获取邮箱验证码", "info"], waiting_email_code_manual: ["等待手动验证码", "wait"],
  submitting_email_code: ["提交邮箱验证码", "info"], opening_login: ["打开登录页", "info"],
  recovering_browser: ["恢复 Chromium", "info"], browser_closed: ["Chromium 已关闭", "wait"],
  browser_error: ["Chromium 异常", "bad"], entering_credentials: ["输入账号密码", "info"],
  waiting_for_login: ["等待登录结果", "info"], logging_in: ["登录中", "info"],
  login_failed: ["登录失败", "bad"], not_configured: ["未配置凭据", "bad"],
  not_logged_in: ["未登录", "wait"], not_started: ["尚未启动", "wait"],
  pending: ["等待登录", "wait"], queued: ["排队中", "wait"], running: ["生成中", "info"],
  in_progress: ["生成中", "info"], succeeded: ["已完成", "ok"], completed: ["已完成", "ok"],
  failed: ["失败", "bad"],
};

function normalizeStatus(status) {
  return ({ in_progress: "running", completed: "succeeded" })[status] || status;
}

function pill(status) {
  const normalized = normalizeStatus(status);
  const [label, className] = stateMap[normalized] || [normalized || "未知", ""];
  return `<span class="pill ${className}">${esc(label)}</span>`;
}

function setConnection(online, text) {
  $("connectionDot").className = `connection-dot ${online ? "online" : "offline"}`;
  $("connectionText").textContent = text || (online ? "服务已连接" : "连接异常");
}

function showAuth(message = "") {
  $("auth").classList.remove("hidden");
  $("authError").textContent = message;
  setConnection(false, "等待连接");
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${state.key}` };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  let data = null;
  try { data = await response.json(); } catch { /* response may be empty */ }
  if (response.status === 401) {
    showAuth("Admin Key 不正确或已失效");
    throw new Error("未授权");
  }
  if (!response.ok) {
    const detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail || data || {});
    throw new Error(data?.error?.message || detail || `HTTP ${response.status}`);
  }
  return data;
}

function toast(title, message = "", type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.innerHTML = `<strong>${esc(title)}</strong>${message ? `<span>${esc(message)}</span>` : ""}`;
  $("toastRegion").append(node);
  setTimeout(() => node.remove(), 4200);
}

function formatTime(timestamp, compact = false) {
  if (!timestamp) return "—";
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return compact
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString();
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

function query(params) {
  const output = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") output.set(key, value);
  });
  return output.toString();
}

async function connect(event) {
  event?.preventDefault();
  const entered = $("adminKey").value.trim();
  if (entered) state.key = entered;
  try {
    state.config = await api("/admin/config/status");
    renderConfig(state.config);
    sessionStorage.setItem("sd2api_admin_key", state.key);
    $("auth").classList.add("hidden");
    setConnection(true);
    await refreshCurrent(false);
  } catch (error) {
    $("authError").textContent = error.message;
  }
}

function currentRoute() {
  const value = location.hash.replace(/^#\/?/, "");
  return routes[value] ? value : "generate";
}

function navigate(route, updateHash = true) {
  if (!routes[route]) route = "generate";
  state.route = route;
  if (updateHash && location.hash !== `#/${route}`) history.replaceState(null, "", `#/${route}`);
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.route === route));
  $$(".page").forEach((page) => page.classList.toggle("active", page.dataset.page === route));
  $("pageEyebrow").textContent = routes[route].eyebrow;
  $("pageTitle").textContent = routes[route].title;
  $("pageSubtitle").textContent = routes[route].subtitle;
  closeMobileMenu();
  refreshCurrent(false);
}

function renderConfig(config) {
  const notes = [];
  if (config.mode !== "browser_pool") notes.push("当前模式不是 browser_pool，号池功能不可用。");
  if (!config.temp_mail_configured) notes.push("尚未配置 cf_temp_mail，邮箱验证码需要手动处理。");
  if (!config.credential_encryption) notes.push("没有可用的凭据加密主密钥。");
  $("configNotice").textContent = notes.join(" ");
  $("configNotice").classList.toggle("hidden", notes.length === 0);
}

function enabledCreditTotal(accounts) {
  return accounts.flatMap((account) => account.subaccounts || [])
    .filter((sub) => sub.enabled && sub.seedance_access === true && sub.credits !== null)
    .reduce((sum, sub) => sum + Number(sub.credits || 0), 0);
}

function renderPool(pool, accounts = state.accounts) {
  state.pool = pool;
  const running = Number(pool.running_jobs || pool.queued_jobs || 0);
  $("gCredits").textContent = enabledCreditTotal(accounts).toLocaleString();
  $("gOnline").textContent = pool.logged_in || 0;
  $("gParallel").textContent = pool.max_parallel || 0;
  $("gRunning").textContent = running;
  $("sTotal").textContent = pool.total || 0;
  $("sOnline").textContent = pool.logged_in || 0;
  $("sLogin").textContent = pool.logging_in || 0;
  $("sCaptcha").textContent = pool.captcha_required || 0;
  $("sSubs").textContent = pool.enabled_subaccounts || 0;
  $("sParallel").textContent = pool.max_parallel || 0;
  $("navAccountBadge").textContent = pool.total || 0;
  $("navAccountBadge").classList.toggle("hidden", !pool.total);
  $("navTaskBadge").textContent = running;
  $("navTaskBadge").classList.toggle("hidden", !running);
}

function renderRecentTasks(items) {
  const root = $("recentGenerateTasks");
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact-empty"><strong>还没有任务</strong><p>从左侧表单创建第一个视频。</p></div>';
    return;
  }
  root.innerHTML = items.slice(0, 4).map((task) => `
    <article class="recent-task">
      <div class="recent-task-top"><span class="mono">${esc(task.id)}</span>${pill(task.status)}</div>
      <p>${esc(task.prompt || "无提示词")}</p>
      <footer><span>${esc(task.upstream_model || task.model)}</span><span>${task.seconds}s · ${formatTime(task.updated_at, true)}</span></footer>
    </article>`).join("");
}

async function refreshGenerate() {
  const [pool, accountData, taskData] = await Promise.all([
    api("/admin/pool/status"),
    api("/admin/accounts"),
    api("/admin/tasks?limit=8&refresh_pending=true"),
  ]);
  state.accounts = accountData.data || [];
  state.tasks = taskData.data || [];
  renderPool(pool, state.accounts);
  renderRecentTasks(state.tasks);
  if (state.focusTask?.id) {
    try {
      const task = await api(`/v1/videos/${encodeURIComponent(state.focusTask.id)}`);
      state.focusTask = { ...task, status: normalizeStatus(task.status) };
      renderFocusTask();
    } catch (error) {
      console.warn(error);
    }
  }
}

function backendLabel(account) {
  if (account.backend === "protocol") return '<span class="pill ok">HTTP 协议</span><span class="cell-sub">Chromium 已关闭</span>';
  if (account.backend === "browser") return '<span class="pill wait">Chromium 登录</span><span class="cell-sub">完成后自动关闭</span>';
  return '<span class="pill">已停止</span>';
}

function renderSubaccounts(account) {
  const items = account.subaccounts || [];
  if (!items.length) return '<div class="table-empty">尚未发现子账号。登录后点击“刷新子账号”获取权限与 Credits。</div>';
  return `<div class="subaccounts">${items.map((sub) => `
    <div class="sub-row">
      <label class="sub-name">
        <input type="checkbox" data-sub-toggle data-account-id="${esc(account.id)}" data-advertiser-id="${esc(sub.advertiser_id)}" ${sub.enabled ? "checked" : ""} ${sub.seedance_access !== true ? "disabled" : ""}>
        <div><strong>${esc(sub.name)}</strong><span class="cell-sub mono">${esc(sub.advertiser_id)} · ${esc(sub.account_type)}</span></div>
      </label>
      <div>${sub.seedance_access === true ? '<span class="pill ok">SD2 可用</span>' : sub.seedance_access === false ? '<span class="pill bad">无 SD2 权限</span>' : '<span class="pill wait">未检查</span>'}</div>
      <div><strong>${sub.credits ?? "—"}</strong><span class="cell-sub">Credits</span></div>
      <div>${sub.active ? '<span class="pill info">当前</span>' : '<span class="muted">待调度</span>'}</div>
      <div class="sub-error">${esc(sub.last_error || "")}</div>
    </div>`).join("")}</div>`;
}

function renderAccounts(items) {
  const body = $("accounts");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">还没有账号。点击“添加账号”开始构建号池。</td></tr>';
    return;
  }
  body.innerHTML = items.map((account) => `
    <tr>
      <td><span class="cell-title">${esc(account.name)}</span><span class="cell-sub">${esc(account.username || "")}</span><span class="cell-sub mono">${esc(account.id)}</span></td>
      <td>${pill(account.login_state)}<span class="cell-sub">${account.logged_in ? "协议会话有效" : "会话不可用"}</span></td>
      <td>${backendLabel(account)}</td>
      <td><strong>${(account.subaccounts || []).filter((sub) => sub.enabled).length}</strong> / ${(account.subaccounts || []).length}<span class="cell-sub">已启用 / 已发现</span></td>
      <td>${account.busy ? '<span class="pill info">运行中</span>' : '<span class="muted">空闲</span>'}<span class="cell-sub">队列 ${account.queued || 0}</span></td>
      <td><span class="task-error-inline">${esc(account.login_error || account.last_error || "")}</span></td>
      <td><div class="row-actions">
        <button class="button ghost" data-account-action="login" data-account-id="${esc(account.id)}" type="button">登录</button>
        <button class="button ghost" data-account-action="refresh" data-account-id="${esc(account.id)}" type="button">刷新子账号</button>
        <button class="button ghost" data-account-action="focus" data-account-id="${esc(account.id)}" type="button">打开 Chromium</button>
        <button class="button ghost" data-account-action="edit" data-account-id="${esc(account.id)}" type="button">编辑</button>
        <button class="button ghost" data-account-action="${account.running ? "stop" : "start"}" data-account-id="${esc(account.id)}" type="button">${account.running ? "停止" : "启动"}</button>
        <button class="button danger" data-account-action="delete" data-account-id="${esc(account.id)}" type="button">删除</button>
      </div></td>
    </tr>
    <tr class="subaccount-row"><td colspan="7">${renderSubaccounts(account)}</td></tr>`).join("");
}

async function refreshAccounts() {
  const [pool, config, accountData] = await Promise.all([
    api("/admin/pool/status"), api("/admin/config/status"), api("/admin/accounts"),
  ]);
  state.accounts = accountData.data || [];
  state.config = config;
  renderConfig(config);
  renderPool(pool, state.accounts);
  renderAccounts(state.accounts);
}

function logLevelLabel(level) {
  return ({ info: "INFO", success: "SUCCESS", warning: "WARN", error: "ERROR" })[level] || String(level || "INFO").toUpperCase();
}

function renderLogs(items) {
  $("logCount").textContent = `${items.length} 条`;
  $("navLogBadge").classList.toggle("hidden", !items.some((item) => item.level === "error"));
  if (!items.length) {
    $("logs").innerHTML = '<div class="empty-state"><strong>没有匹配的日志</strong><p>调整筛选条件后重试。</p></div>';
    return;
  }
  $("logs").innerHTML = items.map((item) => {
    const details = item.details ? JSON.stringify(item.details, null, 2) : "";
    const context = [item.account_id && `账号 ${item.account_id}`, item.task_id && `任务 ${item.task_id}`].filter(Boolean).join("\n");
    return `<article class="log-entry">
      <time class="log-time">${formatTime(item.created_at)}</time>
      <div><span class="log-level ${esc(item.level)}">${esc(logLevelLabel(item.level))}</span></div>
      <div class="log-message"><strong>${esc(item.message)}</strong>${details ? `<pre>${esc(details)}</pre>` : ""}</div>
      <div class="log-context">${esc(context || item.category)}</div>
    </article>`;
  }).join("");
}

async function refreshLogs() {
  const params = query({
    limit: 300,
    level: $("logLevel").value,
    category: $("logCategory").value,
    search: $("logSearch").value.trim(),
  });
  const data = await api(`/admin/logs?${params}`);
  state.logs = data.data || [];
  renderLogs(state.logs);
}

function renderVideoSummary(summary) {
  $("vTotal").textContent = summary.total || 0;
  $("vActive").textContent = Number(summary.queued || 0) + Number(summary.running || 0);
  $("vSucceeded").textContent = summary.succeeded || 0;
  $("vFailed").textContent = summary.failed || 0;
}

function renderTasks(items) {
  $("videoCount").textContent = `${items.length} 条`;
  const body = $("tasks");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">没有匹配的视频任务。</td></tr>';
    return;
  }
  body.innerHTML = items.map((task) => {
    const alias = task.model !== task.upstream_model ? `<span class="cell-sub">请求别名 ${esc(task.model)}</span>` : "";
    const error = task.error_message ? `<span class="task-error-inline">${esc(task.error_code || "generation_failed")} · ${esc(task.error_message)}</span>` : "";
    return `<tr>
      <td class="task-main"><span class="cell-title mono">${esc(task.id)}</span><span class="task-prompt">${esc(task.prompt || "无提示词")}</span>${error}</td>
      <td class="model-label"><strong>${esc(task.upstream_model || task.model)}</strong>${alias}<span class="cell-sub">${esc(String(task.api || "").toUpperCase())}</span></td>
      <td>${pill(task.status)}<span class="cell-sub">${Number(task.progress || 0)}%</span></td>
      <td><strong>${task.seconds}s</strong><span class="cell-sub">${esc(task.resolution || "720p")}</span></td>
      <td><span class="cell-title">${esc(task.account_id || "—")}</span><span class="cell-sub mono">${esc(task.advertiser_id || "—")}</span></td>
      <td><span class="cell-title">${formatTime(task.updated_at)}</span><span class="cell-sub">创建于 ${formatTime(task.created_at)}</span></td>
      <td><div class="row-actions">
        ${task.downloadable ? `<button class="button primary" data-task-action="download" data-task-id="${esc(task.id)}" type="button">下载</button>` : ""}
        <button class="button ghost" data-task-action="copy" data-task-id="${esc(task.id)}" type="button">复制 ID</button>
        <button class="button danger" data-task-action="delete" data-task-id="${esc(task.id)}" type="button">删除记录</button>
      </div></td>
    </tr>`;
  }).join("");
}

async function refreshVideos() {
  const params = query({
    limit: 200,
    status: $("videoStatus").value,
    search: $("videoSearch").value.trim(),
    refresh_pending: $("videoAutoRefresh").checked,
  });
  const data = await api(`/admin/tasks?${params}`);
  state.tasks = data.data || [];
  state.taskSummary = data.summary || {};
  renderVideoSummary(state.taskSummary);
  renderTasks(state.tasks);
  const active = Number(state.taskSummary.queued || 0) + Number(state.taskSummary.running || 0);
  $("navTaskBadge").textContent = active;
  $("navTaskBadge").classList.toggle("hidden", !active);
}

async function refreshCurrent(userInitiated = false) {
  if (!state.key || state.refreshing || !$("auth").classList.contains("hidden")) return;
  state.refreshing = true;
  $("refreshButton").disabled = true;
  try {
    if (state.route === "generate") await refreshGenerate();
    if (state.route === "accounts") await refreshAccounts();
    if (state.route === "logs") await refreshLogs();
    if (state.route === "videos") await refreshVideos();
    setConnection(true);
    $("lastUpdated").textContent = `更新于 ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (userInitiated && state.route !== "generate") toast("已刷新", routes[state.route].title);
  } catch (error) {
    console.error(error);
    setConnection(false, "刷新失败");
    if (userInitiated) toast("刷新失败", error.message, "error");
  } finally {
    state.refreshing = false;
    $("refreshButton").disabled = false;
  }
}

function setGenerationMode(mode) {
  $("generationMode").value = mode;
  $$(".mode-button").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("imageUploadBlock").classList.toggle("hidden", mode !== "image");
  $("referenceUploadBlock").classList.toggle("hidden", mode !== "reference");
}

function renderFiles(input, target) {
  const files = [...input.files];
  $(target).innerHTML = files.map((file) => `
    <div class="file-item"><div><strong>${esc(file.name)}</strong><span>${formatBytes(file.size)}</span></div><span class="file-kind">${esc((file.type || "file").split("/")[0])}</span></div>`).join("");
}

function updateDuration() {
  const seconds = Number($("generationDuration").value);
  $("durationValue").textContent = `${seconds} 秒`;
  $("creditEstimate").textContent = `${seconds} Credits`;
  $("generateSubmit").querySelector("small").textContent = `预计 ${seconds} Credits`;
}

function validateReferences(files) {
  const counts = { image: 0, video: 0, audio: 0 };
  files.forEach((file) => { if (counts[file.type.split("/")[0]] !== undefined) counts[file.type.split("/")[0]] += 1; });
  if (!counts.image && !counts.video) throw new Error("参考生视频至少需要一张图片或一个视频");
  if (counts.image > 9 || counts.video > 3 || counts.audio > 3) throw new Error("参考素材上限为 9 张图片、3 个视频和 3 段音频");
}

async function submitGeneration(event) {
  event.preventDefault();
  const mode = $("generationMode").value;
  const prompt = $("generationPrompt").value.trim();
  const firstFrame = $("firstFrameInput").files[0];
  const references = [...$("referenceInput").files];
  $("generateError").textContent = "";
  try {
    if (!prompt) throw new Error("请输入提示词");
    if (mode === "image" && !firstFrame) throw new Error("图生视频需要上传一张首帧图片");
    if (mode === "reference") validateReferences(references);
    const body = new FormData();
    body.append("model", $("generationModel").value);
    body.append("prompt", prompt);
    body.append("seconds", $("generationDuration").value);
    body.append("size", "720x1280");
    if (mode === "image") body.append("input_reference", firstFrame);
    if (mode === "reference") references.forEach((file) => body.append("reference_media", file));
    $("generateSubmit").disabled = true;
    $("generateSubmit").querySelector("span").textContent = "正在提交…";
    const task = await api("/v1/videos", { method: "POST", body });
    state.focusTask = { ...task, status: normalizeStatus(task.status) };
    renderFocusTask();
    toast("任务已提交", task.id);
    await refreshGenerate();
  } catch (error) {
    $("generateError").textContent = error.message;
    toast("提交失败", error.message, "error");
  } finally {
    $("generateSubmit").disabled = false;
    $("generateSubmit").querySelector("span").textContent = "开始生成";
  }
}

function renderFocusTask() {
  const task = state.focusTask;
  if (!task) return;
  const status = normalizeStatus(task.status);
  const progress = status === "succeeded" || status === "failed" ? 100 : status === "running" ? Math.max(50, Number(task.progress || 0)) : 8;
  $("generationEmpty").classList.add("hidden");
  $("generationResult").classList.remove("hidden");
  $("generationResult").innerHTML = `
    <div class="task-focus-head">${pill(status)}<span class="mono task-id">${esc(task.id)}</span></div>
    <div class="task-progress"><span style="width:${progress}%"></span></div>
    <div class="task-meta"><div><span>模型</span><strong>${esc(task.model || "seedance-2.0")}</strong></div><div><span>时长</span><strong>${esc(task.seconds || task.duration || 5)} 秒</strong></div></div>
    ${task.error ? `<div class="task-error">${esc(task.error.code || "generation_failed")}<br>${esc(task.error.message || "生成失败")}</div>` : ""}
    <div class="row-actions">
      ${status === "succeeded" ? `<button class="button primary" data-focus-action="download" type="button">下载视频</button>` : ""}
      <button class="button ghost" data-route-link="videos" type="button">视频管理</button>
    </div>`;
}

function openAddAccount() {
  state.editId = null;
  $("dialogTitle").textContent = "添加账号";
  $("passwordHint").textContent = "";
  $("accountForm").reset();
  $("remarkField").classList.add("hidden");
  $("accountForm").elements.name.disabled = true;
  $("accountForm").elements.password.required = true;
  $("startField").classList.remove("hidden");
  $("accountForm").elements.auto_login.checked = true;
  $("accountForm").elements.start.checked = true;
  $("formError").textContent = "";
  $("accountDialog").showModal();
}

function openEditAccount(id) {
  const account = state.accounts.find((item) => item.id === id);
  if (!account) return;
  state.editId = id;
  $("dialogTitle").textContent = "编辑账号与备注";
  $("passwordHint").textContent = "（留空表示不修改）";
  $("accountForm").reset();
  $("remarkField").classList.remove("hidden");
  $("accountForm").elements.name.disabled = false;
  $("accountForm").elements.name.required = true;
  $("accountForm").elements.password.required = false;
  $("startField").classList.add("hidden");
  $("accountForm").elements.name.value = account.name || "";
  $("accountForm").elements.username.value = account.username || "";
  $("accountForm").elements.auto_login.checked = Boolean(account.auto_login);
  $("formError").textContent = "";
  $("accountDialog").showModal();
}

async function saveAccount(event) {
  event.preventDefault();
  const form = new FormData($("accountForm"));
  const data = { username: form.get("username"), auto_login: form.get("auto_login") === "on" };
  const password = form.get("password");
  if (password) data.password = password;
  if (state.editId) data.name = form.get("name");
  else data.start = form.get("start") === "on";
  try {
    await api(state.editId ? `/admin/accounts/${encodeURIComponent(state.editId)}` : "/admin/accounts", {
      method: state.editId ? "PATCH" : "POST",
      body: JSON.stringify(data),
    });
    $("accountDialog").close();
    toast(state.editId ? "账号已更新" : "账号已添加");
    await refreshAccounts();
  } catch (error) {
    $("formError").textContent = error.message;
  }
}

async function accountAction(action, id) {
  if (action === "edit") return openEditAccount(id);
  if (action === "delete" && !confirm(`删除账号 ${id} 的管理记录？浏览器 Profile 会保留。`)) return;
  try {
    if (action === "delete") await api(`/admin/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
    else if (action === "refresh") await api(`/admin/accounts/${encodeURIComponent(id)}/subaccounts/refresh`, { method: "POST", body: JSON.stringify({ check_access: true }) });
    else await api(`/admin/accounts/${encodeURIComponent(id)}/${action}`, { method: "POST", body: action === "login" ? JSON.stringify({ wait: false }) : undefined });
    toast("操作已提交", ({ login: "登录流程已启动", refresh: "子账号已刷新", focus: "Chromium 已打开", start: "账号已启动", stop: "账号已停止", delete: "账号已移除" })[action] || action);
    await refreshAccounts();
  } catch (error) {
    toast("操作失败", error.message, "error");
  }
}

async function toggleSubaccount(input) {
  try {
    await api(`/admin/accounts/${encodeURIComponent(input.dataset.accountId)}/subaccounts/${encodeURIComponent(input.dataset.advertiserId)}`, {
      method: "PATCH", body: JSON.stringify({ enabled: input.checked }),
    });
    toast(input.checked ? "子账号已启用" : "子账号已停用", input.dataset.advertiserId);
    await refreshAccounts();
  } catch (error) {
    input.checked = !input.checked;
    toast("更新失败", error.message, "error");
  }
}

async function downloadTask(id) {
  try {
    toast("正在准备下载", id);
    const response = await fetch(`/v1/videos/${encodeURIComponent(id)}/content`, { headers: { Authorization: `Bearer ${state.key}` } });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const data = await response.json(); detail = data?.error?.message || data?.detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${id}.mp4`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast("下载已开始", `${id}.mp4`);
  } catch (error) {
    toast("下载失败", error.message, "error");
  }
}

async function deleteTask(id) {
  if (!confirm(`删除任务 ${id} 的本地记录？TikTok 上游任务不会被取消。`)) return;
  try {
    await api(`/v1/videos/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (state.focusTask?.id === id) state.focusTask = null;
    toast("任务记录已删除", id);
    await refreshVideos();
  } catch (error) {
    toast("删除失败", error.message, "error");
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制", text);
  } catch {
    toast("复制失败", "请手动选择任务 ID", "error");
  }
}

function openMobileMenu() {
  $("sidebar").classList.add("open");
  $("mobileBackdrop").classList.remove("hidden");
}

function closeMobileMenu() {
  $("sidebar").classList.remove("open");
  $("mobileBackdrop").classList.add("hidden");
}

function debounce(fn, delay = 300) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function bindEvents() {
  $("authForm").addEventListener("submit", connect);
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.route)));
  document.addEventListener("click", (event) => {
    const routeLink = event.target.closest("[data-route-link]");
    if (routeLink) navigate(routeLink.dataset.routeLink);
  });
  $("refreshButton").addEventListener("click", () => refreshCurrent(true));
  $("mobileMenu").addEventListener("click", openMobileMenu);
  $("mobileBackdrop").addEventListener("click", closeMobileMenu);
  $$(".mode-button").forEach((button) => button.addEventListener("click", () => setGenerationMode(button.dataset.mode)));
  $("generationDuration").addEventListener("input", updateDuration);
  $("generationPrompt").addEventListener("input", () => { $("promptCount").textContent = $("generationPrompt").value.length; });
  $("firstFrameInput").addEventListener("change", () => renderFiles($("firstFrameInput"), "firstFramePreview"));
  $("referenceInput").addEventListener("change", () => renderFiles($("referenceInput"), "referencePreview"));
  $("generateForm").addEventListener("submit", submitGeneration);
  $("generationResult").addEventListener("click", (event) => {
    if (event.target.closest('[data-focus-action="download"]') && state.focusTask) downloadTask(state.focusTask.id);
  });
  $("addAccountButton").addEventListener("click", openAddAccount);
  $("accountForm").addEventListener("submit", saveAccount);
  $$('[data-dialog-close]').forEach((button) => button.addEventListener("click", () => $("accountDialog").close()));
  $("accounts").addEventListener("click", (event) => {
    const button = event.target.closest("[data-account-action]");
    if (button) accountAction(button.dataset.accountAction, button.dataset.accountId);
  });
  $("accounts").addEventListener("change", (event) => {
    if (event.target.matches("[data-sub-toggle]")) toggleSubaccount(event.target);
  });
  const refreshLogFilters = debounce(() => refreshCurrent(false));
  $("logSearch").addEventListener("input", refreshLogFilters);
  $("logLevel").addEventListener("change", () => refreshCurrent(false));
  $("logCategory").addEventListener("change", () => refreshCurrent(false));
  const refreshVideoFilters = debounce(() => refreshCurrent(false));
  $("videoSearch").addEventListener("input", refreshVideoFilters);
  $("videoStatus").addEventListener("change", () => refreshCurrent(false));
  $("videoAutoRefresh").addEventListener("change", () => refreshCurrent(false));
  $("tasks").addEventListener("click", (event) => {
    const button = event.target.closest("[data-task-action]");
    if (!button) return;
    if (button.dataset.taskAction === "download") downloadTask(button.dataset.taskId);
    if (button.dataset.taskAction === "copy") copyText(button.dataset.taskId);
    if (button.dataset.taskAction === "delete") deleteTask(button.dataset.taskId);
  });
  window.addEventListener("hashchange", () => navigate(currentRoute(), false));
}

function startPolling() {
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (!state.key || !$("auth").classList.contains("hidden")) return;
    if (state.route === "logs" && !$("logAutoRefresh").checked) return;
    if (state.route === "videos" && !$("videoAutoRefresh").checked) return;
    refreshCurrent(false);
  }, 5000);
}

function init() {
  bindEvents();
  $("novnc").href = `${location.protocol}//${location.hostname}:6080/vnc.html?autoconnect=1&resize=scale`;
  setGenerationMode("text");
  updateDuration();
  navigate(currentRoute(), false);
  startPolling();
  if (state.key) {
    $("adminKey").value = state.key;
    connect();
  } else {
    showAuth();
  }
}

init();
