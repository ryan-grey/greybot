"""Project Gateway events into a scoped journal, excluding session material."""

import json
import re
from datetime import datetime, timezone

from .store import digest
from .profiles import display_profile
from .audit_details import project as audit_details


EVENTS = {
    "GUILD_MEMBER_ADD", "GUILD_MEMBER_REMOVE", "GUILD_MEMBER_UPDATE",
    "GUILD_BAN_ADD", "GUILD_BAN_REMOVE", "GUILD_UPDATE", "GUILD_EMOJIS_UPDATE",
    "GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE", "GUILD_ROLE_DELETE",
    "CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE",
    "THREAD_CREATE", "THREAD_UPDATE", "THREAD_DELETE",
    "MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE", "MESSAGE_DELETE_BULK",
    "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE",
    "INVITE_CREATE", "INVITE_DELETE", "VOICE_STATE_UPDATE",
    "GUILD_AUDIT_LOG_ENTRY_CREATE", "AUTO_MODERATION_ACTION_EXECUTION",
}


class Collector:
    def __init__(self, cfg, store):
        self.cfg, self.store, self.session = cfg, store, ""
        self.voice = {}

    def receive(self, packet):
        if packet.get("op") != 0:
            return
        kind, data = packet.get("t"), packet.get("d") or {}
        if kind == "READY":
            # Only a one-way digest participates in event IDs. Never persist a
            # resumable session ID, resume URL, auth token, or READY payload.
            self.session = digest(data["session_id"])
            self.store.append("ready:" + self.session, self.cfg.guild_id, "COLLECTOR_CONNECTED", "",
                              {"history_complete": False, "note": "New session; gaps may exist before connection"})
            return
        if kind == "GUILD_CREATE" and str(data.get("id")) == self.cfg.guild_id:
            self.voice = {str(v["user_id"]): v.get("channel_id") for v in data.get("voice_states", [])}
            return
        if not self.session or kind not in EVENTS:
            return
        guild = str(data.get("guild_id") or (data.get("id") if kind == "GUILD_UPDATE" else ""))
        if guild != self.cfg.guild_id:
            return
        sequence = packet.get("s")
        if not isinstance(sequence, int):
            return
        event_id = f"gateway:{self.session}:{sequence}"
        if kind == "GUILD_AUDIT_LOG_ENTRY_CREATE" and data.get("id"):
            if self.store.audit_seen(guild, str(data["id"])):
                return
            event_id = f"discord-audit:{guild}:{data['id']}"
        if self.store.seen(event_id):
            return
        messages, details = [], {}
        user = data.get("user") or {}
        author = data.get("author") or {}
        profile_user = user or author
        if profile_user.get("id") and profile_user.get("username"):
            known = self.store.profile(guild, str(profile_user["id"]))
            if kind in {"GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE"} or not known:
                member = data if kind.startswith("GUILD_MEMBER_") else data.get("member") or {}
                self.store.save_profile(guild, str(profile_user["id"]), display_profile(guild, profile_user, member))
        subject = str(user.get("id") or data.get("user_id") or data.get("target_id") or author.get("id") or "")
        # Preserve IDs and event facts, never entire Discord API objects. Invite
        # codes, webhook URLs, attachments and audit-log reasons are excluded.
        payload = {key: data[key] for key in (
            "id", "channel_id", "message_id", "role_id", "roles", "ids", "pending",
            "joined_at", "communication_disabled_until", "action_type", "target_id",
            "user_id", "mute", "deaf", "self_mute", "self_deaf") if key in data}
        if kind == "GUILD_MEMBER_UPDATE" and user.get("username"):
            fresh = display_profile(guild, user, data)
            payload["profile_changed"] = bool(known and any(known["value"].get(key) != fresh.get(key) for key in ("avatar_url", "username")))
        if kind == "VOICE_STATE_UPDATE":
            before = self.voice.get(subject)
            after = data.get("channel_id")
            payload["feed_voice_changes"] = ([] if before == after else
                (["voice_left"] if before else []) + (["voice_joined"] if after else []))
            self.voice[subject] = after
            if before:
                payload["previous_channel_id"] = before
        if user:
            payload["user_id"] = user.get("id", "")
        if kind.startswith("GUILD_ROLE_") and data.get("role"):
            role = data["role"]
            payload["role"] = {k: role[k] for k in ("id", "name", "permissions", "position") if k in role}
        if kind.startswith("CHANNEL_") or kind.startswith("THREAD_"):
            payload.update({k: data[k] for k in ("name", "type", "parent_id", "permission_overwrites") if k in data})
        if kind == "GUILD_AUDIT_LOG_ENTRY_CREATE":
            payload["actor"] = data.get("user_id")
            payload.update(audit_details(data))
            # Audit subjects are the affected member, channel, or role, not actor.
            if data.get("target_id"):
                subject = str(data["target_id"])
            # Changed field names are safe metadata; arbitrary values may hold
            # secrets/private content and need an explicit capture policy.
            payload["changed_fields"] = [c.get("key") for c in data.get("changes", [])]
            for change in data.get("changes", []):
                if change.get("key") == "communication_disabled_until":
                    value = change.get("new_value")
                    payload["timeout_active"] = bool(value and datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(timezone.utc))
        if kind in {"MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE"}:
            mid = str(data["id"])
            prior = self.store.message(guild, mid)
            if kind == "MESSAGE_UPDATE":
                payload["text_edited"] = bool("content" in data and (
                    data["content"] != prior["content"] if prior else data.get("edited_timestamp")))
            if kind == "MESSAGE_CREATE":
                payload["contains_invite"] = bool(re.search(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)[A-Za-z0-9-]+", data.get("content", ""), re.I))
            payload["prior_message_observed"] = prior is not None
            if not subject and prior:
                subject = prior["author"]
            if self.cfg.capture_content:
                content = data.get("content", prior["content"] if prior else "")
                if kind == "MESSAGE_UPDATE" and payload.get("text_edited"):
                    if prior:
                        details["content_before"] = prior["content"]
                    if "content" in data:
                        details["content_after"] = data["content"]
                elif kind == "MESSAGE_DELETE" and prior:
                    details["content_before"] = prior["content"]
                # Text is in the private search index only, not the immutable
                # archive. Message deletion cannot recover unseen message text.
                messages.append((mid, str(data.get("channel_id", "")),
                                 subject, content, kind == "MESSAGE_DELETE"))
        if kind == "MESSAGE_DELETE_BULK" and self.cfg.capture_content:
            for mid in data.get("ids", []):
                prior = self.store.message(guild, str(mid))
                if prior:
                    messages.append((str(mid), prior["channel"], prior["author"], prior["content"], True))
        if kind == "INVITE_CREATE":
            payload["inviter"] = (data.get("inviter") or {}).get("id")
            payload["attribution"] = "No member attribution inferred from invite creation"
        self.store.record_event(event_id, guild, kind, subject, payload, messages, details)

    def raw(self, message):
        try:
            packet = json.loads(message)
        except (ValueError, TypeError, UnicodeDecodeError):
            return
        if isinstance(packet, dict):
            self.receive(packet)
