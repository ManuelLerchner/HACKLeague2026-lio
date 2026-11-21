document.querySelector('#review-evidence-find')?.addEventListener('click', async () => {
  const list = document.querySelector('#review-evidence-results');
  try {
    const response = await fetch(`/api/v1/review-evidence?q=${encodeURIComponent(document.querySelector('#review-evidence-search').value)}`);
    if (!response.ok) throw new Error('Sign in to search evidence.');
    list.replaceChildren();
    for (const fact of await response.json()) {
      const item = document.createElement('li');
      item.textContent = `${fact.path} · ${fact.locator} · reference: ${fact.id}`;
      list.append(item);
    }
  } catch (error) { list.textContent = error.message; }
});
document.querySelector('#document-review-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const overall = new FormData(form);
  const value = (fields, name) => fields.querySelector(`[name="${name}"]`).value.trim();
  const evidence = (fields, name) => value(fields, `${name}_fact_id`) ? {fact_id:value(fields, `${name}_fact_id`),quote:value(fields, `${name}_quote`)} : null;
  const items = [...form.querySelectorAll('fieldset[data-item]')].filter(fields => value(fields, 'po_line_id')).map(fields => ({
    item_id:fields.dataset.item,po_line_id:value(fields,'po_line_id'),reviewer:overall.get('reviewer'),note:overall.get('note'),
    attribution_evidence:evidence(fields,'attribution'),part_alias_evidence:evidence(fields,'part_alias'),
    unit:value(fields,'unit') || null,unit_evidence:evidence(fields,'unit'),baseline_terms_evidence:evidence(fields,'baseline_terms')
  }));
  const notification = document.querySelector('#notification');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = await fetch('/api/v1/documents/review', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({analysis_id:form.dataset.analysis,items})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Select an item and complete the required evidence fields.');
    location.assign(`/sources/${form.dataset.source}`);
  } catch (error) { notification.hidden=false; notification.textContent=error.message; button.disabled=false; }
});
