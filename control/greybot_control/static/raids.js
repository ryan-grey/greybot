"use strict";
let raidData, editing;
const $ = s => document.querySelector(s);
const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function profile(p) {const n=el("span",undefined,"raid-person");if(p.avatar_url?.startsWith("https://cdn.discordapp.com/")){const img=el("img");img.src=p.avatar_url;img.alt="";img.width=24;img.height=24;n.append(img);}n.append(el("span",p.name||"Former member"));return n;}
function notice(s) {$("#raid-notice").textContent=s;}
async function request(body) {
  body.request_id=crypto.randomUUID().replaceAll("-","");
  const r=await fetch("/api/raids",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":raidData.csrf},body:JSON.stringify(body)});
  const data=await r.json();if(!r.ok)throw Error(data.detail||"Request failed");
  notice("Request queued. Refresh shortly to see the confirmed change.");
}
function action(label,body){const b=el("button",label);b.type="button";b.onclick=async()=>{b.disabled=true;try{await request(body);}catch(e){notice(e.message);b.disabled=false;}};return b;}
function localInput(seconds){const d=new Date(seconds*1000);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function render(){
  const list=$("#raid-list");list.replaceChildren();const filter=$("#raid-filter").value;
  for(const row of raidData.events){const e=row.event;const current=!row.historical&&e.state==="open"&&e.closingTime>Date.now()/1000;
    if(location.hash!=="#"+row.id&&(filter==="current"&&!current||filter==="history"&&current))continue;
    const box=el("details",undefined,"box raid-event");box.id=row.id;box.open=location.hash==="#"+row.id;
    const summary=el("summary");summary.append(el("strong",e.title),el("span",`#${e.channel_name} · ${new Date(e.startTime*1000).toLocaleString()} · ${current?"Open":e.state}`,"muted"));box.append(summary);
    const body=el("div",undefined,"raid-body");body.append(el("p",e.description||"","raid-description"),profile(e.leader));
    const calendar=el("a","Add to calendar");calendar.href="/api/raids/"+encodeURIComponent(row.id)+"/calendar.ics";calendar.className="raid-calendar";body.append(calendar);
    if(current&&raidData.enabled){const controls=el("div",undefined,"filters");const select=el("select");select.setAttribute("aria-label","Class and specialization");for(const c of e.choices){const o=el("option",c.label);o.value=c.value;select.append(o);}const sign=el("button","Sign up / change");sign.onclick=async()=>{try{await request({operation:"signup",raid_id:row.id,value:select.value});}catch(err){notice(err.message);}};controls.append(select,sign);
      const statuses=el("select");statuses.setAttribute("aria-label","Attendance status");for(const s of ["Bench","Late","Tentative","Absence"]){const o=el("option",s);o.value=s;statuses.append(o);}const status=el("button","Set status");status.onclick=async()=>{try{await request({operation:"status",raid_id:row.id,value:statuses.value});}catch(err){notice(err.message);}};controls.append(statuses,status,action("Withdraw",{operation:"withdraw",raid_id:row.id}));body.append(controls);
      const note=el("input");note.maxLength=500;note.placeholder="Your signup note";note.setAttribute("aria-label","Your signup note");note.value=e.signUps.find(s=>String(s.userId)===raidData.member.id)?.note||"";const save=el("button","Save note");save.onclick=async()=>{try{await request({operation:"note",raid_id:row.id,value:note.value});}catch(err){notice(err.message);}};const wrap=el("div",undefined,"filters");wrap.append(note,save);body.append(wrap);
    }
    if(row.can_manage&&raidData.enabled){const manage=el("div",undefined,"filters");const edit=el("button","Edit event");edit.onclick=()=>{editing=row;const f=$("#raid-edit form");f.elements.title.value=e.title;f.elements.when.value=localInput(e.startTime);f.elements.description.value=e.description||"";$("#raid-edit").showModal();};manage.append(edit,action(e.state==="open"?"Close signups":"Reopen signups",{operation:e.state==="open"?"close":"open",raid_id:row.id,revision:row.revision}));body.append(manage);}
    const table=el("table",undefined,"raid-roster");const head=el("thead");const tr=el("tr");for(const label of ["Member","Class / specialization","Status / note"]){tr.append(el("th",label));}head.append(tr);table.append(head);const tbody=el("tbody");
    for(const s of e.signUps){const tr=el("tr");const member=el("td");member.append(profile(s.display));tr.append(member,el("td",[s.className,s.specName].filter(Boolean).join(" · ")),el("td",[s.roleName,s.note].filter(Boolean).join(" · ")));tbody.append(tr);}table.append(tbody);body.append(table);if(!e.signUps.length)body.append(el("p","No signups yet.","muted"));box.append(body);list.append(box);
  }
  if(!list.children.length)list.append(el("p","No events in this view.","muted"));
  const totals=new Map();for(const row of raidData.events){if(row.event.startTime>Date.now()/1000)continue;const seen=new Set();const primary=new Set(row.event.classes.filter(c=>(c.type||"primary")==="primary").map(c=>c.name));for(const s of row.event.signUps){if(seen.has(s.userId)||!primary.has(s.className))continue;seen.add(s.userId);const t=totals.get(s.userId)||{profile:s.display,count:0};t.count++;totals.set(s.userId,t);}}
  const scores=$("#raid-attendance");scores.replaceChildren();for(const t of [...totals.values()].sort((a,b)=>b.count-a.count)){const r=el("div",undefined,"raid-score");r.append(profile(t.profile),el("span",`${t.count} signups`));scores.append(r);}
}
async function load(){try{const r=await fetch("/api/raids");if(r.status===401){$("#raid-login").hidden=false;notice("Sign in to view events available to your server roles.");return;}const d=await r.json();if(!r.ok)throw Error(d.detail||"Could not load raids");raidData=d;$("#raid-login").hidden=true;$("#raid-member").replaceChildren(profile(d.member));$("#raid-create").hidden=!d.channels.length||!d.enabled;const select=$("#raid-form").elements.channel;select.replaceChildren();for(const c of d.channels){const o=el("option","#"+c.name);o.value=c.id;select.append(o);}$("#raid-requests").textContent=d.requests.map(r=>`${new Date(r.created*1000).toLocaleTimeString()}: ${r.state==="denied"?"blocked by access or event checks":r.state==="unknown"?"result needs administrator review":r.state}`).join(" · ");if(!d.enabled)notice("Raid migration preview — changes are not enabled yet.");render();}catch(e){notice(e.message);}}
$("#raid-form").onsubmit=async ev=>{ev.preventDefault();const f=ev.currentTarget;try{await request({operation:"create",title:f.elements.title.value,channel:f.elements.channel.value,template:f.elements.template.value,when:new Date(f.elements.when.value).toISOString(),description:f.elements.description.value});}catch(e){notice(e.message);}};
$("#raid-save").onclick=async ev=>{ev.preventDefault();const f=$("#raid-edit form");if(!f.reportValidity())return;try{const start=new Date(f.elements.when.value).getTime()/1000;await request({operation:"edit",raid_id:editing.id,revision:editing.revision,value:{title:f.elements.title.value,description:f.elements.description.value,startTime:start,closingTime:start}});$("#raid-edit").close();}catch(e){notice(e.message);}};
$("#reload").onclick=load;$("#raid-filter").onchange=render;load();
