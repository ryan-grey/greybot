"use strict";
globalThis.describeGreybotEvent = function(row, resolve, serverName = "the server") {
  const p = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload || {};
  const name = (kind, id, fallback) => id ? resolve(kind, String(id))?.name || fallback : fallback;
  const member = row.display_member?.name || name("members", row.subject, "A member");
  const actor = name("members", p.actor || p.user_id, "An administrator");
  const channel = name("channels", p.channel_id || p.channel || row.channel, "a channel");
  const target = p.target_id || row.subject;
  const changed = (p.changes || []).map(c => ({nick:"server nickname",name:"name",color:"role color",permissions:"permissions",
    allow:"allowed permissions",deny:"denied permissions",communication_disabled_until:"timeout",mute:"server mute",deaf:"server deafen",
    parent_id:"category",rate_limit_per_user:"slow mode",nsfw:"age restriction",hoist:"role grouping",mentionable:"role mentions",
    bitrate:"voice quality",user_limit:"voice capacity",position:"role order"}[c.field] || c.field.replaceAll("_"," ")));
  const changes = changed.length ? " (" + changed.join(", ") + ")" : "";
  if (row.kind === "GUILD_AUDIT_LOG_ENTRY_CREATE") {
    const roleNames = values => (values || []).map(r => name("roles",r.id,r.name || "an unavailable role")).join(", ");
    const targetChannel = name("channels", target, "a deleted or unavailable channel");
    const targetRole = name("roles", target, "a deleted or unavailable role");
    switch(p.action_type) {
      case 1: return `${actor} updated ${serverName}${changes}`;
      case 10: return `${actor} created ${targetChannel}`;
      case 11: return `${actor} edited ${targetChannel}${changes}`;
      case 12: return `${actor} deleted ${targetChannel}`;
      case 13: case 14: case 15: {
        const who = p.overwrite_target ? name(p.overwrite_target.type===0?"roles":"members",p.overwrite_target.id,"a role or member") : "a role or member";
        return `${actor} ${p.action_type===15?"removed":"changed"} ${who}'s permissions in ${targetChannel}${changes}`;
      }
      case 20: return `${actor} kicked ${member} from the server`;
      case 21: return `${actor} removed inactive members`;
      case 22: return `${actor} banned ${member}`;
      case 23: return `${actor} removed ${member}'s ban`;
      case 24: return `${actor} changed ${member}'s member settings${changes}`;
      case 25: {
        const details=[];
        if(p.roles_added?.length) details.push(`added ${roleNames(p.roles_added)}`);
        if(p.roles_removed?.length) details.push(`removed ${roleNames(p.roles_removed)}`);
        return details.length ? `${actor} ${details.join(" and ")} for ${member}` : `${actor} changed ${member}'s roles (earlier role details were not captured)`;
      }
      case 26: return `${actor} moved members to another voice channel`;
      case 27: return `${actor} disconnected members from voice`;
      case 28: return `${actor} added a bot to the server`;
      case 30: return `${actor} created the ${targetRole} role`;
      case 31: return `${actor} edited the ${targetRole} role${changes}`;
      case 32: return `${actor} deleted the ${targetRole} role`;
      case 40: return `${actor} created a server invite`;
      case 41: return `${actor} edited a server invite`;
      case 42: return `${actor} deleted a server invite`;
      case 50: case 51: case 52: return `${actor} ${p.action_type===50?"created":p.action_type===51?"edited":"deleted"} a webhook`;
      case 60: case 61: case 62: return `${actor} ${p.action_type===60?"added":p.action_type===61?"edited":"removed"} a server emoji`;
      case 72: return `${actor} deleted messages from ${member}`;
      case 73: return `${actor} deleted multiple messages`;
      case 74: return `${actor} pinned a message`;
      case 75: return `${actor} unpinned a message`;
      case 110: case 111: case 112: return `${actor} ${p.action_type===110?"created":p.action_type===111?"edited":"deleted"} a thread`;
      default: return `${actor} made a server change${changes}; open to see the captured details`;
    }
  }
  const action = {admin_role:"a role change",channel_visibility:"a channel visibility change",verify_role:"membership verification",
    self_role:"a self-service role change",role_panel:"a role selection post",mute:"a persistent mute",unmute:"a mute removal",timeout:"a timeout",kick:"a kick",ban:"a ban",automod:"automatic moderation"}[p.action] || "an administrative action";
  const labels={
    GUILD_MEMBER_ADD:`${member} joined the server`, GUILD_MEMBER_REMOVE:`${member} left the server`,
    GUILD_MEMBER_UPDATE:`Discord updated ${member}'s member profile or role list`,
    GUILD_BAN_ADD:`${member} was banned`,GUILD_BAN_REMOVE:`${member}'s ban was removed`,
    MESSAGE_CREATE:`${member} posted a message in ${channel}`, MESSAGE_UPDATE:`A message in ${channel} was updated${p.text_edited?" with edited text":""}`,
    MESSAGE_DELETE:`A message from ${member} was deleted in ${channel}`, MESSAGE_DELETE_BULK:`Multiple messages were deleted in ${channel}`,
    MESSAGE_REACTION_ADD:`${member} reacted to a message in ${channel}`,MESSAGE_REACTION_REMOVE:`${member} removed a reaction in ${channel}`,
    VOICE_STATE_UPDATE:`${member} ${p.feed_voice_changes?.includes("voice_joined")?"joined "+channel:p.feed_voice_changes?.includes("voice_left")?"left "+name("channels",p.previous_channel_id,"voice"):"changed their voice settings"}`,
    ADMIN_LOGIN:`${member} signed in to the admin site`,SETTINGS_CHANGED:`${actor} changed greyBot's settings`,
    ACTION_REQUESTED:`${actor} requested ${action} for ${member}`,
    ACTION_RESULT:`greyBot finished an action for ${member}: ${{completed:"completed",denied:"blocked by permission checks",unknown:"result uncertain; check Discord before retrying"}[p.state] || p.state || "open for its recorded result"}`,
    COLLECTOR_CONNECTED:"greyBot connected to Discord and began recording server events",
    COLLECTOR_RESUMED:"greyBot reconnected to Discord and resumed recording server events",
    COLLECTOR_DISCONNECTED:"greyBot lost its Discord connection; event collection may have a gap",
    HOST_HEALTH_GAP:p.restart_confirmed?"greyNAS restarted between health checks":"greyNAS monitoring missed a period; open for the observed time window",
    REVIEW_CHECKED:`${actor} ${p.checked?"checked off":"reopened"} a Needs Review item`,
    CHANNEL_VISIBILITY_APPLIED:`${member} ${p.hidden?"hid":"restored"} ${channel} in their channel list`,
    MUTE_APPLIED:`${member} was muted until an administrator revokes it`, MUTE_RELEASED:`${member}'s persistent mute was removed`,
    MUTE_PREPARED:`greyBot prepared ${member}'s mute and saved the original permissions`,MUTE_NEEDS_REVIEW:`${member}'s mute needs administrator attention`,
    MUTE_RELEASE_REQUESTED:`An administrator requested removal of ${member}'s mute`,
    AUTOMOD_MESSAGE_DELETED:`greyBot removed a message from ${member} for automatic moderation`,AUTOMOD_INFRACTION:`greyBot recorded a spam infraction for ${member}`,
    ROLE_PANEL_PUBLISHED:`greyBot posted role selection buttons in ${channel}`,
    RAID_HISTORY_IMPORTED:`greyBot preserved ${p.title || "a raid event"} and ${p.signup_count ?? 0} signup records from ${channel}`,
    RAID_CREATED:`${actor} created ${p.title || "a raid signup"} in ${channel}`,
    RAID_REQUEST_REJECTED:`greyBot could not apply ${member}'s raid request: ${p.reason || "access or event checks failed"}`,
    RAID_CHANGED:`${actor} ${ {signup:"changed their signup for",withdraw:"withdrew from",note:"updated their note for",status:"changed their attendance status for",edit:"edited",close:"closed signups for",open:"reopened signups for",cancel:"cancelled"}[p.operation] || "updated"} ${p.title || "a raid event"}`,
    ONBOARDING_GATE_PLANNED:"greyBot saved the planned new-member channel restrictions",
    ONBOARDING_GATE_STEP_VERIFIED:"greyBot applied and verified one new-member permission change",
    ONBOARDING_GATE_APPLIED:"greyBot finished applying the new-member verification gate",
    INVITE_CREATE:"A new server invite was created",INVITE_DELETE:"A server invite was deleted",
    GUILD_UPDATE:"The server's settings changed",GUILD_EMOJIS_UPDATE:"The server's emojis changed",
    AUTO_MODERATION_ACTION_EXECUTION:"Discord applied an automatic moderation rule"
  };
  if(labels[row.kind])return labels[row.kind];
  if (/^(CHANNEL|THREAD|GUILD_ROLE)_(CREATE|UPDATE|DELETE)$/.test(row.kind)) {
    const type=row.kind.startsWith("GUILD_ROLE")?"role":row.kind.startsWith("THREAD")?"thread":"channel";
    return `A ${type} was ${{CREATE:"created",UPDATE:"updated",DELETE:"deleted"}[row.kind.split("_").at(-1)]}${p.name?": "+p.name:""}`;
  }
  return "greyBot recorded a server operation; open to see what was captured";
};
