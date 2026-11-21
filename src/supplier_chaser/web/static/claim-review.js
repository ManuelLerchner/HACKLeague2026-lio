const REVIEWER_KEY = "supplier-chaser:reviewer";

function rememberedReviewer() {
  try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; }
}

function rememberReviewer(name) {
  try { localStorage.setItem(REVIEWER_KEY, name); } catch { /* storage unavailable */ }
}

for (const input of document.querySelectorAll("[data-remember-reviewer]")) {
  if (!input.value) input.value = rememberedReviewer();
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-find-review-evidence]");
  if (!button) return;
  const form = button.closest("form");
  const result = form.querySelector("[data-review-result]");
  button.disabled = true;
  try {
    const response = await fetch(`/api/v1/review-evidence?q=${encodeURIComponent(form.elements.evidence_search.value)}`);
    if (!response.ok) throw new Error("Could not load evidence references. Check your session.");
    const items = await response.json();
    for (const select of form.querySelectorAll("select")) {
      select.replaceChildren(new Option("Choose supporting evidence", ""));
      for (const fact of items) select.add(new Option(`${fact.path} · ${fact.locator}`, fact.id));
    }
    result.textContent = `${items.length} source references found. Inspect the original and quote the relevant passage.`;
  } catch (error) { result.textContent = error.message; }
  finally { button.disabled = false; }
});

document.addEventListener("submit", async event => {
  const form = event.target.closest("[data-claim-review]");
  if (!form) return;
  event.preventDefault();
  const fields = new FormData(form);
  const result = form.querySelector("[data-review-result]");
  const body = {reviewer: fields.get("reviewer"), note: fields.get("note"), acknowledged_risks: fields.getAll("acknowledge")};
  if (fields.get("unit")) {
    body.unit = fields.get("unit");
    body.unit_evidence = {fact_id: fields.get("unit_fact"), quote: fields.get("unit_quote")};
  }
  if (fields.has("baseline_confirmed")) {
    body.baseline_terms_evidence = {fact_id: fields.get("terms_fact"), quote: fields.get("terms_quote")};
  }
  if (!body.unit && !body.baseline_terms_evidence && !body.acknowledged_risks.length) {
    result.textContent = "Confirm the terms, acknowledge a flag or supply a unit before recording the review.";
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await fetch(`/api/v1/claims/${form.dataset.claimReview}/review`, {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the review fields and evidence quotes.");
    rememberReviewer(body.reviewer);
    result.textContent = "Review recorded. The worker rechecks the commitment now; this page updates when it is done.";
    document.dispatchEvent(new CustomEvent("supplier-chaser:action-queued", {detail: {form}}));
  } catch (error) { result.textContent = error.message; button.disabled = false; }
});
