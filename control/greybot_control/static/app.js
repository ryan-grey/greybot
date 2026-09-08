"use strict";
const $ = (s) => document.querySelector(s);
let csrf = "", revision = 0, cursor = 0, active = "dashboard";
let auditDefaults = [];
let directory = {channels: [], roles: [], members: [], server: {name: "Server"}};
const entityMaps = {channels: new Map(), roles: new Map(), members: new Map()};
function entity(kind, id) {
  const item = entityMaps[kind]?.get(String(id));
  if (!item) return {name: {members: "Unavailable member", channels: "Deleted or unavailable channel", roles: "Deleted or unavailable role"}[kind] || "Unavailable reference"};
  let name = item.name;
  if (kind === "channels" && item.type !== 4) name = "#" + name;
  if (item.active === false) name += " (last known)";
  return {...item, name};
}
function reference(kind, id) {
  const info = entity(kind, id), span = document.createElement("span");
  span.className = "entity-reference";
  if (info.avatar_url?.startsWith("https://cdn.discordapp.com/")) {
    const img = document.createElement("img"); img.src = info.avatar_url; img.alt = "";
    img.className = "entity-avatar"; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.remove(), {once: true}); span.append(img);
  }
  span.append(document.createTextNode(info.name));
  return span;
}
function channelLabel(item) {
  const parent = entityMaps.channels.get(item.parent_id);
  return `${entity("channels", item.id).name}${parent ? " · " + parent.name : ""}`;
}
function syncPicker(host) {
  const selected = host.querySelector(".picker-selected");
  selected.replaceChildren();
  host.querySelectorAll('input[type="hidden"]').forEach(input => {
    if (!input.value) return;
    const item = reference(host.dataset.kind, input.value);
    const parent = entityMaps.channels.get(entityMaps.channels.get(input.value)?.parent_id);
    if (host.dataset.kind === "channels" && parent) item.append(document.createTextNode(" · " + parent.name));
    selected.append(item);
  });
}
function setMultiple(host, values) {
  const name = host.querySelector('input[type="hidden"]').name;
  host.querySelectorAll('input[type="hidden"]').forEach(input => input.remove());
  for (const value of values.length ? values : [""]) {
    const input = document.createElement("input"); input.type = "hidden"; input.name = name; input.value = value; host.prepend(input);
  }
  syncPicker(host);
}
function setupPickers() {
  document.querySelectorAll(".entity-picker").forEach((host, index) => {
    if (host.querySelector(".picker-search")) return;
    const hidden = host.querySelector('input[type="hidden"]'), label = document.createElement("label"), search = document.createElement("input"), selected = document.createElement("div"), results = document.createElement("div");
    label.className = "picker-label"; label.textContent = host.dataset.label;
    search.id = `entity-search-${index}`; label.htmlFor = search.id;
    search.className = "picker-search"; search.placeholder = "Search by name"; search.autocomplete = "off";
    results.id = `entity-results-${index}`; results.className = "picker-results"; results.hidden = true;
    search.setAttribute("aria-controls", results.id); search.setAttribute("aria-expanded", "false");
    selected.className = "picker-selected";
    const close = () => { results.hidden = true; search.setAttribute("aria-expanded", "false"); };
    const choose = id => {
      if (host.dataset.multiple) {
        const ids = Array.from(host.querySelectorAll('input[type="hidden"]')).map(input => input.value).filter(Boolean);
        setMultiple(host, !id ? [] : ids.includes(id) ? ids.filter(value => value !== id) : [...ids, id]);
      } else hidden.value = id;
      search.value = ""; syncPicker(host); close();
    };
    const show = () => {
      const term = search.value.toLocaleLowerCase(); results.replaceChildren();
      const clear = document.createElement("button"); clear.type = "button"; clear.textContent = "Clear selection"; clear.addEventListener("click", () => choose("")); results.append(clear);
      const items = directory[host.dataset.kind].filter(item =>
        (!host.dataset.current || item.active !== false) && (!host.dataset.text || [0, 5].includes(item.type)) &&
        `${item.name} ${item.username || ""} ${host.dataset.kind === "channels" ? channelLabel(item) : ""}`.toLocaleLowerCase().includes(term));
      for (const item of items.slice(0, 40)) {
        const button = document.createElement("button"); button.type = "button"; button.append(reference(host.dataset.kind, item.id));
        if (host.dataset.multiple) {
          const chosen = Array.from(host.querySelectorAll('input[type="hidden"]')).some(input => input.value === item.id);
          button.setAttribute("aria-pressed", String(chosen));
          if (chosen) button.prepend(document.createTextNode("✓ "));
        }
        if (host.dataset.kind === "channels" && item.parent_id) {
          const context = document.createElement("small"); context.textContent = entityMaps.channels.get(item.parent_id)?.name || ""; button.append(context);
        }
        if (host.dataset.kind === "members" && item.username) {
          const handle = document.createElement("small"); handle.textContent = "@" + item.username; button.append(handle);
        }
        button.addEventListener("click", () => choose(item.id)); results.append(button);
      }
      if (!items.length) { const empty = document.createElement("p"); empty.textContent = "No matching names available"; results.append(empty); }
      results.hidden = false; search.setAttribute("aria-expanded", "true");
    };
    search.addEventListener("input", () => { if (!host.dataset.multiple) hidden.value = ""; syncPicker(host); show(); });
    search.addEventListener("focus", show);
    search.addEventListener("keydown", event => {
      if (event.key === "Escape") close();
      if (event.key === "Enter") { event.preventDefault(); results.querySelectorAll("button")[1]?.focus(); }
      if (event.key === "ArrowDown") { event.preventDefault(); results.querySelector("button")?.focus(); }
    });
    host.addEventListener("focusout", event => { if (!host.contains(event.relatedTarget)) close(); });
    host.append(label, search, selected, results); syncPicker(host);
  });
}
async function loadDirectory() {
  directory = await api("/api/directory");
  for (const kind of ["members", "channels", "roles"]) {
    directory[kind].sort((a, b) => a.name.localeCompare(b.name));
    entityMaps[kind] = new Map(directory[kind].map(item => [item.id, item]));
  }
  setupPickers();
  if (directory.stale) notice("Discord's directory is temporarily unavailable; showing last-known names.");
}
function translatedText(text) {
  return String(text).replace(/<(@!?|@&|#)(\d+)>/g, (_, prefix, id) => entity(prefix === "#" ? "channels" : prefix === "@&" ? "roles" : "members", id).name)
    .replace(/<a?:([A-Za-z0-9_]+):\d+>/g, (_, name) => `:${name}:`);
}
function targetKind(row) {
  const action = row.payload?.action_type;
  if ([10, 11, 12, 13, 14, 15, 110, 111, 112].includes(action)) return "channels";
  if ([30, 31, 32].includes(action)) return "roles";
  if ([20, 22, 23, 24, 25, 26, 27, 28, 72].includes(action)) return "members";
  return "unknown";
}
function fieldKind(key, row) {
  if (key === "subject" && row.kind === "GUILD_AUDIT_LOG_ENTRY_CREATE") return targetKind(row);
  if (["actor", "author", "subject", "user", "user_id", "inviter"].includes(key)) return "members";
  if (["channel", "channels", "channel_id", "parent_id", "previous_channel_id", "welcome_channel", "goodbye_channel", "audit_channel", "audit_ignored_channels", "afk_channel_id", "system_channel_id", "rules_channel_id"].includes(key)) return "channels";
  if (["role", "role_id", "roles", "roles_added", "roles_removed", "self_roles", "verification_role"].includes(key)) return "roles";
  if (key === "target_id") return targetKind(row);
  return null;
}
function displayValue(value, key, row, depth = 0) {
  const span = document.createElement("span"), kind = fieldKind(key, row);
  if (value === null || value === "") { span.textContent = "None"; return span; }
  if (key === "subject" && row.payload?.action_type === 1) { span.textContent = directory.server.name; return span; }
  if (key === "kind") { span.textContent = eventNames[value] || String(value).toLowerCase().replaceAll("_", " "); return span; }
  if (key === "action_type") { span.textContent = directory.audit_actions?.[String(value)] || "Other Discord action"; return span; }
  if (["permissions", "allow", "deny"].includes(key) && /^\d+$/.test(String(value))) {
    const bits = BigInt(value), labels = Object.entries(directory.permission_names || {}).filter(([flag]) => (bits & BigInt(flag)) !== 0n).map(([, name]) => name);
    span.textContent = labels.join(", ") || (bits === 0n ? "None" : "Permission details unavailable"); return span;
  }
  if (key === "color" && Number.isInteger(value)) { span.textContent = "#" + value.toString(16).padStart(6, "0").toUpperCase(); return span; }
  if (key === "communication_disabled_until" && value) { span.textContent = new Date(value).toLocaleString(); return span; }
  if (["observed", "created", "occurred_at"].includes(key) && typeof value === "number") { span.textContent = new Date(value * 1000).toLocaleString(); return span; }
  if (Array.isArray(value)) {
    const list = document.createElement("ul");
    for (const item of value) { const li = document.createElement("li"); li.append(displayValue(item, key, row, depth + 1)); list.append(li); }
    if (!value.length) span.textContent = "None";
    return value.length ? list : span;
  }
  if (kind && typeof value !== "object") return reference(kind, value);
  if (["guild", "guild_id"].includes(key)) { span.textContent = directory.server.name; return span; }
  if (value && typeof value === "object" && depth < 8) {
    if (kind === "roles") { span.textContent = value.name || entity("roles", value.id).name; return span; }
    if (key === "changes" && value.field) {
      const list = document.createElement("dl");
      const title = document.createElement("strong"); title.textContent = value.field.replaceAll("_", " ").replace(/^./, x => x.toUpperCase()); span.append(title);
      for (const side of ["before", "after"]) {
        if (!(side in value)) continue;
        const term = document.createElement("dt"), detail = document.createElement("dd"); term.textContent = side === "before" ? "Before" : "After";
        detail.append(displayValue(value[side], value.field, row, depth + 1)); list.append(term, detail);
      }
      span.append(list); return span;
    }
    if (key === "overwrite_target") return reference(value.type === 0 ? "roles" : "members", value.id);
    if (key === "permission_overwrites") {
      span.append(reference(value.type === 0 ? "roles" : "members", value.id));
      return span;
    }
    return displayFields(value, row, depth + 1);
  }
  if (key === "id" || key.endsWith("_id") || key === "ids") {
    if (row.kind?.startsWith("CHANNEL_") || row.kind?.startsWith("THREAD_")) return reference("channels", value);
    const channel = row.channel || row.payload?.channel_id;
    if (channel && (row.kind?.startsWith("MESSAGE_") || row.content !== undefined) && /^\d+$/.test(String(value))) {
      const link = document.createElement("a"); link.textContent = "Open message in " + entity("channels", channel).name;
      link.href = `https://discord.com/channels/${encodeURIComponent(directory.server.id)}/${encodeURIComponent(channel)}/${encodeURIComponent(value)}`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
    }
    span.textContent = key === "ids" || row.kind?.startsWith("MESSAGE_") ? "Message reference" : "Event reference"; return span;
  }
  span.textContent = typeof value === "boolean" ? (value ? "Yes" : "No") : translatedText(value);
  return span;
}
function displayFields(object, row, depth = 0) {
  const list = document.createElement("dl"); list.className = "record-fields";
  const hidden = new Set(["hash", "previous", "seq", "event_id", "display_member", "avatar_url", "request_id", "detail_digest"]);
  for (const [key, value] of Object.entries(object)) {
    if (hidden.has(key)) continue;
    if (key === "details" && value && !Object.keys(value).length) continue;
    const term = document.createElement("dt"), detail = document.createElement("dd");
    term.textContent = key.replace(/_id$/, "").replaceAll("_", " ").replace(/^./, x => x.toUpperCase());
    detail.append(displayValue(value, key, row, depth)); list.append(term, detail);
  }
  return list;
}
const eventNames = {
  GUILD_MEMBER_ADD: "Member joined", GUILD_MEMBER_REMOVE: "Member left",
  GUILD_MEMBER_UPDATE: "Member updated", GUILD_AUDIT_LOG_ENTRY_CREATE: "Discord audit entry",
  MESSAGE_CREATE: "Message posted", MESSAGE_DELETE: "Message deleted", MESSAGE_UPDATE: "Message edited",
  ACTION_REQUESTED: "Moderation requested", ACTION_RESULT: "Moderation result",
  SETTINGS_CHANGED: "Settings changed", ADMIN_LOGIN: "Administrator signed in",
  COLLECTOR_CONNECTED: "Collector connected",
  MUTE_PREPARED: "Persistent mute prepared", MUTE_APPLIED: "Muted until admin release",
  MUTE_NEEDS_REVIEW: "Mute needs administrator review",
  AUTOMOD_MESSAGE_DELETED: "Automatic moderation deleted a message", AUTOMOD_INFRACTION: "Private spam infraction",
  MUTE_RELEASE_REQUESTED: "Mute release requested", MUTE_RELEASED: "Mute revoked"
};
const descriptions = {
  dashboard: "Your Scrambled server at a glance.",
  review: "Review roles, channel access and permission inheritance.",
  activity: "Server activity from captured messages, reactions and voice events.",
  events: "Review your server's activity and moderation history.",
  messages: "Search messages captured in your private server index.",
  moderation: "Manage members and review every moderation request.",
  settings: "Configure greyBot's server features."
};
function notice(text) { $("#notice").textContent = text; $("#notice").hidden = !text; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...options.headers } });
  if (response.status === 401) { location.assign("/"); throw Error("Please sign in again"); }
  const body = await response.json();
  if (!response.ok) throw Error(typeof body.detail === "string" ? body.detail : "Check the form values and retry");
  return body;
}
function showRecords(selector, rows, label, append = false) {
  const host = $(selector);
  if (!append) host.replaceChildren();
  if (!rows.length && !host.children.length) {
    const empty = document.createElement("p"); empty.className = "empty";
    empty.textContent = selector === "#mute-list" ? "No persistent mutes are recorded." : selector === "#action-list" ? "No moderation requests are recorded." : "No records found. Collection begins when the worker connects; earlier history requires an explicit import.";
    host.append(empty); return;
  }
  for (const row of rows) {
    const node = document.createElement("details"), summary = document.createElement("summary"), title = document.createElement("span"), time = document.createElement("time");
    const expanded = {...row, ...(row.payload ? {payload: JSON.parse(row.payload)} : {}), ...(row.body ? {body: JSON.parse(row.body)} : {})};
    if ((row.kind?.startsWith("CHANNEL_") || row.kind?.startsWith("THREAD_")) && expanded.payload?.name && !entityMaps.channels.has(String(expanded.payload.id))) {
      entityMaps.channels.set(String(expanded.payload.id), {id: String(expanded.payload.id), name: expanded.payload.name, type: expanded.payload.type, active: false});
    }
    node.className = "record";
    if (row.display_member) {
      const profile = row.display_member, identity = document.createElement("span");
      identity.className = "member-identity";
      entityMaps.members.set(profile.id, profile);
      identity.title = `${profile.name}${profile.last_known ? " · Last known profile" : ""}`;
      if (profile.avatar_url?.startsWith("https://cdn.discordapp.com/")) {
        const avatar = document.createElement("img");
        avatar.src = profile.avatar_url; avatar.alt = ""; avatar.width = 28; avatar.height = 28;
        avatar.loading = "lazy"; avatar.referrerPolicy = "no-referrer";
        avatar.addEventListener("error", () => avatar.remove(), {once: true}); identity.append(avatar);
      }
      const name = document.createElement("span"); name.textContent = profile.name;
      if(selector !== "#event-list") identity.append(name);
      title.prepend(identity);
    }
    title.append(document.createTextNode(translatedText(label(row))));
    const channel = row.channel || expanded.payload?.channel_id;
    if (channel) { const context = document.createElement("span"); context.className = "record-context"; context.textContent = entity("channels", channel).name; title.append(context); }
    time.textContent = new Date((expanded.payload?.occurred_at || row.observed || row.created) * 1000).toLocaleString();
    const detail = displayFields(expanded, expanded);
    summary.append(title, time); node.append(summary, detail); host.append(node);
  }
}
async function loadEvents(older = false) {
  const query = new URLSearchParams(new FormData($("#event-filter")));
  if (older) query.set("before", cursor);
  const rows = await api("/api/events?" + query);
  showRecords("#event-list", rows, r => describeGreybotEvent(r, entity, directory.server.name), older);
  cursor = rows.length ? rows.at(-1).seq : cursor;
  $("#older").disabled = rows.length < 100;
}
async function loadMessages() {
  const rows = await api("/api/messages?" + new URLSearchParams(new FormData($("#message-filter"))));
  showRecords("#message-list", rows, r => (r.deleted ? "[Deleted] " : "") + r.content.slice(0, 110));
}
async function loadActions() {
  showRecords("#action-list", await api("/api/actions"), r => `${r.kind} · ${r.state}`);
  showRecords("#mute-list", await api("/api/mutes"), r => `Mute ${r.state.replaceAll("_", " ")} · until admin release`);
}
function renderSettings(settings) {
  revision = settings.revision;
  const form = $("#settings-form"), values = settings.values, host = $("#audit-events"), groups = new Map();
  form.elements.welcome_channel.value = values.welcome_channel;
  form.elements.verification_enabled.value = String(Boolean(values.verification_enabled));
  form.elements.verification_role.value = values.verification_role || "";
  form.elements.welcome_enabled.value = String(Boolean(values.welcome_enabled));
  form.elements.moderation_enabled.value = String(Boolean(values.moderation_enabled));
  form.elements.goodbye_enabled.value = String(Boolean(values.goodbye_enabled));
  form.elements.goodbye_channel.value = values.goodbye_channel || "";
  form.elements.audit_feed_enabled.value = String(Boolean(values.audit_feed_enabled));
  form.elements.audit_channel.value = values.audit_channel;
  setMultiple(form.querySelector('[data-label="Ignored channels"]'), values.audit_ignored_channels);
  setMultiple(form.querySelector('[data-label="Self-service roles"]'), values.self_roles || []);
  document.querySelectorAll(".entity-picker").forEach(syncPicker);
  form.elements.audit_ignore_bots.checked = values.audit_ignore_bots;
  form.elements.audit_show_avatars.checked = values.audit_show_avatars;
  auditDefaults = settings.audit_default_events;
  host.replaceChildren();
  for (const event of settings.audit_event_catalog) {
    if (!groups.has(event.group)) {
      const group = document.createElement("fieldset"), legend = document.createElement("legend");
      legend.textContent = event.group; group.append(legend); host.append(group); groups.set(event.group, group);
    }
    const label = document.createElement("label"), input = document.createElement("input");
    label.className = "check-row"; input.type = "checkbox"; input.name = "audit_events"; input.value = event.key;
    input.checked = values.audit_events.includes(event.key); label.append(input, document.createTextNode(event.label));
    groups.get(event.group).append(label);
  }
}
async function refresh() {
  const status = await api("/api/status"); csrf = status.csrf;
  await loadDirectory();
  const dashboard = await api("/api/dashboard");
  entityMaps.members.set(dashboard.member.id, dashboard.member);
  $("#signed-in-member").replaceChildren(reference("members", dashboard.member.id));
  renderHealth(dashboard.health);
  $("#mode").textContent = status.enforcing ? "Actions enabled" : "Observe only";
  $("#integrity").textContent = status.journal_valid ? "Chain verified" : "Check failed";
  $("#pending").textContent = status.archive_pending;
  $("#feed-issues").textContent = status.feed_issues || 0;
  $("#archive-note").textContent = status.archive_mode === "nas" ? "Audit copies on greyNAS. No dashboard editing or deletion; the NAS owner retains recovery and removal access." : status.archive_configured ? `Locked archive configured for ${status.retention_days} days. Pending records are not yet protected.` : "Local journal only. Audit archive is not configured.";
  $("#submit-action").disabled = !status.enforcing;
  $("#moderation-note").textContent = status.enforcing ? "Your access and role hierarchy are checked before each action." : "Moderation actions are not enabled yet.";
  $("#search-note").textContent = status.content_indexing ? "Deleted messages are available only if previously captured." : "Message text collection is currently off; imported records remain searchable.";
  if (active === "events") await loadEvents();
  if (active === "messages") await loadMessages();
  if (active === "moderation") await loadActions();
  if (active === "settings") renderSettings(await api("/api/settings"));
  if (active === "review") await loadReview();
  if (active === "activity") await loadActivity();
}
function handle(fn) { return async (event) => { event?.preventDefault(); notice(""); try { await fn(event); } catch (error) { notice(error.message); } }; }
document.querySelectorAll("[data-panel]").forEach(button => button.addEventListener("click", handle(async () => {
  active = button.dataset.panel;
  history.replaceState(null,"", "#"+active);
  document.querySelectorAll(".panel").forEach(p => { p.hidden = p.id !== active; });
  document.querySelectorAll("[data-panel]").forEach(b => b.classList.toggle("active", b === button));
  $("#heading").textContent = {dashboard: "Dashboard", review: "Needs Review", activity: "Activity scoreboard", events: "Event history", messages: "Message search", moderation: "Moderation", settings: "Settings"}[active];
  $("#page-description").textContent = descriptions[active]; await refresh();
})));
$("#refresh").addEventListener("click", handle(refresh));
$("#older").addEventListener("click", handle(() => loadEvents(true)));
$("#event-filter").addEventListener("submit", handle(() => loadEvents()));
$("#message-filter").addEventListener("submit", handle(loadMessages));
$("#logout").addEventListener("click", handle(async () => { await api("/auth/logout", { method: "POST" }); location.assign("/"); }));
$("#settings-form").addEventListener("submit", handle(async () => {
  const form = $("#settings-form"), data = new FormData(form);
  const result = await api("/api/settings", { method: "PUT", body: JSON.stringify({
    revision, self_roles: data.getAll("self_roles").filter(Boolean), welcome_channel: form.elements.welcome_channel.value,
    verification_enabled: form.elements.verification_enabled.value === "true", verification_role: form.elements.verification_role.value,
    welcome_enabled: form.elements.welcome_enabled.value === "true",
    moderation_enabled: form.elements.moderation_enabled.value === "true",
    audit_channel: form.elements.audit_channel.value, audit_feed_enabled: form.elements.audit_feed_enabled.value === "true",
    goodbye_enabled: form.elements.goodbye_enabled.value === "true", goodbye_channel: form.elements.goodbye_channel.value,
    audit_events: data.getAll("audit_events"), audit_ignore_bots: form.elements.audit_ignore_bots.checked,
    audit_show_avatars: form.elements.audit_show_avatars.checked,
    audit_ignored_channels: data.getAll("audit_ignored_channels").filter(Boolean)
  }) });
  revision = result.revision; notice("Settings saved. Automatic moderation: " + (result.values.moderation_enabled ? "on" : "off") + "; audit posting: " + (result.values.audit_feed_enabled ? "on" : "off") + "; departures: " + (result.values.goodbye_enabled ? "on" : "off") + ".");
}));
$("#publish-role-panel").addEventListener("click", handle(async () => {
  const button = $("#publish-role-panel"), form = $("#settings-form");
  button.dataset.requestId ||= crypto.randomUUID().replaceAll("-", "");
  await api("/api/role-panel", {method: "POST", body: JSON.stringify({
    request_id: button.dataset.requestId, channel: form.elements.role_panel_channel.value
  })});
  button.disabled = true;
  notice("Role panel queued. Check Moderation → Recent requests for its result before publishing another.");
}));
$("#audit-preset").addEventListener("click", () => {
  const form = $("#settings-form");
  form.querySelectorAll('[name="audit_events"]').forEach(input => { input.checked = auditDefaults.includes(input.value); });
  form.elements.audit_ignore_bots.checked = false; form.elements.audit_show_avatars.checked = true;
  setMultiple(form.querySelector('[data-label="Ignored channels"]'), []);
  notice("Event preset selected. Save settings to apply it with the posting mode shown above.");
});
function syncActionDuration() { $("#timeout-duration").hidden = $("#action-form").elements.action.value !== "timeout"; }
$("#action-form").elements.action.addEventListener("change", syncActionDuration);
$("#action-form").addEventListener("submit", handle(async () => {
  const form = $("#action-form"), body = Object.fromEntries(new FormData(form));
  if (!body.user) throw Error("Select a member from the name search first");
  body.minutes = Number(body.minutes); body.request_id = form.dataset.requestId || crypto.randomUUID();
  form.dataset.requestId = body.request_id;
  await api("/api/actions", { method: "POST", body: JSON.stringify(body) });
  delete form.dataset.requestId; form.reset(); syncActionDuration(); form.querySelectorAll(".entity-picker").forEach(syncPicker); await loadActions(); notice("Action queued for authorization and archive checks.");
}));
function localTime(value) { return value ? new Date(value * 1000).toLocaleString() : "Not yet observed"; }
function renderHealth(health) {
  const current = health.status, stale = current && Date.now() / 1000 - current.last > 90;
  const summary = current?.boot ? `Up since ${localTime(current.boot)}` : "Host boot time not yet available";
  $("#host-uptime").textContent = summary + (stale ? " · Monitor overdue" : "");
  $("#health-summary").textContent = `${summary}. Monitoring since ${localTime(current?.first)}. Last seen ${localTime(current?.last)}.`;
  $("#health-note").textContent = health.note;
  $("#health-gaps").replaceChildren();
  for (const gap of health.gaps) {
    const row = document.createElement("div"); row.className = "record";
    row.textContent = `${gap.connection ? "Discord connection outage" : gap.restart_confirmed ? "NAS restart detected" : "Monitoring gap"}: ${localTime(gap.last_seen)} → ${gap.recovered ? localTime(gap.recovered) : "Ongoing"}${gap.recovered ? ` (${Math.round(gap.recovered-gap.last_seen)} seconds between observations)` : ""}`;
    $("#health-gaps").append(row);
  }
  if (!health.gaps.length) $("#health-gaps").textContent = "No recorded monitoring gaps.";
}
async function loadReview() {
  const data = await api("/api/review"), host = $("#review-list"); host.replaceChildren();
  for (const item of data.items) {
    const row = document.createElement("label"), check = document.createElement("input"), content = document.createElement("span");
    row.className = "record check-row"; check.type = "checkbox"; check.checked = item.checked;
    if (item.kind === "retire_role") { content.append("Remove the retired ", reference("roles", item.role), " role; review affected members: "); for (const id of item.members) content.append(reference("members", id), " "); }
    if (item.kind === "unsynced_channel") content.append(reference("channels", item.channel), " does not inherit permissions from ", reference("channels", item.category));
    if (item.kind === "alternate_access") {
      content.append(reference("members", item.member), " can access ", reference("channels", item.channel), " without any of its explicitly allowed roles: ");
      for (const id of item.roles) content.append(reference("roles", id), " ");
      content.append(` · ${item.source}. Confirm intended access.`);
    }
    check.addEventListener("change", handle(async () => {
      try { await api("/api/review", {method:"POST",body:JSON.stringify({item:item.id,checked:check.checked})}); }
      catch (error) { check.checked = !check.checked; throw error; }
    }));
    row.append(check, content); host.append(row);
  }
  if (!data.items.length) host.textContent = "No current review items.";
}
async function loadActivity() {
  const data = await api("/api/activity"); $("#activity-method").textContent = data.method;
  $("#activity-rows").replaceChildren();
  data.rows.forEach((item, index) => {
    const row = document.createElement("tr");
    for (const value of [index+1, reference("members",item.member),item.score,item.messages,item.reactions,Math.floor(item.voice_seconds/60)]) {
      const cell=document.createElement("td"); cell.append(value instanceof Node ? value : document.createTextNode(value)); row.append(cell);
    }
    $("#activity-rows").append(row);
  });
}
const selectedRoleMembers = new Set(); let reviewedRoleChange = null;
function invalidateRoleReview() { reviewedRoleChange = null; $("#role-confirmation").hidden = true; }
function filteredRoleMembers() {
  const role=$("#role-member-filter").value, term=$("#role-member-search").value.toLocaleLowerCase();
  return directory.members.filter(m=>m.active && (!role || m.roles?.includes(role)) && m.name.toLocaleLowerCase().includes(term));
}
function renderRoleMembers() {
  const host=$("#role-member-options"); host.replaceChildren();
  for (const member of filteredRoleMembers()) {
    const label=document.createElement("label"), check=document.createElement("input"); label.className="check-row"; check.type="checkbox"; check.checked=selectedRoleMembers.has(member.id);
    check.addEventListener("change",()=>{check.checked?selectedRoleMembers.add(member.id):selectedRoleMembers.delete(member.id); invalidateRoleReview(); $("#role-selection-count").textContent=`· ${selectedRoleMembers.size} selected`;});
    label.append(check,reference("members",member.id)); host.append(label);
  }
  $("#role-selection-count").textContent=`· ${selectedRoleMembers.size} selected`;
}
$("#open-member-roles").addEventListener("click",()=>{
  const filter=$("#role-member-filter"); filter.replaceChildren(new Option("All members",""));
  directory.roles.filter(r=>r.active).forEach(r=>filter.add(new Option(r.name,r.id)));
  renderRoleMembers(); $("#roles-dialog").showModal();
});
$("#open-health").addEventListener("click",()=>$("#health-dialog").showModal());
document.querySelectorAll("[data-close]").forEach(b=>b.addEventListener("click",()=>document.getElementById(b.dataset.close).close()));
$("#role-member-search").addEventListener("input",renderRoleMembers);
$("#role-member-filter").addEventListener("change",renderRoleMembers);
$("#select-visible-members").addEventListener("click",()=>{filteredRoleMembers().forEach(m=>selectedRoleMembers.add(m.id)); invalidateRoleReview();renderRoleMembers();});
$("#clear-role-members").addEventListener("click",()=>{selectedRoleMembers.clear();invalidateRoleReview();renderRoleMembers();});
$("#member-roles-form").addEventListener("submit",handle(async()=>{
  const form=$("#member-roles-form"), role=form.elements.role.value;
  if (!role || !selectedRoleMembers.size) throw Error("Select members and a role first");
  reviewedRoleChange={members:[...selectedRoleMembers].sort(),role,operation:form.elements.operation.value,request_id:crypto.randomUUID()};
  $("#role-confirmation-text").textContent=`${reviewedRoleChange.operation === "add" ? "Add" : "Remove"} ${entity("roles",role).name} for ${selectedRoleMembers.size} selected members?`;
  $("#role-confirmation").hidden=false;
}));
$("#apply-member-roles").addEventListener("click",handle(async()=>{
  if(!reviewedRoleChange) return;
  const form=$("#member-roles-form");
  if (form.elements.role.value!==reviewedRoleChange.role || form.elements.operation.value!==reviewedRoleChange.operation) {invalidateRoleReview();throw Error("Selection changed; review it again");}
  const button=$("#apply-member-roles"); button.disabled=true;
  try { const result=await api("/api/member-roles",{method:"POST",body:JSON.stringify(reviewedRoleChange)}); $("#role-result").textContent=`${result.jobs.length} requests queued; check Moderation for each result.`; invalidateRoleReview(); }
  finally {button.disabled=false;}
}));
const initialPanel = document.querySelector(`[data-panel="${["dashboard","review","events","messages","moderation","settings","activity"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "dashboard"}"]`);
initialPanel.click();
