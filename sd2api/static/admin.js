const $ = (id) => document.getElementById(id);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[char]));
const formatUnix = (value) => value
  ? new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12: false })
  : "—";

const state = {
  key: sessionStorage.getItem("sd2api_admin_key") || "",
  route: "generate",
  accounts: [],
  pool: null,
  config: null,
  tasks: [],
  logs: [],
  logPage: 1,
  logPageSize: 50,
  logTotal: 0,
  videoPage: 1,
  videoPageSize: 50,
  videoTotal: 0,
  taskSummary: {},
  analytics: null,
  analyticsRange: "30d",
  editId: null,
  focusTask: null,
  refreshing: false,
  refreshQueued: false,
  timer: null,
};

async function loadAppVersion() {
  try {
    const response = await fetch("/admin/version", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    $("appVersion").textContent = `v${payload.version || "unknown"}`;
  } catch {
    $("appVersion").textContent = "未知";
  }
}

loadAppVersion();

const routes = {
  generate: { eyebrow: "CREATE", title: "生视频", subtitle: "创建并跟踪 Seedance 视频任务" },
  accounts: { eyebrow: "ACCOUNT POOL", title: "号池管理", subtitle: "管理登录账号、子账号权限与 Credits" },
  logs: { eyebrow: "ACTIVITY", title: "日志", subtitle: "查看账号、登录、视频与系统事件" },
  analytics: { eyebrow: "BEST TIME", title: "最佳生成时段", subtitle: "根据历史等待时间判断每天适合提交视频的时段" },
  videos: { eyebrow: "VIDEO LIBRARY", title: "视频管理", subtitle: "自动同步状态并管理所有生成任务" },
  settings: { eyebrow: "RUNTIME CONFIG", title: "系统配置", subtitle: "编辑可复制的非敏感运行配置" },
};

const settingsSchema = [
  { title: "基础服务", description: "服务模式、上游地址和本地持久化路径。", fields: [
    ["mode", "运行模式", "select", [["browser_pool", "browser_pool（推荐）"], ["browser", "browser（网页回退）"], ["direct", "direct（旧模式）"]]],
    ["tiktok_base_url", "TikTok 基础地址", "text"],
    ["tiktok_user_agent", "浏览器 User-Agent", "text"],
    ["database", "数据库文件", "text"],
    ["request_timeout", "请求超时（秒）", "number", { min: 1, max: 300, step: 1 }],
  ]},
  { title: "浏览器与登录", description: "Chromium 只负责登录，协议模式下生成任务不会依赖浏览器常驻。", fields: [
    ["browser_profile", "浏览器 Profile 目录", "text"],
    ["browser_channel", "浏览器 Channel", "text", { placeholder: "Docker/Chromium 留空" }],
    ["browser_headless", "无头浏览器", "boolean"],
    ["browser_autostart", "启动时恢复号池", "boolean"],
    ["novnc_public_port", "noVNC 公网端口", "number", { min: 1, max: 65535 }],
    ["browser_max_wait", "浏览器最长等待（秒）", "number", { min: 60, max: 7200 }],
    ["auto_login", "自动登录", "boolean"],
    ["login_timeout", "登录超时（秒）", "number", { min: 60, max: 3600 }],
    ["relogin_interval", "登录状态检查间隔（秒）", "number", { min: 30, max: 86400 }],
    ["session_keepalive_interval", "浏览器保活间隔（秒）", "number", { min: 3600, max: 86400 }],
    ["temp_mail_poll_seconds", "邮箱轮询间隔（秒）", "number", { min: 1, max: 30, step: 0.5 }],
    ["temp_mail_timeout", "邮箱验证码超时（秒）", "number", { min: 30, max: 900 }],
  ]},
  { title: "号池调度", description: "控制全池活动任务、启动并发和每日额度熔断。", fields: [
    ["pool_max_pending", "全池最大活动任务", "number", { min: 1, max: 100000 }],
    ["pool_daily_quota_codes", "每日额度错误码", "text", { placeholder: "多个错误码用逗号分隔" }],
    ["pool_rate_limit_cooldown", "RPM 限流冷却（秒）", "number", { min: 1, max: 3600 }],
    ["pool_generation_limit_cooldown", "5 分钟限流冷却（秒）", "number", { min: 1, max: 3600 }],
    ["pool_start_concurrency", "号池启动并发", "number", { min: 1, max: 50 }],
  ]},
  { title: "飞书通知", description: "账号需要验证码、手动接码、重新登录或浏览器恢复时通知个人或群聊。", fields: [
    ["feishu_enabled", "启用飞书通知", "boolean"],
    ["feishu_notify_manual_action", "人工操作时通知", "boolean"],
    ["feishu_app_id", "App ID", "text", { placeholder: "cli_xxxxxxxxxxxxxxxx" }],
    ["feishu_app_secret", "App Secret", "password", { autocomplete: "new-password" }],
    ["feishu_receive_id_type", "接收对象类型", "select", [["chat_id", "群聊"], ["open_id", "个人"]]],
    ["feishu_receive_id", "接收对象", "target"],
    ["feishu_instance_name", "实例名称", "text", { placeholder: "例如：实例 A" }],
    ["feishu_novnc_url", "noVNC 地址", "text", { placeholder: "例如：http://服务器:16080/vnc.html" }],
  ]},
  { title: "协议上传", description: "素材上传并发、直传阈值和分片大小，字节数可直接复制。", fields: [
    ["protocol_upload_concurrency", "上传并发", "number", { min: 1, max: 16 }],
    ["protocol_direct_upload_bytes", "直传阈值（字节）", "number", { min: 262144, max: 67108864 }],
    ["protocol_slice_bytes", "分片大小（字节）", "number", { min: 1048576, max: 33554432 }],
    ["upload_dir", "暂存目录", "text"],
    ["upload_max_bytes", "总上传上限（字节）", "number", { min: 1024, max: 524288000 }],
    ["upload_image_max_bytes", "图片上限（字节）", "number", { min: 1024, max: 104857600 }],
    ["upload_video_max_bytes", "视频上限（字节）", "number", { min: 1024, max: 524288000 }],
    ["upload_audio_max_bytes", "音频上限（字节）", "number", { min: 1024, max: 104857600 }],
    ["upload_max_pixels", "图片像素上限", "number", { min: 1000000, max: 200000000 }],
  ]},
];

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
  keepalive: ["保活中", "info"], failed: ["失败", "bad"],
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
    const detail = typeof data?.detail === "string"
      ? data.detail
      : (data ? JSON.stringify(data.detail || data) : "");
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

function setNovncUrl(port = 6080) {
  const novncUrl = new URL(location.href);
  novncUrl.port = String(port);
  novncUrl.pathname = "/vnc.html";
  novncUrl.search = "?autoconnect=1&resize=scale";
  novncUrl.hash = "";
  $("novnc").href = novncUrl.toString();
}

function renderConfig(config) {
  if (config.version) $("appVersion").textContent = "v" + config.version;
  setNovncUrl(config.novnc_public_port || 6080);
  const notes = [];
  if (config.mode !== "browser_pool") notes.push("当前模式不是 browser_pool，号池功能不可用。");
  if (!config.temp_mail_configured) notes.push("尚未配置 cf_temp_mail，邮箱验证码需要手动处理。");
  if (!config.credential_encryption) notes.push("没有可用的凭据加密主密钥。");
  $("configNotice").textContent = notes.join(" ");
  $("configNotice").classList.toggle("hidden", notes.length === 0);
}

function settingsFieldMarkup(field, config) {
  const [name, label, type, options = {}] = field;
  const value = config[name];
  if (type === "boolean") {
    return `<label class="setting-toggle"><span><strong>${esc(label)}</strong><small>${name}</small></span><span class="switch"><input name="${esc(name)}" type="checkbox" ${value ? "checked" : ""}><span class="switch-track"><span class="switch-thumb"></span></span></span></label>`;
  }
  if (type === "select") {
    return `<label class="field setting-field"><span>${esc(label)}<small>${esc(name)}</small></span><select name="${esc(name)}">${options.map(([optionValue, optionLabel]) => `<option value="${esc(optionValue)}" ${value === optionValue ? "selected" : ""}>${esc(optionLabel)}</option>`).join("")}</select></label>`;
  }
  if (type === "target") {
    return `<label class="field setting-field"><span>${esc(label)}<small>${esc(name)}</small></span><select name="${esc(name)}" data-current-value="${esc(value)}"><option value="${esc(value)}">${value ? "正在读取飞书通讯录…" : "请先保存 App ID 和 App Secret"}</option></select></label>`;
  }
  const attributes = typeof options === "object" ? Object.entries(options).map(([key, item]) => `${key}="${esc(item)}"`).join(" ") : "";
  return `<label class="field setting-field"><span>${esc(label)}<small>${esc(name)}</small></span><input name="${esc(name)}" type="${type}" value="${esc(value)}" ${attributes}></label>`;
}

function renderSettings(data) {
  const config = data.config || {};
  state.runtimeConfig = config;
  $("configFilePath").textContent = data.path || "config.json";
  $("configSourceBadge").textContent = ({ file: "配置文件", legacy_env: "旧环境变量兼容", defaults: "内置默认值", explicit: "临时配置" })[data.source] || data.source || "配置文件";
  $("settingsGroups").innerHTML = settingsSchema.map((group) => `<section class="settings-group"><div class="settings-group-heading"><h3>${esc(group.title)}</h3><p>${esc(group.description)}</p></div><div class="settings-grid">${group.fields.map((field) => settingsFieldMarkup(field, config)).join("")}</div></section>`).join("");
  const secret = $("settingsForm").elements.feishu_app_secret;
  if (secret && data.feishu_secret_configured) secret.placeholder = "已配置，留空保留原密钥";
  const targetType = $("settingsForm").elements.feishu_receive_id_type;
  if (targetType) targetType.addEventListener("change", () => loadFeishuTargets(true));
  if (data.feishu_secret_configured) loadFeishuTargets(false);
  $("settingsRestartHint").textContent = data.restart_required ? "上次修改包含需重启项目" : "部分路径和运行模式修改保存后需要重启服务";
}

async function loadFeishuTargets(clearSelection) {
  const form = $("settingsForm");
  const type = form.elements.feishu_receive_id_type.value;
  const select = form.elements.feishu_receive_id;
  const current = clearSelection ? "" : String(select.dataset.currentValue || select.value || "");
  select.disabled = true;
  select.innerHTML = '<option value="">正在读取飞书通讯录…</option>';
  try {
    const result = await api(`/admin/notifications/feishu/targets?receive_id_type=${encodeURIComponent(type)}`);
    const items = result.data || [];
    select.innerHTML = `<option value="">请选择${type === "chat_id" ? "群聊" : "联系人"}</option>${items.map((item) => `<option value="${esc(item.id)}" ${item.id === current ? "selected" : ""}>${esc(item.name)}</option>`).join("")}`;
    select.dataset.currentValue = current;
    if (!items.length) toast("未读取到接收对象", "请确认飞书应用的通讯录或群聊权限", "error");
  } catch (error) {
    select.innerHTML = `<option value="${esc(current)}">${current ? "当前已保存对象（通讯录读取失败）" : "通讯录读取失败"}</option>`;
    toast("读取飞书通讯录失败", error.message, "error");
  } finally {
    select.disabled = false;
  }
}

function renderApiKeys(items) {
  const root = $("apiKeys");
  if (!items.length) {
    root.innerHTML = '<div class="table-empty">尚未配置 API Key。</div>';
    return;
  }
  root.innerHTML = items.map((item) => `<div class="api-key-row">
    <div><strong>${esc(item.name)}</strong><span class="cell-sub mono">${esc(item.masked_key)}</span></div>
    <div><span class="cell-sub">${item.managed ? `创建于 ${formatTime(item.created_at)}` : "来自环境变量，需在部署配置中修改"}</span></div>
    <div>${item.managed ? `<button class="button danger" data-api-key-delete="${esc(item.id)}" type="button">删除</button>` : '<span class="pill info">只读</span>'}</div>
  </div>`).join("");
}

async function refreshApiKeys() {
  const data = await api("/admin/api-keys");
  renderApiKeys(data.data || []);
}

async function refreshSettings() {
  const [config, keys] = await Promise.all([api("/admin/config"), api("/admin/api-keys")]);
  renderSettings(config);
  renderApiKeys(keys.data || []);
}

async function addApiKey(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const customKey = String(form.get("key") || "").trim();
  const body = { name: String(form.get("name") || "API Key").trim() };
  if (customKey) body.key = customKey;
  try {
    const result = await api("/admin/api-keys", { method: "POST", body: JSON.stringify(body) });
    let copied = false;
    if (!customKey && navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(result.key); copied = true; } catch (_) {}
    }
    if (!customKey && !copied) {
      await api(`/admin/api-keys/${encodeURIComponent(result.id)}`, { method: "DELETE" });
      throw new Error("浏览器未允许写入剪贴板，本次生成已自动撤销；请授权剪贴板后重试，或输入自定义 Key");
    }
    event.currentTarget.reset();
    event.currentTarget.elements.name.value = "API Key";
    await refreshApiKeys();
    toast("API Key 已添加", customKey ? `已保存为 ${result.masked_key}` : "完整 Key 已复制到剪贴板，请立即保存");
  } catch (error) {
    toast("添加失败", error.message, "error");
  }
}

async function deleteApiKey(id) {
  if (!confirm("删除这个 API Key？使用它的客户端会立即无法鉴权。")) return;
  try {
    await api(`/admin/api-keys/${encodeURIComponent(id)}`, { method: "DELETE" });
    await refreshApiKeys();
    toast("API Key 已删除");
  } catch (error) {
    toast("删除失败", error.message, "error");
  }
}

function settingsPayload() {
  const form = new FormData($("settingsForm"));
  const config = {};
  settingsSchema.flatMap((group) => group.fields).forEach(([name, , type]) => {
    if (type === "boolean") config[name] = form.get(name) === "on";
    else if (type === "number") config[name] = Number(form.get(name));
    else config[name] = String(form.get(name) ?? "").trim();
  });
  return config;
}

async function saveSettings(event) {
  event.preventDefault();
  const config = settingsPayload();
  $("settingsError").textContent = "";
  $("settingsSave").disabled = true;
  try {
    const result = await api("/admin/config", { method: "PUT", body: JSON.stringify(config) });
    renderSettings(result);
    toast("配置已保存", result.restart_required ? "部分修改需要重启服务后生效" : "修改已写入 config.json");
  } catch (error) {
    $("settingsError").textContent = error.message;
    toast("保存失败", error.message, "error");
  } finally {
    $("settingsSave").disabled = false;
  }
}

async function testFeishuNotification() {
  const button = $("feishuTest");
  button.disabled = true;
  try {
    await api("/admin/config", { method: "PUT", body: JSON.stringify(settingsPayload()) });
    const result = await api("/admin/notifications/feishu/test", { method: "POST" });
    toast("测试消息已发送", result.message_id || "请检查飞书接收端");
  } catch (error) {
    toast("测试发送失败", error.message, "error");
  } finally {
    button.disabled = false;
  }
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
  return `<div class="subaccounts"><div class="subaccounts-head"><span>子账号</span><span>Seedance 权限</span><span>余额</span><span>加入调度</span><span>说明</span></div>${items.map((sub) => {
    const canSchedule = sub.seedance_access === true;
    const switchLabel = sub.quota_blocked ? "额度熔断中" : sub.rate_limited ? "频率冷却中" : sub.enabled ? "已加入调度" : canSchedule ? "未加入调度" : "不可加入调度";
    const note = canSchedule
      ? (sub.quota_blocked
        ? `今日额度受限，暂停至 ${formatUnix(sub.quota_blocked_until)}`
        : sub.rate_limited
          ? `TikTok 频率限制，暂停至 ${formatUnix(sub.rate_limited_until)}`
        : sub.enabled
          ? `运行中 ${sub.active_tasks || 0} · 今日已提交 ${sub.tasks_today || 0}`
          : "不会接收新任务")
      : "缺少 Seedance 权限";
    const schedulable = canSchedule;
    return `
    <div class="sub-row">
      <div class="sub-name"><div><strong>${esc(sub.name)}</strong><span class="cell-sub mono">${esc(sub.advertiser_id)} · ${esc(sub.account_type)}</span></div></div>
      <div>${sub.seedance_access === true ? '<span class="pill ok">SD2 可用</span>' : sub.seedance_access === false ? '<span class="pill bad">无 SD2 权限</span>' : '<span class="pill wait">未检查</span>'}</div>
      <div><strong>${sub.credits ?? "—"}</strong><span class="cell-sub">Credits</span></div>
      <label class="schedule-control ${schedulable ? "" : "disabled"}">
        <span class="switch">
          <input type="checkbox" data-sub-toggle data-account-id="${esc(account.id)}" data-advertiser-id="${esc(sub.advertiser_id)}" aria-label="${esc(`将 ${sub.name} 加入生成调度`)}" ${sub.enabled ? "checked" : ""} ${schedulable ? "" : "disabled"}>
          <span class="switch-track"><span class="switch-thumb"></span></span>
        </span>
        <span class="switch-label">${esc(switchLabel)}</span>
      </label>
      <div><span class="sub-note ${canSchedule && !sub.quota_blocked && !sub.rate_limited ? "" : "denied"}">${esc(note)}</span>${sub.last_error && !sub.quota_blocked && !sub.rate_limited ? `<span class="sub-error">${esc(sub.last_error)}</span>` : ""}</div>
    </div>`;
  }).join("")}</div>`;
}

function keepaliveLabel(account) {
  const labels = {
    running: "保活中，Chromium 已启动",
    succeeded: "最近保活成功",
    failed: "最近保活失败",
    interrupted: "保活被重启中断",
    idle: "等待首次保活",
  };
  const state = account.keepalive_active ? "running" : (account.keepalive_state || "idle");
  const next = account.keepalive_next_at ? ` · 下次 ${formatTime(account.keepalive_next_at)}` : "";
  const waiting = account.keepalive_active && Number(state.pool?.keepalive_waiting_requests || 0)
    ? ` · ${Number(state.pool.keepalive_waiting_requests)} 个请求等待`
    : "";
  const error = account.keepalive_error ? ` · ${account.keepalive_error}` : "";
  return `<span class="cell-sub">${esc(labels[state] || state)}${esc(waiting)}${esc(next)}${esc(error)}</span>`;
}

function renderAccounts(items) {
  const body = $("accounts");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">还没有账号。点击“添加账号”开始构建号池。</td></tr>';
    return;
  }
  body.innerHTML = items.map((account) => `
    <tr>
      <td><span class="cell-title">${esc(account.username || account.email_address || account.name)}</span>${account.name && account.name !== account.username && !String(account.name).startsWith("account_") ? `<span class="cell-sub">${esc(account.name)}</span>` : ""}</td>
      <td>${pill(account.keepalive_active ? "keepalive" : account.login_state)}<span class="cell-sub">${account.logged_in ? "协议会话有效" : "会话不可用"}</span>${keepaliveLabel(account)}</td>
      <td>${backendLabel(account)}</td>
      <td><strong>${(account.subaccounts || []).filter((sub) => sub.enabled).length}</strong> / ${(account.subaccounts || []).length}<span class="cell-sub">已加入调度 / 已发现</span></td>
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

function renderPagination(rootId, type, page, pageSize, total) {
  const root = $(rootId);
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const start = total ? (page - 1) * pageSize + 1 : 0;
  const end = Math.min(page * pageSize, total);
  root.innerHTML = `<span class="pagination-summary">显示 ${start}-${end}，共 ${total} 条</span>
    <label class="pagination-size">每页 <select data-pagination-size="${type}" aria-label="每页条数">${[50, 100, 200, 500].map((size) => `<option value="${size}" ${size === pageSize ? "selected" : ""}>${size}</option>`).join("")}</select> 条</label>
    <button class="button ghost" data-pagination-page="${type}" data-page="${page - 1}" type="button" ${page <= 1 ? "disabled" : ""}>上一页</button>
    <span class="pagination-pages">第 ${page} / ${pages} 页</span>
    <button class="button ghost" data-pagination-page="${type}" data-page="${page + 1}" type="button" ${page >= pages ? "disabled" : ""}>下一页</button>`;
}

function renderLogs(items) {
  $("logCount").textContent = `共 ${state.logTotal} 条`;
  $("navLogBadge").classList.toggle("hidden", !items.some((item) => item.level === "error"));
  if (!items.length) {
    $("logs").innerHTML = '<div class="empty-state"><strong>没有匹配的日志</strong><p>调整筛选条件后重试。</p></div>';
    return;
  }
  $("logs").innerHTML = items.map((item) => {
    const details = item.details ? JSON.stringify(item.details, null, 2) : "";
    const context = [item.account_email && `账号 ${item.account_email}`, item.task_id && `任务 ${item.task_id}`].filter(Boolean).join("\n");
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
    limit: state.logPageSize,
    page: state.logPage,
    level: $("logLevel").value,
    category: $("logCategory").value,
    search: $("logSearch").value.trim(),
  });
  const data = await api(`/admin/logs?${params}`);
  state.logs = data.data || [];
  state.logTotal = data.pagination?.total || 0;
  const pages = Math.max(1, Math.ceil(state.logTotal / state.logPageSize));
  if (state.logPage > pages) { state.logPage = pages; return refreshLogs(); }
  renderLogs(state.logs);
  renderPagination("logPagination", "log", state.logPage, state.logPageSize, state.logTotal);
}

function formatDuration(seconds, compact = false) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "—";
  const value = Math.max(0, Math.round(Number(seconds)));
  if (value < 60) return `${value} 秒`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (!hours) return `${minutes} 分${compact || value % 60 === 0 ? "" : ` ${value % 60} 秒`}`;
  return `${hours} 小时${minutes ? ` ${minutes} 分` : ""}`;
}

function niceDurationMax(value) {
  const candidates = [900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400];
  return candidates.find((item) => item >= value) || Math.ceil(value / 86400) * 86400;
}

function hourLabel(hour) {
  return `${String(hour).padStart(2, "0")}:00`;
}

function hourRanges(hours = []) {
  if (!hours.length) return "暂无足够样本";
  const sorted = [...new Set(hours)].sort((a, b) => a - b);
  if (sorted.length === 24) return "全天";
  const ranges = [];
  let start = sorted[0];
  let previous = sorted[0];
  for (const hour of sorted.slice(1)) {
    if (hour === previous + 1) { previous = hour; continue; }
    ranges.push([start, previous + 1]);
    start = previous = hour;
  }
  ranges.push([start, previous + 1]);
  if (ranges.length > 1 && ranges[0][0] === 0 && ranges.at(-1)[1] === 24) {
    const first = ranges.shift();
    const last = ranges.pop();
    ranges.unshift([last[0], first[1]]);
  }
  return ranges.map(([from, to]) => `${String(from).padStart(2, "0")}:00–${String(to % 24).padStart(2, "0")}:00`).join("、");
}

function renderHourlyDurationChart(hourly = []) {
  const root = $("hourlyDurationChart");
  const populated = hourly.filter((item) => item.sample_count > 0);
  if (!populated.length) {
    root.innerHTML = '<div class="chart-empty"><span>⌁</span><strong>暂无已完成任务</strong><p>选择更长的统计周期，或等待更多任务完成。</p></div>';
    return;
  }
  const width = matchMedia("(max-width: 680px)").matches ? 920 : Math.max(760, Math.round(root.clientWidth || 1100));
  const height = 410;
  const margin = { top: 20, right: 18, bottom: 76, left: 70 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maxValue = niceDurationMax(Math.max(...populated.map((item) => item.average_seconds || 0)) * 1.08);
  const y = (value) => margin.top + plotHeight - Math.max(0, Number(value || 0)) / maxValue * plotHeight;
  const slot = plotWidth / 24;
  const barWidth = Math.max(12, Math.min(28, slot * .68));
  const ticks = [0, .25, .5, .75, 1];
  const grid = ticks.map((ratio) => {
    const value = maxValue * ratio;
    const lineY = y(value);
    return `<g class="chart-gridline"><line x1="${margin.left}" y1="${lineY}" x2="${width - margin.right}" y2="${lineY}"></line><text x="${margin.left - 12}" y="${lineY + 4}">${esc(formatDuration(value, true))}</text></g>`;
  }).join("");
  const marks = hourly.map((item) => {
    const x = margin.left + slot * item.hour + slot / 2;
    const barY = item.average_seconds === null ? margin.top + plotHeight : y(item.average_seconds);
    const barHeight = item.average_seconds === null ? 0 : margin.top + plotHeight - barY;
    const title = `${hourLabel(item.hour)}–${hourLabel((item.hour + 1) % 24)}\n平均耗时 ${formatDuration(item.average_seconds)}\n历史样本 ${item.sample_count} 个\n置信度 ${{high:"较高",medium:"一般",low:"较低",none:"无数据"}[item.confidence]}`;
    return `<g class="hour-bar-group level-${item.level} ${item.sample_count ? "has-data" : "no-data"}"><title>${esc(title)}</title>
      <rect class="hour-bar" x="${x - barWidth / 2}" y="${barY}" width="${barWidth}" height="${barHeight}" rx="5"></rect>
      ${item.sample_count ? `<text class="hour-value" x="${x}" y="${Math.max(margin.top + 12, barY - 7)}">${esc(formatDuration(item.average_seconds, true))}</text>` : `<line class="no-data-mark" x1="${x - 6}" y1="${margin.top + plotHeight - 3}" x2="${x + 6}" y2="${margin.top + plotHeight - 3}"></line>`}
      <text class="hour-label" x="${x}" y="${height - 38}">${String(item.hour).padStart(2, "0")}</text>
      <text class="hour-samples" x="${x}" y="${height - 17}">${item.sample_count ? `${item.sample_count}个` : "—"}</text>
      <rect class="chart-hitbox" x="${x - slot / 2}" y="${margin.top}" width="${slot}" height="${plotHeight}"></rect></g>`;
  }).join("");
  root.innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${grid}<text class="chart-axis-title" x="15" y="${margin.top + plotHeight / 2}" transform="rotate(-90 15 ${margin.top + plotHeight / 2})">平均生成耗时</text>${marks}</svg><div class="hour-axis-hint">一天中的提交时间（本地时区）</div>`;
}

function renderDurationHeatmap(days = []) {
  const root = $("durationHeatmap");
  if (!days.length) {
    root.innerHTML = '<div class="heatmap-empty">暂无逐日数据</div>';
    return;
  }
  const dayRows = [...days].reverse().map((day) => {
    const label = new Date(`${day.date}T00:00:00`).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
    const cells = day.hours.map((item) => `<div class="heat-cell level-${item.level}" title="${esc(`${day.date} ${hourLabel(item.hour)} · ${formatDuration(item.average_seconds)} · ${item.sample_count} 个样本`)}"><span>${item.average_seconds === null ? "" : formatDuration(item.average_seconds, true)}</span></div>`).join("");
    return `<div class="heatmap-row"><div class="heatmap-date">${esc(label)}</div>${cells}</div>`;
  }).join("");
  const labels = Array.from({length:24}, (_, hour) => `<span>${hour % 2 === 0 ? String(hour).padStart(2, "0") : ""}</span>`).join("");
  root.innerHTML = `<div class="heatmap-scroll"><div class="heatmap-hours"><span></span>${labels}</div>${dayRows}</div>`;
}

function renderAnalytics(data) {
  state.analytics = data;
  const recommended = data.recommended_hours || [];
  const busy = data.busy_hours || [];
  $("recommendedHours").textContent = hourRanges(recommended);
  $("busyHours").textContent = hourRanges(busy);
  $("analyticsSamples").textContent = `${data.completed_samples || 0} 个历史任务`;
  $("recommendedDetail").textContent = recommended.length ? "这些时段平均等待不超过 30 分钟" : "当前还没有平均等待不超过 30 分钟的时段";
  $("busyDetail").textContent = busy.length ? "这些时段历史平均等待超过 1 小时" : "当前没有平均等待超过 1 小时的时段";
  renderHourlyDurationChart(data.hourly || []);
  renderDurationHeatmap(data.heatmap || []);
}

async function refreshAnalytics() {
  const params = query({
    range: state.analyticsRange,
    timezone_offset: -new Date().getTimezoneOffset(),
  });
  renderAnalytics(await api(`/admin/analytics/durations?${params}`));
}

function renderVideoSummary(summary) {
  $("vTotal").textContent = summary.total || 0;
  $("vToday").textContent = summary.today || 0;
  $("vWeek").textContent = summary.week || 0;
  $("vActive").textContent = Number(summary.queued || 0) + Number(summary.running || 0);
  $("vSucceeded").textContent = summary.succeeded || 0;
  $("vFailed").textContent = summary.failed || 0;
}

function renderTasks(items) {
  $("videoCount").textContent = `共 ${state.videoTotal} 条`;
  const body = $("tasks");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="8" class="table-empty">没有匹配的视频任务。</td></tr>';
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
      <td><span class="cell-title mono">${esc(task.api_key || "—")}</span></td>
      <td><span class="cell-title">${esc(task.account_email || "—")}</span><span class="cell-sub mono">${esc(task.advertiser_id || "—")}</span></td>
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
    limit: state.videoPageSize,
    page: state.videoPage,
    status: $("videoStatus").value,
    search: $("videoSearch").value.trim(),
    refresh_pending: $("videoAutoRefresh").checked,
    timezone_offset: -new Date().getTimezoneOffset(),
  });
  const data = await api(`/admin/tasks?${params}`);
  state.tasks = data.data || [];
  state.videoTotal = data.pagination?.total || 0;
  const pages = Math.max(1, Math.ceil(state.videoTotal / state.videoPageSize));
  if (state.videoPage > pages) { state.videoPage = pages; return refreshVideos(); }
  state.taskSummary = data.summary || {};
  renderVideoSummary(state.taskSummary);
  renderTasks(state.tasks);
  renderPagination("videoPagination", "video", state.videoPage, state.videoPageSize, state.videoTotal);
  const active = Number(state.taskSummary.queued || 0) + Number(state.taskSummary.running || 0);
  $("navTaskBadge").textContent = active;
  $("navTaskBadge").classList.toggle("hidden", !active);
}

async function refreshCurrent(userInitiated = false) {
  if (!state.key || !$("auth").classList.contains("hidden")) return;
  if (state.refreshing) {
    state.refreshQueued = true;
    return;
  }
  state.refreshing = true;
  $("refreshButton").disabled = true;
  try {
    if (state.route === "generate") await refreshGenerate();
    if (state.route === "accounts") await refreshAccounts();
    if (state.route === "logs") await refreshLogs();
    if (state.route === "analytics") await refreshAnalytics();
    if (state.route === "videos") await refreshVideos();
    if (state.route === "settings") await refreshSettings();
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
    if (state.refreshQueued) {
      state.refreshQueued = false;
      refreshCurrent(false);
    }
  }
}

function setGenerationMode(mode) {
  $("generationMode").value = mode;
  $$(".mode-button").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("imageUploadBlock").classList.toggle("hidden", mode !== "image");
  $("referenceUploadBlock").classList.toggle("hidden", mode !== "reference");
  const modelSelect = $("generationModel");
  [...modelSelect.options].forEach((option) => {
    const unavailable = option.dataset.referenceOnly === "true" && mode !== "reference";
    option.hidden = unavailable;
    option.disabled = unavailable;
  });
  if (modelSelect.selectedOptions[0]?.disabled) modelSelect.value = "seedance-2.0";
  updateDuration();
}

function renderFiles(input, target) {
  const files = [...input.files];
  $(target).innerHTML = files.map((file) => `
    <div class="file-item"><div><strong>${esc(file.name)}</strong><span>${formatBytes(file.size)}</span></div><span class="file-kind">${esc((file.type || "file").split("/")[0])}</span></div>`).join("");
}

function updateDuration() {
  const seconds = Number($("generationDuration").value);
  const selectedModel = $("generationModel").selectedOptions[0];
  const rate = Number(selectedModel?.dataset.creditRate || 1);
  const credits = seconds * rate;
  $("durationValue").textContent = `${seconds} 秒`;
  $("generationModelBadge").textContent = (selectedModel?.textContent || "Seedance 2.0")
    .replace("Dreamina ", "").replace(/（.*$/, "");
  $("creditEstimate").textContent = `${credits} Credits`;
  $("generateSubmit").querySelector("small").textContent = `预计 ${credits} Credits`;
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
  $("accountForm").elements.name.value = account.name && !String(account.name).startsWith("account_") ? account.name : (account.username || account.email_address || "");
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
  const account = state.accounts.find((item) => item.id === id);
  const email = account?.username || account?.email_address || "该账号";
  if (action === "delete" && !confirm(`删除账号 ${email} 的管理记录？浏览器 Profile 会保留。`)) return;
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
    toast(input.checked ? "已加入生成调度" : "已移出生成调度", input.dataset.advertiserId);
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
  $("generationModel").addEventListener("change", updateDuration);
  $("generationPrompt").addEventListener("input", () => { $("promptCount").textContent = $("generationPrompt").value.length; });
  $("firstFrameInput").addEventListener("change", () => renderFiles($("firstFrameInput"), "firstFramePreview"));
  $("referenceInput").addEventListener("change", () => renderFiles($("referenceInput"), "referencePreview"));
  $("generateForm").addEventListener("submit", submitGeneration);
  $("generationResult").addEventListener("click", (event) => {
    if (event.target.closest('[data-focus-action="download"]') && state.focusTask) downloadTask(state.focusTask.id);
  });
  $("addAccountButton").addEventListener("click", openAddAccount);
  $("accountForm").addEventListener("submit", saveAccount);
  $("settingsForm").addEventListener("submit", saveSettings);
  $("feishuTest").addEventListener("click", testFeishuNotification);
  $("apiKeyForm").addEventListener("submit", addApiKey);
  $("apiKeyGenerate").addEventListener("click", () => {
    $("apiKeyForm").elements.key.value = "";
    $("apiKeyForm").requestSubmit();
  });
  $("apiKeys").addEventListener("click", (event) => {
    const button = event.target.closest("[data-api-key-delete]");
    if (button) deleteApiKey(button.dataset.apiKeyDelete);
  });
  $$('[data-dialog-close]').forEach((button) => button.addEventListener("click", () => $("accountDialog").close()));
  $("accounts").addEventListener("click", (event) => {
    const button = event.target.closest("[data-account-action]");
    if (button) accountAction(button.dataset.accountAction, button.dataset.accountId);
  });
  $("accounts").addEventListener("change", (event) => {
    if (event.target.matches("[data-sub-toggle]")) toggleSubaccount(event.target);
  });
  const refreshLogFilters = debounce(() => { state.logPage = 1; refreshCurrent(false); });
  $("logSearch").addEventListener("input", refreshLogFilters);
  $("logLevel").addEventListener("change", () => { state.logPage = 1; refreshCurrent(false); });
  $("logCategory").addEventListener("change", () => { state.logPage = 1; refreshCurrent(false); });
  $$('[data-analytics-range]').forEach((button) => button.addEventListener("click", () => {
    state.analyticsRange = button.dataset.analyticsRange;
    $$('[data-analytics-range]').forEach((item) => item.classList.toggle("active", item === button));
    refreshCurrent(false);
  }));
  const refreshVideoFilters = debounce(() => { state.videoPage = 1; refreshCurrent(false); });
  $("videoSearch").addEventListener("input", refreshVideoFilters);
  $("videoStatus").addEventListener("change", () => { state.videoPage = 1; refreshCurrent(false); });
  $("videoAutoRefresh").addEventListener("change", () => refreshCurrent(false));
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pagination-page]");
    if (!button || button.disabled) return;
    const type = button.dataset.paginationPage;
    state[`${type}Page`] = Number(button.dataset.page);
    refreshCurrent(false);
  });
  document.addEventListener("change", (event) => {
    const select = event.target.closest("[data-pagination-size]");
    if (!select) return;
    const type = select.dataset.paginationSize;
    state[`${type}PageSize`] = Number(select.value);
    state[`${type}Page`] = 1;
    refreshCurrent(false);
  });
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
    if (state.route === "analytics" || state.route === "settings") return;
    refreshCurrent(false);
  }, 5000);
}

function init() {
  bindEvents();
  setNovncUrl();
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
