const ui = {
  nav: Array.from(document.querySelectorAll("[data-panel]")),
  panels: Array.from(document.querySelectorAll(".admin-panel")),
  pageTitle: document.querySelector("#page-title"),
  adminName: document.querySelector("#admin-name"),
  adminAvatar: document.querySelector("#admin-avatar"),
  logout: document.querySelector("#admin-logout"),
  changePassword: document.querySelector("#admin-change-password"),
  passwordDialog: document.querySelector("#admin-password-dialog"),
  passwordForm: document.querySelector("#admin-password-form"),
  passwordClose: document.querySelector("#admin-password-close"),
  passwordNote: document.querySelector("#admin-password-note"),
  currentPassword: document.querySelector("#admin-current-password"),
  newPassword: document.querySelector("#admin-new-password"),
  confirmPassword: document.querySelector("#admin-confirm-password"),
  passwordSubmit: document.querySelector("#admin-password-submit"),
  userRows: document.querySelector("#user-rows"),
  userSearch: document.querySelector("#user-search"),
  userStatusFilter: document.querySelector("#user-status-filter"),
  refreshUsers: document.querySelector("#refresh-users"),
  openCreateUser: document.querySelector("#open-create-user"),
  createUserDialog: document.querySelector("#create-user-dialog"),
  createUserForm: document.querySelector("#create-user-form"),
  createUserSubmit: document.querySelector("#create-user-submit"),
  resetPasswordDialog: document.querySelector("#reset-password-dialog"),
  resetPasswordForm: document.querySelector("#reset-password-form"),
  resetPasswordSubmit: document.querySelector("#reset-password-submit"),
  resetUserLabel: document.querySelector("#reset-user-label"),
  uploadForm: document.querySelector("#admin-upload-form"),
  files: document.querySelector("#admin-files"),
  fileLabel: document.querySelector("#admin-file-label"),
  recreate: document.querySelector("#admin-recreate"),
  uploadSubmit: document.querySelector("#admin-upload-submit"),
  uploadStatus: document.querySelector("#admin-upload-status"),
  documentRows: document.querySelector("#document-rows"),
  refreshDocuments: document.querySelector("#refresh-documents"),
  auditRows: document.querySelector("#audit-rows"),
  refreshAudit: document.querySelector("#refresh-audit"),
  refreshPilot: document.querySelector("#refresh-pilot"),
  refreshMaintenance: document.querySelector("#refresh-maintenance"),
  runCleanup: document.querySelector("#run-cleanup"),
  pilotRunSuccess: document.querySelector("#pilot-run-success"),
  pilotRunCount: document.querySelector("#pilot-run-count"),
  pilotRunP95: document.querySelector("#pilot-run-p95"),
  pilotActiveUsers: document.querySelector("#pilot-active-users"),
  pilotActiveSessions: document.querySelector("#pilot-active-sessions"),
  pilotTokens: document.querySelector("#pilot-tokens"),
  pilotKnowledge: document.querySelector("#pilot-knowledge"),
  pilotIndexing: document.querySelector("#pilot-indexing"),
  pilotFeedback: document.querySelector("#pilot-feedback"),
  pilotDisk: document.querySelector("#pilot-disk"),
  pilotModel: document.querySelector("#pilot-model"),
  pilotRuntime: document.querySelector("#pilot-runtime"),
  pilotWarnings: document.querySelector("#pilot-warnings"),
  cleanupConversations: document.querySelector("#cleanup-conversations"),
  cleanupSessions: document.querySelector("#cleanup-sessions"),
  cleanupAudits: document.querySelector("#cleanup-audits"),
  cleanupConversationPolicy: document.querySelector("#cleanup-conversation-policy"),
  cleanupSessionPolicy: document.querySelector("#cleanup-session-policy"),
  cleanupAuditPolicy: document.querySelector("#cleanup-audit-policy"),
  toast: document.querySelector("#admin-toast"),
};

const state = {
  user: null,
  users: [],
  resetUser: null,
  csrfCookieName: "evidence_rag_csrf",
  passwordChangeForced: false,
};

const panelTitles = { users: "账号管理", knowledge: "知识库管理", audit: "审计记录", pilot: "试点运行" };
const statusLabels = { pending: "待审核", active: "已启用", disabled: "已停用" };
const roleLabels = { admin: "管理员", employee: "普通员工" };
const auditLabels = {
  "auth.bootstrap_admin_created": "创建首位管理员",
  "auth.user_registered": "员工申请账号",
  "auth.password_changed": "用户修改密码",
  "admin.user_created": "管理员新建账号",
  "admin.user_updated": "调整账号状态",
  "admin.user_password_reset": "重置用户密码",
  "admin.knowledge_document_deleted": "删除知识文档",
  "admin.pilot_retention_cleanup": "清理到期试点数据",
  "system.interrupted_tasks_reconciled": "处理服务重启遗留任务",
};

function readCookie(name) {
  const prefix = `${name}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

function showToast(message, error = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", error);
  ui.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { ui.toast.hidden = true; }, 3800);
}

async function request(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = readCookie(state.csrfCookieName);
    if (token) headers.set("X-CSRF-Token", token);
  }
  const response = await fetch(url, { ...options, method, headers, credentials: "same-origin" });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* 空响应 */ }
  if (!response.ok) {
    const code = payload?.error?.code;
    if (response.status === 401) window.location.replace("/login?next=%2Fadmin");
    if (code === "password_change_required") openPasswordDialog(true);
    if (code === "permission_denied") window.location.replace("/");
    const error = new Error(payload?.error?.message || payload?.detail?.[0]?.msg || `请求失败（${response.status}）`);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  return payload;
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(`${value}Z`));
}

function option(value, label, current) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  item.selected = value === current;
  return item;
}

function switchPanel(name) {
  ui.nav.forEach((button) => button.classList.toggle("active", button.dataset.panel === name));
  ui.panels.forEach((panel) => { panel.hidden = panel.id !== `panel-${name}`; });
  ui.pageTitle.textContent = panelTitles[name];
  if (name === "users") loadUsers();
  if (name === "knowledge") loadDocuments();
  if (name === "audit") loadAuditLogs();
  if (name === "pilot") Promise.all([loadPilotOverview(), loadMaintenance()]);
}

function formatBytes(value) {
  if (value === null || value === undefined) return "暂不可用";
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const milliseconds = Number(value);
  return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(1)} 秒` : `${Math.round(milliseconds)} ms`;
}

function formatRate(value) {
  return value === null || value === undefined ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

async function loadPilotOverview() {
  try {
    const data = await request("/api/v1/admin/pilot/overview");
    ui.pilotRunSuccess.textContent = formatRate(data.runs?.success_rate);
    ui.pilotRunCount.textContent = data.runs ? `${data.runs.total} 次问答 · ${data.runs.failed} 次失败` : "暂不可用";
    ui.pilotRunP95.textContent = formatDuration(data.runs?.p95_duration_ms);
    ui.pilotActiveUsers.textContent = data.accounts ? String(data.accounts.active_employees) : "暂不可用";
    ui.pilotActiveSessions.textContent = data.accounts ? `${data.accounts.active_sessions} 个有效登录会话` : "暂不可用";
    ui.pilotTokens.textContent = data.tokens ? Number(data.tokens.total_tokens || 0).toLocaleString("zh-CN") : "暂不可用";
    ui.pilotKnowledge.textContent = data.knowledge ? `${data.knowledge.documents} 篇文档 · ${data.knowledge.child_chunks} 个检索块` : "暂不可用";
    ui.pilotIndexing.textContent = data.indexing ? `${data.indexing.completed} 成功 · ${data.indexing.failed} 失败 · ${data.indexing.stale} 卡住` : "暂不可用";
    ui.pilotFeedback.textContent = data.feedback ? `${data.feedback.positive} 个赞 · ${data.feedback.negative} 个踩 · ${formatRate(data.feedback.positive_rate)}` : "暂不可用";
    ui.pilotDisk.textContent = formatBytes(data.disk_free_bytes);
    ui.pilotModel.textContent = data.model_state === "ready" ? "已就绪" : data.model_state;
    ui.pilotRuntime.textContent = data.runtime ? `${data.runtime.requests_total} 次请求 · P95 ${formatDuration(data.runtime.p95_latency_ms)} · ${data.runtime.responses_5xx} 次 5xx` : "暂不可用";
    ui.pilotWarnings.innerHTML = "";
    (data.warnings || []).forEach((warning) => {
      const item = document.createElement("li");
      item.textContent = warning;
      ui.pilotWarnings.append(item);
    });
    if (!(data.warnings || []).length) ui.pilotWarnings.innerHTML = "<li>当前没有需要处理的试点告警。</li>";
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadMaintenance() {
  try {
    const data = await request("/api/v1/admin/pilot/maintenance");
    ui.cleanupConversations.textContent = String(data.conversations_to_delete ?? "暂不可用");
    ui.cleanupSessions.textContent = String(data.sessions_to_delete ?? "暂不可用");
    ui.cleanupAudits.textContent = String(data.audit_logs_to_delete ?? "暂不可用");
    ui.cleanupConversationPolicy.textContent = `保留 ${data.conversation_retention_days} 天`;
    ui.cleanupSessionPolicy.textContent = `失效后保留 ${data.session_retention_days} 天`;
    ui.cleanupAuditPolicy.textContent = `保留 ${data.audit_retention_days} 天`;
    ui.runCleanup.disabled = !(data.conversations_to_delete || data.sessions_to_delete || data.audit_logs_to_delete);
  } catch (error) {
    showToast(error.message, true);
  }
}

let cleanupOperationId = null;
async function runMaintenanceCleanup() {
  if (!window.confirm("将永久删除超过保留期的数据，且不会删除当前有效会话。确认执行吗？")) return;
  ui.runCleanup.disabled = true;
  cleanupOperationId ||= crypto.randomUUID();
  try {
    const data = await request("/api/v1/admin/pilot/maintenance/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": cleanupOperationId },
      body: JSON.stringify({ confirmation: "PURGE_EXPIRED_PILOT_DATA" }),
    });
    if (data.status === "partial_failed") {
      showToast("部分清理尚未完成，可以再次点击重试。已完成的部分不会重复执行。", true);
    } else {
      cleanupOperationId = null;
      showToast(`清理完成：${data.conversations_to_delete} 个历史会话，${data.sessions_to_delete} 个失效登录会话，${data.audit_logs_to_delete} 条过期审计`);
    }
    await Promise.all([loadMaintenance(), loadPilotOverview(), loadAuditLogs()]);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui.runCleanup.disabled = false;
  }
}

function userIdentityCell(user) {
  const cell = document.createElement("td");
  const name = document.createElement("strong");
  name.textContent = user.display_name;
  const username = document.createElement("small");
  username.textContent = `@${user.username}`;
  cell.append(name, username);
  return cell;
}

function renderUsers(users) {
  ui.userRows.innerHTML = "";
  if (!users.length) {
    ui.userRows.innerHTML = '<tr><td colspan="6" class="empty-cell">没有符合条件的账号</td></tr>';
    return;
  }
  users.forEach((user) => {
    const row = document.createElement("tr");
    row.append(userIdentityCell(user));

    const roleCell = document.createElement("td");
    const roleSelect = document.createElement("select");
    roleSelect.append(option("employee", "普通员工", user.role), option("admin", "管理员", user.role));
    roleSelect.disabled = user.user_id === state.user.user_id;
    roleCell.append(roleSelect);

    const statusCell = document.createElement("td");
    const statusSelect = document.createElement("select");
    statusSelect.append(
      option("pending", "待审核", user.status),
      option("active", "已启用", user.status),
      option("disabled", "已停用", user.status),
    );
    statusSelect.disabled = user.user_id === state.user.user_id;
    statusCell.append(statusSelect);

    const loginCell = document.createElement("td");
    loginCell.textContent = formatDate(user.last_login_at);

    const securityCell = document.createElement("td");
    const statusPill = document.createElement("span");
    statusPill.className = `status-pill status-${user.status}`;
    statusPill.textContent = user.must_change_password ? "需修改密码" : statusLabels[user.status];
    securityCell.append(statusPill);
    if (user.locked_until) {
      const lock = document.createElement("small");
      lock.textContent = `锁定至 ${formatDate(user.locked_until)}`;
      securityCell.append(lock);
    }

    const actions = document.createElement("td");
    const actionRow = document.createElement("div");
    actionRow.className = "action-row";
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "保存";
    save.disabled = user.user_id === state.user.user_id;
    save.addEventListener("click", () => saveUser(user, roleSelect.value, statusSelect.value, save));
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = "重置密码";
    reset.addEventListener("click", () => openResetPassword(user));
    actionRow.append(save, reset);
    actions.append(actionRow);
    row.append(roleCell, statusCell, loginCell, securityCell, actions);
    ui.userRows.append(row);
  });
}

async function loadUserStats() {
  const statuses = ["pending", "active", "disabled"];
  const results = await Promise.all(statuses.map((status) => request(`/api/v1/admin/users?status=${status}&limit=1`)));
  const counts = Object.fromEntries(statuses.map((status, index) => [status, results[index].total]));
  document.querySelector("#stat-pending").textContent = counts.pending;
  document.querySelector("#stat-active").textContent = counts.active;
  document.querySelector("#stat-disabled").textContent = counts.disabled;
  document.querySelector("#stat-total").textContent = counts.pending + counts.active + counts.disabled;
}

async function loadUsers() {
  const params = new URLSearchParams({ limit: "200" });
  if (ui.userSearch.value.trim()) params.set("search", ui.userSearch.value.trim());
  if (ui.userStatusFilter.value) params.set("status", ui.userStatusFilter.value);
  try {
    const result = await request(`/api/v1/admin/users?${params}`);
    state.users = result.items || [];
    renderUsers(state.users);
    await loadUserStats();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveUser(user, role, status, button) {
  button.disabled = true;
  try {
    await request(`/api/v1/admin/users/${encodeURIComponent(user.user_id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, status }),
    });
    showToast(`已更新 ${user.display_name} 的账号状态`);
    await loadUsers();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function createUser(event) {
  event.preventDefault();
  ui.createUserSubmit.disabled = true;
  try {
    await request("/api/v1/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.querySelector("#create-username").value,
        display_name: document.querySelector("#create-display-name").value,
        password: document.querySelector("#create-password").value,
        role: document.querySelector("#create-role").value,
        status: document.querySelector("#create-status").value,
        must_change_password: document.querySelector("#create-must-change").checked,
      }),
    });
    ui.createUserDialog.close();
    ui.createUserForm.reset();
    document.querySelector("#create-must-change").checked = true;
    showToast("账号创建成功");
    await loadUsers();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui.createUserSubmit.disabled = false;
  }
}

function openResetPassword(user) {
  state.resetUser = user;
  ui.resetUserLabel.textContent = `${user.display_name}（${user.username}）`;
  ui.resetPasswordForm.reset();
  ui.resetPasswordDialog.showModal();
}

async function resetPassword(event) {
  event.preventDefault();
  const password = document.querySelector("#reset-password").value;
  if (password !== document.querySelector("#reset-password-confirm").value) {
    showToast("两次输入的临时密码不一致", true);
    return;
  }
  ui.resetPasswordSubmit.disabled = true;
  try {
    await request(`/api/v1/admin/users/${encodeURIComponent(state.resetUser.user_id)}/reset-password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ temporary_password: password }),
    });
    ui.resetPasswordDialog.close();
    showToast("密码已重置，该用户需要重新登录并修改密码");
    await loadUsers();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui.resetPasswordSubmit.disabled = false;
  }
}

function renderDocuments(documents) {
  ui.documentRows.innerHTML = "";
  if (!documents.length) {
    ui.documentRows.innerHTML = '<tr><td colspan="6" class="empty-cell">知识库中暂无文档</td></tr>';
    return;
  }
  documents.forEach((document) => {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const title = document.createElement("strong");
    title.textContent = document.title;
    const source = document.createElement("small");
    source.textContent = document.original_filename || document.source;
    identity.append(title, source);
    const type = document.createElement("td"); type.textContent = document.doc_type;
    const version = document.createElement("td"); version.textContent = document.version;
    const chunks = document.createElement("td"); chunks.textContent = `${document.parent_chunk_count} 父块 / ${document.child_chunk_count} 子块`;
    const updated = document.createElement("td"); updated.textContent = formatDate(document.updated_at);
    const actions = document.createElement("td");
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "danger"; remove.textContent = "删除";
    remove.addEventListener("click", () => deleteDocument(document, remove));
    const actionRow = document.createElement("div"); actionRow.className = "action-row"; actionRow.append(remove); actions.append(actionRow);
    row.append(identity, type, version, chunks, updated, actions);
    ui.documentRows.append(row);
  });
}

async function loadDocuments() {
  try {
    const result = await request("/api/v1/knowledge/documents?limit=200");
    renderDocuments(result.items || []);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function deleteDocument(document, button) {
  if (!window.confirm(`确认从知识库删除“${document.title}”？删除后将不再用于新的问答。`)) return;
  button.disabled = true;
  try {
    const result = await request(`/api/v1/admin/knowledge/documents/${encodeURIComponent(document.document_id)}`, { method: "DELETE" });
    showToast(`已删除 ${result.title}（${result.child_chunk_count} 个检索块）`);
    await Promise.all([loadDocuments(), loadAuditLogs()]);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function pollUpload(url) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const result = await request(url);
    ui.uploadStatus.hidden = false;
    ui.uploadStatus.textContent = `状态：${result.status} · ${result.completed_count}/${result.file_count} 个文件 · ${result.chunk_count} 个检索块`;
    if (["completed", "partial_failed", "failed"].includes(result.status)) return result;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error("索引任务等待超时");
}

async function uploadKnowledge(event) {
  event.preventDefault();
  const files = Array.from(ui.files.files || []);
  if (!files.length) { showToast("请先选择知识文件", true); return; }
  if (ui.recreate.checked && !window.confirm("重建完成后将用本批文档替换当前知识库，确认继续吗？")) return;
  const data = new FormData();
  files.forEach((file) => data.append("files", file));
  data.append("recreate", String(ui.recreate.checked));
  ui.uploadSubmit.disabled = true;
  ui.uploadStatus.hidden = false;
  ui.uploadStatus.textContent = "正在保存文件并创建索引任务…";
  try {
    const created = await request("/api/v1/knowledge/uploads", { method: "POST", body: data });
    const result = await pollUpload(created.status_url);
    if (result.status === "completed") {
      showToast(`导入完成：${result.document_count} 篇文档，${result.chunk_count} 个检索块`);
      ui.files.value = "";
      ui.fileLabel.textContent = "选择一个或多个文件";
      await loadDocuments();
    } else {
      showToast("部分文件导入失败，请查看任务状态", true);
    }
  } catch (error) {
    ui.uploadStatus.textContent = error.message;
    showToast(error.message, true);
  } finally {
    ui.uploadSubmit.disabled = false;
  }
}

function renderAuditLogs(items) {
  ui.auditRows.innerHTML = "";
  if (!items.length) {
    ui.auditRows.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无审计记录</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    const time = document.createElement("td"); time.textContent = formatDate(item.created_at);
    const actor = document.createElement("td"); actor.textContent = item.actor_username || "系统";
    const action = document.createElement("td"); action.textContent = auditLabels[item.action] || item.action;
    const target = document.createElement("td"); target.textContent = item.target_id || "—";
    const outcome = document.createElement("td");
    const pill = document.createElement("span"); pill.className = "status-pill status-active"; pill.textContent = item.outcome === "success" ? "成功" : item.outcome; outcome.append(pill);
    row.append(time, actor, action, target, outcome);
    ui.auditRows.append(row);
  });
}

async function loadAuditLogs() {
  try {
    const result = await request("/api/v1/admin/audit-logs?limit=100");
    renderAuditLogs(result.items || []);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function logout() {
  try { await request("/api/v1/auth/logout", { method: "POST" }); }
  finally { window.location.replace("/login?logged_out=1"); }
}

function openPasswordDialog(forced = false) {
  state.passwordChangeForced = forced;
  ui.passwordClose.hidden = forced;
  ui.passwordNote.textContent = forced
    ? "当前使用的是临时密码，请先设置至少 12 个字符的新密码。"
    : "新密码至少 12 个字符，保存后需要重新登录。";
  ui.passwordForm.reset();
  ui.passwordDialog.showModal();
  ui.currentPassword.focus();
}

async function submitPasswordChange(event) {
  event.preventDefault();
  if (ui.newPassword.value !== ui.confirmPassword.value) {
    showToast("两次输入的新密码不一致", true);
    return;
  }
  ui.passwordSubmit.disabled = true;
  try {
    await request("/api/v1/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: ui.currentPassword.value,
        new_password: ui.newPassword.value,
      }),
    });
    window.location.replace("/login?password_changed=1");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui.passwordSubmit.disabled = false;
  }
}

async function initialize() {
  try {
    const config = await request("/api/v1/auth/config");
    state.csrfCookieName = config.csrf_cookie_name;
    state.user = await request("/api/v1/auth/me");
    if (state.user.role !== "admin") {
      window.location.replace("/");
      return;
    }
    ui.adminName.textContent = state.user.display_name;
    ui.adminAvatar.textContent = state.user.display_name.trim().slice(0, 1) || "管";
    if (state.user.must_change_password) {
      openPasswordDialog(true);
      return;
    }
    await loadUsers();
  } catch (error) {
    if (error.status !== 401) showToast(error.message, true);
  }
}

ui.nav.forEach((button) => button.addEventListener("click", () => switchPanel(button.dataset.panel)));
ui.logout.addEventListener("click", logout);
ui.changePassword.addEventListener("click", () => openPasswordDialog(false));
ui.passwordClose.addEventListener("click", () => ui.passwordDialog.close());
ui.passwordForm.addEventListener("submit", submitPasswordChange);
ui.passwordDialog.addEventListener("cancel", (event) => {
  if (state.passwordChangeForced) event.preventDefault();
});
ui.refreshUsers.addEventListener("click", loadUsers);
ui.userStatusFilter.addEventListener("change", loadUsers);
ui.userSearch.addEventListener("keydown", (event) => { if (event.key === "Enter") loadUsers(); });
ui.openCreateUser.addEventListener("click", () => ui.createUserDialog.showModal());
ui.createUserForm.addEventListener("submit", createUser);
ui.resetPasswordForm.addEventListener("submit", resetPassword);
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => document.querySelector(`#${button.dataset.closeDialog}`).close()));
ui.files.addEventListener("change", () => { const count = (ui.files.files || []).length; ui.fileLabel.textContent = count ? `已选择 ${count} 个文件` : "选择一个或多个文件"; });
ui.uploadForm.addEventListener("submit", uploadKnowledge);
ui.refreshDocuments.addEventListener("click", loadDocuments);
ui.refreshAudit.addEventListener("click", loadAuditLogs);
ui.refreshPilot.addEventListener("click", loadPilotOverview);
ui.refreshMaintenance.addEventListener("click", loadMaintenance);
ui.runCleanup.addEventListener("click", runMaintenanceCleanup);
initialize();
