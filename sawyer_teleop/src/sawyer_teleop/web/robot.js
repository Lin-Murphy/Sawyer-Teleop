// Read-only projection of server-validated FK frames. No IK or control commands.
export function feedbackMetric(state, phase, localFresh=true) {
  const timing=state.feedback_diagnostics||{}, age=state.feedback_age_ms;
  const sourceFault=timing.source_fault;
  const finite=typeof age==='number'&&Number.isFinite(age)&&age>=0;
  if(!localFresh)return {value:'—',unit:'service unavailable',detail:'The local A service is not responding.'};
  if(timing.source_valid===false)return {value:'—',unit:'invalid B feedback',detail:'B reports: '+(sourceFault||'feedback invalid')};
  if(phase!=='connected'||!state.robot_connected)return {value:'—',unit:'no valid feedback',detail:state.mode==='error'?(state.message||timing.fault||'Connection failed.'):'Connect B to receive feedback.'};
  if(!finite||state.feedback_valid!==true||timing.fresh!==true){
    const reason=sourceFault||timing.fault||'feedback unavailable';
    const comparable=finite&&timing.source_valid===true&&timing.budget_valid===true;
    return {value:'—',unit:'feedback unavailable',detail:reason+(comparable?' · last sample age '+age.toFixed(1)+' ms':'')};
  }
  return {value:age.toFixed(1),unit:'milliseconds',detail:'Conservative age of the latest valid B sample, including clock uncertainty.'};
}

export class RobotView {
  constructor(canvas, overlay, status) {
    this.canvas=canvas; this.overlay=overlay; this.status=status; this.enabled=false;
    this.packet=null; this.deadline=0; this.reset();
    new ResizeObserver(()=>this.draw()).observe(canvas);
    canvas.addEventListener('pointerdown',e=>{if(!this.enabled)return;this.drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId);});
    canvas.addEventListener('pointermove',e=>{if(!this.drag)return;this.yaw+=(e.clientX-this.drag[0])*.4;this.elevation=Math.max(-10,Math.min(85,this.elevation+(e.clientY-this.drag[1])*.3));this.drag=[e.clientX,e.clientY];this.draw();});
    for(const type of ['pointerup','pointercancel'])canvas.addEventListener(type,()=>this.drag=null);
    canvas.addEventListener('wheel',e=>{if(!this.enabled)return;e.preventDefault();this.zoom=Math.max(.45,Math.min(2.5,this.zoom*Math.exp(-e.deltaY/1200)));this.draw();},{passive:false});
    canvas.addEventListener('keydown',e=>{if(!this.enabled)return;const deltas={ArrowLeft:[-5,0],ArrowRight:[5,0],ArrowUp:[0,5],ArrowDown:[0,-5]};if(deltas[e.key]){e.preventDefault();this.yaw+=deltas[e.key][0];this.elevation=Math.max(-10,Math.min(85,this.elevation+deltas[e.key][1]));this.draw();}});
    setInterval(()=>{if(this.enabled)this.draw();},150);
  }
  reset(){this.yaw=225;this.elevation=25;this.zoom=1.2;if(this.canvas)this.draw();}
  accept(packet,requestStarted){this.packet=packet;this.deadline=requestStarted+(packet?.remaining_ms||0);this.draw();}
  setEnabled(enabled){this.enabled=enabled;if(!enabled){this.packet=null;this.deadline=0;}this.draw();}
  stale(){this.deadline=0;this.draw();}
  draw(){
    const c=this.canvas,ctx=c.getContext('2d'),w=c.clientWidth,h=c.clientHeight;
    if(!w||!h)return;
    const ratio=Math.min(devicePixelRatio||1,2);if(c.width!==Math.round(w*ratio)||c.height!==Math.round(h*ratio)){c.width=Math.round(w*ratio);c.height=Math.round(h*ratio);}
    ctx.setTransform(ratio,0,0,ratio,0,0);ctx.fillStyle='#f2f6fa';ctx.fillRect(0,0,w,h);
    const rad=Math.PI/180,y=this.yaw*rad,e=this.elevation*rad;
    const dir=[Math.cos(y)*Math.cos(e),Math.sin(y)*Math.cos(e),Math.sin(e)],right=[-Math.sin(y),Math.cos(y),0],up=cross(dir,right);
    const scale=Math.min(w/2.6,h/1.65)*this.zoom;
    const project=p=>{const r=sub(p,[.35,0,.3]);return [dot(r,right)*scale+w/2,-dot(r,up)*scale+h/2];};
    const line=(a,b,color,width=1)=>{const pa=project(a),pb=project(b);ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=width;ctx.moveTo(...pa);ctx.lineTo(...pb);ctx.stroke();};
    for(let n=-5;n<=6;n++){const v=n*.2;line([v,-1,0],[v,1.2,0],'#dee7ef');line([-1,v,0],[1.2,v,0],'#dee7ef');}
    const colors=['#ba5555','#368876','#4f80b9'];
    for(let k=0;k<3;k++){const end=[0,0,0];end[k]=.3;line([0,0,0],end,colors[k],2);const p=project(end);ctx.fillStyle=colors[k];ctx.font='11px Segoe UI';ctx.fillText('XYZ'[k],p[0]+4,p[1]-4);}
    const p=this.enabled?this.packet:null,frames=p?.frames;
    const fresh=!!(p?.fresh&&performance.now()<this.deadline);
    this.status.textContent=!this.enabled?'DISPLAY OFF':fresh?'LIVE FEEDBACK':frames?'STALE · FROZEN':'WAITING FOR FEEDBACK';
    this.status.className='view-status '+(fresh?'live':frames?'stale':'');
    this.overlay.hidden=!!frames;
    this.overlay.textContent=!this.enabled?'Enable 3D view to see actual feedback.':p?.status||'Waiting for a valid B model and actual joint feedback.';
    if(!frames)return;
    const points=frames.map(f=>[f[0][3],f[1][3],f[2][3]]),faces=[];
    tube(faces,[0,0,-.025],[0,0,.025],.11,[75,92,114]);
    for(let i=1;i<points.length;i++)tube(faces,points[i-1],points[i],.043,fresh?[201,69,71]:[154,168,185]);
    for(let i=1;i<frames.length;i++){const f=frames[i],axis=[f[0][2],f[1][2],f[2][2]];tube(faces,add(points[i],mul(axis,-.025)),add(points[i],mul(axis,.025)),.055,[190,200,212]);}
    faces.sort((a,b)=>dot(mean(a[0]),dir)-dot(mean(b[0]),dir));
    for(const [verts,color]of faces){const normal=norm(cross(sub(verts[1],verts[0]),sub(verts[2],verts[0]))),shade=.64+.36*Math.abs(dot(normal,[.3,-.4,.866]));ctx.fillStyle=`rgb(${color.map(x=>Math.round(x*shade)).join(',')})`;ctx.beginPath();verts.forEach((v,i)=>{const xy=project(v);i?ctx.lineTo(...xy):ctx.moveTo(...xy);});ctx.closePath();ctx.fill();}
    const tip=frames.at(-1),point=points.at(-1);for(let k=0;k<3;k++)line(point,add(point,mul([tip[0][k],tip[1][k],tip[2][k]],.12)),colors[k],2);
  }
}
const add=(a,b)=>a.map((v,i)=>v+b[i]),sub=(a,b)=>a.map((v,i)=>v-b[i]),mul=(a,n)=>a.map(v=>v*n),dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0),cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],norm=a=>mul(a,1/Math.max(1e-12,Math.sqrt(dot(a,a)))),mean=vs=>mul(vs.reduce(add,[0,0,0]),1/vs.length);
function tube(out,start,end,radius,color){const delta=sub(end,start);if(Math.sqrt(dot(delta,delta))<1e-8)return;const axis=norm(delta),side=norm(cross(axis,Math.abs(axis[2])<.9?[0,0,1]:[0,1,0])),up=cross(axis,side),a=[],b=[];for(let i=0;i<12;i++){const t=i*Math.PI/6,ring=mul(add(mul(side,Math.cos(t)),mul(up,Math.sin(t))),radius);a.push(add(start,ring));b.push(add(end,ring));}out.push([a.slice().reverse(),color],[b,color]);for(let i=0;i<12;i++)out.push([[a[i],a[(i+1)%12],b[(i+1)%12],b[i]],color]);}
