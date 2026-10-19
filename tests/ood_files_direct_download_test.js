'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const fs = require('node:fs');
const script = path.join(__dirname, '../playbooks/roles/openondemand/files/files-direct-download.js');
const api = require(script);
const origin = 'https://ood.example.test';
const base = '/pun/sys/dashboard/files/fs/home/user/';
const file = (name, size = 3) => ({ name, type: 'f', size, mode: 0x81a4, download_url: base + encodeURIComponent(name) + '?download=1' });
const dir = name => ({ name, type: 'd', mode: 0x41ed, url: base + encodeURIComponent(name), download_url: base + encodeURIComponent(name) + '?download=1' });
function env(fetch) { return { origin, fetch, signal: new AbortController().signal, progress() {} }; }
function json(data) { return { ok: true, status: 200, json: async () => data }; }
function memoryDirectory() {
  const directories = new Map();
  const files = new Map();
  return {
    directories, files,
    async getDirectoryHandle(name, options = {}) {
      if (files.has(name)) throw Object.assign(new Error(), { name: 'TypeMismatchError' });
      if (!directories.has(name)) {
        if (!options.create) throw Object.assign(new Error(), { name: 'NotFoundError' });
        directories.set(name, memoryDirectory());
      }
      return directories.get(name);
    },
    async getFileHandle(name, options = {}) {
      if (!files.has(name)) {
        if (!options.create) throw Object.assign(new Error(), { name: 'NotFoundError' });
        const record = { content: [], aborted: false, closed: false };
        record.createWritable = async () => ({
          write: async value => { record.content = record.content.concat(Array.from(value)); },
          close: async () => { record.closed = true; },
          abort: async () => { record.aborted = true; record.content = []; }
        });
        files.set(name, record);
      }
      return files.get(name);
    }
  };
}
function response(data, headers = { 'Content-Disposition': 'attachment; filename="file"' }, status = 200) {
  return new Response(new Uint8Array(data), { status, headers });
}

test('multiple files and nested/empty/dot directories retain names and never request ZIP', async () => {
  const calls = [];
  const setting = env(async (value, options) => {
    const url = new URL(value); calls.push(url);
    assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
    if (url.searchParams.has('can_download')) return json({ can_download: true });
    if (url.pathname.endsWith('/results')) return json({ filesystem: 'fs', files: [file('日本語 #%.dat'), dir('.empty')] });
    return json({ filesystem: 'fs', files: [] });
  });
  const entries = await api.plan([file('a.dat'), dir('results'), file('b.dat')], setting);
  assert.deepEqual(entries.map(e => e.parts), [['a.dat'], ['results'], ['results', '日本語 #%.dat'], ['results', '.empty'], ['b.dat']]);
  assert.ok(calls.every(u => !u.searchParams.has('download')));
  const target = memoryDirectory();
  setting.fetch = async () => response([0, 128, 255]);
  assert.equal(await api.save(entries, target, setting), 3);
  assert.deepEqual(target.files.get('a.dat').content, [0, 128, 255]);
  assert.ok(target.directories.get('results').directories.has('.empty'));
  assert.ok(target.directories.get('results').files.get('日本語 #%.dat').closed);
});

test('directory size limit, permission errors and invalid listings stop preflight', async () => {
  await assert.rejects(api.plan([dir('large')], env(async () => json({ can_download: false, error_message: 'too large' }))), /too large/);
  await assert.rejects(api.plan([dir('denied')], env(async () => json({ error_message: 'denied' }))), /denied/);
  await assert.rejects(api.plan([file('secret')], env(() => {})).then(() => api.plan([{ ...file('x'), download_url: undefined }], env(() => {}))), /読み取り/);
  await assert.rejects(api.plan([dir('bad')], env(async url => json(url.includes('can_download') ? { can_download: true } : { filesystem: 'fs' }))), /一覧/);
});

test('unsafe names, links, remote filesystem and name collisions fail before download', async () => {
  for (const name of ['..', '.', '/etc/passwd', 'a\\b', 'a\0b', '']) assert.throws(() => api.safeName(name));
  assert.throws(() => api.localURL('https://elsewhere.test/files/fs/home/x', origin));
  assert.throws(() => api.localURL('/pun/sys/dashboard/files/s3/x', origin));
  await assert.rejects(api.plan([{ ...dir('loop'), mode: 0xa1ff }], env(() => assert.fail())), /シンボリック/);
  await assert.rejects(api.plan([file('A'), file('a')], env(() => assert.fail())), /重複/);
  await assert.rejects(api.plan([], env(() => assert.fail())), /選択/);
});

test('entry and depth limits stop excessive traversal', async () => {
  await assert.rejects(api.plan(Array.from({ length: 10001 }, (_, i) => file(`${i}`)), env(() => assert.fail())), /10,000/);
  const setting = env(async url => json(url.includes('can_download') ? { can_download: true } : { filesystem: 'fs', files: [dir('next')] }));
  await assert.rejects(api.plan([dir('loop')], setting), /64階層/);
});

test('new destination and local file collisions never overwrite existing data', async () => {
  const target = memoryDirectory();
  await target.getDirectoryHandle('stamp', { create: true });
  assert.equal((await api.newDestination(target, 'stamp')).name, 'stamp-1');
  const old = await target.getFileHandle('a', { create: true }); old.content = [9];
  const entries = [{ parts: ['a'], url: base + 'a?download=1', size: 1 }];
  await assert.rejects(api.save(entries, target, env(async () => response([1]))), /同名/);
  assert.deepEqual(old.content, [9]);
});

test('HTTP, login HTML and redirect responses are not saved as files', async () => {
  const entries = [{ parts: ['x'], url: base + 'x?download=1', size: 1 }];
  for (const result of [response([1], {}, 403), response([1], { 'Content-Type': 'text/html' }), { ok: true, redirected: true, status: 200, body: null }]) {
    const target = memoryDirectory();
    await assert.rejects(api.save(entries, target, env(async () => result)), /取得に失敗/);
    assert.equal(target.files.size, 0);
  }
});

test('truncated, growing and interrupted streams abort writes', async () => {
  for (const content of [[1], [1, 2, 3]]) {
    const target = memoryDirectory();
    await assert.rejects(api.save([{ parts: ['x'], url: base + 'x?download=1', size: 2 }], target, env(async () => response(content))), /サイズ/);
    assert.ok(target.files.get('x').aborted); assert.equal(target.files.get('x').closed, false);
  }
  const target = memoryDirectory();
  const controller = new AbortController();
  const setting = env(async () => response([1, 2])); setting.signal = controller.signal;
  let clock = 0, updates = 0;
  setting.now = () => (clock += 500);
  setting.progress = () => { if (++updates > 1) controller.abort(); };
  await assert.rejects(api.save([{ parts: ['x'], url: base + 'x?download=1', size: 2 }], target, setting), { name: 'AbortError' });
  assert.ok(target.files.get('x').aborted);
});

test('empty file streams commit successfully', async () => {
  const target = memoryDirectory();
  assert.equal(await api.save([{ parts: ['zero'], url: base + 'zero?download=1', size: 0 }], target, env(async () => response([]))), 1);
  assert.ok(target.files.get('zero').closed);
});

test('network interruption aborts current file and retains completed files', async () => {
  const target = memoryDirectory();
  let call = 0;
  const setting = env(async () => {
    if (call++ === 0) return response([1]);
    let chunks = 0;
    return new Response(new ReadableStream({
      pull(controller) {
        if (chunks++ === 0) controller.enqueue(new Uint8Array([2]));
        else controller.error(new Error('connection lost'));
      }
    }), { headers: { 'Content-Disposition': 'attachment' } });
  });
  await assert.rejects(api.save([
    { parts: ['done'], url: base + 'done?download=1', size: 1 },
    { parts: ['failed'], url: base + 'failed?download=1', size: 2 }
  ], target, setting), /connection lost/);
  assert.deepEqual(target.files.get('done').content, [1]);
  assert.ok(target.files.get('done').closed);
  assert.ok(target.files.get('failed').aborted);
});

test('UI shows selection initialization errors instead of rejecting the click promise', async () => {
  const button = { dataset: {}, disabled: true }, cancel = {}, status = {};
  let pickerCalls = 0;
  const win = {
    document: { readyState: 'complete', getElementById: id => ({ 'ood-direct-download-btn': button, 'ood-direct-download-cancel': cancel, 'ood-direct-download-status': status })[id], addEventListener() {} },
    isSecureContext: true, location: { origin }, fetch: async () => assert.fail(),
    showDirectoryPicker: () => { pickerCalls++; }
  };
  vm.runInNewContext(fs.readFileSync(script, 'utf8'), { window: win, URL, AbortController, performance });
  assert.equal(button.disabled, false);
  await button.onclick();
  assert.match(status.textContent, /一覧が準備できていません/);
  const broken = () => { throw new Error('selection unavailable'); };
  broken.fn = { dataTable: { isDataTable: () => true } }; win.jQuery = broken;
  await button.onclick();
  assert.match(status.textContent, /selection unavailable/);
  assert.equal(pickerCalls, 0);
});

test('progress counts only files, omits long names and calculates transfer-wide mean rate', async () => {
  const target = memoryDirectory();
  const messages = [];
  let time = 0;
  const setting = env(async () => { time += 1000; return response(new Uint8Array(1048576)); });
  setting.now = () => time;
  setting.progress = message => messages.push(message);
  const longName = 'very-long-file-name-'.repeat(20) + '.dat';
  const count = await api.save([
    { parts: ['folder'], directory: true },
    { parts: ['folder', 'empty'], directory: true },
    { parts: ['folder', longName], url: base + 'first?download=1', size: 1048576 },
    { parts: ['second'], url: base + 'second?download=1', size: 1048576 }
  ], target, setting);
  assert.equal(count, 2);
  assert.ok(messages.some(message => message.startsWith('1/2ファイルをダウンロード中')));
  assert.ok(messages.some(message => message.startsWith('2/2ファイルをダウンロード中')));
  assert.match(messages.at(-1), /^2\/2ファイル保存済み · 1\.0 MiB\/s（平均）$/);
  assert.ok(messages.every(message => !message.includes(longName) && !message.includes('second')));
  assert.ok(messages.every(message => !/NaN|Infinity/.test(message)));
});

test('zero byte and directory-only downloads have accurate file counts', async () => {
  const messages = [];
  const setting = env(async () => response([]));
  setting.now = () => 0;
  setting.progress = message => messages.push(message);
  assert.equal(await api.save([{ parts: ['zero'], url: base + 'zero?download=1', size: 0 }], memoryDirectory(), setting), 1);
  assert.equal(messages.at(-1), '1/1ファイル保存済み · 0.0 MiB/s（平均）');
  messages.length = 0;
  assert.equal(await api.save([{ parts: ['empty'], directory: true }], memoryDirectory(), setting), 0);
  assert.deepEqual(messages, []);
});

async function clickWithName(selection, name, target = memoryDirectory(), action = 'submit') {
  const button = { dataset: {} }, cancel = {}, status = {};
  let pickerCalls = 0, dialogCalls = 0, inspectionRequests = 0;
  const requestSignals = [];
  const dialog = { open: false, showModal() { this.open = true; dialogCalls++; }, close() { this.open = false; } };
  const form = {}, nameCancel = { focus() {} }, nameError = { textContent: '' }, summary = {}, nameSubmit = {};
  const nameFields = {}, spinner = {}, close = { focus() {} };
  const progressMessages = [];
  const progress = { get textContent() { return progressMessages.at(-1) || ''; }, set textContent(value) { progressMessages.push(value); } };
  const phases = [];
  const listeners = new Map();
  const nameInput = { value: name, focus() {}, select() {} };
  const jquery = () => ({ DataTable: () => ({ rows: () => ({ data: () => ({ toArray: () => selection }) }) }) });
  jquery.fn = { dataTable: { isDataTable: () => true } };
  const win = {
    document: { readyState: action === 'loading' ? 'loading' : 'complete', getElementById: id => ({
      'ood-direct-download-btn': button, 'ood-direct-download-cancel': cancel,
      'ood-direct-download-status': status, 'ood-direct-download-name': nameInput,
      'ood-direct-download-dialog': dialog, 'ood-direct-download-name-form': form,
      'ood-direct-download-name-cancel': nameCancel, 'ood-direct-download-name-error': nameError,
      'ood-direct-download-summary': summary, 'ood-direct-download-name-submit': nameSubmit,
      'ood-direct-download-name-fields': nameFields, 'ood-direct-download-spinner': spinner,
      'ood-direct-download-close': close, 'ood-direct-download-progress': progress
    })[id], addEventListener: (event, handler) => listeners.set(event, handler) },
    isSecureContext: true, location: { origin }, performance, jQuery: jquery,
    showDirectoryPicker: async () => {
      pickerCalls++;
      phases.push({ phase: 'choosing', spinner: !spinner.hidden, nameHidden: nameFields.hidden, cancel: !nameCancel.hidden, dialog: dialog.open });
      if (action === 'picker-cancel' || action === 'close-error') throw Object.assign(new Error(), { name: 'AbortError' });
      return target;
    },
    fetch: async (url, options) => {
      if (options && options.signal) requestSignals.push(options.signal);
      if (url.includes('can_download')) {
        inspectionRequests++;
        return json(action === 'fail-size' ? { can_download: false, error_message: 'size denied' } : { can_download: true });
      }
      if (!url.includes('download=')) return json({ filesystem: 'fs', files: [file('inside', 1), dir('empty')] });
      phases.push({ phase: 'transferring', spinner: !spinner.hidden, nameHidden: nameFields.hidden, cancel: !nameCancel.hidden, dialog: dialog.open });
      assert.equal(status.textContent, '');
      if (action === 'cancel-transfer') nameCancel.onclick();
      return response([4]);
    }
  };
  // Supply empty listings for the nested empty folder.
  const fetch = win.fetch;
  win.fetch = async (url, options) => url.includes('/empty') && !url.includes('can_download')
    ? json({ filesystem: 'fs', files: [] }) : fetch(url, options);
  vm.runInNewContext(fs.readFileSync(script, 'utf8'), { window: win, URL, AbortController, performance });
  if (action === 'loading') {
    assert.equal(button.onclick, undefined);
    listeners.get('DOMContentLoaded')();
    const handler = button.onclick;
    listeners.get('turbo:load')();
    assert.equal(button.onclick, handler);
  }
  const opened = button.onclick();
  if (dialog.open) {
    assert.equal(nameSubmit.disabled, true);
    assert.equal(summary.textContent, '合計サイズを計算中…');
    assert.equal(nameFields.hidden, true);
    assert.equal(spinner.hidden, false);
    assert.equal(nameCancel.hidden, false);
    form.onsubmit({ preventDefault() {} });
    assert.equal(pickerCalls, 0);
    if (action === 'cancel-during') nameCancel.onclick();
  }
  await opened;
  // Opening the name dialog must not request a native picker or start a transfer.
  const pickerCallsBeforeSubmit = pickerCalls;
  let pickerCalledSynchronously = false;
  if (dialog.open) {
    assert.equal(pickerCalls, 0);
    assert.equal(button.disabled, true);
    if (action === 'cancel') nameCancel.onclick();
    else if (action === 'escape') { dialog.oncancel({ preventDefault() {} }); }
    else {
      if (!nameSubmit.disabled) {
        assert.equal(nameFields.hidden, false);
        assert.equal(spinner.hidden, true);
        assert.equal(nameCancel.hidden, false);
        if (selection.length > 1 || action === 'rename-single') nameInput.value = name;
      }
      const submitted = form.onsubmit({ preventDefault() {} });
      pickerCalledSynchronously = pickerCalls > 0;
      await submitted;
      if (action === 'close-finished' || action === 'close-error') {
        assert.equal(close.hidden, false);
        close.onclick();
      }
    }
  }
  return { pageStatus: status.textContent, pageStatusHidden: status.hidden, target, message: nameError.textContent || status.textContent || progress.textContent, pickerCalls, dialogCalls,
    pickerCallsBeforeSubmit, pickerCalledSynchronously, summary: summary.textContent, inspectionRequests, phases, progressMessages,
    spinnerHidden: spinner.hidden, closeVisible: !close.hidden, cancelVisible: !nameCancel.hidden,
    inspectionAborted: requestSignals.some(signal => signal.aborted), submitDisabled: nameSubmit.disabled, dialogOpen: dialog.open, buttonDisabled: button.disabled };
}

test('single folder uses its own name without repeating its top-level directory', async () => {
  const target = memoryDirectory();
  await target.getDirectoryHandle('results', { create: true });
  const result = await clickWithName([dir('results')], 'ignored-custom-name', target);
  assert.match(result.message, /保存完了/);
  const saved = target.directories.get('results-1');
  assert.ok(saved.files.has('inside'));
  assert.ok(saved.directories.has('empty'));
  assert.equal(saved.directories.has('results'), false);
  assert.equal(target.directories.has('ignored-custom-name'), false);
  assert.equal(result.dialogCalls, 1);
});

test('multiple selection uses the entered name and retains original folder names', async () => {
  const result = await clickWithName([dir('results'), file('extra', 1)], '解析結果');
  assert.match(result.message, /保存完了/);
  assert.equal(result.dialogCalls, 1);
  assert.equal(result.pickerCallsBeforeSubmit, 0);
  assert.equal(result.pickerCalledSynchronously, true);
  assert.equal(result.summary, '合計サイズ: 2 B（2ファイル）');
  assert.equal(result.inspectionRequests, 1);
  const saved = result.target.directories.get('解析結果');
  assert.ok(saved.files.has('extra'));
  assert.ok(saved.directories.get('results').files.has('inside'));
});

test('invalid group names are rejected before the picker or any local creation', async () => {
  for (const name of ['', '..', 'parent/child']) {
    const result = await clickWithName([file('a'), file('b')], name);
    assert.match(result.message, /保存できないファイル名/);
    assert.equal(result.pickerCalls, 0);
    assert.equal(result.target.directories.size, 0);
  }
});

test('name dialog cancel and Escape do not open picker or create folders', async () => {
  for (const action of ['cancel', 'escape']) {
    const result = await clickWithName([file('a'), file('b')], 'group', memoryDirectory(), action);
    assert.equal(result.dialogCalls, 1);
    assert.equal(result.pickerCalls, 0);
    assert.equal(result.target.directories.size, 0);
    assert.equal(result.dialogOpen, false);
    assert.equal(result.buttonDisabled, false);
    assert.equal(result.pageStatus, '');
    assert.equal(result.pageStatusHidden, true);
  }
});

test('total size summary excludes directories and handles units, zero and overflow', () => {
  assert.equal(api.selectionSummary([{ directory: true }, { size: 0 }]), '合計サイズ: 0 B（1ファイル）');
  assert.equal(api.selectionSummary([{ directory: true }]), '合計サイズ: 0 B（0ファイル）');
  assert.equal(api.selectionSummary([{ size: 1024 }]), '合計サイズ: 1.00 KiB（1ファイル）');
  assert.equal(api.selectionSummary([{ size: 1048576 }, { size: 524288 }]), '合計サイズ: 1.50 MiB（2ファイル）');
  assert.equal(api.selectionSummary([{ size: 1610612736 }]), '合計サイズ: 1.50 GiB（1ファイル）');
  assert.throws(() => api.selectionSummary([{ size: Number.MAX_SAFE_INTEGER }, { size: 1 }]), /大きすぎる/);
});

test('cancelling during size inspection aborts requests and ignores late results', async () => {
  const result = await clickWithName([dir('results'), file('extra', 1)], 'group', memoryDirectory(), 'cancel-during');
  assert.equal(result.pickerCalls, 0);
  assert.equal(result.target.directories.size, 0);
  assert.equal(result.inspectionAborted, true);
  assert.equal(result.dialogOpen, false);
  assert.equal(result.submitDisabled, true);
  assert.equal(result.buttonDisabled, false);
  assert.equal(result.pageStatus, '');
  assert.equal(result.pageStatusHidden, true);
  assert.equal(result.summary, '合計サイズを計算中…');
});

test('size inspection failure blocks saving and never reports zero as the total', async () => {
  const result = await clickWithName([dir('results'), file('extra', 1)], 'group', memoryDirectory(), 'fail-size');
  assert.equal(result.pickerCalls, 0);
  assert.equal(result.submitDisabled, true);
  assert.equal(result.dialogOpen, true);
  assert.equal(result.target.directories.size, 0);
  assert.match(result.message, /size denied/);
  assert.equal(result.summary, '合計サイズを確認できませんでした。');
});

test('same popup stays open through native selection, file progress and completion', async () => {
  const result = await clickWithName([file('a', 1), file('b', 1)], 'group');
  assert.equal(result.pickerCalledSynchronously, true);
  assert.equal(result.dialogCalls, 1);
  assert.equal(result.dialogOpen, true);
  assert.equal(result.closeVisible, true);
  assert.equal(result.cancelVisible, false);
  assert.equal(result.spinnerHidden, true);
  assert.match(result.message, /保存完了: 2\/2ファイル/);
  assert.ok(result.progressMessages.some(text => text.includes('1/2ファイルをダウンロード中') && text.includes('MiB/s')));
  assert.ok(result.progressMessages.some(text => text.includes('2/2ファイルをダウンロード中')));
  for (const phase of result.phases) {
    assert.equal(phase.dialog, true);
    assert.equal(phase.nameHidden, true);
    assert.equal(phase.spinner, true);
    assert.equal(phase.cancel, true);
  }
});

test('cancelling a transfer aborts it and cannot be replaced by a stale completion', async () => {
  const result = await clickWithName([file('a', 1), file('b', 1)], 'group', memoryDirectory(), 'cancel-transfer');
  assert.equal(result.inspectionAborted, true);
  assert.equal(result.dialogOpen, false);
  assert.equal(result.buttonDisabled, false);
  assert.equal(result.pageStatus, '');
  assert.equal(result.pageStatusHidden, true);
  assert.doesNotMatch(result.message, /保存完了/);
});

test('native picker cancellation reports cancellation in the open popup', async () => {
  const result = await clickWithName([file('a', 1)], 'download', memoryDirectory(), 'picker-cancel');
  assert.equal(result.pickerCalledSynchronously, true);
  assert.equal(result.dialogOpen, true);
  assert.equal(result.closeVisible, true);
  assert.equal(result.spinnerHidden, true);
  assert.match(result.message, /保存を中止/);
  assert.equal(result.target.directories.size, 0);
});

test('dialog handlers install after page readiness and remain idempotent on Turbo load', async () => {
  const result = await clickWithName([file('a', 1)], 'download', memoryDirectory(), 'loading');
  assert.match(result.message, /保存完了: 1\/1ファイル/);
  assert.equal(result.dialogCalls, 1);
});

test('single folder defaults to its original name but can be renamed after calculation', async () => {
  const result = await clickWithName([dir('results')], 'renamed', memoryDirectory(), 'rename-single');
  assert.match(result.message, /保存完了/);
  assert.equal(result.dialogCalls, 1);
  const saved = result.target.directories.get('renamed');
  assert.ok(saved.files.has('inside'));
  assert.equal(saved.directories.has('results'), false);
});

test('only selected root folders are size-checked and nested listings remain intact', async () => {
  const checks = [], listings = [];
  const setting = env(async value => {
    const url = new URL(value);
    if (url.searchParams.has('can_download')) {
      checks.push(url.pathname);
      return json({ can_download: true });
    }
    listings.push(url.pathname);
    if (url.pathname.endsWith('/first')) return json({ filesystem: 'fs', files: [dir('child'), file('one', 1)] });
    if (url.pathname.endsWith('/child')) return json({ filesystem: 'fs', files: [file('two', 2), dir('empty')] });
    return json({ filesystem: 'fs', files: [] });
  });
  const entries = await api.plan([dir('first'), dir('second'), file('loose', 3)], setting);
  assert.deepEqual(checks, [base + 'first', base + 'second']);
  assert.ok(listings.includes(base + 'child'));
  assert.ok(listings.includes(base + 'empty'));
  assert.deepEqual(entries.map(entry => entry.parts), [
    ['first'], ['first', 'child'], ['first', 'child', 'two'],
    ['first', 'child', 'empty'], ['first', 'one'], ['second'], ['loose']
  ]);
  assert.equal(api.selectionSummary(entries), '合計サイズ: 6 B（3ファイル）');
});

test('nested listing denial still rejects the plan without any extra size check', async () => {
  let checks = 0;
  const setting = env(async value => {
    const url = new URL(value);
    if (url.searchParams.has('can_download')) { checks++; return json({ can_download: true }); }
    if (url.pathname.endsWith('/root')) return json({ filesystem: 'fs', files: [dir('denied')] });
    return json({ error_message: 'nested access denied' });
  });
  await assert.rejects(api.plan([dir('root')], setting), /nested access denied/);
  assert.equal(checks, 1);
});

test('closing completed or cancelled result leaves no message or empty status row on page', async () => {
  for (const action of ['close-finished', 'close-error']) {
    const result = await clickWithName([file('a', 1)], 'download', memoryDirectory(), action);
    assert.equal(result.dialogOpen, false);
    assert.equal(result.buttonDisabled, false);
    assert.equal(result.pageStatus, '');
    assert.equal(result.pageStatusHidden, true);
    assert.match(result.message, action === 'close-finished' ? /保存完了/ : /保存を中止/);
  }
});
