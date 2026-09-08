"use strict";
let verification, challengeToken = "", widget, statusTimer, waitingSince;
const status = document.querySelector("#verify-status"), submit = document.querySelector("#verify-submit");
function clearChallenge() {
  challengeToken = ""; submit.disabled = true;
  if (widget !== undefined) { window.turnstile.remove(widget); widget = undefined; }
}
function checkLater() {
  waitingSince ??= Date.now();
  if (Date.now() - waitingSince >= 120000) {
    status.textContent = "Your role has not been confirmed yet. Contact a server administrator; you do not need to repeat the check.";
    return;
  }
  statusTimer = setTimeout(() => loadVerification().catch(error => {
    status.textContent = error.message;
  }), 5000);
}
async function loadVerification() {
  clearTimeout(statusTimer);
  const response = await fetch("/api/verification", {credentials: "same-origin"});
  if (response.status === 401) {
    clearChallenge();
    document.querySelector("#verify-login").hidden = false;
    status.textContent = "Sign in with your Discord account to continue."; return;
  }
  const data = await response.json();
  if (!response.ok) throw Error(data.detail || "Verification is unavailable.");
  verification = data;
  const member = document.querySelector("#verify-member"); member.replaceChildren();
  const image = document.createElement("img"); image.src = data.member.avatar_url; image.alt = ""; image.width = 40; image.height = 40;
  member.append(image, document.createTextNode(" " + data.member.name));
  if (data.verified) { clearChallenge(); status.textContent = "You're verified. Return to Discord to join the conversation."; return; }
  if (["queued", "executing"].includes(data.request_state)) {
    clearChallenge(); status.textContent = "Verification accepted. Your member role is being applied.";
    checkLater(); return;
  }
  if (["denied", "unknown", "completed"].includes(data.request_state)) {
    clearChallenge();
    status.textContent = "Your member role could not be confirmed. Contact a server administrator to review your verification.";
    return;
  }
  if (!data.enabled) { status.textContent = "Verification is not available yet. Contact a server administrator."; return; }
  if (data.screening_pending) { status.textContent = "Accept the server rules in Discord first, then reload this page."; return; }
  status.textContent = "Complete the check below to receive your member role.";
  if (!window.turnstile) {
    const script = document.createElement("script"); script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.onload = renderChallenge; script.onerror = () => { status.textContent = "The verification provider could not load. Reload to try again."; };
    document.head.append(script);
  } else renderChallenge();
}
function renderChallenge() {
  if (widget !== undefined) window.turnstile.remove(widget);
  widget = window.turnstile.render("#challenge", {sitekey: verification.site_key, action: "greybot-verify", cData: verification.member.id, theme: "dark",
    callback: token => { challengeToken = token; submit.disabled = false; },
    "expired-callback": () => { challengeToken = ""; submit.disabled = true; },
    "error-callback": () => { challengeToken = ""; submit.disabled = true; status.textContent = "The check failed. Reload to try again."; }});
}
submit.addEventListener("click", async () => {
  submit.disabled = true;
  try {
    const response = await fetch("/api/verification", {method: "POST", credentials: "same-origin", headers: {
      "Content-Type": "application/json", "X-CSRF-Token": verification.csrf}, body: JSON.stringify({token: challengeToken})});
    const result = await response.json(); challengeToken = "";
    if (!response.ok) throw Error(result.detail || "Verification failed.");
    status.textContent = result.verified ? "You're verified. Return to Discord." : "Verification accepted. Your member role is being applied.";
    clearChallenge();
    if (result.queued) { waitingSince = Date.now(); checkLater(); }
  } catch (error) { status.textContent = error.message; if (widget !== undefined) window.turnstile.reset(widget); }
});
loadVerification().catch(error => { status.textContent = error.message; });
