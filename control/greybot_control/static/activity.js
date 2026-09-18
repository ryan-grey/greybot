"use strict";
const notice=document.querySelector("#activity-notice"), body=document.querySelector("#activity-table tbody");
let timer=0;
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function duration(seconds){return `${Math.floor(seconds/3600)}h ${String(Math.floor(seconds%3600/60)).padStart(2,"0")}m`;}
function day(stamp){return new Date(stamp*1000).toLocaleDateString(undefined,{month:"long",day:"numeric",year:"numeric"});}
function ago(stamp){const minutes=Math.max(0,Math.round((Date.now()/1000-stamp)/60));if(minutes<60)return `${minutes} min ago`;if(minutes<2880)return `${Math.round(minutes/60)} h ago`;return day(stamp);}
function card(title,value,detail){const box=element("div",undefined,"box dashboard-card");box.append(element("h2",title),element("p",value),element("p",detail,"muted"));return box;}
async function load(){
  const response=await fetch("/api/activity"), login=document.querySelector("#activity-login");
  if(response.status===401){login.hidden=false;notice.textContent="Sign in with your Discord account. Only Scrambled members can see this.";return;}
  const data=await response.json(); if(!response.ok)throw Error(typeof data.detail==="string"?data.detail:"Voice activity is unavailable");
  login.hidden=true; notice.textContent="";
  document.title=`${data.server} · Voice activity`;
  document.querySelector("#activity-period").textContent=`${data.server} · ${day(data.since)} to today · ${data.afk.length?data.afk.join(", ")+" (AFK) is not counted":"no AFK channel set"}`;
  const summary=document.querySelector("#activity-summary");
  summary.replaceChildren(
    card("Members ranked",String(data.rows.length),`${data.in_voice_now} in voice right now`),
    card("Hours together",String(Math.round(data.total_seconds/3600)),`since ${day(data.since)}`),
    card("Busiest channels",data.busiest.slice(0,3).map(c=>c.channel).join(", ")||"None yet",data.busiest.slice(0,3).map(c=>duration(c.seconds)).join(" · ")),
    card("Gaps in the record",String(data.gaps),"mostly seconds long; time in a gap is not counted"));
  body.replaceChildren(...data.rows.map((row,index)=>{
    const tr=element("tr"), who=element("td"), avatar=element("img");
    avatar.src=row.avatar_url; avatar.alt=""; avatar.width=avatar.height=20; avatar.className="mark"; avatar.loading="lazy";
    const cell=element("div",undefined,"member-cell"); cell.append(avatar,element("span",row.name+(row.active?"":" (left the server)"))); who.append(cell);
    tr.append(element("td",String(index+1)),who,element("td",duration(row.seconds)),element("td",String(row.stays)),element("td",duration(row.longest)),
      element("td",row.top.map(t=>t.channel).join(", ")),element("td",row.in_voice?`In ${row.in_voice} now`:ago(row.last)));
    return tr;}));
  document.querySelector("#activity-updated").textContent=`Updated ${new Date(data.updated*1000).toLocaleTimeString()} · refreshes every ${Math.round(data.refresh_seconds/60)} minutes`;
  for(const id of ["#activity-summary","#activity-board","#activity-method"])document.querySelector(id).hidden=false;
  clearTimeout(timer); timer=setTimeout(refresh,data.refresh_seconds*1000);
}
function refresh(){load().catch(error=>notice.textContent=error.message);}
document.querySelector("#activity-refresh").addEventListener("click",refresh);
refresh();
