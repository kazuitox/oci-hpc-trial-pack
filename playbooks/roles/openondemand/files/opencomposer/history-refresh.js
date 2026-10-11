/* OCI HPC Trial Pack: refresh only the OpenComposer History results. */
(() => {
  const button = document.getElementById('_historyRefresh');
  const message = document.getElementById('_historyRefreshStatus');
  if (!button || !message) return;

  // The upstream listeners close over the original rows. Bind the new rows
  // after replacement so selection and the batch action modals remain usable.
  function bindSelection(results) {
    const tbody = results.querySelector('#_historyTbody');
    const selectAll = results.querySelector('#_historySelectAll');
    const rows = tbody ? Array.from(tbody.rows) : [];
    if (selectAll) {
      selectAll.checked = rows.length > 0 && rows.every(row => row.querySelector('td input[type="checkbox"]')?.checked);
      selectAll.addEventListener('change', () => {
        rows.forEach(row => {
          const checkbox = row.querySelector('td input[type="checkbox"]');
          if (checkbox) checkbox.checked = selectAll.checked;
        });
        ocHistory.updateBatch(rows);
      });
    }
    rows.forEach(row => {
      const checkbox = row.querySelector('td input[type="checkbox"]');
      if (checkbox) checkbox.addEventListener('change', () => ocHistory.updateBatch(rows));
    });
    ocHistory.updateBatch(rows);
  }

  button.addEventListener('click', async () => {
    if (button.disabled) return;
    const results = document.getElementById('_historyResults');
    // An open Bootstrap modal must finish closing before its DOM is removed.
    if (!results || results.querySelector('.modal.show')) {
      message.textContent = 'Close the job details before refreshing.';
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    button.disabled = true;
    results.setAttribute('aria-busy', 'true');
    message.textContent = 'Refreshing…';
    try {
      const response = await fetch(window.location.href, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal
      });
      if (!response.ok || response.redirected) throw new Error('History request failed');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = page.getElementById('_historyResults');
      // Login and scheduler error pages must never replace the current table.
      if (!replacement || page.querySelector('.alert-warning')) throw new Error('Invalid History response');
      if (results.querySelector('.modal.show')) throw new Error('Job details are open');
      results.querySelectorAll('.modal').forEach(modal => {
        const instance = window.bootstrap?.Modal.getInstance(modal);
        if (instance) instance.dispose();
      });
      const selected = new Set(Array.from(results.querySelectorAll('td input[type="checkbox"]:checked'))
        .map(input => input.id));
      results.replaceChildren(...Array.from(replacement.childNodes));
      results.querySelectorAll('td input[type="checkbox"]').forEach(input => {
        input.checked = selected.has(input.id);
      });
      bindSelection(results);
      message.textContent = 'Updated.';
    } catch (error) {
      message.textContent = 'Refresh failed. Please try again.';
    } finally {
      clearTimeout(timer);
      button.disabled = false;
      results.removeAttribute('aria-busy');
    }
  });
})();
