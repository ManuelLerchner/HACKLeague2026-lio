document.addEventListener("submit", async event => {
  const form = event.target.closest("[data-decision]");
  if (!form) return;
  event.preventDefault();
  const fields = new FormData(form);
  const result = form.querySelector("[data-decision-result]");
  const body = Object.fromEntries([...fields.entries()].filter(([, value]) => value !== ""));
  const button = event.submitter;
  if (button?.name) body[button.name] = button.value;
  if (body.choice === "renewal_confirmed" && !body.valid_until) {
    result.textContent = "Enter the date until which the renewed agreement applies.";
    form.querySelector('[name="valid_until"]')?.focus();
    return;
  }
  try { localStorage.setItem("supplier-chaser:reviewer", body.decided_by || ""); } catch { /* storage unavailable */ }
  form.querySelectorAll("button").forEach(b => { b.disabled = true; });
  try {
    const response = await fetch("/api/v1/decisions", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the decision fields.");
    result.textContent = `Decision recorded. Rechecking ${data.rechecking_lines} order line(s); this page updates when done.`;
    document.dispatchEvent(new CustomEvent("supplier-chaser:action-queued", {detail: {form}}));
  } catch (error) {
    result.textContent = error.message;
    form.querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
});
