// Run with Node.js; the DOM stub exercises catalog behavior without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const mac = fs.existsSync(path.join(root, 'mac')) ? path.join(root, 'mac') : root;
const remote = fs.existsSync(path.join(root, 'remote_web')) ? path.join(root, 'remote_web') : path.join(root, '..', 'remote_web');

class Option {
  constructor(text, value, _default = false, selected = false) {
    this.textContent = text; this.value = value; this.selected = selected;
  }
}
class Element {
  constructor(tag = 'DIV') {
    this.tagName = tag.toUpperCase(); this.children = []; this._value = '';
    this.disabled = false; this.style = {}; this.listeners = {};
    this.classList = {add() {}, remove() {}, toggle() {}};
    this.lastChild = {textContent: ''};
  }
  get value() { return this._value; }
  set value(value) { this._value = value; }
  get options() { return this.children; }
  get selectedOptions() { return this.children.filter(node => node.value === this.value); }
  replaceChildren(...nodes) {
    this.children = nodes;
    if (this.tagName === 'SELECT') this.value = (nodes.find(node => node.selected) || nodes[0] || {}).value || '';
  }
  prepend(node) { this.children.unshift(node); if (node.selected) this.value = node.value; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  setAttribute() {}
}
function page(file) {
  const html = fs.readFileSync(file, 'utf8');
  const nodes = new Map();
  for (const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"([^>]*)>/g)) {
    const element = new Element(match[1]);
    element.disabled = /\bdisabled\b/.test(match[3]);
    element.value = match[3].match(/\bvalue="([^"]*)"/)?.[1] || '';
    nodes.set('#' + match[2], element);
  }
  const saved = new Map();
  const storage = {getItem(key) { return saved.get(key) ?? null; }, setItem(key, value) { saved.set(key, String(value)); }, removeItem(key) { saved.delete(key); }};
  const context = vm.createContext({
    Option, sessionStorage: storage, localStorage: storage,
    window: {location: {protocol: 'http:', hostname: 'localhost'}},
    document: {
      querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, new Element()); return nodes.get(selector); },
      querySelectorAll() { return []; }, createElement(tag) { return new Element(tag); },
    },
    fetch: async () => ({ok: true, json: async () => ({})}),
    setTimeout() {}, setInterval() { return 1; }, clearInterval() {}, confirm() { return true; },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context, {filename: file});
  return {context, nodes};
}
async function check(file, isRemote) {
  const {context, nodes} = page(file);
  const select = nodes.get(isRemote ? '#preset' : '#model-preset');
  const button = nodes.get(isRemote ? '#switch' : '#switch-model');
  const refresh = isRemote ? 'refreshModels()' : 'refreshModel()';
  nodes.get(isRemote ? '#key' : '#api-key').value = 'test-key';
  const catalog = {current_model: 'vendor/New-9B', presets: {old: 'vendor/Old-4B', future: 'vendor/New-9B'}};
  let response = catalog;
  context.fetch = async () => ({ok: true, json: async () => response});
  await vm.runInContext(refresh, context);
  assert.equal(select.value, 'future');
  assert.equal(select.options.length, 2);
  assert.equal(button.disabled, false);

  response = {current_model: 'vendor/Custom-8B', presets: catalog.presets};
  await vm.runInContext(refresh, context);
  assert.equal(select.value, '');
  assert.equal(button.disabled, true, 'an unconfigured current model must not be guessed from its size');

  response = {presets: {}};
  await assert.rejects(vm.runInContext(refresh, context), /没有提供可用/);
  assert.equal(select.disabled, true);
  assert.equal(button.disabled, true);

  let resolve;
  context.fetch = () => new Promise(done => { resolve = done; });
  const pending = vm.runInContext(refresh, context);
  vm.runInContext("resetModelOptions('changed')", context);
  resolve({ok: true, json: async () => catalog});
  await pending;
  assert.equal(select.disabled, true, 'an outdated response must not restore the old catalog');

  if (isRemote) {
    context.fetch = async () => ({ok: true, json: async () => catalog});
    await vm.runInContext(refresh, context);
    vm.runInContext('busy(true)', context);
    assert.equal(select.disabled, true);
    assert.equal(button.disabled, true);
    vm.runInContext('busy(false)', context);
    assert.equal(button.disabled, false);
    const live = {
      id: 'a'.repeat(32), state: 'running', batch_size: 3, processed: 2, planned_total: 10,
      successful: 2, failed: 0, elapsed_seconds: 10, estimated_remaining_seconds: 40,
      current_source: 'next.png', records: [],
    };
    context.fetch = async url => ({ok: true, json: async () => url === '/api/runs/active' ? {run: live} : live});
    await vm.runInContext('restoreRun()', context);
    assert.equal(vm.runInContext('runId', context), live.id);
    assert.equal(vm.runInContext('sessionStorage.getItem("tagger-run-id")', context), live.id);
    assert.equal(nodes.get('#done').textContent, 2);
    assert.match(nodes.get('#state').textContent, /每批最多 3 张/);
    assert.equal(nodes.get('#start').disabled, true);
    assert.equal(nodes.get('#pause').disabled, false);
    context.fetch = async () => ({ok: false, status: 404, json: async () => ({detail: '任务已结束'})});
    await vm.runInContext('update()', context);
    assert.equal(vm.runInContext('runId', context), null);
    assert.equal(nodes.get('#start').disabled, false);
    assert.equal(nodes.get('#pause').disabled, true);
  }
  console.log(path.basename(path.dirname(path.dirname(file))) + ': dynamic selection, unknown model, empty catalog, stale response OK');
}
module.exports = (async () => {
  await check(path.join(mac, 'web/index.html'), false);
  await check(path.join(remote, 'web/index.html'), true);
})();
if (require.main === module) module.exports.catch(error => { console.error(error); process.exitCode = 1; });
