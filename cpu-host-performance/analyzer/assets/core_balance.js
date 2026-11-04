(function(){
'use strict';
const root=document.getElementById('core-balance');if(!root)return;
const data=JSON.parse(document.getElementById('core-data').textContent);
const el=id=>document.getElementById('core-'+id), ns='http://www.w3.org/2000/svg';
let selected=new Set(['util']);
const fmt=v=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(2):'N/A';
function node(tag,attrs,text){const n=document.createElementNS(ns,tag);Object.keys(attrs||{}).forEach(k=>n.setAttribute(k,attrs[k]));if(text!==undefined)n.textContent=text;return n;}
function toggles(){
 const box=el('metrics');box.textContent='';
 data.metrics.filter(m=>m.group===el('group').value).forEach(m=>{
  const b=document.createElement('button');b.type='button';b.className='metric-toggle';b.textContent=m.label;b.dataset.metric=m.key;
  b.setAttribute('aria-pressed',String(selected.has(m.key)));b.style.borderColor=m.color;
  b.addEventListener('click',()=>{selected.has(m.key)?selected.delete(m.key):selected.add(m.key);b.setAttribute('aria-pressed',String(selected.has(m.key)));draw();});box.appendChild(b);
 });
}
function draw(){
 const group=el('group').value, metrics=data.metrics.filter(m=>m.group===group&&selected.has(m.key));
 const min=el('min').value===''?-Infinity:Number(el('min').value), max=el('max').value===''?Infinity:Number(el('max').value);
 const rows=data.cpus.filter(r=>(el('numa').value==='all'||String(r.numa)===el('numa').value)&&r.cpu>=min&&r.cpu<=max);
 const plot=el('plot');plot.textContent='';el('tooltip').textContent='悬停或键盘聚焦图中CPU，查看数值；橙色为利用率≥90%的热点（经验阈值）。';
 const svg=node('svg',{viewBox:'0 0 1080 330',role:'img','aria-label':'交互式各核心指标图','data-cpu-count':rows.length,'data-chart-style':el('style').value});plot.appendChild(svg);
 const valid=rows.flatMap(r=>metrics.map(m=>r[m.key])).filter(v=>typeof v==='number'&&Number.isFinite(v));
 el('summary').textContent='显示 '+rows.length+' / '+data.cpus.length+' CPU · '+metrics.map(m=>m.label).join(' / ')+' · 单位 '+(metrics[0]?metrics[0].unit:'—')+' · 缺失值保留N/A';
 if(!rows.length||!metrics.length||!valid.length){svg.appendChild(node('text',{x:55,y:120,fill:'#a6b4c7'},!rows.length?'筛选范围内无CPU':!metrics.length?'请选择至少一个指标':'该指标缺少可用数据（N/A）'));root.querySelector('.core-static').style.display='none';return;}
 const x0=65,y0=32,w=980,h=245,step=w/rows.length;
 const ymax=group==='time'?100:Math.max(1,...valid)*1.15, Y=v=>y0+h*(1-v/ymax), X=i=>x0+(i+.5)*step;
 let last=null, band=0;
 rows.forEach((r,i)=>{if(r.numa!==last){band++;last=r.numa;}svg.appendChild(node('rect',{x:x0+i*step,y:y0,width:step+.2,height:h,fill:band%2?'#192534':'#111b27',opacity:'.6'}));});
 for(let i=0;i<=4;i++){const v=ymax*i/4,y=Y(v);svg.appendChild(node('line',{x1:x0,x2:x0+w,y1:y,y2:y,stroke:'#354151'}));svg.appendChild(node('text',{x:3,y:y+4,fill:'#9eaec2','font-size':11},v.toFixed(group==='time'?0:1)));}
 svg.appendChild(node('text',{x:8,y:17,fill:'#a6b4c7','font-size':12},metrics[0].unit));
 metrics.forEach((m,mi)=>{
  let segment=[];function flush(){if(segment.length){svg.appendChild(node('polyline',{points:segment.join(' '),fill:'none',stroke:m.color,'stroke-width':1.8}));segment=[];}}
  const known=rows.map((r,i)=>({v:r[m.key],i})).filter(o=>typeof o.v==='number'&&Number.isFinite(o.v));
  rows.forEach((r,i)=>{const v=r[m.key];if(typeof v!=='number'||!Number.isFinite(v)){flush();return;}
   if(el('style').value==='bar'){const bw=Math.max(.4,step*.8/metrics.length);svg.appendChild(node('rect',{x:x0+i*step+step*.1+mi*bw,y:Y(v),width:bw,height:Math.max(0,y0+h-Y(v)),fill:m.color,opacity:.85}));}
   else segment.push(X(i)+','+Y(v));
   if(r.util>=data.hot_threshold)svg.appendChild(node('circle',{cx:X(i),cy:Y(v),r:2.6,fill:'#ff956e','data-hot-cpu':r.cpu}));
  });flush();
  if(known.length){
   const avg=known.reduce((a,o)=>a+o.v,0)/known.length;
   svg.appendChild(node('line',{x1:x0,x2:x0+w,y1:Y(avg),y2:Y(avg),stroke:m.color,'stroke-dasharray':'4 5',opacity:.7}));
   const smallest=known.reduce((a,b)=>a.v<=b.v?a:b), largest=known.reduce((a,b)=>a.v>=b.v?a:b);
   [smallest,largest].forEach(o=>svg.appendChild(node('circle',{cx:X(o.i),cy:Y(o.v),r:3.5,fill:'none',stroke:m.color})));
   svg.appendChild(node('text',{x:68+mi*320,y:15,fill:m.color,'font-size':10},m.label+' 平均 '+fmt(avg)+' · min '+fmt(smallest.v)+' · max '+fmt(largest.v)));
  }
 });
 rows.forEach((r,i)=>{
  const text='CPU '+r.cpu+' · NUMA '+(r.numa===null?'未知':r.numa)+' · 观测 '+fmt(r.observed_sec)+'秒\n'+metrics.map(m=>m.label+': '+fmt(r[m.key])+' '+m.unit).join(' | ');
  const hit=node('rect',{x:x0+i*step,y:y0,width:step,height:h,fill:'transparent',tabindex:0,'aria-label':text,'data-cpu':r.cpu});
  hit.appendChild(node('title',{},text));hit.addEventListener('mouseenter',()=>el('tooltip').textContent=text);hit.addEventListener('focus',()=>el('tooltip').textContent=text);svg.appendChild(hit);
  if(i%Math.max(1,Math.floor(rows.length/16))===0||i===rows.length-1)svg.appendChild(node('text',{x:X(i),y:297,fill:'#a6b4c7','font-size':10,'text-anchor':'middle'},r.cpu));
 });
 svg.appendChild(node('text',{x:550,y:322,fill:'#a6b4c7','font-size':12,'text-anchor':'middle'},'CPU编号 · 背景按NUMA分组 · 虚线为所选CPU的指标均值'));
 root.querySelector('.core-static').style.display='none';
}
el('group').addEventListener('change',()=>{const first=data.metrics.find(m=>m.group===el('group').value);selected=new Set([first.key]);toggles();draw();});
el('numa').addEventListener('change',()=>{el('style').value=el('numa').value==='all'?'line':'bar';draw();});
['min','max'].forEach(id=>el(id).addEventListener('input',draw));
['style','min','max'].forEach(id=>el(id).addEventListener('change',draw));
el('reset').addEventListener('click',()=>{el('numa').value='all';el('min').value='';el('max').value='';el('style').value='line';draw();});
toggles();draw();root.querySelector('.core-controls').style.display='flex';
})();
