"use strict";

/* ------------------------------------------------------------------ *
 * 贪吃蛇游戏逻辑
 * ------------------------------------------------------------------ */

const COLS = 20;
const ROWS = 20;
const CELL = 20; // canvas is 400x400

const canvas = document.getElementById("board");
const ctx = canvas.getContext("2d");

const scoreEl = document.getElementById("score");
const bestEl = document.getElementById("best");
const restartBtn = document.getElementById("restartBtn");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlayTitle");
const overlayMsg = document.getElementById("overlayMsg");
const overlayBtn = document.getElementById("overlayBtn");
const leaderboardEl = document.getElementById("leaderboard");

const DIRS = {
  up: { x: 0, y: -1 },
  down: { x: 0, y: 1 },
  left: { x: -1, y: 0 },
  right: { x: 1, y: 0 },
};

const OPPOSITE = {
  up: "down",
  down: "up",
  left: "right",
  right: "left",
};

let snake = [];
let food = null;
let dir = "right";
let nextDir = "right";
let score = 0;
let aiMode = false; // AI 自动模式
let best = Number(localStorage.getItem("snake_best") || 0);
let running = false;
let gameOver = false;
let loopTimer = null;
const SPEED = 130; // ms per tick

// AI 模式：自动寻找食物
function aiGetNextDirection() {
  if (!food) return null;
  
  const head = snake[0];
  
  // BFS 寻找最短路径到食物
  function bfs(start, target, blocked) {
    const queue = [[start]];
    const visited = new Set();
    visited.add(keyOf(start.x, start.y));
    
    while (queue.length > 0) {
      const path = queue.shift();
      const pos = path[path.length - 1];
      
      if (pos.x === target.x && pos.y === target.y) {
        return path.slice(1); // 返回除起点外的路径
      }
      
      for (const d of [DIRS.up, DIRS.down, DIRS.left, DIRS.right]) {
        const nx = pos.x + d.x;
        const ny = pos.y + d.y;
        
        if (nx < 0 || ny < 0 || nx >= COLS || ny >= ROWS) continue;
        if (blocked.has(keyOf(nx, ny))) continue;
        if (visited.has(keyOf(nx, ny))) continue;
        
        visited.add(keyOf(nx, ny));
        queue.push([...path, { x: nx, y: ny }]);
      }
    }
    return null; // 无路径
  }
  
  const blocked = occupiedSet();
  const path = bfs(head, food, blocked);
  
  if (path && path.length > 0) {
    const next = path[0];
    if (next.x > head.x) return "right";
    if (next.x < head.x) return "left";
    if (next.y > head.y) return "down";
    if (next.y < head.y) return "up";
  }
  
  // 如果找不到食物路径，尝试找安全的空位
  for (const d of [DIRS.up, DIRS.down, DIRS.left, DIRS.right]) {
    const nx = head.x + d.x;
    const ny = head.y + d.y;
    if (nx >= 0 && ny >= 0 && nx < COLS && ny < ROWS && !blocked.has(keyOf(nx, ny))) {
      if (d === DIRS.up) return "up";
      if (d === DIRS.down) return "down";
      if (d === DIRS.left) return "left";
      if (d === DIRS.right) return "right";
    }
  }
  
  return null;
}

bestEl.textContent = best;

/* ------------------------------------------------------------------ *
 * 基础工具
 * ------------------------------------------------------------------ */

function randInt(n) {
  return Math.floor(Math.random() * n);
}

function keyOf(x, y) {
  return x + "," + y;
}

function sameCell(a, b) {
  return a.x === b.x && a.y === b.y;
}

function occupiedSet() {
  const set = new Set();
  for (const seg of snake) set.add(keyOf(seg.x, seg.y));
  return set;
}

function spawnFood() {
  const occupied = occupiedSet();
  const free = [];
  for (let y = 0; y < ROWS; y++) {
    for (let x = 0; x < COLS; x++) {
      if (!occupied.has(keyOf(x, y))) free.push({ x, y });
    }
  }
  if (free.length === 0) return null; // board full -> win
  food = free[randInt(free.length)];
}

function resetGame() {
  snake = [
    { x: 9, y: 10 },
    { x: 8, y: 10 },
    { x: 7, y: 10 },
  ];
  dir = "right";
  nextDir = "right";
  score = 0;
  gameOver = false;
  scoreEl.textContent = score;
  spawnFood();
  hideOverlay();
  draw();
}

/* ------------------------------------------------------------------ *
 * 渲染
 * ------------------------------------------------------------------ */

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // subtle grid
  ctx.strokeStyle = "rgba(148,163,184,0.08)";
  ctx.lineWidth = 1;
  for (let i = 1; i < COLS; i++) {
    ctx.beginPath();
    ctx.moveTo(i * CELL, 0);
    ctx.lineTo(i * CELL, canvas.height);
    ctx.stroke();
  }
  for (let i = 1; i < ROWS; i++) {
    ctx.beginPath();
    ctx.moveTo(0, i * CELL);
    ctx.lineTo(canvas.width, i * CELL);
    ctx.stroke();
  }

  // food
  if (food) {
    ctx.fillStyle = "#ef4444";
    ctx.beginPath();
    ctx.arc(food.x * CELL + CELL / 2, food.y * CELL + CELL / 2, CELL / 2 - 2, 0, Math.PI * 2);
    ctx.fill();
  }

  // snake
  snake.forEach((seg, i) => {
    const head = i === 0;
    ctx.fillStyle = head ? "#4ade80" : "#22c55e";
    const pad = head ? 1 : 2;
    ctx.fillRect(seg.x * CELL + pad, seg.y * CELL + pad, CELL - pad * 2, CELL - pad * 2);
    if (head) {
      // eyes
      ctx.fillStyle = "#052e16";
      const ex = seg.x * CELL + CELL / 2;
      const ey = seg.y * CELL + CELL / 2;
      const off = 4;
      const d = DIRS[dir];
      ctx.beginPath();
      ctx.arc(ex + d.x * off - d.y * 3, ey + d.y * off - d.x * 3, 2, 0, Math.PI * 2);
      ctx.arc(ex + d.x * off + d.y * 3, ey + d.y * off + d.x * 3, 2, 0, Math.PI * 2);
      ctx.fill();
    }
  });
}

/* ------------------------------------------------------------------ *
 * 游戏循环
 * ------------------------------------------------------------------ */

function step() {
  if (!running || gameOver) return;

  dir = nextDir;
  const head = snake[0];
  const d = DIRS[dir];
  const nx = head.x + d.x;
  const ny = head.y + d.y;

  // wall collision
  if (nx < 0 || ny < 0 || nx >= COLS || ny >= ROWS) {
    endGame(false);
    return;
  }

  const willEat = food && nx === food.x && ny === food.y;
  const body = willEat ? snake : snake.slice(0, -1);

  // self collision (tail moves away unless eating)
  for (const seg of body) {
    if (seg.x === nx && seg.y === ny) {
      endGame(false);
      return;
    }
  }

  snake.unshift({ x: nx, y: ny });
  if (willEat) {
    score += 10;
    scoreEl.textContent = score;
    if (score > best) {
      best = score;
      localStorage.setItem("snake_best", String(best));
      bestEl.textContent = best;
    }
    spawnFood();
    if (!food) {
      endGame(true); // board full, you win
      return;
    }
  } else {
    snake.pop();
  }

  draw();
}

function endGame(won) {
  gameOver = true;
  running = false;
  if (loopTimer) {
    clearInterval(loopTimer);
    loopTimer = null;
  }
  draw();

  if (won) {
    overlayTitle.textContent = "🎉 你赢了！";
    overlayMsg.textContent = "棋盘已填满，太厉害了！得分 " + score;
  } else {
    overlayTitle.textContent = "游戏结束";
    overlayMsg.textContent = "本局得分 " + score + "，最高 " + best;
  }
  showOverlay();
  if (score > 0) {
    promptSaveScore();
  }
}

function startLoop() {
  if (loopTimer) clearInterval(loopTimer);
  loopTimer = setInterval(step, SPEED);
}

function startGame() {
  resetGame();
  running = true;
  startLoop();
  // 如果是 AI 模式，启动 AI 循环
  if (aiMode) startAiLoop();
}

// AI 自动控制循环
function startAiLoop() {
  if (!aiMode || !running || gameOver) return;
  
  // 清除已有的 loopTimer
  if (loopTimer) clearInterval(loopTimer);
  
  // AI 模式下使用固定速度 80ms
  loopTimer = setInterval(() => {
    if (running && !gameOver) {
      // AI 自动计算下一步方向
      const aiDir = aiGetNextDirection();
      if (aiDir && OPPOSITE[aiDir] !== dir) {
        nextDir = aiDir;
      }
      step();
    }
  }, 80);
}

/* ------------------------------------------------------------------ *
 * 覆盖层
 * ------------------------------------------------------------------ */

function showOverlay() {
  overlay.classList.remove("hidden");
}

function hideOverlay() {
  overlay.classList.add("hidden");
}

/* ------------------------------------------------------------------ *
 * 键盘控制
 * ------------------------------------------------------------------ */

function handleKey(e) {
  const key = e.key.toLowerCase();
  let wanted = null;
  if (key === "arrowup" || key === "w") wanted = "up";
  else if (key === "arrowdown" || key === "s") wanted = "down";
  else if (key === "arrowleft" || key === "a") wanted = "left";
  else if (key === "arrowright" || key === "d") wanted = "right";

  if (!wanted) return;

  e.preventDefault();

  if (!running && !gameOver) {
    // start on first directional key
    startGame();
  }
  if (!running) return;

  if (OPPOSITE[wanted] !== dir) {
    nextDir = wanted;
  }
}

/* ------------------------------------------------------------------ *
 * 排行榜 API
 * ------------------------------------------------------------------ */

async function fetchLeaderboard() {
  try {
    const res = await fetch("/api/scores");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    renderLeaderboard(data.scores || []);
  } catch (err) {
    console.error("加载排行榜失败:", err);
    leaderboardEl.innerHTML = '<li class="empty">排行榜加载失败</li>';
  }
}

function renderLeaderboard(scores) {
  if (!scores.length) {
    leaderboardEl.innerHTML = '<li class="empty">暂无记录</li>';
    return;
  }
  leaderboardEl.innerHTML = "";
  scores.forEach((entry, i) => {
    const li = document.createElement("li");
    const rank = document.createElement("span");
    rank.className = "rank";
    rank.textContent = i + 1;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name;
    const pts = document.createElement("span");
    pts.className = "pts";
    pts.textContent = entry.score;
    li.append(rank, name, pts);
    leaderboardEl.appendChild(li);
  });
}

async function saveScore(name, score) {
  const res = await fetch("/api/scores", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, score }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "保存失败");
  }
}

/* ------------------------------------------------------------------ *
 * 保存分数（游戏结束时弹出昵称输入）
 * ------------------------------------------------------------------ */

function promptSaveScore() {
  const name = window.prompt("恭喜获得 " + score + " 分！\n输入昵称上榜（留空跳过）：", "");
  if (name === null) return; // cancelled
  const trimmed = name.trim();
  if (!trimmed) return;
  saveScore(trimmed, score)
    .then(() => fetchLeaderboard())
    .catch((err) => {
      console.error("保存分数失败:", err);
      alert("保存失败：" + err.message);
    });
}

/* ------------------------------------------------------------------ *
 * 事件绑定与初始化
 * ------------------------------------------------------------------ */

restartBtn.addEventListener("click", startGame);
overlayBtn.addEventListener("click", startGame);
window.addEventListener("keydown", handleKey);

// AI 模式切换
const aiToggle = document.getElementById("aiToggle");
aiToggle.addEventListener("click", () => {
  aiMode = !aiMode;
  aiToggle.textContent = aiMode ? "🤖 AI 模式（开）" : "🤖 AI 模式";
  aiToggle.style.background = aiMode ? "#22c55e" : "";
  aiToggle.style.color = aiMode ? "#fff" : "";
  
  if (aiMode && running && !gameOver) {
    // 重新开始游戏以启用 AI
    startGame();
  } else if (!aiMode && gameOver) {
    // 关闭 AI 时如果游戏结束，重置游戏
    resetGame();
  }
});

resetGame();
fetchLeaderboard();
