const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

function identity(){$('#identity').textContent=`demo-user | ${$('#space').value} | ${$('#category').value}`;}
async function refresh(){identity();const state=await runtime();document.body.dataset.space=$('#space').value;document.body.dataset.category=$('#category').value;document.body.dataset.authorized=String(state.confirmed);$('#confirm').hidden=!new URLSearchParams(location.search).has('operator');show(state.confirmed?'授权已完成':'等待人工确认');}
$('#open').onclick=async()=>{const state=await runtime();if($('#category').value==='sensitive'&&!state.confirmed){$('#content').innerHTML='<p data-handoff="required">需要人工确认，当前未读取受限资料</p>';show('请移交工作人员');return;}await action('open-restricted',{space:$('#space').value,category:$('#category').value});$('#content').innerHTML=`<article data-record="DOC-17" data-space="${$('#space').value}" data-version="1">已授权资料 DOC-17</article>`;show('资料已打开');};
$('#confirm').onclick=async()=>{await action('human-confirm',{space:$('#space').value});await refresh();};$('#check').onclick=refresh;$('#space').onchange=refresh;$('#category').onchange=refresh;await refresh();
