"use strict";
const host=document.querySelector("#channel-options"), status=document.querySelector("#channel-status");
async function load(){
  const response=await fetch("/api/channel-choices");
  if(response.status===401){status.textContent="Sign in to manage your channels.";return;}
  const data=await response.json(); if(!response.ok) throw Error(data.detail||"Channel preferences unavailable");
  document.querySelector("#channel-login").hidden=true; host.replaceChildren(); status.textContent=data.member.name;
  for(const channel of data.channels){
    const label=document.createElement("label"), check=document.createElement("input"); label.className="record check-row"; check.type="checkbox";check.checked=!channel.hidden;
    check.addEventListener("change",async()=>{
      check.disabled=true; const request_id=crypto.randomUUID();
      try {const r=await fetch("/api/channel-choices",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":data.csrf},body:JSON.stringify({channel:channel.id,hidden:!check.checked,request_id})}); const result=await r.json(); if(!r.ok)throw Error(result.detail||"Request failed");status.textContent="Preference queued; refresh shortly to confirm the result.";}
      catch(error){status.textContent=error.message;check.checked=!check.checked;check.disabled=false;}
    });
    label.append(check,document.createTextNode(`${[2,13].includes(channel.type)?"Voice · ":"#"}${channel.name}`));host.append(label);
  }
  if(!data.channels.length)host.textContent="No channels are available to customize.";
}
function refresh(){load().catch(error=>status.textContent=error.message);}
document.querySelector("#channel-refresh").addEventListener("click",refresh);refresh();
