"""Teamtailor's form binding and rendered required/upload state (shared guards)."""

FORM_JS = r"""
(() => {
  const supported = location.protocol === 'https:' && !location.username && !location.password &&
    ['', '443'].includes(location.port) && /^[a-z0-9-]+\.teamtailor\.com$/.test(location.hostname);
  if (!supported) return null;
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const job = location.pathname.match(/^\/jobs\/(\d+)-[^/]+\/?$/);
  const forms = [...document.querySelectorAll('[id="job-application-form"]')];
  const form = forms.length === 1 && forms[0].tagName === 'FORM' ? forms[0] : null;
  const reqs = [...document.querySelectorAll('[name="candidate[job_applications_attributes][0][job_id]"]')];
  const req = reqs.length === 1 ? reqs[0] : null;
  const action = form ? new URL(form.action, location.href) : null;
  const bound = !!(job && form && visible(form) && action.origin === location.origin &&
    action.pathname === '/applications' && !action.search && !action.hash &&
    req?.type === 'hidden' && !req.matches(':disabled') && req.form === form && req.value === job[1]);
  const labels = el => [...(el.labels || [])].map(label => label.textContent).join(' ');
  const required = el => !!el.required || el.getAttribute('aria-required') === 'true' ||
    /\*|\bObligatorio\b/i.test(labels(el)) ||
    (el.form === form && !!el.closest('[data-question-mandatory="true"]')) ||
    (el.type === 'file' && el.closest('[data-forms--inputs--upload-required-value="true"]')?.closest('form') === form);
  const uploaded = el => {
    if (!bound || el.type !== 'file' || el.form !== form) return '';
    const box = el.closest('[data-forms--inputs--upload-required-value]');
    if (!box || box.closest('form') !== form || !box.classList.contains('dz-max-files-reached')) return '';
    const previews = [...box.querySelectorAll('[data-controller~="forms--inputs--upload-preview"].dz-success.dz-complete')]
      .filter(preview => visible(preview) && preview.closest('form') === form);
    if (previews.length !== 1) return '';
    const preview = previews[0];
    const urls = [...preview.querySelectorAll('input[name="candidate[resume_remote_url]"][data-forms--inputs--upload-preview-target="urlInput"]')]
      .filter(input => input.form === form && !input.matches(':disabled') && !visible(input) && input.type === 'text' && input.value.trim());
    if (urls.length !== 1) return '';
    let url;
    try { url = new URL(urls[0].value); } catch (_) { return ''; }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    const links = [...preview.querySelectorAll('[data-forms--inputs--upload-preview-target="link"]')]
      .filter(link => visible(link) && link.innerText.trim());
    return links.length === 1 ? links[0].innerText.trim() : '';
  };
  const present = el => el.type === 'radio' ? !!el.name && [...form.elements].some(other =>
    other.type === 'radio' && other.name === el.name && other.checked &&
    !other.matches(':disabled') && visible(other)) :
    el.type === 'checkbox' ? el.checked : !!uploaded(el) || !!(el.value || '').trim();
  const presented = el => visible(el) || (el.type === 'file' &&
    ([...(el.labels || [])].some(visible) || !!uploaded(el)));
  const invalid = el => el.getAttribute('aria-invalid') === 'true' ||
    (!uploaded(el) && !el.checkValidity());
  return {form: bound ? form : null, requisition: job?.[1] || '', required, uploaded,
    present, presented, invalid};
})()
"""
