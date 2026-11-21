(() => {
  const route = location.pathname.match(/^\/(cases|sources)\/([a-f0-9-]{36})\/?$/i);
  if (!route) return;

  const kind = route[1];
  const endpoint = kind === "cases"
    ? `/api/v1/cases/${route[2]}`
    : `/api/v1/sources/${route[2]}/progress`;
  const notification = document.querySelector("#notification");
  if (!notification) return;

  const controls = "input:not([type=hidden]):not([type=button]):not([type=submit]), textarea, select, [contenteditable=true]";
  const saved = new Map();
  const submitted = new Map();
  const dirty = new Set();
  let previous = null;
  let inFlight = false;
  let stopped = false;
  let failures = 0;
  let refreshNeeded = false;
  let refreshScheduled = false;
  let hasQueuedAction = false;

  function value(element) {
    if (element.type === "checkbox" || element.type === "radio") return element.checked;
    if (element.multiple) return JSON.stringify([...element.selectedOptions].map(option => option.value));
    return element.isContentEditable ? element.textContent : element.value;
  }

  function remember(element) {
    saved.set(element, value(element));
    dirty.delete(element);
  }

  document.querySelectorAll(controls).forEach(remember);
  document.addEventListener("submit", event => {
    if (!(event.target instanceof Element)) return;
    event.target.querySelectorAll(controls).forEach(element => submitted.set(element, value(element)));
  }, true);
  document.addEventListener("click", event => {
    if (!event.target.closest?.('button[data-action="approve-send"]')) return;
    const draft = document.querySelector("#draft-body");
    if (draft) submitted.set(draft, value(draft));
  }, true);
  for (const eventName of ["input", "change"]) {
    document.addEventListener(eventName, event => {
      const element = event.target;
      if (!(element instanceof Element) || !element.matches(controls)) return;
      if (!saved.has(element) || saved.get(element) !== value(element)) dirty.add(element);
      else dirty.delete(element);
    });
  }

  function interacting() {
    return dirty.size > 0 || document.activeElement?.matches(controls)
      || document.querySelector("dialog[open]");
  }

  function notice(message, allowRefresh = false) {
    notification.replaceChildren(document.createTextNode(message));
    if (allowRefresh) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = dirty.size ? "Discard edits & refresh" : "Refresh now";
      button.addEventListener("click", () => {
        if (dirty.size && !window.confirm("Discard your unsaved edits and show the updated result?")) return;
        location.reload();
      });
      notification.append(document.createTextNode(" "), button);
    }
    notification.hidden = false;
  }

  function refreshWhenSafe() {
    if (!refreshNeeded || refreshScheduled) return;
    if (interacting()) {
      notice("New results are ready. Refresh when you have finished reviewing or editing.", true);
      return;
    }
    if (document.hidden) return;
    refreshScheduled = true;
    window.setTimeout(() => {
      refreshScheduled = false;
      if (interacting()) notice("New results are ready. Refresh when you have finished reviewing or editing.", true);
      else if (!document.hidden) location.reload();
    }, 900);
  }

  function snapshot(data) {
    if (kind === "sources") {
      return {signature: JSON.stringify([data.pending, data.revision, data.status]),
        pending: Boolean(data.pending), failed: data.status === "error" || data.status === "failed"};
    }
    const jobs = (data.jobs || []).map(job => [job.type, job.status, job.error]).sort();
    const messages = (data.messages || []).map(message => [message.id, message.status, message.at]);
    const findings = (data.document_findings || []).map(document => [document.source_id, document.status, document.summary]);
    return {signature: JSON.stringify([data.state, data.pending_work, data.updated_at, jobs, messages, findings]),
      pending: Boolean(data.pending_work), failed: jobs.some(job => job[1] === "failed")};
  }

  async function poll() {
    if (document.hidden || inFlight || stopped) return;
    inFlight = true;
    try {
      const response = await fetch(endpoint, {headers: {Accept: "application/json"}, cache: "no-store"});
      if (response.status === 401 || response.status === 403) {
        stopped = true;
        notice("Your session expired. Sign in again to resume live updates.");
        return;
      }
      if (!response.ok) throw new Error("Progress unavailable");
      const current = snapshot(await response.json());
      failures = 0;
      if (previous && current.signature !== previous.signature) {
        if (!current.pending) {
          refreshNeeded = true;
          notice(current.failed ? "An action needs attention. Loading its result." : "Results updated. Loading the latest state.");
        } else {
          notice("Work is queued or running. This page will update when the result is ready.");
        }
      } else if (!previous && hasQueuedAction && !current.pending) {
        refreshNeeded = true;
      }
      previous = current;
      refreshWhenSafe();
    } catch {
      failures += 1;
      if (failures === 3 && !refreshNeeded) notice("Live updates are temporarily unavailable. Retrying automatically.");
    } finally {
      inFlight = false;
    }
  }

  document.addEventListener("supplier-chaser:action-queued", event => {
    const detail = event.detail || {};
    function acceptSubmitted(element) {
      if (submitted.has(element) && submitted.get(element) === value(element)) remember(element);
    }
    if (detail.form instanceof Element) detail.form.querySelectorAll(controls).forEach(acceptSubmitted);
    for (const element of detail.savedElements || []) {
      if (element instanceof Element && element.matches(controls)) acceptSubmitted(element);
    }
    hasQueuedAction = true;
    poll();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { refreshWhenSafe(); poll(); }
  });
  document.addEventListener("focusout", () => window.setTimeout(refreshWhenSafe, 0));
  window.setInterval(poll, 5000);
  poll();
})();
