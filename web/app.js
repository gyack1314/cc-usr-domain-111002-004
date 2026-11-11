const $=(s,root=document)=>root.querySelector(s);
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};
const gate=()=>{$('#content').innerHTML='<p data-handoff="required">需要人工确认，当前未读取受限资料</p>';show('请移交工作人员');};

function identity(){$('#identity').textContent=`${data.user} | ${$('#space').value} | ${$('#category').value}`;}
async function refresh(){identity();const state=await runtime();document.body.dataset.space=$('#space').value;document.body.dataset.category=$('#category').value;document.body.dataset.authorized=String(state.confirmed);$('#confirm').hidden=!new URLSearchParams(location.search).has('operator');show(state.confirmed?'授权已完成':'等待人工确认');}

$('#open').onclick=async()=>{
  const state=await runtime();
  if($('#category').value==='sensitive'&&!state.confirmed){gate();return;}
  await action('open-restricted',{space:$('#space').value,category:$('#category').value});
  const docs=data.documents.filter(doc=>doc.category===$('#category').value);
  if(!docs.length){$('#content').innerHTML='<p data-empty="true">该分类暂无可读资料</p>';show('资料已打开');return;}
  $('#content').innerHTML=docs.map(doc=>`<article data-record="${html(doc.id)}" data-space="${html($('#space').value)}" data-version="${html(state.version)}"><h3>${html(doc.title)}</h3><p class="meta">${html(doc.id)} · 版本 ${html(state.version)}</p><button data-open-doc="${html(doc.id)}">读取资料</button><section class="doc-body" hidden></section></article>`).join('');
  show('资料列表已打开');
};

$('#content').addEventListener('click',async(event)=>{
  const button=event.target.closest('button[data-open-doc]');
  if(!button)return;
  const id=button.dataset.openDoc;
  const doc=data.documents.find(item=>item.id===id);
  if(!doc)return;
  const state=await runtime();
  if($('#category').value==='sensitive'&&!state.confirmed){gate();return;}
  await action('open-restricted',{space:$('#space').value,category:$('#category').value,record:id});
  const body=button.closest('article').querySelector('.doc-body');
  body.hidden=false;
  body.innerHTML=`<p class="body">${html(doc.body)}</p><p data-private="true">${html(doc.private_note)}</p>`;
  show(`已读取 ${id}`);
});

$('#confirm').onclick=async()=>{await action('human-confirm',{space:$('#space').value});await refresh();};
$('#check').onclick=refresh;
$('#space').onchange=refresh;
$('#category').onchange=refresh;
await refresh();
