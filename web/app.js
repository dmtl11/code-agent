const eventsEl = document.querySelector("#events");
const filesEl = document.querySelector("#files");
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
const modeButtons = document.querySelectorAll("#mode-switch [data-mode]");
const contextStatusEl = document.querySelector("#context-status");
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
const monitorScopeEl = document.querySelector("#monitor-scope");
const monitorKpisEl = document.querySelector("#monitor-kpis");
const monitorProvidersEl = document.querySelector("#monitor-providers");
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

clearButton.addEventListener("click", async () => {
  eventsEl.innerHTML = '<li class="empty">Ask the agent to change code in this workspace.</li>';
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
monitorRefreshButton.addEventListener("click", refreshMonitoring);

refreshButton.addEventListener("click", refreshFiles);
workspaceEl.addEventListener("change", () => {
  activeFile = "";
  resetEditor();
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
  const protocol = provider.protocol === "anthropic" ? "Anthropic" : "OpenAI-compatible";
  providerHintEl.textContent = `${protocol} · ${provider.default_model}`;
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
  reviewCountEl.textContent = pending.length ? `(${pending.length})` : "";
  if (!reviewChanges.length) {
    reviewsEl.innerHTML = '<li class="empty">Run the agent to create review changes.</li>';
    updateReviewControls();
    return;
  }
  for (const change of reviewChanges) {
    const item = document.createElement("li");
    item.className = `review-item ${change.status === "merged" ? "merged" : ""}`;
    const head = document.createElement("div");
    head.className = "review-item-head";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.reviewId = change.id;
    checkbox.disabled = change.status !== "pending";
    checkbox.addEventListener("change", updateReviewControls);
    const label = document.createElement("label");
    label.textContent = change.path;
    const meta = document.createElement("span");
    meta.className = "review-meta";
    meta.textContent = `${change.status} · ${change.run_id}`;
    label.append(meta);
    head.append(checkbox, label);
    const diff = document.createElement("pre");
    diff.className = "review-diff";
    diff.textContent = reviewDiff(change);
    item.append(head, diff);
    reviewsEl.append(item);
  }
  updateReviewControls();
}

function reviewDiff(change) {
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

function updateReviewControls() {
  const selected = reviewsEl.querySelectorAll("input[type=checkbox]:checked");
  reviewMergeButton.disabled = selected.length < 2;
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
    monitorToolsEl.innerHTML = "";
    monitorErrorsEl.innerHTML = "";
  }
}

function renderMonitoring(payload) {
  const summary = payload.summary || {};
  monitorScopeEl.textContent = payload.scope === "session" ? "Session monitor" : "Workspace monitor";
  const cards = [
    [summary.llm_requests || 0, "LLM requests"],
    [`${summary.llm_error_rate || 0}%`, "LLM error rate"],
    [`${summary.run_error_rate || 0}%`, "run error rate"],
    [formatTokens(summary.total_tokens || 0), `tokens · ${summary.actual_usage_calls || 0} actual`],
    [`${summary.avg_latency_ms || 0} ms`, `average latency · p95 ${summary.p95_latency_ms || 0} ms`],
    [`${summary.prompt_cache_hit_rate || 0}%`, "prompt cache hit"],
    [summary.tool_calls || 0, `tool calls · ${summary.tool_error_rate || 0}% errors`],
    [summary.compactions || 0, "context compactions"],
  ];
  monitorKpisEl.innerHTML = cards.map(([value, label]) => `<div class="monitor-kpi"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join("");
  renderMetricRows(monitorProvidersEl, payload.providers || [], (item) => `${item.name} · ${item.requests} request(s)`, (item) => `${item.total_tokens} tokens · ${item.error_rate}% errors · ${item.avg_latency_ms} ms avg`);
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
  for (const metric of rows) {
    const row = document.createElement("div");
    row.className = "monitor-row";
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
  if (!files.length) {
    filesEl.innerHTML = '<li class="empty">No files yet</li>';
    return;
  }

  for (const file of files) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const name = document.createElement("span");
    const meta = document.createElement("span");
    button.className = `file ${file.path === activeFile ? "active" : ""}`;
    button.type = "button";
    button.dataset.path = file.path;
    name.className = "file-name";
    meta.className = "file-meta";
    name.textContent = file.path;
    meta.textContent = formatBytes(file.size);
    button.append(name, meta);
    button.addEventListener("click", () => openFile(file));
    item.append(button);
    filesEl.append(item);
  }
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
  document.querySelectorAll(".file").forEach((button) => {
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
  const pre = document.createElement("pre");
  const title = document.createElement("strong");
  item.className = `event ${className(event)}`;
  title.textContent = titleFor(event);
  pre.textContent = bodyFor(event);
  item.append(title, pre);
  eventsEl.append(item);
  eventsEl.scrollTop = eventsEl.scrollHeight;
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
  return ["user", "assistant", "final", "error"].includes(event.type);
}

function titleFor(event) {
  if (event.type === "assistant") return "Assistant";
  if (event.type === "final") return event.ok ? "Final" : "Failed";
  if (event.type === "user") return "You";
  return "Error";
}

function bodyFor(event) {
  if (event.type === "assistant") return event.content || "";
  if (event.type === "final") return event.content || "";
  if (event.type === "user") return event.content || "";
  return event.message || "Unknown error";
}

function className(event) {
  if (event.type === "error") return "error";
  if (event.type === "assistant") return "assistant";
  if (event.type === "user") return "user";
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
