document.addEventListener("submit", async (event) => {
  const extraction = event.target.closest("[data-extraction-form]");
  if (extraction) {
    event.preventDefault();
    const button = extraction.querySelector("button");
    const result = extraction.querySelector(".mapping-result");
    button.disabled = true;
    try {
      const response = await fetch("/api/v1/sources/extract", {
        method: "POST", headers: {"Content-Type": "application/json",
          "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
        body: JSON.stringify({source_fact_id: extraction.dataset.fact,
          po_line_id: extraction.elements.po_line_id.value})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Extraction rejected.");
      result.textContent = "Extraction queued. Refresh after the worker processes the document.";
    } catch (error) { result.textContent = error.message; }
    finally { button.disabled = false; }
    return;
  }
  const form = event.target.closest("[data-mapping-form]");
  if (!form) return;
  event.preventDefault();
  const result = form.querySelector(".mapping-result");
  const buttons = [...form.querySelectorAll("button")];
  const action = event.submitter?.value || "preview";
  const mapping = {kind: form.dataset.kind, columns: {}, constants: {}, date_formats: {},
    filters: {}, decimal_separator: form.elements.decimal_separator.value,
    thousands_separator: form.elements.thousands_separator.value || null,
    trust_contacts: form.elements.trust_contacts?.checked || false,
    note: form.elements.note.value};
  for (const row of form.querySelectorAll("[data-field]")) {
    const field = row.dataset.field;
    const column = row.querySelector("[data-column]").value;
    const constant = row.querySelector("[data-constant]").value;
    const dateFormat = row.querySelector("[data-date-format]")?.value;
    if (column) mapping.columns[field] = column;
    if (constant !== "") mapping.constants[field] = constant;
    if (dateFormat) mapping.date_formats[field] = dateFormat;
  }
  if (form.elements.filter_column.value) {
    mapping.filters[form.elements.filter_column.value] = form.elements.filter_values.value.split(",").map(x => x.trim());
  }
  buttons.forEach(button => button.disabled = true);
  try {
    const response = await fetch(`/api/v1/sources/${action}`, {
      method: "POST", headers: {"Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify({table_fact_id: form.dataset.table, mapping})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Check the mapping fields.");
    result.textContent = action === "apply"
      ? `Queued ${data.rows} rows for import. Refresh after the worker processes the job.`
      : `${data.row_count} valid rows. Preview:\n\n` + data.sample.map(([locator, values]) =>
        locator + "\n" + Object.entries(values).map(([key, value]) =>
          `${key.replaceAll("_", " ")}: ${value === null ? "unknown" : typeof value === "object" ? JSON.stringify(value) : value}`
        ).join("\n")).join("\n\n");
  } catch (error) {
    result.textContent = error.message;
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
});
