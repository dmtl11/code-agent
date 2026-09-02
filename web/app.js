const eventsEl = document.querySelector("#events");
const filesEl = document.querySelector("#files");
const fileCountEl = document.querySelector("#file-count");
const statusEl = document.querySelector("#status");
const runButton = document.querySelector("#run");
const clearButton = document.querySelector("#clear");
const refreshButton = document.querySelector("#refresh");
const taskEl = document.querySelector("#task");
const workspaceEl = document.querySelector("#workspace");
const editorEl = document.querySelector("#code-editor");
const fileTitleEl = document.querySelector("#file-title");
const languageEl = document.querySelector("#language");
const saveFileButton = document.querySelector("#save-file");
const runFileButton = document.querySelector("#run-file");
const terminalEl = document.querySelector("#terminal");
const terminalResizeEl = document.querySelector("#terminal-resize");
const editorPanelEl = document.querySelector(".editor");
const terminalPanelEl = document.querySelector("#terminal-panel");
const modeButtons = document.querySelectorAll("#mode-switch [data-mode]");
const contextStatusEl = document.querySelector("#context-status");
const agentViewTitleEl = document.querySelector("#agent-view-title");
const providerEl = document.querySelector("#provider");
const providerHintEl = document.querySelector("#provider-hint");
const chatTab = document.querySelector("#chat-tab");
const reviewTab = document.querySelector("#review-tab");
const monitorTab = document.querySelector("#monitor-tab");
const chatView = document.querySelector("#chat-view");
const reviewView = document.querySelector("#review-view");
const monitorView = document.querySelector("#monitor-view");
const reviewsEl = document.querySelector("#reviews");
const reviewCountEl = document.querySelector("#review-count");
const reviewSummaryEl = document.querySelector("#review-summary");
const reviewRefreshButton = document.querySelector("#review-refresh");
const reviewMergeButton = document.querySelector("#review-merge");
const reviewRollbackButton = document.querySelector("#review-rollback");
const monitorScopeEl = document.querySelector("#monitor-scope");
const monitorKpisEl = document.querySelector("#monitor-kpis");
const monitorProvidersEl = document.querySelector("#monitor-providers");
const monitorRoutesEl = document.querySelector("#monitor-routes");
const monitorToolsEl = document.querySelector("#monitor-tools");
const monitorErrorsEl = document.querySelector("#monitor-errors");
const monitorRefreshButton = document.querySelector("#monitor-refresh");
let activeFile = "";
let openFileRequestId = 0;
let activeMode = "code";
let conversationHistory = [];
let providerCatalog = new Map();
let sessionId = localStorage.getItem("code-agent-session-id") || "";
let reviewChanges = [];
const collapsedFolders = new Set();

initializeTerminalResize();

function initializeTerminalResize() {
  const storageKey = "code-agent-terminal-ratio";
  const defaultRatio = 0.3;
  let preferredRatio = defaultRatio;
  let drag = null;
  try {
    const saved = Number(localStorage.getItem(storageKey));
    if (Number.isFinite(saved) && saved > 0 && saved < 1) preferredRatio = saved;
  } catch { /* Resizing also works when browser storage is unavailable. */ }

  function bounds() {
    const available = Math.max(1, editorPanelEl.clientHeight
      - editorPanelEl.querySelector(".editor-head").offsetHeight - terminalResizeEl.offsetHeight);
    return {
      available,
      minimum: Math.min(80, available * 0.4) / available,
      maximum: 1 - Math.min(120, available * 0.4) / available,
    };
  }

  function render() {
    const { minimum, maximum } = bounds();
    const ratio = Math.max(minimum, Math.min(maximum, preferredRatio));
    editorPanelEl.style.setProperty("--editor-share", `${1 - ratio}fr`);
    editorPanelEl.style.setProperty("--terminal-share", `${ratio}fr`);
    terminalResizeEl.setAttribute("aria-valuemin", String(Math.round(minimum * 100)));
    terminalResizeEl.setAttribute("aria-valuemax", String(Math.round(maximum * 100)));
    terminalResizeEl.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    terminalResizeEl.setAttribute("aria-valuetext", `${Math.round(ratio * 100)}% terminal`);
  }

  function setRatio(ratio) {
    const { minimum, maximum } = bounds();
    preferredRatio = Math.max(minimum, Math.min(maximum, ratio));
    render();
  }

  function save() {
    try { localStorage.setItem(storageKey, String(preferredRatio)); } catch { /* Optional preference. */ }
  }

  function endDrag() {
    if (!drag) return;
    const pointerId = drag.pointerId;
    drag = null;
    document.body.classList.remove("resizing-terminal");
    if (terminalResizeEl.hasPointerCapture(pointerId)) terminalResizeEl.releasePointerCapture(pointerId);
    save();
  }

  terminalResizeEl.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    drag = { pointerId: event.pointerId, y: event.clientY, height: terminalPanelEl.getBoundingClientRect().height };
    terminalResizeEl.setPointerCapture(event.pointerId);
    terminalResizeEl.focus({ preventScroll: true });
    document.body.classList.add("resizing-terminal");
  });
  terminalResizeEl.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    setRatio((drag.height + drag.y - event.clientY) / bounds().available);
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) terminalResizeEl.addEventListener(type, endDrag);
  window.addEventListener("blur", endDrag);
  terminalResizeEl.addEventListener("dblclick", () => {
    preferredRatio = defaultRatio;
    render();
    save();
  });
  terminalResizeEl.addEventListener("keydown", (event) => {
    const { minimum, maximum } = bounds();
    const step = event.shiftKey ? 0.1 : 0.03;
    const current = Math.max(minimum, Math.min(maximum, preferredRatio));
    if (event.key === "ArrowUp") setRatio(current + step);
    else if (event.key === "ArrowDown") setRatio(current - step);
    else if (event.key === "Home") setRatio(minimum);
    else if (event.key === "End") setRatio(maximum);
    else return;
    event.preventDefault();
    save();
  });
  new ResizeObserver(render).observe(editorPanelEl);
  render();
}

const EMPTY_CHAT = `
  <li class="empty chat-empty">
    <strong>Start a coding task</strong>
    <span>Ask the agent to inspect, edit, and verify this workspace.</span>
  </li>`;

clearButton.addEventListener("click", async () => {
  eventsEl.innerHTML = EMPTY_CHAT;
  conversationHistory = [];
  contextStatusEl.textContent = "Context ready";
  if (sessionId) {
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/clear`, { method: "POST" });
  }
  await refreshReviews();
});

modeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    activeMode = button.dataset.mode;
    modeButtons.forEach((item) => item.classList.toggle("active", item === button));
    taskEl.placeholder = modePlaceholder(activeMode);
    runButton.textContent = activeMode === "context" ? "Inspect" : "Run";
  });
});

providerEl.addEventListener("change", () => {
  updateProviderHint();
});

chatTab.addEventListener("click", () => switchPanel("chat"));
reviewTab.addEventListener("click", () => switchPanel("review"));
monitorTab.addEventListener("click", () => switchPanel("monitor"));
reviewRefreshButton.addEventListener("click", refreshReviews);
reviewMergeButton.addEventListener("click", mergeSelectedReviews);
reviewRollbackButton.addEventListener("click", rollbackSelectedReviews);
monitorRefreshButton.addEventListener("click", refreshMonitoring);

refreshButton.addEventListener("click", refreshFiles);
workspaceEl.addEventListener("change", () => {
  sessionId = "";
  localStorage.removeItem("code-agent-session-id");
  conversationHistory = [];
  reviewChanges = [];
  eventsEl.innerHTML = EMPTY_CHAT;
  contextStatusEl.textContent = "New workspace · new session";
  activeFile = "";
  resetEditor();
  renderReviews();
  refreshFiles();
});
saveFileButton.addEventListener("click", saveActiveFile);
runFileButton.addEventListener("click", runActiveFile);

runButton.addEventListener("click", async () => {
  const task = taskEl.value.trim() || modePlaceholder(activeMode);
  statusEl.textContent = "running";
  runButton.disabled = true;
  removeEmptyEvent();
  addEvent({ type: "user", content: task });
  taskEl.value = "";

  try {
    const response = await fetch("/api/run-stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task,
        workspace: workspaceEl.value,
        mode: activeMode,
        provider: providerEl.value,
        session_id: sessionId,
        history: conversationHistory,
      }),
    });
    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`);
    }
    const finalEvent = await readEventStream(response.body);
    if (finalEvent?.session_id) {
      sessionId = finalEvent.session_id;
      localStorage.setItem("code-agent-session-id", sessionId);
    }
    if (finalEvent?.ok && Array.isArray(finalEvent.exchange)) {
      conversationHistory.push(...finalEvent.exchange);
      conversationHistory = conversationHistory.slice(-20);
    }
    statusEl.textContent = "done";
    await refreshFiles();
    await refreshReviews();
    await refreshMonitoring();
  } catch (error) {
    addEvent({ type: "error", message: error.message });
    statusEl.textContent = "error";
  } finally {
    runButton.disabled = false;
  }
});

refreshFiles();
loadProviders();
restoreSession();

async function loadProviders() {
  try {
    const response = await fetch("/api/providers");
    const payload = await response.json();
    if (!payload.ok || !Array.isArray(payload.providers)) return;
    providerCatalog = new Map(payload.providers.map((provider) => [provider.id, provider]));
    const selected = providerEl.value;
    providerEl.innerHTML = "";
    for (const provider of payload.providers) {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.label;
      providerEl.append(option);
    }
    providerEl.value = providerCatalog.has(selected) ? selected : payload.providers[0]?.id;
    updateProviderHint();
  } catch (error) {
    providerHintEl.textContent = "Provider list unavailable; using local selection";
  }
}

function updateProviderHint() {
  const provider = providerCatalog.get(providerEl.value);
  if (!provider) return;
  if (provider.protocol === "router") {
    providerHintEl.textContent = `Auto · task-aware model selection · ${formatTokens(provider.context_tokens || 0)} context`;
    return;
  }
  const protocol = provider.protocol === "router"
    ? "Smart cascade"
    : provider.protocol === "anthropic" ? "Anthropic" : "OpenAI-compatible";
  providerHintEl.textContent = `${protocol} · ${provider.default_model} · ${formatTokens(provider.context_tokens || 0)} context`;
}

async function readEventStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalEvent = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      addEvent(event);
      if (event.type === "session" && event.session_id) {
        sessionId = event.session_id;
        localStorage.setItem("code-agent-session-id", sessionId);
      }
      if (event.type === "final") finalEvent = event;
      if (event.type === "final" && !event.ok) statusEl.textContent = "error";
    }
  }

  if (buffer.trim()) {
    const event = JSON.parse(buffer);
    addEvent(event);
    if (event.type === "final") finalEvent = event;
  }
  return finalEvent;
}

async function restoreSession() {
  if (!sessionId) return;
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error("Session unavailable");
    const payload = await response.json();
    conversationHistory = (payload.messages || [])
      .filter((message) => ["user", "assistant"].includes(message.role) && !message.tool_calls && typeof message.content === "string")
      .slice(-20);
    if (!conversationHistory.length) return;
    removeEmptyEvent();
    for (const message of conversationHistory) {
      addEvent({ type: message.role, content: message.content });
    }
    const memory = payload.memory || {};
    const memoryItems = (memory.completed_tasks || []).length;
    contextStatusEl.textContent = `Session restored · ${memoryItems} completed task(s)`;
    await refreshReviews();
    await refreshMonitoring();
  } catch (error) {
    localStorage.removeItem("code-agent-session-id");
    sessionId = "";
  }
}

function switchPanel(panel) {
  const review = panel === "review";
  const monitor = panel === "monitor";
  chatTab.classList.toggle("active", !review && !monitor);
  reviewTab.classList.toggle("active", review);
  monitorTab.classList.toggle("active", monitor);
  chatTab.setAttribute("aria-selected", String(!review && !monitor));
  reviewTab.setAttribute("aria-selected", String(review));
  monitorTab.setAttribute("aria-selected", String(monitor));
  agentViewTitleEl.textContent = review ? "Review changes" : monitor ? "Run monitor" : "Workspace chat";
  chatView.hidden = review || monitor;
  reviewView.hidden = !review;
  monitorView.hidden = !monitor;
  if (monitor) refreshMonitoring();
}

async function refreshReviews() {
  if (!sessionId) {
    reviewChanges = [];
    renderReviews();
    return;
  }
  try {
    const response = await fetch(`/api/reviews?session_id=${encodeURIComponent(sessionId)}`);
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Cannot load reviews");
    reviewChanges = payload.reviews || [];
    renderReviews();
  } catch (error) {
    reviewChanges = [];
    reviewSummaryEl.textContent = error.message;
    reviewsEl.innerHTML = `<li class="empty">${escapeHtml(error.message)}</li>`;
    updateReviewControls();
  }
}

function renderReviews() {
  reviewsEl.innerHTML = "";
  const pending = reviewChanges.filter((change) => change.status === "pending");
  reviewSummaryEl.textContent = pending.length ? `${pending.length} change(s) waiting for review.` : "No changes to review.";
  reviewCountEl.textContent = pending.length ? pending.length : "";
  if (!reviewChanges.length) {
    reviewsEl.innerHTML = '<li class="empty">Run the agent to create review changes.</li>';
    updateReviewControls();
    return;
  }
  for (const change of reviewChanges) {
    const item = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const toggle = document.createElement("span");
    const checkbox = document.createElement("input");
    const icon = document.createElement("span");
    const file = document.createElement("span");
    const fileName = document.createElement("strong");
    const path = document.createElement("span");
    const stats = document.createElement("span");
    const status = document.createElement("span");
    const diffText = reviewDiff(change);
    const counts = diffStats(diffText);
    item.className = "review-list-item";
    details.className = `review-item ${change.status !== "pending" ? "resolved" : ""}`;
    details.open = change.status === "pending";
    toggle.className = "review-toggle";
    toggle.textContent = "›";
    checkbox.type = "checkbox";
    checkbox.dataset.reviewId = change.id;
    checkbox.disabled = change.status !== "pending";
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", updateReviewControls);
    const type = fileType(change.path);
    icon.className = `file-icon ${type.className}`;
    icon.textContent = type.label;
    file.className = "review-file";
    fileName.textContent = change.path.split("/").pop();
    path.textContent = `${change.path} · ${change.run_id}`;
    file.append(fileName, path);
    stats.className = "diff-stats";
    stats.innerHTML = `<span class="diff-add-count">+${counts.added}</span><span class="diff-del-count">-${counts.removed}</span>`;
    status.className = "review-status";
    status.textContent = change.status;
    summary.append(toggle, checkbox, icon, file, stats, status);
    details.append(summary, renderDiff(diffText));
    item.append(details);
    reviewsEl.append(item);
  }
  updateReviewControls();
}

function reviewDiff(change) {
  if (typeof change.diff === "string" && change.diff) return change.diff;
  const before = String(change.before_preview || "").split("\n");
  const after = String(change.after_preview || "").split("\n");
  if (before.join("\n") === after.join("\n")) return "No textual difference.";
  return [
    "--- before",
    ...before.map((line) => `- ${line}`),
    "+++ after",
    ...after.map((line) => `+ ${line}`),
  ].join("\n");
}

function diffStats(diff) {
  const lines = String(diff || "").split("\n");
  return {
    added: lines.filter((line) => line.startsWith("+") && !line.startsWith("+++")).length,
    removed: lines.filter((line) => line.startsWith("-") && !line.startsWith("---")).length,
  };
}

function renderDiff(diff) {
  const container = document.createElement("div");
  container.className = "diff-view";
  const lines = String(diff || "No textual difference.").split("\n");
  for (const line of lines) {
    const row = document.createElement("div");
    const gutter = document.createElement("span");
    const code = document.createElement("span");
    let kind = "context";
    let symbol = "";
    if (line.startsWith("@@")) {
      kind = "hunk";
      symbol = "@@";
    } else if (line.startsWith("+++") || line.startsWith("---")) {
      kind = "header";
    } else if (line.startsWith("+")) {
      kind = "add";
      symbol = "+";
    } else if (line.startsWith("-")) {
      kind = "remove";
      symbol = "-";
    }
    row.className = `diff-line ${kind}`;
    gutter.className = "diff-gutter";
    gutter.textContent = symbol;
    code.className = "diff-code";
    code.textContent = line;
    row.append(gutter, code);
    container.append(row);
  }
  return container;
}

function updateReviewControls() {
  const selected = reviewsEl.querySelectorAll("input[type=checkbox]:checked");
  reviewMergeButton.disabled = selected.length < 2;
  reviewRollbackButton.disabled = selected.length < 1;
}

async function rollbackSelectedReviews() {
  const selected = [...reviewsEl.querySelectorAll("input[type=checkbox]:checked")].map((input) => Number(input.dataset.reviewId));
  if (!selected.length) return;
  reviewRollbackButton.disabled = true;
  try {
    const response = await fetch("/api/reviews/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, change_ids: selected }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Rollback failed");
    terminalEl.textContent = `Rolled back ${payload.paths.join(", ")}.`;
    await refreshReviews();
    await refreshFiles();
    if (activeFile) await openFile({ path: activeFile, language: languageEl.textContent });
  } catch (error) {
    reviewSummaryEl.textContent = error.message;
    updateReviewControls();
  }
}

async function mergeSelectedReviews() {
  const selected = [...reviewsEl.querySelectorAll("input[type=checkbox]:checked")].map((input) => Number(input.dataset.reviewId));
  if (selected.length < 2) return;
  reviewMergeButton.disabled = true;
  try {
    const response = await fetch("/api/reviews/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, change_ids: selected }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Merge failed");
    terminalEl.textContent = `Merged ${payload.paths.join(", ")}.`;
    await refreshReviews();
    if (activeFile) await openFile({ path: activeFile, language: languageEl.textContent });
  } catch (error) {
    reviewSummaryEl.textContent = error.message;
    updateReviewControls();
  }
}

async function refreshMonitoring() {
  const params = new URLSearchParams({ workspace: workspaceEl.value });
  if (sessionId) params.set("session_id", sessionId);
  try {
    const response = await fetch(`/api/monitoring?${params}`);
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Cannot load monitoring");
    renderMonitoring(payload);
  } catch (error) {
    monitorScopeEl.textContent = error.message;
    monitorKpisEl.innerHTML = '<div class="empty">Monitoring data unavailable.</div>';
    monitorProvidersEl.innerHTML = "";
    monitorRoutesEl.innerHTML = "";
    monitorToolsEl.innerHTML = "";
    monitorErrorsEl.innerHTML = "";
  }
}

function renderMonitoring(payload) {
  const summary = payload.summary || {};
  monitorScopeEl.textContent = payload.scope === "session" ? "Session monitor" : "Workspace monitor";
  const cards = [
    { value: summary.llm_requests || 0, label: "LLM requests", tone: "cyan" },
    { value: `${summary.llm_error_rate || 0}%`, label: "LLM error rate", tone: summary.llm_errors ? "red" : "green" },
    { value: `${summary.run_error_rate || 0}%`, label: "Run error rate", tone: summary.failed_runs ? "red" : "green" },
    { value: formatTokens(summary.total_tokens || 0), label: `Tokens · ${summary.actual_usage_calls || 0} actual`, tone: "purple" },
    { value: `${summary.avg_latency_ms || 0} ms`, label: `Average · p95 ${summary.p95_latency_ms || 0} ms`, tone: "amber" },
    { value: `${summary.prompt_cache_hit_rate || 0}%`, label: "Prompt cache hit", tone: "cyan" },
    { value: summary.tool_calls || 0, label: `Tool calls · ${summary.tool_error_rate || 0}% errors`, tone: "green" },
    { value: summary.compactions || 0, label: "Context compactions", tone: "purple" },
    { value: summary.route_decisions || 0, label: `Auto routes · ${summary.route_fallbacks || 0} fallback(s)`, tone: "amber" },
  ];
  monitorKpisEl.innerHTML = "";
  for (const card of cards) {
    const item = document.createElement("div");
    const value = document.createElement("strong");
    const label = document.createElement("span");
    item.className = "monitor-kpi";
    item.dataset.tone = card.tone;
    value.textContent = card.value;
    label.textContent = card.label;
    item.append(value, label);
    monitorKpisEl.append(item);
  }
  renderMetricRows(monitorProvidersEl, payload.providers || [], (item) => `${item.name} · ${item.requests} request(s)`, (item) => `${item.total_tokens} tokens · ${item.error_rate}% errors · ${item.avg_latency_ms} ms avg`);
  renderMetricRows(monitorRoutesEl, payload.routes || [], (item) => item.name, (item) => `${item.requests} selection(s) · ${item.error_rate}% used fallback`);
  renderMetricRows(monitorToolsEl, payload.tools || [], (item) => item.name, (item) => `${item.requests} call(s) · ${item.error_rate}% errors`);
  monitorErrorsEl.innerHTML = "";
  const errors = payload.recent_errors || [];
  if (!errors.length) {
    monitorErrorsEl.innerHTML = '<div class="empty">No recent errors.</div>';
    return;
  }
  for (const error of errors) {
    const row = document.createElement("div");
    row.className = "monitor-row monitor-error";
    row.style.setProperty("--metric-width", "100%");
    const title = document.createElement("strong");
    title.textContent = error.kind || "error";
    const detail = document.createElement("span");
    detail.textContent = error.error || "Unknown error";
    row.append(title, detail);
    monitorErrorsEl.append(row);
  }
}

function renderMetricRows(container, rows, title, detail) {
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }
  const maximum = Math.max(...rows.map((metric) => Number(metric.requests || 0)), 1);
  for (const metric of rows) {
    const row = document.createElement("div");
    row.className = "monitor-row";
    row.style.setProperty("--metric-width", `${Math.max(4, Number(metric.requests || 0) / maximum * 100)}%`);
    row.style.setProperty("--metric-color", Number(metric.error_rate || 0) > 0 ? "var(--red)" : "var(--accent)");
    const name = document.createElement("strong");
    name.textContent = title(metric);
    const metadata = document.createElement("span");
    metadata.textContent = detail(metric);
    row.append(name, metadata);
    container.append(row);
  }
}

async function refreshFiles() {
  const params = new URLSearchParams({ workspace: workspaceEl.value });
  const response = await fetch(`/api/files?${params}`);
  const payload = await response.json();
  if (!payload.ok) {
    filesEl.innerHTML = `<li class="empty">${escapeHtml(payload.error || "Cannot load files")}</li>`;
    return [];
  }
  const files = payload.files || [];
  renderFiles(files);
  return files;
}

function renderFiles(files) {
  filesEl.innerHTML = "";
  fileCountEl.textContent = `${files.length} file${files.length === 1 ? "" : "s"}`;
  if (!files.length) {
    filesEl.innerHTML = '<div class="empty">No files yet</div>';
    return;
  }

  const tree = buildFileTree(files);
  const root = document.createElement("ul");
  root.className = "tree-group";
  renderTreeLevel(tree, root, "");
  filesEl.append(root);
}

function buildFileTree(files) {
  const root = { directories: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let node = root;
    for (const directory of parts.slice(0, -1)) {
      if (!node.directories.has(directory)) {
        node.directories.set(directory, { directories: new Map(), files: [] });
      }
      node = node.directories.get(directory);
    }
    node.files.push({ ...file, name: parts.at(-1) || file.path });
  }
  return root;
}

function renderTreeLevel(node, container, parentPath) {
  const directories = [...node.directories.entries()].sort(([left], [right]) => left.localeCompare(right));
  for (const [name, childNode] of directories) {
    const folderPath = parentPath ? `${parentPath}/${name}` : name;
    const item = document.createElement("li");
    const button = document.createElement("button");
    const children = document.createElement("ul");
    const expanded = !collapsedFolders.has(folderPath);
    button.className = "tree-folder";
    button.type = "button";
    button.setAttribute("aria-expanded", String(expanded));
    button.title = folderPath;
    button.innerHTML = '<span class="tree-chevron" aria-hidden="true">›</span><span class="tree-folder-icon" aria-hidden="true"></span>';
    const label = document.createElement("span");
    label.className = "tree-name";
    label.textContent = name;
    button.append(label);
    children.className = "tree-group tree-children";
    children.hidden = !expanded;
    button.addEventListener("click", () => {
      const nextExpanded = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(nextExpanded));
      children.hidden = !nextExpanded;
      if (nextExpanded) collapsedFolders.delete(folderPath);
      else collapsedFolders.add(folderPath);
    });
    renderTreeLevel(childNode, children, folderPath);
    item.append(button, children);
    container.append(item);
  }

  const sortedFiles = [...node.files].sort((left, right) => left.name.localeCompare(right.name));
  for (const file of sortedFiles) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const icon = document.createElement("span");
    const name = document.createElement("span");
    const meta = document.createElement("span");
    const type = fileType(file.path);
    button.className = `file-node ${file.path === activeFile ? "active" : ""}`;
    button.type = "button";
    button.dataset.path = file.path;
    button.title = file.path;
    icon.className = `file-icon ${type.className}`;
    icon.textContent = type.label;
    name.className = "file-name";
    name.textContent = file.name;
    meta.className = "file-meta";
    meta.textContent = formatBytes(file.size);
    button.append(icon, name, meta);
    button.addEventListener("click", () => openFile(file));
    item.append(button);
    container.append(item);
  }
}

function fileType(path) {
  const extension = path.includes(".") ? path.split(".").pop().toLowerCase() : "";
  const types = {
    py: { className: "py", label: "PY" },
    js: { className: "js", label: "JS" },
    jsx: { className: "js", label: "JS" },
    ts: { className: "ts", label: "TS" },
    tsx: { className: "ts", label: "TS" },
    html: { className: "html", label: "<>" },
    css: { className: "css", label: "#" },
    json: { className: "json", label: "{}" },
    md: { className: "md", label: "M" },
    cpp: { className: "cpp", label: "C+" },
    cc: { className: "cpp", label: "C+" },
    h: { className: "cpp", label: "H" },
    txt: { className: "text", label: "T" },
  };
  return types[extension] || { className: "text", label: "·" };
}

async function openFile(file) {
  const requestId = ++openFileRequestId;
  activeFile = file.path;
  fileTitleEl.textContent = file.path;
  languageEl.textContent = file.language || "Text";
  editorEl.disabled = false;
  editorEl.value = `Loading ${file.path}...`;
  saveFileButton.disabled = true;
  runFileButton.disabled = true;
  renderActiveFile();

  const params = new URLSearchParams({ workspace: workspaceEl.value, path: file.path });
  try {
    const response = await fetch(`/api/file?${params}`);
    const payload = await response.json();
    if (requestId !== openFileRequestId) return;
    if (!payload.ok) {
      editorEl.value = "";
      terminalEl.textContent = payload.error || "Cannot read file";
      return;
    }
    editorEl.value = payload.content;
    saveFileButton.disabled = false;
    runFileButton.disabled = false;
    terminalEl.textContent = `Opened ${file.path}.`;
  } catch (error) {
    if (requestId !== openFileRequestId) return;
    editorEl.value = "";
    terminalEl.textContent = error.message;
  }
}

async function saveActiveFile() {
  if (!activeFile) return;
  saveFileButton.disabled = true;
  try {
    const response = await fetch("/api/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workspace: workspaceEl.value,
        path: activeFile,
        content: editorEl.value,
      }),
    });
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error || "Save failed");
    terminalEl.textContent = `Saved ${payload.path} (${formatBytes(payload.size)}).`;
    await refreshFiles();
  } catch (error) {
    terminalEl.textContent = error.message;
  } finally {
    saveFileButton.disabled = false;
  }
}

async function runActiveFile() {
  if (!activeFile) return;
  runFileButton.disabled = true;
  terminalEl.textContent = `Running ${activeFile}...`;
  try {
    await saveActiveFile();
    const response = await fetch("/api/run-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace: workspaceEl.value, path: activeFile }),
    });
    const payload = await response.json();
    terminalEl.textContent = payload.output || payload.error || "No output.";
  } catch (error) {
    terminalEl.textContent = error.message;
  } finally {
    runFileButton.disabled = false;
  }
}

function resetEditor() {
  openFileRequestId++;
  fileTitleEl.textContent = "No file selected";
  languageEl.textContent = "Text";
  editorEl.value = "// Choose a file from the left to edit it.";
  editorEl.disabled = true;
  saveFileButton.disabled = true;
  runFileButton.disabled = true;
  terminalEl.textContent = "No file command has run yet.";
}

function renderActiveFile() {
  document.querySelectorAll(".file-node").forEach((button) => {
    const isActive = button.dataset.path === activeFile;
    button.classList.toggle("active", isActive);
  });
}

function addEvent(event) {
  if (event.type === "context") {
    const used = formatTokens(event.estimated_tokens || 0);
    const max = formatTokens(event.max_tokens || 0);
    const compacted = event.compacted_blocks ? ` · compacted ${event.compacted_blocks}` : "";
    contextStatusEl.textContent = `Context ${used} / ${max}${compacted}`;
    return;
  }
  if (!shouldDisplayEvent(event)) return;
  const item = document.createElement("li");
  const head = document.createElement("div");
  const avatar = document.createElement("span");
  const title = document.createElement("span");
  const content = document.createElement("div");
  item.className = `event ${className(event)}`;
  head.className = "event-head";
  avatar.className = "event-avatar";
  avatar.textContent = avatarFor(event);
  title.className = "event-title";
  title.textContent = titleFor(event);
  content.className = "event-content";
  head.append(avatar, title);
  if (event.type === "route") {
    content.append(renderRoute(event));
  } else {
    content.append(renderMarkdown(bodyFor(event)));
  }
  item.append(head, content);
  eventsEl.append(item);
  scrollEventsToBottom();
}

function avatarFor(event) {
  if (event.type === "user") return "U";
  if (event.type === "route") return "↗";
  if (event.type === "error") return "!";
  return "AI";
}

function renderRoute(event) {
  const card = document.createElement("div");
  const model = document.createElement("div");
  const chips = document.createElement("div");
  const reason = document.createElement("div");
  card.className = "route-card";
  model.className = "route-model";
  model.textContent = `${event.selected_provider || "unavailable"} / ${event.selected_model || "unknown"}`;
  chips.className = "route-chips";
  const values = [
    event.task_type || "general",
    event.stage || "auto",
    `score ${event.score ?? 0}`,
  ];
  if (event.fallback_count) values.push(`${event.fallback_count} fallback(s)`);
  for (const value of values) {
    const chip = document.createElement("span");
    chip.className = "route-chip";
    chip.textContent = value;
    chips.append(chip);
  }
  reason.className = "route-reason";
  reason.textContent = Array.isArray(event.reasons) ? event.reasons.join(" · ") : "Task-aware selection";
  card.append(model, chips, reason);
  return card;
}

function renderMarkdown(value) {
  const fragment = document.createDocumentFragment();
  const lines = String(value || "").replace(/\r\n/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^```\s*([\w+-]*)\s*$/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      fragment.append(renderCodeBlock(codeLines.join("\n"), fence[1] || "text"));
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(element, heading[2]);
      fragment.append(element);
      index += 1;
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const list = document.createElement("ul");
      while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
        const item = document.createElement("li");
        appendInlineMarkdown(item, lines[index].replace(/^[-*]\s+/, ""));
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const list = document.createElement("ol");
      while (index < lines.length && /^\d+\.\s+/.test(lines[index])) {
        const item = document.createElement("li");
        appendInlineMarkdown(item, lines[index].replace(/^\d+\.\s+/, ""));
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    if (/^>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quoteLines.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      appendLinesWithBreaks(quote, quoteLines);
      fragment.append(quote);
      continue;
    }

    const paragraph = document.createElement("p");
    const paragraphLines = [];
    while (index < lines.length && lines[index].trim() && !isMarkdownBlockStart(lines[index])) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    if (!paragraphLines.length) {
      paragraphLines.push(line);
      index += 1;
    }
    appendLinesWithBreaks(paragraph, paragraphLines);
    fragment.append(paragraph);
  }
  return fragment;
}

function isMarkdownBlockStart(line) {
  return /^```/.test(line) || /^(#{1,3})\s+/.test(line) || /^[-*]\s+/.test(line)
    || /^\d+\.\s+/.test(line) || /^>\s?/.test(line);
}

function appendLinesWithBreaks(container, lines) {
  lines.forEach((line, index) => {
    if (index) container.append(document.createElement("br"));
    appendInlineMarkdown(container, line);
  });
}

function appendInlineMarkdown(container, text) {
  const source = String(text || "");
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    container.append(document.createTextNode(source.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      container.append(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      container.append(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      emphasis.textContent = token.slice(1, -1);
      container.append(emphasis);
    } else {
      const parts = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
      if (parts) {
        const link = document.createElement("a");
        link.textContent = parts[1];
        link.href = parts[2];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        container.append(link);
      }
    }
    cursor = match.index + token.length;
  }
  container.append(document.createTextNode(source.slice(cursor)));
}

function renderCodeBlock(code, language) {
  const wrapper = document.createElement("div");
  const head = document.createElement("div");
  const label = document.createElement("span");
  const copy = document.createElement("button");
  const pre = document.createElement("pre");
  const content = document.createElement("code");
  wrapper.className = "code-block";
  head.className = "code-block-head";
  label.textContent = language;
  copy.className = "code-copy";
  copy.type = "button";
  copy.textContent = "Copy";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(code);
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy"; }, 1200);
    } catch (_) {
      copy.textContent = "Unavailable";
    }
  });
  content.textContent = code;
  pre.append(content);
  head.append(label, copy);
  wrapper.append(head, pre);
  return wrapper;
}

function scrollEventsToBottom() {
  eventsEl.scrollTop = eventsEl.scrollHeight;
  requestAnimationFrame(() => {
    eventsEl.scrollTop = eventsEl.scrollHeight;
  });
}

function removeEmptyEvent() {
  eventsEl.querySelectorAll(".empty").forEach((item) => item.remove());
}

function modePlaceholder(mode) {
  if (mode === "ask") return "Ask a question about this repository.";
  if (mode === "architect") return "Describe the change you want planned.";
  if (mode === "context") return "Show the repository map and retained context.";
  return "Describe a coding task for the agent.";
}

function formatTokens(value) {
  if (value < 1000) return String(value);
  return `${(value / 1000).toFixed(1)}k`;
}

function shouldDisplayEvent(event) {
  return ["user", "assistant", "final", "error", "route"].includes(event.type);
}

function titleFor(event) {
  if (event.type === "assistant") return "Assistant";
  if (event.type === "final") return event.ok ? "Final" : "Failed";
  if (event.type === "user") return "You";
  if (event.type === "route") return "Auto Route";
  return "Error";
}

function bodyFor(event) {
  if (event.type === "assistant") return event.content || "";
  if (event.type === "final") return event.content || "";
  if (event.type === "user") return event.content || "";
  if (event.type === "route") {
    const selected = `${event.selected_provider || "unavailable"} / ${event.selected_model || "unknown"}`;
    const reason = Array.isArray(event.reasons) ? event.reasons.join(", ") : "task score";
    const fallback = event.fallback_count ? ` · ${event.fallback_count} fallback(s)` : "";
    return `${selected}\n${event.task_type || "general"} · ${event.stage || "auto"} · score ${event.score ?? 0}${fallback}\n${reason}`;
  }
  return event.message || "Unknown error";
}

function className(event) {
  if (event.type === "error") return "error";
  if (event.type === "assistant") return "assistant";
  if (event.type === "user") return "user";
  if (event.type === "route") return "route";
  if (event.type === "final") return "final";
  return "";
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char];
  });
}
