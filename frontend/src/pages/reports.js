import {jobs,reports,storage} from '../api/entities.js';
import {download} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {DeliveryStatus,ReclaimReason,formatBytes} from '../constants/enums.js';

const isAdmin=()=>session.user.role==='admin';

function storagePanel(stats){
 return '<section class="panel"><div class="section-title"><h2>归档容量上限</h2><span>按两份附件实际字节计费</span></div>'
 +'<div class="capacity"><div><span>已用容量</span><strong>'+formatBytes(stats.used_bytes)+'</strong></div>'
 +'<div><span>容量上限</span><strong>'+formatBytes(stats.capacity_limit_bytes)+'</strong></div>'
 +'<div><span>剩余可用</span><strong class="'+(stats.over_limit?'over':'')+'">'+formatBytes(stats.available_bytes)+(stats.over_limit?'（已超限）':'')+'</strong></div>'
 +'<div><span>附件登记</span><strong>'+stats.attachment_count+' 份</strong></div></div>'
 +'<form id="limit-form"><label>容量上限（字节，仅负责人可改）<input name="capacity_limit_bytes" type="number" min="1" step="1" required value="'+stats.capacity_limit_bytes+'" '+(isAdmin()?'':'disabled')+'></label>'
 +'<button type="submit" class="primary" '+(isAdmin()?'':'disabled')+'>保存上限</button></form>'
 +'<p class="hint">调低上限不会移除任何保留项，也不会立即回收；空间在下一次写入不足时按最早未保留归档回收，仍不足则排队。单份附件超过上限会被明确拒绝。</p>'
 +'<div class="status-counts">'+Object.entries(stats.deliveries).map(([k,v])=>'<span><b>'+v+'</b> '+esc(DeliveryStatus[k]||k)+'</span>').join('')+'</div></section>';
}

function deliveryCard(d){
 const visible=d.visible;
 const canAct=isAdmin();
 const actions=[];
 if(visible){
  actions.push('<button class="text-button" data-download="'+d.id+'" data-format="csv">CSV ↓</button>');
  actions.push('<button class="text-button" data-download="'+d.id+'" data-format="json">JSON ↓</button>');
  if(isAdmin())actions.push(d.retained
    ?'<button class="text-button" data-release="'+d.id+'">解除保留</button>'
    :'<button class="text-button" data-retain="'+d.id+'">保留</button>');
 }else if(d.status==='queued'){
  if(isAdmin())actions.push('<button class="text-button" data-retry="'+d.id+'">立即重试</button>');
 }
 const cls='delivery status-'+d.status+(d.retained?' is-retained':'');
 return '<div class="'+cls+'"><div><strong>'+esc(d.filename||('归档 #'+d.id))+'</strong>'
 +'<p>'+esc(d.destination)+' · '+date(d.created_at)+'</p>'
 +'<small>'+esc(DeliveryStatus[d.status]||d.status)+' · '+formatBytes(d.size_bytes)
 +(d.attachment_kinds.length?' · 附件 '+d.attachment_kinds.join(' / '):'')+' · 尝试 '+d.attempts+' 次</small>'
 +(d.note?'<p class="queue-note">'+esc(d.note)+'</p>':'')+'</div>'
 +'<div class="delivery-actions">'+actions.join('')+'</div></div>';
}

function reclaimsPanel(records){
 return '<section class="panel"><div class="section-title"><h2>回收记录</h2><span>'+records.length+' 条（含原因与释放字节）</span></div>'
 +(records.length?'<div class="reclaim-list">'+records.map(r=>'<div class="reclaim-row"><div><strong>归档 #'+r.delivery_id+'</strong><p>'+esc(ReclaimReason[r.reason]||r.reason)+(r.removed_kinds&&r.removed_kinds.length?' · 移除 '+r.removed_kinds.join(' / '):'')+'</p>'+(r.detail?'<small>'+esc(r.detail)+'</small>':'')+'</div><div class="reclaim-meta"><strong>'+formatBytes(r.released_bytes)+'</strong><span>'+date(r.created_at)+'</span></div></div>').join('')+'</div>'
 :'<p class="empty">暂无回收记录。</p>')+'</section>';
}

export async function reportPage(){
 const [list,deliveries,allJobs,stats,reclaims]=await Promise.all([
  reports.list(),reports.deliveries(),jobs.list(),storage.stats(),storage.reclaims()]);
 workspace.reports=list;
 const approved=allJobs.filter(j=>j.approved&&j.status==='completed');
 return heading('05 / DISTRIBUTION','定时分发与归档','把批准后的结果交付到本地业务收件箱。容量按 CSV 与 JSON 两份附件的实际字节计费；空间不足先回收最早未保留、未在下载的归档，仍不足则排队。','<button class="button" id="refresh-reports">刷新收件箱 ↻</button>')
 +'<div class="report-grid"><section class="panel"><div class="section-title"><h2>创建分发计划</h2></div><form id="report-form"><label>简报名称<input name="name" required minlength="2" value="日间业务复核简报"></label><label>已批准分析<select name="job_id" required>'+approved.map(j=>'<option value="'+j.id+'">'+esc(j.name)+'</option>').join('')+'</select></label><label>业务接收组<input name="destination" value="业务复核组 · 本地收件箱" required></label><div class="parameter-grid"><label>分发周期<select name="interval_seconds"><option value="86400">每日</option><option value="604800">每周</option><option value="60">每分钟（验收）</option></select></label><label>首次执行<select name="delay_seconds"><option value="0">立即</option><option value="60">1 分钟后</option><option value="3600">1 小时后</option></select></label></div><button type="submit" class="primary" '+(isAdmin()&&approved.length?'':'disabled')+'>保存分发计划</button></form><p class="hint">由后台 worker 按到期时间生成文件。仅负责人可创建或暂停；接收组为本地标签。</p></section>'
 +storagePanel(stats)+'</div>'
 +'<div class="report-grid secondary"><section><div class="section-title"><h2>运行中的计划</h2><span>'+list.filter(r=>r.enabled).length+' 条已启用</span></div><div class="schedule-list">'+list.map(r=>'<article><div><strong>'+esc(r.name)+'</strong><p>'+esc(r.destination)+'</p><small>下次 '+date(r.next_run)+' · 周期 '+r.interval_seconds+' 秒</small></div><button class="button small" data-toggle="'+r.id+'" '+(isAdmin()?'':'disabled')+'>'+(r.enabled?'暂停':'启用')+'</button></article>').join('')+'</div></section>'
 +'<section class="panel inbox"><div class="section-title"><h2>本地收件箱与归档</h2><span>'+deliveries.length+' 条记录</span></div>'+deliveries.map(deliveryCard).join('')+(!deliveries.length?'<p class="empty">计划执行后，文件会出现在此处。等待片刻后刷新。</p>':'')+'</section></div>'
 +reclaimsPanel(reclaims);
}

export function bindReports(refresh){
 document.querySelector('#refresh-reports').onclick=refresh;
 submit(document.querySelector('#report-form'),async f=>{
  await reports.create({name:f.get('name'),job_id:Number(f.get('job_id')),destination:f.get('destination'),interval_seconds:Number(f.get('interval_seconds')),delay_seconds:Number(f.get('delay_seconds'))});
  notify('计划已保存，后台将按到期时间归档');await refresh();
 });
 const limitForm=document.querySelector('#limit-form');
 if(limitForm)submit(limitForm,async f=>{
  await storage.setLimit(Number(f.get('capacity_limit_bytes')));
  notify('容量上限已更新（保留项不受影响）');await refresh();
 });
 document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{try{await reports.toggle(b.dataset.toggle);await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-download]').forEach(b=>b.onclick=()=>download(b.dataset.download,b.dataset.format).catch(e=>notify(e.message,true)));
 document.querySelectorAll('[data-retain]').forEach(b=>b.onclick=async()=>{try{await storage.retain(b.dataset.retain);notify('已保留，回收时将跳过该归档');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-release]').forEach(b=>b.onclick=async()=>{try{await storage.release(b.dataset.release);notify('已解除保留');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=async()=>{try{const d=await storage.retry(b.dataset.retry);notify(d.visible?'重试成功，归档已进入收件箱':'仍在排队：'+(d.note||''));await refresh();}catch(e){notify(e.message,true);}});
}
