const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const statuses = ['Queued', 'Running', 'Completed', 'Failed', 'Canceled'];
const inputs = Object.fromEntries(statuses.map(s => [`_historyStatus${s}`, {
  value: s.toLowerCase(), checked: s === 'Canceled', addEventListener() {}
}]));
const context = {
  URLSearchParams,
  window: {location: {pathname: '/history', search: '?p=2&sort=Start&order=asc'}},
  document: {
    getElementById(id) { return id === '_filterInput' ? {value: 'job'} : inputs[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
context.ocHistory.applyFilter();
let params = new URLSearchParams(context.window.location.href.split('?')[1]);
assert.equal(params.get('statuses'), 'canceled');
assert.equal(params.get('filter'), 'job');
assert.equal(params.get('sort'), 'Start');
assert.equal(params.get('p'), null);
Object.values(inputs).forEach(input => { input.checked = false; });
context.ocHistory.applyFilter();
params = new URLSearchParams(context.window.location.href.split('?')[1]);
assert.equal(params.get('statuses'), 'nothing');

const actions = {};
context.ocHistory.updateStatusBatch = (action, ids) => { actions[action] = Array.from(ids); };
const rows = statuses.map((state, i) => ({
  querySelector() { return {checked: true}; },
  getElementsByTagName() { return [{}, {textContent: String(i)}, {textContent: state}]; }
}));
context.ocHistory.updateBatch(rows);
assert.deepEqual(actions.CancelJob, ['0', '1']);
assert.deepEqual(actions.DeleteInfo, ['0', '1', '2', '3', '4']);
