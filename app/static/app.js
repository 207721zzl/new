const ui = {
  conversation: document.querySelector("#conversation"),
  form: document.querySelector("#question-form"),
  question: document.querySelector("#question-input"),
  submit: document.querySelector("#submit-button"),
  topK: document.querySelector("#top-k"),
  newChat: document.querySelector("#new-chat-button"),
  runBadge: document.querySelector("#run-badge"),
  runStatus: document.querySelector("#run-status"),
  timeline: document.querySelector("#timeline"),
  queryCard: document.querySelector("#query-card"),
  standaloneQuestion: document.querySelector("#standalone-question"),
  rewrittenQuery: document.querySelector("#rewritten-query"),
  evidenceCount: document.querySelector("#evidence-count"),
  citationSection: document.querySelector("#citation-section"),
  citationList: document.querySelector("#citation-list"),
  healthDot: document.querySelector("#health-dot"),
  healthLabel: document.querySelector("#health-label"),
  historyList: document.querySelector("#history-list"),
  refreshHistory: document.querySelector("#refresh-history"),
  accountAvatar: document.querySelector("#account-avatar"),
  accountName: document.querySelector("#account-name"),
  accountRole: document.querySelector("#account-role"),
  logout: document.querySelector("#logout-button"),
  changePassword: document.querySelector("#change-password-button"),
  passwordDialog: document.querySelector("#password-dialog"),
  passwordForm: document.querySelector("#password-form"),
  passwordClose: document.querySelector("#password-close"),
  passwordNote: document.querySelector("#password-note"),
  currentPassword: document.querySelector("#current-password"),
  newPassword: document.querySelector("#new-password"),
  confirmPassword: document.querySelector("#confirm-password"),
  passwordSubmit: document.querySelector("#password-submit"),
  toast: document.querySelector("#toast"),
};

const state = {
  conversationId: null,
  activeSource: null,
  running: false,
  user: null,
  passwordChangeForced: false,
  csrfCookieName: "evidence_rag_csrf",
};

const nodeLabels = {
  queued: "任务已进入队列",
  workflow: "RAG 工作流开始执行",
  rewrite_query: "完成多轮问题补全与查询改写",
  retrieve_knowledge: "完成 Dense + BM25 混合检索和重排",
  answer_without_knowledge: "证据不足，已安全拒答",
  generate_answer: "根据父块证据生成回答",
  completed: "回答与引用已生成",
  failed: "任务执行失败",
};

function showToast(message, error = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", error);
  ui.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { ui.toast.hidden = true; }, 3600);
}

function readCookie(name) {
  const prefix = `${name}=`;
  const item = document.cookie.split("; ").find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : null;
}

function loginUrl() {
  const next = `${window.location.pathname}${window.location.search}`;
  return `/login?next=${encodeURIComponent(next)}`;
}

async function request(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = readCookie(state.csrfCookieName);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(url, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code = "request_failed";
    try {
      const payload = await response.json();
      message = payload?.error?.message || payload?.detail?.[0]?.msg || message;
      code = payload?.error?.code || code;
    } catch (_) { /* 保留稳定错误 */ }
    if (response.status === 401) {
      window.location.replace(loginUrl());
    } else if (code === "password_change_required") {
      openPasswordDialog(true);
    }
    const error = new Error(message);
    error.code = code;
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return null;
  return response.json();
}

function setRunning(running, label = "处理中") {
  state.running = running;
  ui.submit.disabled = running;
  ui.runBadge.hidden = !running;
  ui.runStatus.textContent = label;
}

function resizeTextarea() {
  ui.question.style.height = "auto";
  ui.question.style.height = `${Math.min(ui.question.scrollHeight, 160)}px`;
}

function appendUserMessage(content) {
  const article = document.createElement("article");
  article.className = "message user-message";
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = content;
  article.append(body);
  ui.conversation.append(article);
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
}

function appendAssistantMessage(answer, citations) {
  const article = document.createElement("article");
  article.className = "message assistant-message assistant-answer";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "ER";
  const body = document.createElement("div");
  body.className = "message-body";
  const label = document.createElement("p");
  label.className = "message-label";
  label.textContent = "EvidenceRAG";
  const text = document.createElement("p");
  text.className = "answer-text";
  text.textContent = answer;
  body.append(label, text);
  if (citations.length) {
    const links = document.createElement("div");
    links.className = "answer-cites";
    citations.forEach((citation) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `[${citation.index}] ${citation.title}`;
      button.addEventListener("click", () => focusCitation(citation.index));
      links.append(button);
    });
    body.append(links);
  }
  article.append(avatar, body);
  ui.conversation.append(article);
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
}

function resetEvidence() {
  ui.timeline.innerHTML = "";
  ui.queryCard.hidden = true;
  ui.citationSection.hidden = true;
  ui.citationList.innerHTML = "";
  ui.evidenceCount.textContent = "0 条";
}

function addTrace(event) {
  const item = document.createElement("li");
  if (event.status === "failed") item.classList.add("failed");
  const index = document.createElement("span");
  index.textContent = String(ui.timeline.children.length + 1);
  const message = document.createElement("p");
  message.textContent = event.message || nodeLabels[event.node] || event.node;
  item.append(index, message);
  ui.timeline.append(item);
}

function renderQuery(query) {
  if (!query) return;
  ui.standaloneQuestion.textContent = query.standalone_question || query.original_query;
  ui.rewrittenQuery.textContent = query.rewritten_query || query.original_query;
  ui.queryCard.hidden = false;
}

function locationLabel(citation) {
  const parts = [];
  if (citation.section_path?.length) parts.push(citation.section_path.join(" / "));
  if (citation.page_start) {
    parts.push(citation.page_end && citation.page_end !== citation.page_start
      ? `第 ${citation.page_start}–${citation.page_end} 页`
      : `第 ${citation.page_start} 页`);
  }
  return parts.join(" · ") || citation.source;
}

function renderCitations(citations) {
  ui.citationList.innerHTML = "";
  ui.evidenceCount.textContent = `${citations.length} 条`;
  ui.citationSection.hidden = citations.length === 0;
  citations.forEach((citation) => {
    const card = document.createElement("article");
    card.className = "citation";
    card.id = `citation-${citation.index}`;
    const head = document.createElement("div");
    head.className = "citation-head";
    const index = document.createElement("span");
    index.className = "citation-index";
    index.textContent = citation.index;
    const title = document.createElement("strong");
    title.textContent = citation.title;
    head.append(index, title);
    const meta = document.createElement("p");
    meta.className = "citation-meta";
    meta.textContent = `${locationLabel(citation)} · ${citation.source}`;
    const score = document.createElement("div");
    score.className = "citation-score";
    const bar = document.createElement("span");
    bar.style.width = `${Math.max(3, Math.min(100, Number(citation.rerank_score || 0) * 100))}%`;
    score.append(bar);
    card.append(head, meta, score);
    ui.citationList.append(card);
  });
}

function focusCitation(index) {
  const card = document.querySelector(`#citation-${index}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("highlight");
  window.setTimeout(() => card.classList.remove("highlight"), 1600);
}

async function finishRun(resultUrl) {
  const result = await request(resultUrl);
  if (result.status === "failed") throw new Error(result.error?.message || "问答任务失败");
  renderQuery(result.query);
  renderCitations(result.citations || []);
  appendAssistantMessage(result.answer || "没有生成回答。", result.citations || []);
  if (result.conversation_id) state.conversationId = result.conversation_id;
  setRunning(false);
  await loadConversations();
}

async function pollRun(resultUrl) {
  const deadline = Date.now() + 4 * 60 * 1000;
  while (Date.now() < deadline) {
    const result = await request(resultUrl);
    if (result.status === "completed") {
      await finishRun(resultUrl);
      return;
    }
    if (result.status === "failed") {
      throw new Error(result.error?.message || "问答任务失败");
    }
    ui.runStatus.textContent = nodeLabels[result.current_node] || "处理中";
    await new Promise((resolve) => window.setTimeout(resolve, 800));
  }
  throw new Error("问答任务等待超时");
}

function streamRun(created) {
  return new Promise((resolve, reject) => {
    const source = new EventSource(created.events_url);
    state.activeSource = source;
    const eventTypes = ["run.created", "workflow.started", "node.completed", "run.completed", "run.failed"];
    eventTypes.forEach((type) => source.addEventListener(type, async (raw) => {
      const event = JSON.parse(raw.data);
      addTrace(event);
      ui.runStatus.textContent = nodeLabels[event.node] || event.message || "处理中";
      if (type === "run.completed") {
        source.close();
        state.activeSource = null;
        try { await finishRun(created.result_url); resolve(); } catch (error) { reject(error); }
      } else if (type === "run.failed") {
        source.close();
        state.activeSource = null;
        reject(new Error(event.message || "问答任务失败"));
      }
    }));
    source.onerror = () => {
      source.close();
      state.activeSource = null;
      window.setTimeout(async () => {
        try {
          await pollRun(created.result_url);
          resolve();
        } catch (error) { reject(error); }
      }, 350);
    };
  });
}

async function submitQuestion(question) {
  if (state.running || !question.trim()) return;
  appendUserMessage(question.trim());
  resetEvidence();
  setRunning(true, "创建任务");
  try {
    const payload = { question: question.trim(), top_k: Number(ui.topK.value) };
    if (state.conversationId) payload.conversation_id = state.conversationId;
    const created = await request("/api/v1/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.conversationId = created.conversation_id;
    await streamRun(created);
  } catch (error) {
    setRunning(false);
    showToast(error.message, true);
    appendAssistantMessage(`暂时无法完成这次问答：${error.message}`, []);
  }
}

function newConversation() {
  if (state.activeSource) state.activeSource.close();
  state.activeSource = null;
  state.conversationId = null;
  setRunning(false);
  resetEvidence();
  ui.conversation.innerHTML = `
    <article class="message assistant-message intro-message">
      <div class="avatar">ER</div>
      <div class="message-body">
        <p class="message-label">EvidenceRAG</p>
        <h2>新会话已经准备好了</h2>
        <p>提出一个需要从知识库查证的问题，我会返回答案和可追溯引用。</p>
      </div>
    </article>`;
  ui.question.focus();
  markActiveConversation();
}

function markActiveConversation() {
  ui.historyList.querySelectorAll("button[data-conversation-id]").forEach((button) => {
    button.classList.toggle(
      "active",
      button.dataset.conversationId === state.conversationId,
    );
  });
}

function renderStoredConversation(conversation) {
  if (state.activeSource) state.activeSource.close();
  state.activeSource = null;
  state.conversationId = conversation.conversation_id;
  setRunning(false);
  resetEvidence();
  ui.conversation.innerHTML = "";
  (conversation.messages || []).forEach((message) => {
    if (message.role === "user") appendUserMessage(message.content);
    else appendAssistantMessage(message.content, []);
  });
  if (!(conversation.messages || []).length) newConversation();
  markActiveConversation();
}

function renderConversationHistory(items) {
  ui.historyList.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "还没有会话记录";
    ui.historyList.append(empty);
    return;
  }
  items.forEach((conversation) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.conversationId = conversation.conversation_id;
    const title = document.createElement("strong");
    title.textContent = conversation.title || "未命名会话";
    const meta = document.createElement("small");
    const messageCount = (conversation.messages || []).length;
    meta.textContent = `${messageCount} 条消息`;
    button.append(title, meta);
    button.addEventListener("click", () => renderStoredConversation(conversation));
    ui.historyList.append(button);
  });
  markActiveConversation();
}

async function loadConversations() {
  try {
    const result = await request("/api/v1/conversations?limit=50");
    renderConversationHistory(result.items || []);
  } catch (error) {
    if (error.status !== 401 && error.code !== "password_change_required") {
      ui.historyList.innerHTML = '<p class="history-empty">会话加载失败</p>';
    }
  }
}

function openPasswordDialog(forced = false) {
  state.passwordChangeForced = forced;
  ui.passwordClose.hidden = forced;
  ui.passwordNote.textContent = forced
    ? "当前使用的是临时密码。请先设置至少 12 个字符的新密码，保存后重新登录。"
    : "新密码至少 12 个字符。修改后需要重新登录。";
  if (!ui.passwordDialog.open) ui.passwordDialog.showModal();
  ui.currentPassword.focus();
}

function closePasswordDialog() {
  if (state.passwordChangeForced) return;
  ui.passwordDialog.close();
  ui.passwordForm.reset();
}

async function submitPasswordChange(event) {
  event.preventDefault();
  const currentPassword = ui.currentPassword.value;
  const newPassword = ui.newPassword.value;
  if (newPassword !== ui.confirmPassword.value) {
    showToast("两次输入的新密码不一致", true);
    return;
  }
  ui.passwordSubmit.disabled = true;
  try {
    await request("/api/v1/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    window.location.replace("/login?password_changed=1");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    ui.passwordSubmit.disabled = false;
  }
}

async function logout() {
  ui.logout.disabled = true;
  try {
    await request("/api/v1/auth/logout", { method: "POST" });
  } finally {
    window.location.replace("/login?logged_out=1");
  }
}

async function initializeAccount() {
  try {
    const config = await request("/api/v1/auth/config");
    state.csrfCookieName = config.csrf_cookie_name;
    state.user = await request("/api/v1/auth/me");
  } catch (error) {
    if (error.status !== 401) showToast(error.message, true);
    return;
  }
  if (state.user.role !== "employee") {
    window.location.replace("/admin");
    return;
  }
  ui.accountName.textContent = state.user.display_name;
  ui.accountRole.textContent = "普通员工";
  ui.accountAvatar.textContent = state.user.display_name.trim().slice(0, 1) || "用";
  if (state.user.must_change_password) {
    openPasswordDialog(true);
    return;
  }
  await loadConversations();
}

async function checkHealth() {
  try {
    const result = await request("/api/v1/health");
    ui.healthDot.className = "status-dot ready";
    ui.healthLabel.textContent = `${result.service} 已连接`;
  } catch (_) {
    ui.healthDot.className = "status-dot failed";
    ui.healthLabel.textContent = "服务不可用";
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = ui.question.value;
  ui.question.value = "";
  resizeTextarea();
  submitQuestion(question);
});
ui.question.addEventListener("input", resizeTextarea);
ui.question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ui.form.requestSubmit(); }
});
ui.newChat.addEventListener("click", newConversation);
ui.refreshHistory.addEventListener("click", loadConversations);
ui.logout.addEventListener("click", logout);
ui.changePassword.addEventListener("click", () => openPasswordDialog(false));
ui.passwordClose.addEventListener("click", closePasswordDialog);
ui.passwordForm.addEventListener("submit", submitPasswordChange);
ui.passwordDialog.addEventListener("cancel", (event) => {
  if (state.passwordChangeForced) event.preventDefault();
});
document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
  ui.question.value = button.dataset.question;
  resizeTextarea();
  ui.question.focus();
}));

checkHealth();
resizeTextarea();
initializeAccount();
