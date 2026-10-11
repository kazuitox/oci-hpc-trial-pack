/* Run the production refresh handler with a small DOM/network test double. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');

function input(id, checked = false) {
  return {id, checked, listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; }};
}
function region(ids) {
  const inputs = ids.map(id => input(id));
  const rows = inputs.map(checkbox => ({querySelector() {return checkbox;}}));
  const all = input('_historySelectAll');
  return {
    inputs, rows, all, attrs: {}, modals: [], open: false,
    get childNodes() {return [this];},
    querySelector(selector) {
      if (selector === '.modal.show') return this.open ? {} : null;
      if (selector === '#_historyTbody') return {rows: this.rows};
      if (selector === '#_historySelectAll') return this.rows.length ? this.all : null;
    },
    querySelectorAll(selector) {
      if (selector === '.modal') return this.modals;
      return selector.endsWith(':checked') ? this.inputs.filter(i => i.checked) : this.inputs;
    },
    replaceChildren(other) {
      this.inputs = other.inputs; this.rows = other.rows; this.all = other.all;
    },
    setAttribute(k, v) {this.attrs[k] = v;},
    removeAttribute(k) {delete this.attrs[k];}
  };
}

async function main() {
  const button = {disabled: false, addEventListener(event, fn) {this.click = fn;}};
  const message = {};
  const results = region(['ocJobId-42', 'ocJobId-43']);
  results.inputs[0].checked = true;
  const initialRows = results.rows;
  let next = region(['ocJobId-42', 'ocJobId-44']);
  let mode = 'ok', calls = 0, options, lastBatch, hold;
  const href = 'https://ood.example/pun/sys/OpenComposer/history?filter=mpi&sort=Start&order=asc&p=2&rows=10';
  const window = {location: {href}};
  const context = {
    window, Set, Array, AbortController, setTimeout, clearTimeout,
    document: {getElementById(id) {return ({_historyRefresh: button,
      _historyRefreshStatus: message, _historyResults: results})[id];}},
    ocHistory: {updateBatch(rows) {lastBatch = rows;}},
    fetch: async (url, opts) => {
      calls++; assert.equal(url, href); options = opts;
      if (mode === 'network') throw new Error('offline');
      if (mode === 'hold') await new Promise(resolve => {hold = resolve;});
      return {ok: mode !== 'http', redirected: mode === 'redirect', text: async () => 'html'};
    },
    DOMParser: class {
      parseFromString() {return {
        getElementById() {return mode === 'login' ? null : next;},
        querySelector() {return mode === 'scheduler' ? {} : null;}
      };}
    }
  };
  vm.runInNewContext(source, context);
  await button.click();
  assert.notEqual(results.rows, initialRows);
  assert.equal(results.inputs[0].checked, true);
  assert.equal(results.inputs[1].checked, false);
  assert.equal(message.textContent, 'Updated.');
  assert.equal(window.location.href, href);
  assert.equal(options.cache, 'no-store');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(button.disabled, false);
  assert.equal(results.attrs['aria-busy'], undefined);
  results.all.checked = true;
  results.all.listeners.change();
  assert.ok(results.inputs.every(i => i.checked));
  assert.deepEqual(lastBatch, results.rows);
  results.inputs[1].checked = false;
  results.inputs[1].listeners.change();
  assert.deepEqual(lastBatch, results.rows);

  next = region([]);
  await button.click();
  assert.equal(results.rows.length, 0);
  assert.equal(lastBatch.length, 0);
  next = region(['ocJobId-45']);
  await button.click();
  assert.equal(results.rows.length, 1);
  const retained = results.rows;
  for (const failure of ['network', 'http', 'redirect', 'login', 'scheduler']) {
    mode = failure;
    await button.click();
    assert.equal(results.rows, retained);
    assert.equal(message.textContent, 'Refresh failed. Please try again.');
    assert.equal(button.disabled, false);
    assert.equal(results.attrs['aria-busy'], undefined);
  }
  mode = 'ok'; results.open = true;
  const before = calls;
  await button.click(); assert.equal(calls, before);
  results.open = false; mode = 'hold';
  const pending = button.click();
  assert.equal(button.disabled, true);
  await button.click(); assert.equal(calls, before + 1);
  results.open = true; hold(); await pending;
  assert.equal(results.rows, retained); // Modal opened during request: keep its DOM.
  assert.equal(button.disabled, false);
  console.log('History refresh behavioral checks passed.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
