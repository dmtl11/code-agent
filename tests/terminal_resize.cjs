// Run with Node and Playwright available via NODE_PATH. No model requests are made.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const artifacts = path.join(root, ".code_agent", "ui-tests");
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://localhost");
  if (url.pathname.startsWith("/api/")) {
    const payloads = {
      "/api/files": { ok: true, files: [] },
      "/api/providers": { ok: true, providers: [{ id: "auto", label: "Auto", protocol: "router", context_tokens: 32000 }] },
      "/api/monitoring": { ok: true, summary: {}, providers: [], tools: [], errors: [], routes: [] },
    };
    response.writeHead(payloads[url.pathname] ? 200 : 400, { "Content-Type": "application/json" });
    response.end(JSON.stringify(payloads[url.pathname] || { ok: false, error: "Unexpected API request in UI test" }));
    return;
  }
  const files = { "/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"], "/styles.css": ["styles.css", "text/css"] };
  const file = files[url.pathname];
  if (!file) { response.writeHead(404); response.end(); return; }
  response.writeHead(200, { "Content-Type": file[1], "Cache-Control": "no-store" });
  response.end(fs.readFileSync(path.join(root, "web", file[0])));
});

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function geometry(page) {
  return page.evaluate(() => {
    const box = selector => {
      const rect = document.querySelector(selector).getBoundingClientRect();
      return { height: rect.height, top: rect.top, bottom: rect.bottom };
    };
    return { terminal: box("#terminal-panel"), editor: box("#code-editor"), chat: box("#chat-view"), frame: box(".editor") };
  });
}

async function main() {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe",
      headless: true,
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const url = `http://127.0.0.1:${server.address().port}/`;
    await page.goto(url, { waitUntil: "networkidle" });
    await settle(page);
    const initial = await geometry(page);
    assert(initial.terminal.height > 200, "Default height must scale with the editor, not stay at 150px");
    await page.locator("#terminal").evaluate(el => { el.textContent = "AssertionError: 16 != 4\n".repeat(90); });
    const separator = page.locator("#terminal-resize");
    const box = await separator.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2 - 160, { steps: 10 });
    await page.mouse.up();
    await settle(page);
    const expanded = await geometry(page);
    assert(expanded.terminal.height > initial.terminal.height + 150, "Drag should enlarge terminal");
    assert(Math.abs(expanded.chat.height - initial.chat.height) < 1, "Resizing must not displace chat");
    assert(expanded.editor.height >= 120 && expanded.editor.bottom <= expanded.terminal.top, "Editor must remain visible");
    assert(await page.locator("#terminal").evaluate(el => el.scrollHeight > el.clientHeight), "Long output should scroll internally");
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, "terminal-resize-desktop.png") });

    await page.reload({ waitUntil: "networkidle" });
    await settle(page);
    assert(Math.abs((await geometry(page)).terminal.height - expanded.terminal.height) < 2, "Ratio should survive reload");
    await separator.focus();
    await page.keyboard.press("ArrowDown");
    await settle(page);
    assert((await geometry(page)).terminal.height < expanded.terminal.height, "ArrowDown should shrink terminal");
    await page.keyboard.press("End");
    await page.setViewportSize({ width: 1366, height: 360 });
    await settle(page);
    const small = await geometry(page);
    assert(small.editor.height >= 119 && small.terminal.bottom <= small.frame.bottom + 1, "Small windows must retain editor space");
    await page.keyboard.press("Home");
    await settle(page);
    assert(Math.abs((await geometry(page)).terminal.height - 80) < 2, "Home should select minimum height");
    await separator.dblclick();
    await page.setViewportSize({ width: 1440, height: 900 });
    await settle(page);
    assert(Math.abs((await geometry(page)).terminal.height - initial.terminal.height) < 2, "Double-click should restore default ratio");
    await page.locator("#review-tab").click();
    assert(await page.locator("#chat-view").isHidden(), "Review must still hide chat");

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    mobile.on("pageerror", error => errors.push(error.message));
    await mobile.goto(url, { waitUntil: "networkidle" });
    await mobile.locator("#terminal-resize").scrollIntoViewIfNeeded();
    await settle(mobile);
    const mobileBefore = await geometry(mobile);
    const touchBox = await mobile.locator("#terminal-resize").boundingBox();
    const cdp = await mobile.context().newCDPSession(mobile);
    const x = touchBox.x + touchBox.width / 2;
    const y = touchBox.y + touchBox.height / 2;
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y - 80 }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await settle(mobile);
    assert((await geometry(mobile)).terminal.height > mobileBefore.terminal.height + 70, "Touch drag should resize terminal");
    assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile layout must not overflow horizontally");
    await mobile.screenshot({ path: path.join(artifacts, "terminal-resize-mobile.png"), fullPage: true });
    assert.deepEqual(errors, [], "No browser errors");
    console.log("PASS: desktop drag, persistence, keyboard, resize bounds, reset, chat isolation, mobile touch and screenshots");
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
