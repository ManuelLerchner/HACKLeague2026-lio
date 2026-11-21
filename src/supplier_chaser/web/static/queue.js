document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-retry-url]");
  if (!button) return;
  button.disabled = true;
  const notice = document.querySelector("#notification");
  try {
    const response = await fetch(button.dataset.retryUrl, {
      method: "POST",
      headers: {"X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content}
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Retry rejected");
    notice.textContent = "Retry queued. The worker will recheck the action before processing it.";
    notice.hidden = false;
    document.dispatchEvent(new CustomEvent("supplier-chaser:action-queued", {detail: {}}));
  } catch (error) {
    notice.textContent = error.message;
    notice.hidden = false;
    button.disabled = false;
  }
});
