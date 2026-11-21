// Run: node scripts/test_preview.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const code = fs.readFileSync(path.join(__dirname, '../src/supplier_chaser/web/static/preview.js'), 'utf8');

function setup() {
  const listeners = {};
  const element = () => ({listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }});
  const frame = element();
  frame.removeAttribute = name => { delete frame[name]; };
  const title = element(), download = element(), open = element(), status = element(), close = element();
  const children = {iframe: frame, '#evidence-preview-title': title,
    '[data-preview-download]': download, '[data-preview-open]': open,
    '[data-preview-status]': status, '[data-preview-close]': close};
  const dialog = element();
  dialog.querySelector = selector => children[selector];
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.listeners.close(); };
  const document = {querySelector: () => dialog, addEventListener: (name, callback) => { listeners[name] = callback; }};
  vm.runInNewContext(code, {document, location: {origin: 'http://localhost:8001'}, URL});
  let focused = false;
  const link = {dataset: {
    previewUrl: '/sources/00000000-0000-0000-0000-000000000000/preview',
    downloadUrl: '/sources/00000000-0000-0000-0000-000000000000/download',
    previewTitle: '<script>supplier content</script>',
  }, isConnected: true, focus: () => { focused = true; }};
  const click = options => {
    let prevented = false;
    listeners.click({target: {closest: () => link}, button: 0,
      preventDefault: () => { prevented = true; }, ...options});
    return prevented;
  };
  return {frame, title, dialog, link, click, close, download, focused: () => focused};
}

let ui = setup();
assert.equal(ui.frame.src, undefined);
assert.equal(ui.click(), true);
assert.equal(ui.dialog.open, true);
assert.equal(ui.title.textContent, '<script>supplier content</script>');
assert.equal(ui.frame.src, 'http://localhost:8001' + ui.link.dataset.previewUrl);
assert.equal(ui.download.href, 'http://localhost:8001' + ui.link.dataset.downloadUrl);
ui.close.listeners.click();
assert.equal(ui.dialog.open, false);
assert.equal(ui.frame.src, undefined);
assert.equal(ui.focused(), true);

ui = setup();
ui.link.dataset.previewUrl = 'https://untrusted.example/sources/00000000-0000-0000-0000-000000000000/preview';
assert.equal(ui.click(), false);
assert.equal(ui.frame.src, undefined);

ui = setup();
ui.link.dataset.downloadUrl = '/api/v1/cases/00000000-0000-0000-0000-000000000000/escalate';
assert.equal(ui.click(), false);
assert.equal(ui.frame.src, undefined);

ui = setup();
assert.equal(ui.click({ctrlKey: true}), false);
assert.equal(ui.frame.src, undefined);
console.log('4 preview scenarios passed: open/close/focus, foreign URL rejection, route restriction, modified-click fallback');
