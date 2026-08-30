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
let activeFile = "";
let openFileRequestId = 0;

clearButton.addEventListener("click", () => {
  eventsEl.innerHTML = '<li class="empty">Ask the agent to change code in this workspace.</li>';
});

refreshButton.addEventListener("click", refreshFiles);
workspaceEl.addEventListener("change", () => {
  activeFile = "";
  resetEditor();
  refreshFiles();
});
saveFileButton.addEventListener("click", saveActiveFile);
runFileButton.addEventListener("click", runActiveFile);

runButton.addEventListener("click", async () => {
  statusEl.textContent = "running";
  runButton.disabled = true;
  eventsEl.innerHTML = "";
  addEvent({ type: "user", content: taskEl.value });

  try {
    const response = await fetch("/api/run-stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task: taskEl.value,
        workspace: workspaceEl.value,
      }),
    });
    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`);
    }
    await readEventStream(response.body);
    statusEl.textContent = "done";
    await refreshFiles();
  } catch (error) {
    addEvent({ type: "error", message: error.message });
    statusEl.textContent = "error";
  } finally {
    runButton.disabled = false;
  }
});

refreshFiles();

async function readEventStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

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
      if (event.type === "final" && !event.ok) statusEl.textContent = "error";
    }
  }

  if (buffer.trim()) addEvent(JSON.parse(buffer));
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
