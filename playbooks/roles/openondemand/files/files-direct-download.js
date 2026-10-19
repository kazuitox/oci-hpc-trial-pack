/* Files extension for Open OnDemand 4.2.5. Uses its authenticated Files API. */
(function (root) {
  'use strict';
  const MAX_ENTRIES = 10000;
  const MAX_DEPTH = 64;

  function safeName(name) {
    if (typeof name !== 'string' || !name || name === '.' || name === '..' || /[\/\\\u0000]/.test(name)) {
      throw new Error('保存できないファイル名です。');
    }
    return name;
  }

  function localURL(value, origin) {
    if (typeof value !== 'string' || !value) throw new Error('ダウンロード先URLがありません。');
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || !url.pathname.includes('/files/fs/')) {
      throw new Error('ZIPなし保存は、このサーバーの通常のファイル領域だけに対応しています。');
    }
    return url;
  }

  async function jsonRequest(value, env) {
    const url = localURL(value, env.origin);
    const response = await env.fetch(url.href, {
      headers: { Accept: 'application/json' }, credentials: 'same-origin',
      cache: 'no-store', redirect: 'error', signal: env.signal
    });
    if (!response.ok || response.redirected) throw new Error(`一覧の取得に失敗しました（HTTP ${response.status}）。`);
    const data = await response.json();
    if (data.error_message) throw new Error(data.error_message);
    return data;
  }

  // Finish enumeration before creating local files. Never silently omit unreadable entries.
  async function plan(selection, env) {
    if (!selection.length) throw new Error('保存するファイルまたはフォルダを選択してください。');
    const entries = [];
    async function visit(rows, parents) {
      if (parents.length >= MAX_DEPTH) throw new Error('フォルダの階層が上限（64階層）を超えています。');
      const names = new Set();
      for (const row of rows) {
        env.signal.throwIfAborted();
        const name = safeName(row.name);
        // Case-insensitive local file systems must not merge two distinct server names.
        const key = name.normalize('NFC').toLowerCase();
        if (names.has(key)) throw new Error(`保存先で名前が重複する可能性があります: ${name}`);
        names.add(key);
        if ((Number(row.mode) & 0xf000) === 0xa000) {
          throw new Error(`シンボリックリンクはZIPなし保存の対象外です: ${name}`);
        }
        if (!row.download_url) throw new Error(`読み取りできない項目があります: ${name}`);
        localURL(row.download_url, env.origin);
        const parts = [...parents, name];
        if (entries.length >= MAX_ENTRIES) throw new Error('項目数の上限（10,000件）を超えています。');
        if (row.type === 'd') {
          // The selected root's recursive check covers its descendants. Avoid
          // repeating du for every nested folder; each listing still enforces access.
          if (parents.length === 0) {
            const check = localURL(row.url, env.origin);
            check.searchParams.set('can_download', '1');
            const permitted = await jsonRequest(check.href, env);
            if (permitted.can_download !== true) throw new Error(permitted.error_message || `取得できないフォルダです: ${name}`);
          }
          entries.push({ parts, directory: true });
          env.progress(`一覧を確認中: ${entries.length}件`);
          const listing = await jsonRequest(row.url, env);
          if (listing.filesystem !== 'fs' || !Array.isArray(listing.files)) throw new Error('通常のファイル一覧を取得できませんでした。');
          await visit(listing.files, parts);
        } else if (row.type === 'f' && Number.isSafeInteger(row.size) && row.size >= 0) {
          entries.push({ parts, url: row.download_url, size: row.size });
        } else {
          throw new Error(`取得できない項目があります: ${name}`);
        }
      }
    }
    await visit(selection, []);
    return entries;
  }

  function selectionSummary(entries) {
    let bytes = 0;
    let files = 0;
    for (const entry of entries) {
      if (entry.directory) continue;
      bytes += entry.size;
      files++;
      if (!Number.isSafeInteger(bytes) || bytes < 0) throw new Error('合計サイズが大きすぎるため確認できません。');
    }
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    const size = unit === 0 ? String(value) : value.toFixed(2);
    return `合計サイズ: ${size} ${units[unit]}（${files}ファイル）`;
  }

  async function newDestination(parent, prefix) {
    safeName(prefix);
    for (let suffix = 0; suffix < 100; suffix++) {
      const name = suffix ? `${prefix}-${suffix}` : prefix;
      try {
        await parent.getDirectoryHandle(name);
      } catch (error) {
        if (error.name === 'NotFoundError') {
          return { handle: await parent.getDirectoryHandle(name, { create: true }), name };
        }
        // A file with the same name also counts as a collision.
        if (error.name !== 'TypeMismatchError') throw error;
      }
    }
    throw new Error('保存用フォルダを作成できませんでした。別の保存先を選択してください。');
  }

  async function save(entries, destination, env) {
    let completed = 0;
    let bytes = 0;
    const total = entries.filter(entry => !entry.directory).length;
    const now = env.now || (() => root.performance.now());
    const started = now();
    let lastUpdate = -Infinity;
    function progress(current, saved, force = false) {
      const time = now();
      // Limit text/aria-live changes during large streamed downloads.
      if (!force && time - lastUpdate < 250) return;
      lastUpdate = time;
      const elapsed = (time - started) / 1000;
      const rate = elapsed > 0 ? bytes / 1048576 / elapsed : 0;
      env.progress(`${current}/${total}ファイル${saved ? '保存済み' : 'をダウンロード中'} · ${rate.toFixed(1)} MiB/s（平均）`);
    }
    for (const entry of entries) {
      env.signal.throwIfAborted();
      let parent = destination;
      const directories = entry.directory ? entry.parts : entry.parts.slice(0, -1);
      for (const name of directories) parent = await parent.getDirectoryHandle(safeName(name), { create: true });
      if (entry.directory) continue;
      const name = safeName(entry.parts[entry.parts.length - 1]);
      progress(completed + 1, false, true);
      env.signal.throwIfAborted();
      const response = await env.fetch(localURL(entry.url, env.origin).href, {
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: env.signal
      });
      if (!response.ok || response.redirected || !response.body ||
          !/^attachment(?:;|$)/i.test(response.headers.get('Content-Disposition') || '')) {
        if (response.body) await response.body.cancel();
        throw new Error(`取得に失敗しました: ${entry.parts.join('/')}（HTTP ${response.status}）。認証期限・権限・サイズ制限を確認してください。`);
      }
      let reader;
      let writer;
      try {
        // Even inside the new destination, do not overwrite a pre-existing local file.
        try {
          await parent.getFileHandle(name);
          throw new Error(`保存先に同名ファイルがあります: ${name}`);
        } catch (error) {
          if (error.name !== 'NotFoundError') throw error;
        }
        const file = await parent.getFileHandle(name, { create: true });
        writer = await file.createWritable();
        reader = response.body.getReader();
        let received = 0;
        while (true) {
          env.signal.throwIfAborted();
          const chunk = await reader.read();
          if (chunk.done) break;
          received += chunk.value.byteLength;
          if (received > entry.size) throw new Error(`転送中にサイズが変わりました: ${name}`);
          await writer.write(chunk.value);
          bytes += chunk.value.byteLength;
          progress(completed + 1, false);
        }
        if (received !== entry.size) throw new Error(`転送サイズが一致しません: ${name}`);
        env.signal.throwIfAborted();
        await writer.close();
        writer = null;
        completed++;
        progress(completed, true, true);
      } catch (error) {
        if (reader) await reader.cancel().catch(() => {});
        else await response.body.cancel().catch(() => {});
        if (writer) await writer.abort().catch(() => {});
        throw error;
      }
    }
    return completed;
  }

  function install() {
    const button = root.document.getElementById('ood-direct-download-btn');
    if (!button || button.dataset.installed) return;
    button.dataset.installed = 'true';
    const get = suffix => root.document.getElementById(`ood-direct-download-${suffix}`);
    const status = get('status'), dialog = get('dialog'), form = get('name-form');
    const nameInput = get('name'), nameError = get('name-error'), nameFields = get('name-fields');
    const cancel = get('name-cancel'), close = get('close'), submit = get('name-submit');
    const summary = get('summary'), spinner = get('spinner'), progress = get('progress');
    let session;

    function render(run) {
      const ready = run.phase === 'ready';
      const terminal = run.phase === 'done' || run.phase === 'error';
      spinner.hidden = ready || terminal;
      nameFields.hidden = !ready;
      nameInput.disabled = !ready;
      submit.hidden = !ready;
      submit.disabled = !ready;
      cancel.hidden = terminal;
      close.hidden = !terminal;
    }
    function dismiss() {
      const run = session;
      if (!run) return;
      const terminal = run.phase === 'done' || run.phase === 'error';
      if (!terminal) run.controller.abort();
      session = null;
      dialog.close();
      button.disabled = false;
      status.textContent = '';
      status.hidden = true;
    }
    function finish(run, message, failed = false) {
      if (session !== run) return;
      run.phase = failed ? 'error' : 'done';
      progress.textContent = message;
      render(run);
      close.focus();
    }
    async function download(run, name) {
      run.phase = 'choosing';
      progress.textContent = '保存先を選択してください。';
      render(run);
      try {
        // Keep this call directly in the submit event, before any await.
        const parent = await root.showDirectoryPicker({ mode: 'readwrite' });
        run.controller.signal.throwIfAborted();
        run.destination = await newDestination(parent, name);
        run.controller.signal.throwIfAborted();
        const singleFolder = run.selection.length === 1 && run.selection[0].type === 'd';
        const entries = singleFolder
          ? run.entries.slice(1).map(entry => ({ ...entry, parts: entry.parts.slice(1) }))
          : run.entries;
        run.phase = 'transferring';
        progress.textContent = 'ダウンロードを開始しています…';
        render(run);
        const count = await save(entries, run.destination.handle, {
          origin: root.location.origin, fetch: root.fetch.bind(root), signal: run.controller.signal,
          progress: message => { if (session === run) progress.textContent = message; }
        });
        finish(run, `保存完了: ${count}/${count}ファイル（${run.destination.name}）`);
      } catch (error) {
        const message = error.name === 'AbortError' ? '保存を中止しました。' : `保存を停止しました: ${error.message}`;
        const remaining = run.destination ? ` 完了済みのファイルは ${run.destination.name} に残っています。失敗したファイルは空のまま残る場合があります。` : '';
        finish(run, message + remaining, true);
      }
    }
    if (dialog && form && nameInput && nameError && nameFields && cancel && close && submit && summary && spinner && progress) {
      cancel.onclick = dismiss;
      close.onclick = dismiss;
      dialog.oncancel = event => { if (event) event.preventDefault(); dismiss(); };
      form.onsubmit = event => {
        event.preventDefault();
        const run = session;
        if (!run || run.phase !== 'ready') return;
        let name;
        try { name = safeName(nameInput.value.trim()); }
        catch (error) { nameError.textContent = error.message; nameInput.focus(); return; }
        nameError.textContent = '';
        return download(run, name);
      };
    }
    button.onclick = async () => {
      if (session) return;
      if (!root.isSecureContext || !root.showDirectoryPicker) {
        status.hidden = false;
        status.textContent = 'ZIPなし保存にはHTTPS接続とChromeまたはEdgeなどの対応ブラウザが必要です。';
        return;
      }
      try {
        if (!root.jQuery || !root.jQuery.fn || !root.jQuery.fn.dataTable ||
            !root.jQuery.fn.dataTable.isDataTable('#directory-contents')) {
          throw new Error('Filesの一覧が準備できていません。画面を再読み込みしてから再試行してください。');
        }
        const selection = root.jQuery('#directory-contents').DataTable().rows({ selected: true }).data().toArray();
        if (!selection.length) { status.hidden = false; status.textContent = '保存する項目を選択してください。'; return; }
        if (!dialog || !form || !nameInput || !nameError || !nameFields || !cancel || !close ||
            !submit || !summary || !spinner || !progress || !dialog.showModal) {
          throw new Error('ダウンロード画面を開けません。画面を再読み込みしてください。');
        }
        const run = { selection: selection.map(row => ({ ...row })), controller: new AbortController(), phase: 'calculating' };
        session = run;
        nameInput.value = selection.length === 1 && selection[0].type === 'd' ? selection[0].name : 'download';
        nameError.textContent = '';
        summary.textContent = '合計サイズを計算中…';
        progress.textContent = 'ファイル一覧と容量を確認しています。';
        status.textContent = '';
        status.hidden = true;
        button.disabled = true;
        render(run);
        dialog.showModal();
        cancel.focus();
        try {
          run.entries = await plan(run.selection, {
            origin: root.location.origin, fetch: root.fetch.bind(root), signal: run.controller.signal,
            progress: message => { if (session === run) progress.textContent = message; }
          });
          if (session !== run || run.controller.signal.aborted) return;
          summary.textContent = selectionSummary(run.entries);
          progress.textContent = 'フォルダ名を確認し、保存先を選択してください。';
          run.phase = 'ready';
          render(run);
          nameInput.focus();
          nameInput.select();
        } catch (error) {
          if (session !== run || run.controller.signal.aborted) return;
          summary.textContent = '合計サイズを確認できませんでした。';
          finish(run, error.message, true);
        }
      } catch (error) {
        if (session) session.controller.abort();
        session = null;
        if (dialog && dialog.open) dialog.close();
        button.disabled = false;
        status.hidden = false;
        status.textContent = `保存を停止しました: ${error.message}`;
      }
    };
    status.textContent = '';
    status.hidden = true;
    button.disabled = false;
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { safeName, localURL, plan, selectionSummary, newDestination, save, install };
  if (root.document) {
    // A view override may execute again after Turbo navigation; install only once per button.
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', install, { once: true });
    else install();
    root.document.addEventListener('turbo:load', install);
  }
})(typeof window === 'undefined' ? globalThis : window);
