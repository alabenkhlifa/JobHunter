"""Read the employer-bound Denodo SAP UI5 application without changing the page."""

SNAPSHOT_JS = r"""(() => {
  if (location.protocol !== 'https:' || location.hostname !== 'career55.sapsf.eu' ||
      location.username || location.password || !['', '443'].includes(location.port) ||
      !['/career', '/careers', '/portalcareer'].includes(location.pathname)) return null;
  const form = document.getElementById('careerform');
  const wizardTag = 'ui5-wizard-xweb-dynamic-content';
  if (!form || !form.querySelector(wizardTag)) return null;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const owned = id => [...document.querySelectorAll('[id="' + id + '"]')];
  const tenants = owned('career_company'), reqs = owned('career_job_req_id');
  const query = new URLSearchParams(location.search);
  const headings = [...form.querySelectorAll('ui5-title-xweb-candidate-experience')]
    .filter(visible).map(e => norm(e.textContent).match(/^(.+)\s+\((\d+)\)$/)).filter(Boolean);
  const brand = [...document.querySelectorAll('a[href]')].find(e => {
    const u = new URL(e.href);
    return visible(e) && u.protocol === 'https:' && !u.username && !u.password &&
      ['', '443'].includes(u.port) && ['www.denodo.com', 'jobs.denodo.com'].includes(u.hostname);
  });
  const requisition = reqs[0]?.value || '';
  const wizard = [...form.querySelectorAll(wizardTag)].filter(visible);
  const identity = owned('careerform').length === 1 && form.tagName === 'FORM' && visible(form) && wizard.length === 1 &&
    tenants.length === 1 && tenants[0].type === 'hidden' && tenants[0].form === form &&
    tenants[0].value === 'denodosl' && reqs.length > 0 && /^\d+$/.test(requisition) &&
    reqs.every(e => e.type === 'hidden' && e.form === form && e.value === requisition) &&
    owned('jobReqId').every(e => e.type === 'hidden' && e.form === form && e.value === requisition) &&
    headings.length === 1 && headings[0][2] === requisition && brand &&
    ((!query.has('company') && location.pathname === '/portalcareer') ||
      (query.getAll('company').length === 1 && query.get('company') === 'denodosl')) &&
    ['career_job_req_id', 'jobReqId'].every(k => !query.has(k) ||
      (query.getAll(k).length === 1 && query.get(k) === requisition));
  const path = e => {
    const root = e.getRootNode(), segments = [];
    let current = e;
    while (current && current.nodeType === 1) {
      if (current.id) { segments.unshift('#' + CSS.escape(current.id)); break; }
      let segment = current.tagName.toLowerCase();
      if (current.parentElement) {
        const siblings = [...current.parentElement.children].filter(n => n.tagName === current.tagName);
        segment += ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')';
      }
      segments.unshift(segment);
      current = current.parentElement;
    }
    return (root.host ? path(root.host) + ' >>> ' : '') + segments.join(' > ');
  };
  const parent = e => e.parentElement || e.getRootNode().host;
  const component = e => {
    for (let p = parent(e); p && p !== form; p = parent(p))
      if (/^UI5-(INPUT|TEXTAREA|CHECKBOX|RADIO-BUTTON|FILE-UPLOADER|DATE-PICKER)-/.test(p.tagName)) return p;
    return null;
  };
  const label = (e, host) => {
    let item = host || e;
    while (item && item !== form && !item.tagName.startsWith('UI5-FORM-ITEM-')) item = parent(item);
    const labels = item && item !== form ? [...item.querySelectorAll('[ui5-label]')].filter(visible) : [];
    return norm(host?.getAttribute('accessible-name') || host?.getAttribute('text') ||
      e.getAttribute('aria-label') || labels.map(l => l.textContent).join(' ') ||
      e.labels?.[0]?.textContent || e.name || (e.type === 'file' ? 'Resume / CV' : 'field'));
  };
  const nodes = [];
  const walk = root => {
    for (const e of root.querySelectorAll('*')) {
      nodes.push(e);
      if (e.shadowRoot) walk(e.shadowRoot);
    }
  };
  walk(form);
  const selected = nodes.filter(e => e.tagName === 'UI5-WIZARD-TAB-XWEB-DYNAMIC-CONTENT' && e.hasAttribute('selected'));
  const step = selected.length === 1 ? selected[0].getAttribute('title-text') || '' : '';
  const inputs = [];
  for (const e of nodes) {
    if (!e.matches('input,textarea,select') || !visible(e) ||
        ['hidden', 'button', 'submit', 'password'].includes(e.type)) continue;
    const host = component(e);
    if (host && /^UI5-(CHECKBOX|RADIO-BUTTON)-/.test(host.tagName)) continue;
    // UI5 Select is represented once by its host below, not by its shadow internals.
    if (e.getRootNode().host?.tagName.startsWith('UI5-SELECT-')) continue;
    const question = label(e, host);
    const item = host?.closest('[ui5-form-item]');
    const disabled = !!e.disabled || !!host?.disabled;
    const collection = host?.closest('[ui5-upload-collection]');
    const stored = e.type === 'file' && collection ? [...collection.querySelectorAll(
      '[ui5-upload-collection-item][upload-state="Complete"]')].filter(visible)
      .map(file => file.getAttribute('file-name')).filter(Boolean) : [];
    const required = !!e.required || e.getAttribute('aria-required') === 'true' ||
      !!host?.required || /\*\s*$/.test(question) ||
      !!item?.querySelector('[ui5-label][required]') ||
      (e.type === 'file' && /^Getting Started/.test(step));
    const selector = path(e);
    inputs.push({tag:e.tagName.toLowerCase(), id:e.id || selector, selector,
      name:e.name || '', type:e.type || 'text', label:question, required:required && !disabled, form:'careerform',
      disabled, readonly:!!e.readOnly || !!host?.readonly,
      stored_files:stored,
      value_present:e.type === 'file' ? !!e.files?.length || !!host?.value || stored.length > 0 :
        e.type === 'radio' ? nodes.some(other => other.matches('input[type=radio]') && other.checked &&
          ((e.name && other.name === e.name) || (item && component(other)?.closest('[ui5-form-item]') === item))) :
        e.type === 'checkbox' ? e.checked : !!norm(e.value),
      invalid:e.getAttribute('aria-invalid') === 'true' || host?.getAttribute('value-state') === 'Negative' ||
        host?.getAttribute('value-state') === 'Error', options:[]});
  }
  for (const e of nodes.filter(e => e.tagName === 'UI5-SELECT-XWEB-DYNAMIC-CONTENT' && visible(e))) {
    const question = label(e, e), selector = path(e);
    const options = [...e.querySelectorAll('[ui5-option]')].map(o => ({
      text:norm(o.textContent), value:o.value || norm(o.textContent), disabled:!!o.disabled}));
    inputs.push({tag:'select', id:selector, selector, name:'', type:'select-one', form:'careerform',
      label:question, required:!!e.required || /\*\s*$/.test(question), disabled:!!e.disabled,
      value_present:!!norm(e.selectedOption?.textContent || e.value) &&
        !/^(no selection|please select|select an option)$/i.test(norm(e.selectedOption?.textContent || e.value)),
      invalid:['Negative','Error'].includes(e.getAttribute('value-state')), options});
  }
  for (const e of nodes.filter(e => /^UI5-(CHECKBOX|RADIO-BUTTON)-/.test(e.tagName) && visible(e))) {
    const radio = e.tagName.startsWith('UI5-RADIO-BUTTON-');
    const item = e.closest('[ui5-form-item]'), selector = path(e), question = label(e, e);
    const required = !e.disabled && (!!e.required || e.hasAttribute('required') ||
      e.getAttribute('aria-required') === 'true' || !!item?.querySelector('[ui5-label][required]'));
    const peers = radio && item ? nodes.filter(n => n.tagName === e.tagName &&
      n.closest('[ui5-form-item]') === item && visible(n)) : [e];
    inputs.push({tag:'input', id:e.id || selector, selector, name:radio && item ? path(item) : '',
      type:radio ? 'radio' : 'checkbox', label:question, required, form:'careerform',
      disabled:!!e.disabled, value_present:peers.some(n => !!n.checked),
      option_label:radio ? norm(e.getAttribute('text') || e.textContent) : '',
      invalid:['Negative','Error'].includes(e.getAttribute('value-state')), options:[]});
  }
  const buttons = nodes.filter(e => /^UI5-BUTTON-XWEB-/.test(e.tagName) && visible(e)).map(e => ({
    text:norm(e.textContent), id:e.id || path(e), selector:path(e), tag:e.tagName.toLowerCase(),
    type:e.type || 'button', name:'', form:'careerform', disabled:!!e.disabled}));
  const challenge = [...document.querySelectorAll('iframe')].some(e => {
    const r=e.getBoundingClientRect();
    return visible(e) && r.width >= 120 && r.height >= 100 &&
      /recaptcha|hcaptcha|captcha-delivery\.com/i.test(e.src);
  });
  const continuation = buttons.some(b => /^(Next|Next Step|Continue|Review|Submit|Submit Application|Apply)$/i.test(b.text));
  const actionable = !!identity && selected.length === 1 && continuation &&
    (inputs.some(e => !e.disabled) || /^Review/.test(step));
  return {url:location.href, title:document.title, text:form.innerText,
    headings:headings.map(h => h[0]), inputs, buttons, links:[], challenge_visible:challenge,
    application_context:{kind:'successfactors_ui5', tenant:'denodosl', requisition,
      role:headings[0]?.[1] || '', brand_host:brand ? new URL(brand.href).hostname : '',
      form:'careerform', step, actionable, identity_valid:!!identity},
    forms:[{id:'careerform', text:form.innerText}], frame_candidates:[]};
})()"""
