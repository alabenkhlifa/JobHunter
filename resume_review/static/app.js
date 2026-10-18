'use strict';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = value => structuredClone(value);
const pathAttr = path => esc(JSON.stringify(path));
const label = key => ({base:'Shared sidebar',tech:'Technology keywords',company_url:'Company link',display_degree:'Degree display label',display_school:'School display label',additional:'Languages',volunteering:'Teaching & training',certification_links:'Certification links',layout:'Layout',max_pages:'Maximum pages',default_project_ids:'Included projects',flavor_id:'Flavor'}[key] || String(key).replaceAll('_',' ').replace(/^./,c=>c.toUpperCase()));
const at = (object,path) => path.reduce((value,key)=>value[key],object);
const set = (object,path,value) => {at(object,path.slice(0,-1))[path.at(-1)]=value;};
const fieldName = path => path.map(label).join(' / ');
let state, section='flavor', projectId, preview, requestPath=null, createKind, zoom=0;
let dirty=false, generation=0, saveTimer, saving=null, rendering=0, conflict=false, localAction=false;
const detailStates=new Map();

async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Review-Client':'studio'},body:JSON.stringify(body)};
  const response = await fetch(path,options);
  const result = await response.json();
  if(!response.ok) { const error = new Error(result.error || 'The request failed.'); error.status=response.status; throw error; }
  return result;
}
function showError(error) {
  $('#error').hidden=false;
  $('#error').innerHTML=esc(error.message || error)+(error.status===409?' <button class="button small" id="reload-state">Load latest saved review</button>':'');
  if(error.status===409) {conflict=true; $('#save-status').textContent='Conflicting changes';}
}
function clearError(){if(!conflict) $('#error').hidden=true;}
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,3500);}
function flavor(){return state.catalog.flavors.find(x=>x.id===state.selection.flavor_id);}
function variant(){return state.catalog.variants.find(x=>x.id===state.selection.variant_id);}
function field(path,title,{multiline=false,rows=3,hint='',className=''}={}) {
  const idPath=path[0]==='base'&&path[1]==='skills'?[...path.slice(0,2),Object.keys(state.catalog.base.skills).indexOf(path[2]),...path.slice(3)]:path;
  const value=at(state.catalog,path), id='f-'+idPath.join('-').replace(/[^a-z0-9-]/gi,'');
  const input = multiline ? `<textarea id="${id}" aria-label="${esc(title)}" data-path="${pathAttr(path)}" rows="${rows}" class="${className}">${esc(value)}</textarea>` : `<input id="${id}" aria-label="${esc(title)}" data-path="${pathAttr(path)}" value="${esc(value)}">`;
  return `<div class="field"><div class="field-top"><label for="${id}">${esc(title)}</label><button class="request-text" data-request="${pathAttr(path)}" title="Request a change to this text">↗ Request change</button></div>${input}${hint?`<div class="field-footer"><span>${esc(hint)}</span><span data-count-for="${id}">${String(value).length} characters</span></div>`:''}</div>`;
}
function info(title,text){return `<div class="info-card"><span>i</span><div><strong>${esc(title)}</strong><p>${esc(text)}</p></div></div>`;}
function markDirty(){dirty=true;generation++;$('#save-status').textContent='Saving changes…';$('#approve').disabled=true;clearTimeout(saveTimer);saveTimer=setTimeout(()=>save().catch(showError),450);}

async function save(){
  clearTimeout(saveTimer);
  if(conflict) throw Object.assign(new Error('Load the latest saved review before continuing.'),{status:409});
  if(saving){await saving;if(dirty)return save();return;}
  if(!dirty)return;
  const current=generation, body={revision:state.revision,catalog:clone(state.catalog),selection:clone(state.selection)};
  saving=api('/api/save',body).then(saved=>{
    if(generation===current){state=saved;dirty=false;}else{
      state={...saved,catalog:state.catalog,selection:state.selection};
    }
    $('#save-status').textContent=dirty?'Saving changes…':'All changes saved ✓';
    renderNav();
    clearError();
  });
  try{await saving;}catch(error){$('#save-status').textContent='Changes not saved';throw error;}finally{saving=null;}
  if(dirty)return save();
  renderPreview();
}
async function action(path,body){
  localAction=true;
  try{await save();state=await api(path,{revision:state.revision,...body});dirty=false;render();renderPreview();clearError();}
  catch(error){showError(error);throw error;}
  finally{localAction=false;}
}
function renderNav(){
  $('#undo').disabled=!state.undo?.length;
  $('#flavor-list').innerHTML=state.catalog.flavors.map(item=>`<button class="nav-item ${item.id===state.selection.flavor_id?'active':''}" data-flavor="${esc(item.id)}"><span class="item-dot"></span><span>${esc(item.label.replace(' (Senior Backend / Senior Software)',''))}<small>${item.confirmation==='candidate-confirmed'?'Imported approved wording':'Draft wording'}</small></span></button>`).join('');
  const variants=state.catalog.variants.filter(item=>item.flavor_id===state.selection.flavor_id);
  $('#variant-list').innerHTML=variants.map(item=>`<button class="nav-item ${item.id===state.selection.variant_id?'active':''}" data-variant="${esc(item.id)}"><span>▯</span><span>${esc(item.label)}<small>${item.max_pages===1?'One-page limit':'Up to two pages'}</small></span></button>`).join('') || '<p class="note">Add a variant to preview this flavor.</p>';
  document.querySelectorAll('[data-section]').forEach(button=>button.classList.toggle('active',button.dataset.section===section));
  $('#breadcrumb').textContent=flavor()?.label.replace(' (Senior Backend / Senior Software)','').toUpperCase()||'RESUME REVIEW';
  const pending=state.requests.filter(x=>x.status==='open').length+state.proposals.filter(x=>x.status==='pending').length;
  $('#request-count').textContent=pending||'';
  const badge=$('#draft-badge');badge.textContent='Review draft';badge.className='badge';
  if(preview && state.approvals.some(x=>x.digest===preview.content_digest)&&!dirty){badge.textContent='Approved';badge.className='badge approved';}
}
function render(){
  document.querySelectorAll('#editor-content details[data-details-key]').forEach(details=>detailStates.set(details.dataset.detailsKey,details.open));
  const scroll=$('.editor').scrollTop;
  renderNav();
  const titles={flavor:'Headline & summary',variant:'Projects & page limit',projects:'Project wording',sidebar:'Contact & sidebar',changes:'Changes & requests'};
  $('#editor-title').textContent=titles[section];
  $('#editor-content').innerHTML=({flavor:flavorEditor,variant:variantEditor,projects:projectEditor,sidebar:sidebarEditor,changes:changesEditor}[section])();
  document.querySelectorAll('#editor-content details[data-details-key]').forEach(details=>{if(detailStates.has(details.dataset.detailsKey))details.open=detailStates.get(details.dataset.detailsKey);});
  document.querySelectorAll('textarea[data-path]').forEach(textarea=>{if(!textarea.classList.contains('summary')){textarea.style.height='auto';textarea.style.height=Math.min(350,Math.max(76,textarea.scrollHeight+3))+'px';}});
  $('.editor').scrollTop=scroll;
}
function flavorEditor(){
  const current=flavor();if(!current)return '';
  const i=state.catalog.flavors.indexOf(current), p=['flavors',i];
  return `<p class="intro">Set the exact introduction for this role family. It stays fixed across the projects and page limits you choose.</p>`+
    field([...p,'label'],'Flavor name')+field([...p,'headline'],'Resume headline',{multiline:true,rows:2,hint:'The title below your name'})+
    field([...p,'summary'],'Professional summary',{multiline:true,rows:8,className:'summary',hint:'Exact wording, used in every variant of this flavor'})+
    info('One flavor, consistent wording','The preview renders what you write. It does not rewrite the summary, shorten bullets, or add projects.')+
    `<div class="stack-buttons"><button class="button small" data-duplicate="flavor">Duplicate flavor</button>${state.catalog.flavors.length>1?'<button class="button small danger" data-delete="flavor">Delete flavor</button>':''}</div>`;
}
function variantEditor(){
  const current=variant();if(!current)return '<p class="empty">This flavor needs a variant. Use the + beside Variants to create one.</p>';
  const i=state.catalog.variants.indexOf(current),p=['variants',i];
  const ordered=[...current.default_project_ids,...current.project_ids.filter(id=>!current.default_project_ids.includes(id))];
  const list=ordered.map(id=>{
    const project=state.catalog.projects.find(x=>x.id===id),included=state.selection.project_ids.includes(id);
    return `<div class="project-check"><input type="checkbox" id="project-${esc(id)}" data-project-toggle="${esc(id)}" ${included?'checked':''}><label for="project-${esc(id)}">${esc(project.label)}<small>${esc(project.company)} · ${(project.entry?.bullets||project.role.bullets||[]).length} bullets</small></label><div class="order-controls"><button class="icon-button" data-project-order="${esc(id)}" data-direction="-1" aria-label="Move ${esc(project.label)} up">↑</button><button class="icon-button" data-project-order="${esc(id)}" data-direction="1" aria-label="Move ${esc(project.label)} down">↓</button></div></div>`;
  }).join('');
  return `<p class="intro">Choose the full project blocks in this version and their order. The renderer preserves every selected bullet.</p>`+
    field([...p,'label'],'Variant name')+
    `<div class="field"><div class="field-top"><label for="page-limit">Page limit</label></div><select id="page-limit" data-path="${pathAttr([...p,'max_pages'])}" data-number><option value="1" ${current.max_pages===1?'selected':''}>1 page</option><option value="2" ${current.max_pages===2?'selected':''}>Up to 2 pages</option></select></div>`+
    `<div class="subheading"><h3>Included projects</h3><span class="tag">${state.selection.project_ids.length} selected</span></div><div class="card">${list}</div>`+
    info('A page limit is a ceiling','A two-page variant can render as one page. Overflow stays visible with a warning, so you can shorten wording or change the selection yourself.')+
    `<div class="stack-buttons"><button class="button small" data-duplicate="variant">Duplicate variant</button>${state.catalog.variants.length>1?'<button class="button small danger" data-delete="variant">Delete variant</button>':''}</div>`;
}
function projectEditor(){
  if(!state.catalog.projects.some(x=>x.id===projectId)) projectId=state.selection.project_ids[0]||state.catalog.projects[0].id;
  const project=state.catalog.projects.find(x=>x.id===projectId),i=state.catalog.projects.indexOf(project),p=['projects',i];
  const entry=project.entry, bullets=entry?['entry','bullets']:['role','bullets'];
  let html=`<p class="intro">Edit the complete, reusable project block. Changes appear in every variant that includes this project.</p><label for="project-picker">Project</label><select id="project-picker" class="small-select">${state.catalog.projects.map(x=>`<option value="${esc(x.id)}" ${x.id===projectId?'selected':''}>${esc(x.label)} — ${esc(x.company)}</option>`).join('')}</select>`;
  html+=field([...p,'label'],'Project label')+`<details><summary>Employer & role details</summary><div class="detail-body">`+
    field([...p,'company'],'Employer')+Object.keys(project.role).filter(k=>k!=='bullets').map(k=>field([...p,'role',k],label(k))).join('')+
    '<p class="hint">Projects grouped under the same employer must share employer details. Use “Update employer across projects” after editing them.</p><button class="button small" id="sync-employer">Update employer across projects</button></div></details>';
  if(entry)html+=`<details open><summary>Project details</summary><div class="detail-body">${Object.keys(entry).filter(k=>k!=='bullets').map(k=>field([...p,'entry',k],k==='dates'?'Client dates (private chronology; not printed)':label(k))).join('')}</div></details>`;
  html+=`<div class="subheading"><h3>Project bullets</h3><span class="tag">${at(state.catalog,[...p,...bullets]).length} bullets</span></div>`;
  html+=arrayEditor([...p,...bullets],at(state.catalog,[...p,...bullets]),'Bullet');
  html+=`<p class="hint">Use **bold wording** for emphasis. Every bullet stays in the selected project block.</p><div class="stack-buttons"><button class="button small" data-duplicate="project">Duplicate project</button></div>`;
  return html;
}
function arrayEditor(path,items,title){
  return items.map((value,i)=>`<div class="list-item">${typeof value==='string'?field([...path,i],`${title} ${i+1}`,{multiline:true,rows:3}):genericEditor([...path,i],value)}<div class="list-tools"><button data-move="${pathAttr([...path,i])}" data-direction="-1" aria-label="Move ${title} ${i+1} up">↑ Move up</button><button data-move="${pathAttr([...path,i])}" data-direction="1" aria-label="Move ${title} ${i+1} down">↓ Move down</button><button data-remove="${pathAttr([...path,i])}" ${items.length===1?'disabled':''}>Remove</button></div></div>`).join('')+`<button class="button small" data-add="${pathAttr(path)}">＋ Add ${esc(title.toLowerCase())}</button>`;
}
function genericEditor(path,value){
  if(path.length===2&&path[0]==='base'&&path[1]==='skills')return skillsEditor(value);
  if(typeof value==='string')return field(path,label(path.at(-1)),{multiline:value.length>100,rows:3});
  if(typeof value==='boolean')return `<div class="project-check"><input type="checkbox" data-path="${pathAttr(path)}" ${value?'checked':''} id="flag-${esc(path.join('-'))}"><label for="flag-${esc(path.join('-'))}">${esc(label(path.at(-1)))}</label></div>`;
  if(typeof value==='number')return `<div class="field"><label>${esc(label(path.at(-1)))}<input type="number" data-number data-path="${pathAttr(path)}" value="${value}"></label></div>`;
  if(Array.isArray(value))return arrayEditor(path,value,'Entry');
  if(value&&typeof value==='object')return Object.entries(value).map(([key,item])=>{
    if(typeof item==='string'||typeof item==='number'||typeof item==='boolean')return genericEditor([...path,key],item);
    return `<details open data-details-key="${pathAttr([...path,key])}"><summary>${esc(label(key))}</summary><div class="detail-body">${genericEditor([...path,key],item)}</div></details>`;
  }).join('');
  return '';
}
function skillsEditor(skills){
  return Object.entries(skills).map(([heading,items],i)=>`<details open data-details-key="${pathAttr(['base','skills',i])}"><summary>${esc(heading)}</summary><div class="detail-body"><div class="field"><div class="field-top"><label for="skill-heading-${i}">Skill group heading</label></div><input id="skill-heading-${i}" aria-label="Skill group heading ${i+1}" data-skill-heading="${i}" value="${esc(heading)}" required><p class="raw-error" data-heading-error role="alert" hidden></p></div>${arrayEditor(['base','skills',heading],items,'Skill')}</div></details>`).join('');
}
function renameSkillGroup(target){
  const skills=state.catalog.base.skills,entries=Object.entries(skills),i=Number(target.dataset.skillHeading);
  const before=entries[i][0],heading=target.value.trim(),details=target.closest('details'),error=details.querySelector('[data-heading-error]');
  const message=!heading?'Enter a skill group heading.':heading!==before&&Object.hasOwn(skills,heading)?'A skill group already uses this heading.':'';
  target.setCustomValidity(message);error.textContent=message;error.hidden=!message;
  if(message){target.setAttribute('aria-invalid','true');$('#save-status').textContent='Skill group heading not saved';return;}
  target.removeAttribute('aria-invalid');
  if(heading===before){$('#save-status').textContent=dirty?'Saving changes…':'All changes saved ✓';return;}
  state.catalog.base.skills=Object.fromEntries(entries.map(([key,items],index)=>[index===i?heading:key,items]));
  details.querySelector('summary').textContent=heading;
  const attributes=['data-path','data-request','data-move','data-add','data-remove'];
  details.querySelectorAll(attributes.map(attribute=>`[${attribute}]`).join(',')).forEach(element=>{
    for(const attribute of attributes){
      if(!element.hasAttribute(attribute))continue;
      const path=JSON.parse(element.getAttribute(attribute));
      if(path[0]==='base'&&path[1]==='skills'&&path[2]===before){path[2]=heading;element.setAttribute(attribute,JSON.stringify(path));}
    }
  });
  markDirty();
}
function sidebarEditor(){
  const base=state.catalog.base;
  return `<p class="intro">Shared content across every flavor and variant. All contact fields, skills, certifications, links, education and teaching entries are editable here.</p>`+
    Object.entries(base).map(([key,value])=>`<details data-details-key="${pathAttr(['base',key])}" ${['name','email','phone','location'].includes(key)?'open':''}><summary>${esc(label(key))}</summary><div class="detail-body">${genericEditor(['base',key],value)}</div></details>`).join('')+
    `<details data-details-key="sidebar-raw"><summary>Advanced: complete shared content</summary><div class="detail-body"><p class="hint">Add categories, optional fields or entries using the renderer's public JSON structure. Apply explicitly after editing.</p><textarea id="sidebar-raw" aria-label="Shared sidebar JSON">${esc(JSON.stringify(base,null,2))}</textarea><div class="stack-buttons"><button class="button small" id="apply-sidebar-json">Apply shared content</button></div></div></details>`;
}
function differences(oldValue,newValue,path=[]){
  if(JSON.stringify(oldValue)===JSON.stringify(newValue))return [];
  if((oldValue&&typeof oldValue==='object')&&(newValue&&typeof newValue==='object')&&Array.isArray(oldValue)===Array.isArray(newValue)){
    const keys=[...new Set([...Object.keys(oldValue),...Object.keys(newValue)])];
    return keys.flatMap(key=>differences(oldValue[key],newValue[key],[...path,Array.isArray(newValue)?Number(key):key]));
  }
  if(path.at(-1)==='confirmation')return [];
  return [{path,before:oldValue,after:newValue}];
}
function diffBlock(path,before,after){
  const text=x=>typeof x==='object'?JSON.stringify(x,null,2):x??'(empty)';
  return `<div class="diff-block"><div class="diff-title">${esc(fieldName(path))}</div><div class="diff-old">${esc(text(before))}</div><div class="diff-new">${esc(text(after))}</div></div>`;
}
function changesEditor(){
  const diffs=differences(state.baseline,state.catalog);
  let html='<p class="intro">See exactly what changed. Leave a request for the assistant, then accept or reject each proposed replacement.</p>';
  html+=`<div class="subheading"><h3>Text proposals</h3><span class="tag">${state.proposals.filter(x=>x.status==='pending').length} pending</span></div>`;
  html+=state.proposals.length?[...state.proposals].reverse().map(item=>`<div class="card"><div class="card-top"><span class="badge ${item.status==='accepted'?'approved':''}">${esc(item.status)}</span><span class="timestamp">${new Date(item.at).toLocaleString()}</span></div>${diffBlock(item.path,item.before,item.after)}<p class="hint">${esc(item.rationale)}</p>${item.status==='pending'?`<div class="stack-buttons"><button class="button small primary" data-proposal="${esc(item.id)}" data-decision="accept">Accept replacement</button><button class="button small" data-proposal="${esc(item.id)}" data-decision="reject">Reject</button></div>`:''}</div>`).join(''):'<div class="empty">Proposed text from the assistant will appear here. Each proposal shows the current and replacement wording.</div>';
  html+=`<div class="subheading"><h3>Your change requests</h3><button class="button small" id="request-from-changes">＋ Add request</button></div>`;
  html+=state.requests.length?[...state.requests].reverse().map(item=>`<div class="card"><div class="card-top"><span class="badge ${item.status==='resolved'?'approved':''}">${esc(item.status)}</span><span class="timestamp">${new Date(item.at).toLocaleString()}</span></div><div class="eyebrow">${item.path?esc(fieldName(item.path)):'GENERAL FEEDBACK'}</div><p class="request-message">${esc(item.message)}</p>${item.before?`<details><summary>Requested text</summary><div class="detail-body hint">${esc(item.before)}</div></details>`:''}</div>`).join(''):'<div class="empty">Use “Request change” beside any text field to attach feedback to that exact wording.</div>';
  html+=`<div class="subheading"><h3>Changes from original</h3><span class="tag">${diffs.length}</span></div>`+ (diffs.length?diffs.map(d=>diffBlock(d.path,d.before,d.after)).join(''):'<div class="empty">Your review still matches the original catalog.</div>');
  html+=`<div class="subheading"><h3>Saved approvals</h3><span class="tag">${state.approvals.length}</span></div>`+(state.approvals.length?[...state.approvals].reverse().map(a=>`<div class="history-item">${esc(state.catalog.flavors.find(f=>f.id===a.selection.flavor_id)?.label||a.selection.flavor_id)} · ${esc(state.catalog.variants.find(v=>v.id===a.selection.variant_id)?.label||a.selection.variant_id)}<time>${new Date(a.at).toLocaleString()} · ${a.selection.project_ids.length} projects · exact version recorded</time></div>`).join(''):'<p class="hint">No versions approved in this workspace yet.</p>');
  html+='<div class="subheading"><h3>Activity</h3></div>'+[...state.history].reverse().slice(0,15).map(item=>`<div class="history-item">${esc(item.action)}<time>${new Date(item.at).toLocaleString()} · revision ${item.revision}</time></div>`).join('');
  return html;
}

async function renderPreview(){
  const ticket=++rendering;
  $('#render-status').textContent='Rendering…';$('#approve').disabled=true;
  try{
    const result=await api('/api/preview');
    if(ticket!==rendering||dirty||result.revision!==state.revision)return;
    preview=result;
    if($('#pages').dataset.preview!==result.id){
      const scroll=$('#pages').scrollTop;
      $('#pages').innerHTML=Array.from({length:result.pages},(_,i)=>`<div class="page"><img src="/previews/${result.id}/page-${i+1}.png" alt="Resume preview, page ${i+1}" width="1013" height="1433"><div class="page-caption"><span>A4 · ACTUAL PDF OUTPUT</span><span>PAGE ${i+1} OF ${result.pages}</span></div></div>`).join('');
      $('#pages').dataset.preview=result.id;$('#pages').scrollTop=scroll;applyZoom();
    }
    $('#page-info').textContent=`${result.pages} ${result.pages===1?'page':'pages'} / ${variant()?.max_pages||'–'} maximum · ${state.selection.project_ids.length} projects`;
    $('#render-status').textContent=result.audit_ok?'Preview up to date ✓':'Needs attention';
    $('#preview-warnings').hidden=!result.warnings.length;
    $('#preview-warnings').innerHTML=result.warnings.map(esc).join('<br>');
    $('#download').href=`/previews/${result.id}/resume.pdf`;$('#download').classList.remove('disabled');
    const approved=state.approvals.some(x=>x.digest===result.content_digest);
    $('#approval-label').textContent=approved?'This exact version is approved':'Review this version';
    $('#approval-detail').textContent=approved?'Further edits create a new review version.':'Approval is saved for this exact text and selection.';
    $('#approve').textContent=approved?'Approved ✓':'Approve version ✓';
    $('#approve').disabled=approved||result.warnings.length>0||!result.audit_ok;
    renderNav();
  }catch(error){
    if(ticket!==rendering)return;
    $('#render-status').textContent='Preview needs attention';$('#preview-warnings').hidden=false;$('#preview-warnings').textContent=error.message;
    $('#download').classList.add('disabled');$('#approve').disabled=true;
    if(!preview)$('#pages').innerHTML='<div class="preview-placeholder"><p>Complete this draft to see its preview.</p><small>The message above explains what is missing. All saved wording is preserved.</small></div>';
  }
}
function applyZoom(){document.querySelectorAll('.page').forEach(page=>{page.style.width=zoom?`${zoom}%`:'100%';page.style.maxWidth=zoom?'none':'';page.style.alignSelf=zoom>100?'flex-start':'';});$('#zoom-label').textContent=zoom?`${zoom}%`:'Fit';}
function openRequest(path=null){
  requestPath=path;$('#request-target').textContent=path?fieldName(path):'General feedback on this resume.';
  $('#request-before').hidden=!path;$('#request-before').textContent=path?at(state.catalog,path):'';
  $('#request-message').value='';$('#request-dialog').showModal();$('#request-message').focus();
}
function slug(value){return value.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'')||'draft';}
function uniqueId(name,items){let id=slug(name),n=2;while(items.some(x=>x.id===id))id=slug(name)+'-'+n++;return id;}
function openCreate(kind){
  createKind=kind;$('#create-title').textContent=kind==='flavor'?'New flavor':'New variant';$('#create-name').value='';
  $('#create-hint').textContent=kind==='flavor'?'Starts with an empty headline and summary, plus one- and two-page variants. The current project selection is copied.':'Copies the current project selection and page limit. All wording stays shared with its flavor.';
  $('#create-dialog').showModal();$('#create-name').focus();
}
function switchFlavor(id){
  state.selection.flavor_id=id;
  const v=state.catalog.variants.find(x=>x.flavor_id===id);
  state.selection.variant_id=v?.id||'';state.selection.project_ids=v?.default_project_ids.slice()||[];
  preview=null;$('#pages').dataset.preview='';section='flavor';markDirty();render();
}
function duplicate(kind){
  if(kind==='flavor'){
    const item=clone(flavor()),old=item.id;item.label+=' (copy)';item.id=uniqueId(item.label,state.catalog.flavors);item.confirmation='draft';state.catalog.flavors.push(item);
    const variants=state.catalog.variants.filter(x=>x.flavor_id===old).map(x=>{const v=clone(x);v.flavor_id=item.id;v.id=uniqueId(item.id+'-'+v.label,state.catalog.variants);v.confirmation='draft';return v;});
    state.catalog.variants.push(...variants);switchFlavor(item.id);
  }else if(kind==='variant'){
    const item=clone(variant());item.label+=' (copy)';item.id=uniqueId(item.label,state.catalog.variants);item.confirmation='draft';state.catalog.variants.push(item);state.selection.variant_id=item.id;markDirty();render();
  }else{
    const item=clone(state.catalog.projects.find(x=>x.id===projectId));item.label+=' (copy)';item.id=uniqueId(item.label,state.catalog.projects);item.confirmation='draft';state.catalog.projects.push(item);
    for(const v of state.catalog.variants)v.project_ids.push(item.id);
    projectId=item.id;markDirty();render();
  }
}

document.addEventListener('input',event=>{
  const target=event.target;
  if(target.hasAttribute('data-skill-heading')){renameSkillGroup(target);return;}
  if(!target.dataset.path)return;
  const path=JSON.parse(target.dataset.path);
  set(state.catalog,path,target.type==='checkbox'?target.checked:target.hasAttribute('data-number')?Number(target.value):target.value);
  const counter=document.querySelector(`[data-count-for="${target.id}"]`);if(counter)counter.textContent=target.value.length+' characters';
  markDirty();
});
document.addEventListener('change',event=>{
  const target=event.target;
  if(target.hasAttribute('data-skill-heading')){target.value=target.value.trim();renameSkillGroup(target);}
  if(target.id==='project-picker'){projectId=target.value;render();}
  if(target.dataset.projectToggle){
    const id=target.dataset.projectToggle;
    if(target.checked)state.selection.project_ids.push(id);else state.selection.project_ids=state.selection.project_ids.filter(x=>x!==id);
    if(!state.selection.project_ids.length){state.selection.project_ids=[id];target.checked=true;toast('Keep at least one selected project.');return;}
    variant().default_project_ids=state.selection.project_ids.slice();markDirty();render();
  }
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.dataset.section){section=button.dataset.section;render();}
    if(button.dataset.flavor){await save();switchFlavor(button.dataset.flavor);}
    if(button.dataset.variant){await save();const item=state.catalog.variants.find(x=>x.id===button.dataset.variant);state.selection.variant_id=item.id;state.selection.project_ids=item.default_project_ids.slice();preview=null;markDirty();render();}
    if(button.dataset.request)openRequest(JSON.parse(button.dataset.request));
    if(button.hasAttribute('data-close'))button.closest('dialog').close();
    if(button.dataset.duplicate)duplicate(button.dataset.duplicate);
    if(button.dataset.delete){
      const kind=button.dataset.delete;
      if(kind==='flavor'){
        const id=flavor().id;
        if(!state.catalog.variants.some(x=>x.flavor_id!==id)){toast('Create a variant for another flavor before deleting this one.');return;}
        state.catalog.flavors=state.catalog.flavors.filter(x=>x.id!==id);state.catalog.variants=state.catalog.variants.filter(x=>x.flavor_id!==id);switchFlavor(state.catalog.flavors[0].id);
      }else{const id=variant().id;const siblings=state.catalog.variants.filter(x=>x.flavor_id===flavor().id&&x.id!==id);if(!siblings.length){toast('Keep at least one variant for this flavor.');return;}state.catalog.variants=state.catalog.variants.filter(x=>x.id!==id);state.selection.variant_id=siblings[0].id;state.selection.project_ids=siblings[0].default_project_ids.slice();markDirty();render();}
    }
    if(button.dataset.move){const path=JSON.parse(button.dataset.move),items=at(state.catalog,path.slice(0,-1)),i=path.at(-1),j=i+Number(button.dataset.direction);if(j>=0&&j<items.length){[items[i],items[j]]=[items[j],items[i]];markDirty();render();}}
    if(button.dataset.projectOrder){const items=state.selection.project_ids,i=items.indexOf(button.dataset.projectOrder),j=i+Number(button.dataset.direction);if(i>=0&&j>=0&&j<items.length){[items[i],items[j]]=[items[j],items[i]];variant().default_project_ids=items.slice();markDirty();render();}}
    if(button.dataset.remove){const path=JSON.parse(button.dataset.remove);at(state.catalog,path.slice(0,-1)).splice(path.at(-1),1);markDirty();render();}
    if(button.dataset.add){const path=JSON.parse(button.dataset.add),items=at(state.catalog,path);const old=items.at(-1);items.push(typeof old==='string'?'New entry — edit this text':clone(old||{}));markDirty();render();}
    if(button.dataset.proposal){await action('/api/proposals/decide',{id:button.dataset.proposal,decision:button.dataset.decision});toast(button.dataset.decision==='accept'?'Replacement applied. Review the updated preview.':'Proposal rejected.');}
    if(button.id==='reload-state'){state=await api('/api/state');dirty=false;conflict=false;$('#error').hidden=true;render();renderPreview();}
    if(button.id==='request-from-changes'||button.id==='new-request')openRequest();
    if(button.id==='add-flavor')openCreate('flavor');if(button.id==='add-variant')openCreate('variant');
    if(button.id==='zoom-in'){zoom=Math.min(170,(zoom||100)+15);applyZoom();}
    if(button.id==='zoom-out'){zoom=Math.max(55,(zoom||100)-15);applyZoom();}
    if(button.id==='apply-sidebar-json'){const base=JSON.parse($('#sidebar-raw').value);state.catalog.base=base;markDirty();await save();render();toast('Shared content updated.');}
    if(button.id==='sync-employer'){
      const project=state.catalog.projects.find(x=>x.id===projectId);
      for(const other of state.catalog.projects.filter(x=>x.company===project.company)){const bullets=other.role.bullets;other.role=clone(project.role);if(bullets)other.role.bullets=bullets;else delete other.role.bullets;}
      markDirty();render();toast('Employer details updated across matching projects.');
    }
    if(button.id==='approve'){
      await save();if(!preview||preview.revision!==state.revision){await renderPreview();}
      $('#approve-description').textContent=`${flavor().label} · ${variant().label} · ${state.selection.project_ids.length} projects · ${preview.pages} rendered ${preview.pages===1?'page':'pages'}.`;
      $('#approve-dialog').showModal();
    }
    if(button.id==='export'){
      await save();const blob=new Blob([JSON.stringify(state,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='resume-review.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    if(button.id==='undo'){await action('/api/undo',{});toast('Last saved edit restored.');}
  }catch(error){showError(error);}
});
$('#request-form').addEventListener('submit',async event=>{
  event.preventDefault();try{await action('/api/requests',{path:requestPath,message:$('#request-message').value});$('#request-dialog').close();toast('Request saved for the assistant to read.');}catch(_){}
});
$('#create-form').addEventListener('submit',event=>{
  event.preventDefault();const name=$('#create-name').value.trim();if(!name)return;
  if(createKind==='flavor'){
    const id=uniqueId(name,state.catalog.flavors),projects=state.selection.project_ids.length?state.selection.project_ids.slice():state.catalog.projects.map(x=>x.id);
    state.catalog.flavors.push({id,label:name,confirmation:'draft',headline:'',summary:''});
    for(const pages of [1,2])state.catalog.variants.push({id:uniqueId(id+'-'+pages+'-pages',state.catalog.variants),label:pages===1?'1 page':'Up to 2 pages',flavor_id:id,confirmation:'draft',max_pages:pages,project_ids:state.catalog.projects.map(x=>x.id),default_project_ids:projects.slice()});
    switchFlavor(id);
  }else{
    const existing=variant(),id=uniqueId(name,state.catalog.variants);
    state.catalog.variants.push({id,label:name,flavor_id:flavor().id,confirmation:'draft',max_pages:existing?.max_pages||1,project_ids:state.catalog.projects.map(x=>x.id),default_project_ids:state.selection.project_ids.length?state.selection.project_ids.slice():state.catalog.projects.map(x=>x.id)});
    state.selection.variant_id=id;state.selection.project_ids=state.catalog.variants.at(-1).default_project_ids.slice();section='variant';markDirty();render();
  }
  $('#create-dialog').close();
});
$('#approve-form').addEventListener('submit',async event=>{
  event.preventDefault();try{await action('/api/approve',{content_digest:preview?.content_digest});$('#approve-dialog').close();toast('Exact version approved in this workspace.');}catch(_){}
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
async function poll(){
  if(!state||dirty||saving||localAction||conflict)return;
  try{
    const remote=await api('/api/state');
    if(remote.revision>state.revision&&!dirty&&!saving&&!localAction){
      state=remote;render();renderPreview();$('#save-status').textContent='New review update received ✓';toast('The review has an update.');
    }
  }catch(_){$('#save-status').textContent='Reconnecting…';}
}
async function start(){
  try{state=await api('/api/state');render();$('#save-status').textContent='All changes saved ✓';await renderPreview();setInterval(poll,2500);}catch(error){showError(error);$('#save-status').textContent='Connection failed';}
}
start();
