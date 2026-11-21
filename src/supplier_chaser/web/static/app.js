document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  const body = action === "approve-send"
    ? {body: document.querySelector("#draft-body").value}
    : action === "escalate" ? {reason: "Buyer requested manual review"} : {};
  button.disabled = true;
  const notification = document.querySelector("#notification");
  try {
    const response = await fetch(`/api/v1/cases/${button.dataset.case}/${action}`, {
      method: "POST", headers: {"Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Request rejected");
    notification.textContent = "Saved. The worker will process queued actions.";
    notification.hidden = false;
    setTimeout(() => location.reload(), 1200);
  } catch (error) {
    notification.textContent = error.message;
    notification.hidden = false;
    button.disabled = false;
  }
});
