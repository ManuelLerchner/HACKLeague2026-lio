// Capture the real app and pitch with Chrome's local debugging protocol.
import {spawn} from 'node:child_process';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';

const root = resolve('.');
const output = resolve('artifacts');
await mkdir(output, {recursive: true});
const chrome = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const child = spawn(chrome, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=9223', `--user-data-dir=${root}/.data/capture-browser`, 'about:blank'],
  {stdio: ['ignore', 'ignore', 'pipe']});
let startupError = '';
child.stderr.on('data', data => {startupError += data.toString();});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let targets;
for (let i = 0; i < 40; i++) {
  try { targets = await (await fetch('http://127.0.0.1:9223/json')).json(); break; }
  catch { await sleep(250); }
}
if (!targets) {child.kill(); throw new Error(`Chrome did not start: ${startupError.slice(0,1000)}`);}
const socket = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
await new Promise(resolve => socket.addEventListener('open', resolve, {once: true}));
let sequence = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const value = JSON.parse(event.data);
  if (value.id && pending.has(value.id)) {
    const {resolve, reject} = pending.get(value.id); pending.delete(value.id);
    value.error ? reject(new Error(value.error.message)) : resolve(value.result);
  }
});
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence; pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function navigate(url) {
  await send('Page.navigate', {url});
  for (let i = 0; i < 40; i++) {
    await sleep(100);
    const result = await send('Runtime.evaluate', {expression: 'document.readyState', returnByValue: true});
    if (result.result.value === 'complete') break;
  }
  await sleep(300);
}
async function screenshot(name) {
  const {data} = await send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  await writeFile(`${output}/${name}.png`, Buffer.from(data, 'base64'));
}
try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', {width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false});
  const mode = process.argv[2] || 'before';
  if (mode === 'video') {
    for (let i = 0; i < 8; i++) {
      await navigate(`file://${root}/docs/video.html?frame=${i}`);
      await screenshot(`video-${i+1}`);
    }
  } else if (mode === 'slides') {
    await send('Emulation.setDeviceMetricsOverride', {width: 1600, height: 900, deviceScaleFactor: 1, mobile: false});
    for (let i = 0; i < 5; i++) {
      await navigate(`file://${root}/docs/pitch.html?slide=${i}`);
      await screenshot(`slide-${i+1}`);
    }
    const {data} = await send('Page.printToPDF', {printBackground: true, paperWidth: 16.6667,
      paperHeight: 9.375, marginTop: 0, marginBottom: 0, marginLeft: 0, marginRight: 0,
      preferCSSPageSize: true});
    await writeFile(`${output}/pitch.pdf`, Buffer.from(data, 'base64'));
  } else {
    const env = await readFile('.env', 'utf8');
    const token = env.match(/^ADMIN_TOKEN=(.+)$/m)?.[1];
    if (!token) throw new Error('Private .env has no admin token');
    const base = 'http://127.0.0.1:8000';
    await send('Network.enable');
    await send('Network.setExtraHTTPHeaders', {headers: {Authorization: `Bearer ${token}`}});
    const cases = await (await fetch(`${base}/api/v1/cases`, {headers: {Authorization: `Bearer ${token}`}})).json();
    const selected = cases.find(c => c.issue_type === 'QUANTITY_MISMATCH');
    if (!selected) throw new Error('No quantity case available');
    await navigate(base); await screenshot(`inbox-${mode}`);
    await navigate(`${base}/cases/${selected.id}`); await screenshot(`case-${mode}`);
    await send('Runtime.evaluate', {expression: 'window.scrollTo(0,650)'});
    await sleep(200); await screenshot(`conversation-${mode}`);
    await navigate(`${base}/system`); await screenshot(`system-${mode}`);
    await send('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    await navigate(base); await screenshot(`mobile-${mode}`);
  }
} finally {
  socket.close(); child.kill();
}
console.log(`Captured ${process.argv[2] || 'before'} into artifacts/`);
