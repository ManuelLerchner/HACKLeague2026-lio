(() => {
  const dialog = document.querySelector("#evidence-preview");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const frame = dialog.querySelector("iframe");
  const title = dialog.querySelector("#evidence-preview-title");
  const download = dialog.querySelector("[data-preview-download]");
  const open = dialog.querySelector("[data-preview-open]");
  const status = dialog.querySelector("[data-preview-status]");
  let opener = null;

  function localRoute(value, pattern) {
    if (!value) return null;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && pattern.test(url.pathname)
        && !url.search && !url.hash ? url.href : null;
    } catch { return null; }
  }

  document.addEventListener("click", event => {
    const link = event.target.closest?.("a[data-preview-url]");
    if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const previewUrl = localRoute(link.dataset.previewUrl, /^\/(?:evidence|sources)\/[a-f0-9-]{36}\/preview$/i);
    const downloadUrl = localRoute(link.dataset.downloadUrl, /^\/(?:evidence\/[a-f0-9-]{36}|sources\/[a-f0-9-]{36}\/download)$/i);
    if (!previewUrl || !downloadUrl) return;
    event.preventDefault();
    opener = link;
    title.textContent = link.dataset.previewTitle || "Original evidence";
    download.href = downloadUrl;
    open.href = previewUrl;
    status.textContent = "Loading original evidence…";
    frame.src = previewUrl;
    if (!dialog.open) dialog.showModal();
  });

  frame.addEventListener("load", () => {
    if (!dialog.open) return;
    status.textContent = "If the preview does not display, open it in a new tab or download the original.";
  });
  frame.addEventListener("error", () => {
    if (dialog.open) status.textContent = "Preview unavailable. Try opening it in a new tab or downloading the original.";
  });
  dialog.querySelector("[data-preview-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    frame.removeAttribute("src");
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
})();
