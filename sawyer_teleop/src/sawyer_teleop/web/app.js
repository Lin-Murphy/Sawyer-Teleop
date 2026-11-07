import {feedbackMetric} from './robot.js';
const $=id=>document.getElementById(id), text=(id,value)=>$(id).textContent=value;
const client=crypto.randomUUID(), storageKey='sawyer-web-token:'+location.port;
let token=new URLSearchParams(location.hash.slice(1)).get('token')||sessionStorage.getItem(storageKey)||'';
if(token){sessionStorage.setItem(storageKey,token);history.replaceState(null,'',location.pathname);}
let sequence=0,last=null,lastOK=0,initialized=false,events=[],eventSeq=0,busy=0,lastBatch='',lastProfileRequest=null;
let frozen=false,networkOK=false;
function notice(message){text('notice',message||'');$('notice').hidden=!message;}
async function api(path,body,timeout=2500){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try{const response=await fetch(path,{method:body?'POST':'GET',headers:{'X-Sawyer-Token':token,'X-Sawyer-Client':client,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,signal:controller.signal,cache:'no-store'});const data=await response.json();if(!response.ok||data.ok===false)throw new Error(data.error||'Request failed');return data;}
  finally{clearTimeout(timer);}
}
async function claim(){try{await api('/api/claim',{});sequence=0;notice('');}catch(e){notice(e.message);}}
async function command(action,options={}){
  if(!last?.owner||!networkOK){notice('Acquire the controls on a connected page first.');return;}
  busy++;render();
  try{await api('/api/action',{seq:++sequence,action,options});notice('');}
  catch(e){notice(e.name==='AbortError'?'Request timed out. Check the session status before retrying; it may already have been accepted.':e.message);}
  finally{busy--;render();}
}
function settings(){return {backend:$('backend').value,host:$('host').value.trim(),serial:$('serial').value.trim(),smoothing:$('smoothing').checked,smoothing_ms:Number($('smoothingMs').value),profile:$('profile').value,joint_protection:$('protection').checked,cameras:$('cameras').checked};}
function recordOptions(){return {name:$('batchName').value,task:$('task').value,root:$('output').value,count:Number($('count').value),seconds:Number($('seconds').value),pause:Number($('pause').value),reset_between:$('resetBetween').checked};}
function fillDefaults(d){$('backend').value=d.backend;$('host').value=d.host;$('serial').value=d.serial;$('smoothing').checked=d.smoothing;if(![...$('smoothingMs').options].some(o=>Number(o.value)===d.smoothing_ms))$('smoothingMs').add(new Option('Custom · '+d.smoothing_ms+' ms',d.smoothing_ms));$('smoothingMs').value=String(d.smoothing_ms);$('profile').value=d.profile;$('protection').checked=d.joint_protection;$('cameras').checked=d.cameras;$('output').value=d.output;}
function optionsList(id,values){const signature=JSON.stringify(values);if($(id).dataset.signature===signature)return;$(id).dataset.signature=signature;$(id).replaceChildren(...values.map(value=>new Option(value,value)));}
function fmt(value,digits=1){return typeof value==='number'&&Number.isFinite(value)?value.toFixed(digits):'—';}
function flag(id,title,value,yes,no){text(id,title+': '+(value==null?'—':value?yes:no));$(id).classList.toggle('bad',value===false);}
function profileReason(p){const backend=$('backend').value;if(p.value==='fast_follow')return backend==='intera'?null:backend!=='gazebo'?'Fast follow requires Gazebo or Real Sawyer.':!last?.defaults.fast_available?'No Gazebo acceleration override is configured.':null;if(p.value.startsWith('intera_cartesian')||p.value==='intera_pose_50hz'){if(backend!=='intera'&&backend!=='gazebo')return 'Requires the native or simulated Intera Motion Controller.';return p.reason;}return null;}
function render(){
  if(!last)return;
  const s=last.state||{},localFresh=networkOK&&performance.now()-lastOK<1500,own=last.owner&&localFresh;
  const connected=last.phase==='connected'&&s.robot_connected&&localFresh,transition=['connecting','disconnecting'].includes(last.phase),idle=last.phase==='disconnected'||(last.phase==='error'&&!last.has_session);
  const phase=s.recording_phase||'idle',recordBusy=s.auto_recording||['recording','review','finishing','countdown'].includes(phase),recordIdle=['idle','complete'].includes(phase),requestBusy=busy>0;
  text('ownerLabel',!localFresh?'Local service unavailable':own?'You have the controls':last.occupied?'Read-only · another page has the controls':'Read-only · controls available');$('claim').hidden=own||!localFresh;$('claim').disabled=last.occupied;
  text('connectionBadge',!localFresh?'Local service disconnected':transition?last.phase==='connecting'?'Connecting…':'Disconnecting…':connected?'B connected':last.phase==='error'?'Session error':'B disconnected');$('connectionBadge').dataset.status=!localFresh?'error':last.phase;
  text('summary',localFresh?last.message:'Connection to the local service was interrupted. Control will not resume automatically.');
  text('usb','Quest USB: '+(last.usb||'Unknown'));
  $('connect').textContent=transition?last.phase==='connecting'?'Connecting…':'Disconnecting…':idle?'Connect':'Disconnect';$('connect').disabled=!own||transition||requestBusy||(!idle&&recordBusy);
  $('stopSession').disabled=!own||idle||last.phase==='disconnecting';
  document.querySelectorAll('[data-connection]').forEach(e=>e.disabled=!own||!idle||requestBusy);
  $('smoothingMs').disabled=$('smoothingMs').disabled||!$('smoothing').checked;
  $('scan').disabled=!own||last.scanning||!idle||requestBusy;$('scan').textContent=last.scanning?'Finding…':'Find';
  optionsList('hostHistory',last.defaults.history||[]);optionsList('deviceList',last.devices||[]);
  const readyMotion=idle||(connected&&s.mode==='paused'&&!s.following&&!s.resetting&&!recordBusy&&!s.native_stopping&&!s.native_stop_failed);
  $('profile').disabled=$('protection').disabled=!own||!readyMotion||transition||requestBusy||last.pending_profile!=null;
  for(const p of last.profiles){const o=[...$('profile').options].find(o=>o.value===p.value);if(o){o.disabled=!!profileReason(p);o.title=profileReason(p)||p.description;}}
  const selected=last.profiles.find(p=>p.value===$('profile').value),reason=selected?profileReason(selected):'Select a motion option.';
  $('apply').disabled=$('profile').disabled||!!reason;
  const applied=last.profiles.find(p=>p.value===s.motion_profile)?.label;
  text('profileNote',(last.pending_profile!=null?'Applying… ':connected?'Active: '+(applied||'Standard')+'. ':'Next connection. ')+(reason||selected?.description||'')+(connected?(s.native_stop_failed?' Locked: B could not confirm a native STOP; restart B to unlock.':' Release grip and end recording before Apply.'):''));
  text('backendLabel',s.backend||'Not connected');
  const timing=s.feedback_diagnostics||{};
  const feedbackFresh=localFresh&&s.feedback_valid!==false&&(typeof s.feedback_age_ms!=='number'||s.feedback_age_ms+performance.now()-lastOK<(timing.a_limit_ms||100));
  ['inputHz','targetHz','rtt','submissionHz','feedbackHz'].forEach((id,i)=>text(id,localFresh?fmt([s.input_rate?.hz,s.target_rate?.hz,s.rtt_ms,s.target_rate?.submission_hz,s.target_rate?.feedback_hz][i]):'—'));
  const feedback=feedbackMetric(s,last.phase,localFresh);text('feedbackAge',feedback.value);text('feedbackUnit',feedback.unit);$('feedbackAge').title=feedback.detail;$('feedbackUnit').title=feedback.detail;
  $('feedbackAge').classList.toggle('bad',!feedbackFresh||!!timing.fault);
  const q=localFresh?s.quest||{}:{};
  text('stream','Quest data: '+(q.fresh?'Receiving':q.received_ns?'Interrupted':'Waiting'));
  flag('headApp','App',q.focused,'Foreground','Background');flag('headMount','Worn',q.mounted,'Yes','No');flag('headTracking','Head',q.head_tracked,'Tracked','Lost');
  for(const [side,id,title]of [['l','leftHand','Left'],['r','rightHand','Right']]){const hand=q.hands?.[side]||{};text(id,title+': '+(!q.fresh?'Waiting / interrupted':!hand.tracked?'Not tracked':!hand.input?'Buttons unavailable':'Tracking'));$(id).classList.toggle('bad',!!q.received_ns&&(!q.fresh||!hand.tracked||!hand.input));}
  const calibrated=connected&&s.calibrated&&q.valid&&!s.resetting;
  text('calibration',calibrated?'Calibrated':s.calibration_result?.startsWith('Calibration failed')?s.calibration_result:'Not calibrated');$('calibration').title=s.calibration_result||'';
  $('calibrate').disabled=!own||!connected||requestBusy||s.resetting||s.following;
  $('resetArm').textContent=s.resetting?'Stop reset':s.backend==='gazebo'?'Reset arm & scene':'Reset arm';$('resetArm').disabled=!own||!connected||(!s.resetting&&(!s.reset_available||recordBusy))||requestBusy;$('resetArm').title=s.reset_available?'Return to the official neutral pose.':s.reset_unavailable_reason||'Connect B first.';
  const batch=s.batch||{},batchKey=JSON.stringify(batch);
  if(batch.name&&batchKey!==lastBatch){$('batchName').value=batch.name;lastBatch=batchKey;}
  document.querySelectorAll('[data-record]').forEach(e=>e.disabled=!own||!recordIdle||requestBusy);
  const names={idle:'Not recording',recording:'● Recording',review:'Ended · save or discard',finishing:'Processing data…',countdown:'Preparing next episode',resetting:'Returning arm to Reset pose',complete:'Batch complete'};
  text('recordPhase',names[phase]||phase);$('recordPhase').parentElement.dataset.phase=phase;
  const seconds=batch.seconds||Number($('seconds').value),delay=s.ready_in_s||0,countdown=s.auto_recording&&['countdown','finishing'].includes(phase);
  text('recordTime',phase==='resetting'?'Resetting arm…':countdown?'Next in '+fmt(delay)+' s':fmt(s.elapsed_s||0)+' / '+fmt(seconds)+' s');
  $('progress').max=countdown?(batch.pause||1):seconds;$('progress').value=countdown?Math.max(0,(batch.pause||0)-delay):Math.min(s.elapsed_s||0,seconds);
  text('counts',`Saved ${s.saved_count||0} / ${batch.count||$('count').value} · Discarded ${s.discarded_count||0} · Total ${fmt(s.saved_seconds||0)} s`);
  $('recordToggle').textContent=phase==='recording'||s.auto_recording?'Save & End':'Start Recording';
  const canStart=recordIdle&&calibrated&&!delay&&!(phase==='complete'&&$('batchName').value===batch.name);
  $('recordToggle').disabled=!own||!connected||requestBusy||!(phase==='recording'||s.auto_recording||canStart);
  $('save').disabled=$('discard').disabled=!own||!connected||requestBusy||!['recording','review'].includes(phase);
  text('recordError',s.recording_error||'');$('recordError').hidden=!s.recording_error;
  text('recordDetail',phase==='resetting'?'Next episode interval starts once the arm is at the Reset pose. Stop reset ends the batch.':countdown?'Recording resumes after the current file is processed.':String(s.last_result||last.last_recording?.path||''));
  if(last.last_recording&&!connected)text('recordDetail',(last.last_recording.note||'')+' '+last.last_recording.path+(last.last_recording.error?' · '+last.last_recording.error:''));
  const rows=[['Feedback check',feedback.detail],['B source fault',timing.source_fault||'—'],['B source at frame creation',timing.source_valid===true?fmt(timing.b_source_age_ms)+' ms · valid':'Unavailable · '+String(timing.source_valid??'unknown')],['B frame → A receive (upper bound)',timing.connected?fmt(timing.delivery_upper_ms)+' ms':'Unavailable'],['A residence',fmt(timing.a_residence_ms)+' ms'],['Source reference remaining',timing.budget_valid?fmt(timing.reference_remaining_ms)+' ms · '+timing.reference_kind:'Unavailable'],['IK elapsed / solve / dispatch',`${fmt(timing.ik_elapsed_ms)} / ${fmt(timing.ik_solve_ms)} / ${fmt(timing.ik_dispatch_wait_ms)} ms`],['Clock uncertainty','±'+fmt(timing.clock_uncertainty_ms)+' ms'],['Limits · A / B latest / B reference',`${fmt(timing.a_limit_ms)} / ${fmt(timing.b_latest_limit_ms)} / ${fmt(timing.b_reference_limit_ms)} ms`],['Input packets / targets sent / submitted',`${s.input_rate?.total||0} / ${s.target_rate?.total||0} / ${s.target_rate?.submitted||0}`]];
  $('diagnostics').replaceChildren(...rows.flatMap(([key,value])=>{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;return [dt,dd];}));
  const lastPause=s.last_pause_diagnostics,lastPauseReason=typeof lastPause?.reason==='string'?lastPause.reason:'';
  text('pauseReason',lastPauseReason||'—');
  const normalRelease=lastPauseReason==='released'||/^Grip released; (recording continues|not recording)$/.test(lastPauseReason);
  $('pauseReason').classList.toggle('pause-fault',Boolean(lastPauseReason)&&!normalRelease);
  text('pauseDetails',lastPause?JSON.stringify(lastPause,null,2):'No pause recorded.');
  renderCameras(localFresh?last.cameras||[]:[]);
}
const cameraViews=new Map();
function cameraView(info){
  let view=cameraViews.get(info.name);if(view)return view;
  const figure=document.createElement('figure'),frame=document.createElement('div'),img=document.createElement('img'),overlay=document.createElement('div'),caption=document.createElement('figcaption'),label=document.createElement('strong'),state=document.createElement('span');
  figure.className='camera';frame.className='camera-frame';overlay.className='camera-overlay';img.alt=info.label;img.hidden=true;
  frame.append(img,overlay);caption.append(label,state);figure.append(frame,caption);$('cameraGrid').append(figure);
  view={info,img,overlay,label,state,url:'',running:false};cameraViews.set(info.name,view);return view;
}
const delay=ms=>new Promise(r=>setTimeout(r,ms));
async function cameraLoop(view){
  view.running=true;
  while(!frozen){
    if(document.hidden||!networkOK||!view.info.live){await delay(400);continue;}
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2000);
    try{
      const response=await fetch('/api/camera?name='+encodeURIComponent(view.info.name),{headers:{'X-Sawyer-Token':token,'X-Sawyer-Client':client},signal:controller.signal,cache:'no-store'});
      if(!response.ok)throw new Error('Camera unavailable');
      const url=URL.createObjectURL(await response.blob());
      await new Promise(resolve=>{view.img.onload=view.img.onerror=resolve;view.img.src=url;});
      if(view.url)URL.revokeObjectURL(view.url);view.url=url;view.img.hidden=false;view.overlay.hidden=true;
      await delay(25);
    }catch(e){await delay(500);}
    finally{clearTimeout(timer);}
  }
  view.running=false;
}
function renderCameras(list){
  if(!list.length&&!cameraViews.size){$('cameraGrid').textContent='No cameras configured.';return;}
  for(const info of list){
    const view=cameraView(info);view.info=info;
    view.label.textContent=info.label;
    const exposure=info.live&&info.exposure,badExposure=exposure&&exposure.state!=='ok';
    view.state.textContent=info.live?fmt(info.fps)+' FPS · '+info.width+'×'+info.height+(badExposure?' · '+(exposure.state==='over'?'Overexposed':'Too dark'):''):'No signal';
    view.state.title=exposure?`Brightness median ${fmt(exposure.p50,0)} · dark ${fmt(exposure.dark_pct)}% · clipped ${fmt(exposure.channel_clip_pct)}% · RGB mean ${exposure.rgb_mean.map(v=>fmt(v,0)).join('/')}`:'';
    view.state.classList.toggle('bad',!info.live||!!badExposure);
    if(!info.live){view.img.hidden=true;view.overlay.hidden=false;view.overlay.textContent=info.error||'Waiting for camera…';}
    if(!view.running)cameraLoop(view);
  }
}
function renderLogs(){if($('pauseLog').checked)return;const area=$('logs'),position=area.scrollTop,filter=$('logFilter').value;const visible=events.filter(e=>filter==='all'||filter==='warnings'&&['warning','error'].includes(e.kind)||filter==='events'&&e.kind!=='motion');area.replaceChildren(...visible.map(e=>{const row=document.createElement('div');row.className='log-line '+(['event','warning','error','motion'].includes(e.kind)?e.kind:'event');const t=document.createElement('span'),body=document.createElement('span');t.className='log-time';t.textContent=e.time;body.textContent=e.text;row.append(t,body);return row;}));area.scrollTop=$('followLog').checked?area.scrollHeight:position;}
async function poll(){
  if(frozen)return;
  try{const data=await api('/api/state?after='+eventSeq+'&view=0');networkOK=true;lastOK=performance.now();last=data;
    if(!initialized){for(const p of data.profiles)$('profile').add(new Option(p.label,p.value));fillDefaults(data.defaults);initialized=true;if(!data.occupied)await claim();}
    if(lastProfileRequest!=null&&data.pending_profile==null){const result=data.state.motion_profile_result;if(result){$('profile').value=result.profile;$('protection').checked=result.joint_protection??data.state.joint_protection?.enabled??true;}}
    lastProfileRequest=data.pending_profile;
    if(data.events.length){events.push(...data.events);events=events.slice(-600);eventSeq=data.event_seq;renderLogs();}
    render();
  }catch(e){networkOK=false;notice(e.name==='AbortError'?'Local service is not responding. The session will stop if the page remains disconnected.':e.message);render();}
  if(!frozen)setTimeout(poll,networkOK?100:700);
}
$('claim').onclick=claim;
$('connect').onclick=()=>command(last.phase==='disconnected'||last.phase==='error'&&!last.has_session?'connect':'disconnect',settings());
$('stopSession').onclick=()=>command('stop_session');
$('scan').onclick=()=>command('scan',{serial:$('serial').value});
$('apply').onclick=()=>command('apply_motion',{profile:$('profile').value,joint_protection:$('protection').checked,backend:$('backend').value});
$('calibrate').onclick=()=>command('calibrate');$('resetArm').onclick=()=>command(last.state.resetting?'stop_reset':'reset_arm');
$('recordToggle').onclick=()=>{const s=last.state,action=s.recording_phase==='recording'?'end_recording':s.auto_recording?'stop_batch':'start_recording';if(action==='start_recording'&&![...document.querySelectorAll('#recordFields input')].every(e=>e.reportValidity()))return;command(action,action==='start_recording'?recordOptions():{});};
$('save').onclick=()=>command('save');$('discard').onclick=()=>command('discard');
for(const id of ['backend','smoothing','profile','protection','batchName','seconds'])$(id).addEventListener('input',()=>{if(id==='backend'&&last){const p=last.profiles.find(p=>p.value===$('profile').value);if(p&&profileReason(p))$('profile').value='standard';}render();});
for(const id of ['logFilter','pauseLog','followLog'])$(id).onchange=renderLogs;
$('clearLog').onclick=()=>{events=[];$('logs').replaceChildren();};
let folderCurrent='';
async function openFolder(path){try{const data=await api('/api/folders?path='+encodeURIComponent(path));folderCurrent=data.path;$('folderPath').value=data.path;$('folderUp').onclick=()=>openFolder(data.parent);$('folderRoots').replaceChildren(...data.roots.map(root=>{const b=document.createElement('button');b.textContent=root;b.onclick=()=>openFolder(root);return b;}));$('folderList').replaceChildren(...data.folders.map(f=>{const b=document.createElement('button');b.textContent='▸ '+f.name;b.onclick=()=>openFolder(f.path);return b;}));$('folderError').hidden=true;}catch(e){text('folderError',e.message);$('folderError').hidden=false;}}
$('browse').onclick=()=>{$('folderDialog').showModal();openFolder($('output').value);};$('folderClose').onclick=()=>$('folderDialog').close();$('folderGo').onclick=()=>openFolder($('folderPath').value);$('folderPath').onkeydown=e=>{if(e.key==='Enter')openFolder($('folderPath').value);};$('folderChoose').onclick=()=>{if(folderCurrent)$('output').value=folderCurrent;$('folderDialog').close();};
window.addEventListener('pagehide',()=>{frozen=true;if(token)navigator.sendBeacon('/api/release',JSON.stringify({token,client}));});
window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
setInterval(()=>{if(networkOK&&performance.now()-lastOK>1500){networkOK=false;render();}},250);
if(token)poll();else{notice('Launch Start-A and open its displayed URL to authorize this page.');text('connectionBadge','Page not authorized');text('summary','Start-A launches the local Web interface.');}
