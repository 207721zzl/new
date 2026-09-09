const ui = {
  loginTab: document.querySelector("#login-tab"),
  registerTab: document.querySelector("#register-tab"),
  loginPanel: document.querySelector("#login-panel"),
  registerPanel: document.querySelector("#register-panel"),
  loginForm: document.querySelector("#login-form"),
  registerForm: document.querySelector("#register-form"),
  loginUsername: document.querySelector("#login-username"),
  loginPassword: document.querySelector("#login-password"),
  registerUsername: document.querySelector("#register-username"),
  registerDisplayName: document.querySelector("#register-display-name"),
  registerPassword: document.querySelector("#register-password"),
  registerConfirmPassword: document.querySelector("#register-confirm-password"),
  loginSubmit: document.querySelector("#login-submit"),
  registerSubmit: document.querySelector("#register-submit"),
  message: document.querySelector("#auth-message"),
};

function showMessage(message, error = false) {
  ui.message.textContent = message;
  ui.message.classList.toggle("error", error);
  ui.message.hidden = false;
}

function usernameIsValid(value) {
  const normalized = String(value || "").normalize("NFKC").trim();
  const characters = Array.from(normalized);
  return characters.length >= 3
    && characters.length <= 64
    && /^[\p{L}\p{N}][\p{L}\p{N}._-]*$/u.test(normalized);
}

function responseErrorMessage(payload, status) {
  if (payload?.error?.message) return payload.error.message;
  const issue = payload?.detail?.[0];
  if (!issue) return `请求失败（${status}）`;
  const field = Array.isArray(issue.loc) ? issue.loc.at(-1) : null;
  const labels = { username: "用户名", display_name: "姓名", password: "密码" };
  const message = String(issue.msg || "").replace(/^Value error,\s*/i, "").trim();
  if (message && message !== issue.msg) return message;
  return `${labels[field] || "提交内容"}格式不正确，请检查后重试。`;
}

function selectTab(name) {
  const isLogin = name === "login";
  ui.loginTab.classList.toggle("active", isLogin);
  ui.registerTab.classList.toggle("active", !isLogin);
  ui.loginTab.setAttribute("aria-selected", String(isLogin));
  ui.registerTab.setAttribute("aria-selected", String(!isLogin));
  ui.loginPanel.hidden = !isLogin;
  ui.registerPanel.hidden = isLogin;
  ui.message.hidden = true;
  (isLogin ? ui.loginUsername : ui.registerUsername).focus();
}

async function apiRequest(url, options = {}) {
  const response = await fetch(url, { ...options, credentials: "same-origin" });
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* 空响应 */ }
  if (!response.ok) {
    const message = responseErrorMessage(payload, response.status);
    const error = new Error(message);
    error.code = payload?.error?.code || "request_failed";
    error.status = response.status;
    throw error;
  }
  return payload;
}

function safeDestination(user) {
  const defaultDestination = user.role === "admin" ? "/admin" : "/";
  if (user.must_change_password) return defaultDestination;
  const requested = new URLSearchParams(window.location.search).get("next");
  const isSafePath = requested && requested.startsWith("/") && !requested.startsWith("//");
  const matchesRole = user.role === "admin"
    ? requested?.startsWith("/admin")
    : !requested?.startsWith("/admin");
  if (isSafePath && matchesRole) {
    return requested;
  }
  return defaultDestination;
}

async function submitLogin(event) {
  event.preventDefault();
  ui.loginSubmit.disabled = true;
  ui.message.hidden = true;
  try {
    const result = await apiRequest("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: ui.loginUsername.value,
        password: ui.loginPassword.value,
      }),
    });
    window.location.replace(safeDestination(result.user));
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    ui.loginSubmit.disabled = false;
  }
}

async function submitRegistration(event) {
  event.preventDefault();
  if (!usernameIsValid(ui.registerUsername.value)) {
    showMessage("用户名需为 3–64 位，首位使用中文、字母或数字，其余可使用点、下划线和短横线。", true);
    return;
  }
  if (ui.registerPassword.value !== ui.registerConfirmPassword.value) {
    showMessage("两次输入的密码不一致。", true);
    return;
  }
  ui.registerSubmit.disabled = true;
  ui.message.hidden = true;
  try {
    const result = await apiRequest("/api/v1/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: ui.registerUsername.value,
        display_name: ui.registerDisplayName.value,
        password: ui.registerPassword.value,
      }),
    });
    ui.registerForm.reset();
    selectTab("login");
    showMessage(result.message || "申请已提交，请等待管理员启用账号。", false);
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    ui.registerSubmit.disabled = false;
  }
}

async function redirectAuthenticatedUser() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("password_changed")) {
    showMessage("密码修改成功，请使用新密码登录。", false);
    return;
  }
  if (params.has("logged_out")) {
    showMessage("你已安全退出。", false);
    return;
  }
  if (params.get("reason") === "session_replaced") {
    showMessage("当前账号已在其他设备登录，本设备已自动退出。", true);
    return;
  }
  try {
    const user = await apiRequest("/api/v1/auth/me");
    window.location.replace(safeDestination(user));
  } catch (_) {
    /* 未登录是登录页的正常状态。 */
  }
}

async function loadClientConfig() {
  try {
    const config = await apiRequest("/api/v1/auth/config");
    if (!config.registration_enabled) {
      ui.registerTab.hidden = true;
      ui.registerPanel.hidden = true;
    }
    [ui.registerPassword, ui.registerConfirmPassword].forEach((input) => {
      input.minLength = config.password_min_length;
    });
  } catch (_) {
    /* 配置读取失败不阻塞登录尝试。 */
  }
}

ui.loginTab.addEventListener("click", () => selectTab("login"));
ui.registerTab.addEventListener("click", () => selectTab("register"));
ui.loginForm.addEventListener("submit", submitLogin);
ui.registerForm.addEventListener("submit", submitRegistration);
if (new URLSearchParams(window.location.search).get("tab") === "register") {
  selectTab("register");
}
loadClientConfig();
redirectAuthenticatedUser();
