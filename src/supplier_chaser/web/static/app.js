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
    document.dispatchEvent(new CustomEvent("supplier-chaser:action-queued", {detail: action === "approve-send" ? {savedElements: [document.querySelector("#draft-body")]} : {}}));
  } catch (error) {
    notification.textContent = error.message;
    notification.hidden = false;
    button.disabled = false;
  }
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form.reply-review");
  if (!form) return;
  event.preventDefault();
  const fields = new FormData(form);
  const decision = event.submitter?.value;
  const notification = document.querySelector("#notification");
  const buttons = form.querySelectorAll("button");
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await fetch(`/api/v1/cases/${form.dataset.case}/review-reply`, {
      method: "POST",
      headers: {"Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify({message_id: form.dataset.message, decision,
        reviewer: fields.get("reviewer"), note: fields.get("note"),
        identity_verified: fields.has("identity_verified")})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Complete the review fields.");
    notification.textContent = decision === "verify"
      ? "Identity review recorded. Extraction queued; commercial terms remain unapproved."
      : "Reply rejected. No supplier claim was accepted.";
    notification.hidden = false;
    document.dispatchEvent(new CustomEvent("supplier-chaser:action-queued", {detail: {form}}));
  } catch (error) {
    notification.textContent = error.message;
    notification.hidden = false;
    buttons.forEach(button => { button.disabled = false; });
  }
});
