// Developer-only screenshots: Node 22+ native WebSocket, fresh Chromium profile.
// Explicit CDP viewport emulation avoids Windows' minimum headless window width.
import { spawn } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const [browser, profile, url, output, theme] = process.argv.slice(2);
if (!browser || !profile || !url || !output || !['docs', 'status', 'api'].includes(theme)) {
  throw new Error('Usage: node capture_preview.mjs BROWSER PROFILE LOCAL_URL OUTPUT THEME');
}
let endpoint;
try {
  endpoint = new URL(url);
} catch {
  throw new Error('Preview URL is invalid');
}
if (endpoint.hostname !== '127.0.0.1') throw new Error('Preview URL must use loopback');
await mkdir(profile, { recursive: true });
await mkdir(output, { recursive: true });
const child = spawn(browser, [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', '--no-first-run',
  '--no-default-browser-check', '--disable-background-networking',
  '--disable-component-update', '--disable-sync', '--remote-debugging-port=0',
  '--user-data-dir=' + profile, 'about:blank',
], { stdio: 'ignore' });
let launchError;
child.on('error', error => { launchError = error; });
let ws;
let nextId = 0;
const pending = new Map();

function call(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('CDP timeout: ' + method));
    }, 15000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

try {
  let port;
  for (let i = 0; i < 150; i++) {
    if (launchError) throw launchError;
    try {
      port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]);
      if (port) break;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await delay(100);
  }
  if (!port) throw new Error('Headless browser did not start its loopback debugger');
  const response = await fetch(`http://127.0.0.1:${port}/json/list`);
  const pages = await response.json();
  const page = pages.find(item => item.type === 'page');
  if (!page) throw new Error('No browser page target');
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('WebSocket connection timed out')), 10000);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('WebSocket connection failed')); }, { once: true });
  });
  ws.addEventListener('message', event => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      for (const task of pending.values()) {
        clearTimeout(task.timer);
        task.reject(new Error('Invalid CDP response'));
      }
      pending.clear();
      ws.close();
      return;
    }
    const task = pending.get(message.id);
    if (!task) return;
    pending.delete(message.id);
    clearTimeout(task.timer);
    if (message.error) task.reject(new Error(message.error.message));
    else task.resolve(message.result);
  });
  ws.addEventListener('close', () => {
    for (const task of pending.values()) {
      clearTimeout(task.timer);
      task.reject(new Error('Browser connection closed'));
    }
    pending.clear();
  });
  await call('Page.enable');
  for (const [suffix, width, height] of [['', 1440, 1100], ['-mobile', 390, 844]]) {
    await call('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    await call('Page.navigate', { url });
    let loaded = false;
    for (let i = 0; i < 100; i++) {
      const result = await call('Runtime.evaluate', {
        expression: `document.readyState === 'complete' && document.documentElement.dataset.theme === ${JSON.stringify(theme)}`,
        returnByValue: true,
      });
      if (result.result.value) { loaded = true; break; }
      await delay(50);
    }
    if (!loaded) throw new Error('Template did not load: ' + theme);
    const metrics = await call('Runtime.evaluate', {
      expression: 'document.fonts.ready.then(() => ({ width: innerWidth, scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth), theme: document.documentElement.dataset.theme }))',
      awaitPromise: true, returnByValue: true,
    });
    const viewport = metrics.result.value;
    if (viewport.width !== width || viewport.scrollWidth > width) {
      throw new Error('Viewport mismatch or horizontal overflow: ' + JSON.stringify(viewport));
    }
    const image = await call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false, fromSurface: true });
    const file = join(output, theme + suffix + '.png');
    await writeFile(file, Buffer.from(image.data, 'base64'));
    console.log(JSON.stringify({ file, ...viewport, height }));
  }
} finally {
  if (ws?.readyState === WebSocket.OPEN) {
    try { await call('Browser.close'); } catch { /* Browser may close before replying. */ }
    ws.close();
  }
  if (child.exitCode === null) {
    const exited = await Promise.race([
      new Promise(resolve => child.once('exit', () => resolve(true))),
      delay(3000).then(() => false),
    ]);
    if (!exited) child.kill();
  }
}
