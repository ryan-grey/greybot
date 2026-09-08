"""Discord feed preferences, independent of the complete private journal.

The dispatcher applies these preferences only to new Discord posts; complete
private collection and search remain independent.
"""

AUDIT_EVENTS = {
    "member_muted": ("Moderation", "Member muted"),
    "member_unmuted": ("Moderation", "Member unmuted"),
    "moderation_ban": ("Moderation", "Moderation ban"),
    "moderation_unban": ("Moderation", "Moderation unban"),
    "message_updated": ("Messages", "Message edited"),
    "message_deleted": ("Messages", "Message deleted"),
    "invite_posted": ("Messages", "Invite posted"),
    "nickname_changed": ("Members", "Nickname changed"),
    "member_banned": ("Members", "Member banned"),
    "member_joined": ("Members", "Member joined"),
    "member_left": ("Members", "Member left"),
    "member_unbanned": ("Members", "Member unbanned"),
    "user_updated": ("Members", "User profile updated"),
    "role_created": ("Roles", "Role created"),
    "role_updated": ("Roles", "Role updated"),
    "role_deleted": ("Roles", "Role deleted"),
    "member_roles_changed": ("Roles", "Member roles changed"),
    "voice_joined": ("Voice", "Joined voice channel"),
    "voice_left": ("Voice", "Left voice channel"),
    "server_edited": ("Server", "Server edited"),
    "emojis_updated": ("Server", "Emojis updated"),
    "channel_created": ("Channels", "Channel created"),
    "channel_updated": ("Channels", "Channel updated"),
    "channel_deleted": ("Channels", "Channel deleted"),
}
DEFAULT_AUDIT_EVENTS = tuple(key for key in AUDIT_EVENTS if not key.startswith("voice_"))


def default_feed():
    return {"audit_feed_enabled": False, "audit_channel": "",
            "audit_events": list(DEFAULT_AUDIT_EVENTS), "audit_ignore_bots": False,
            "audit_show_avatars": True, "audit_ignored_channels": []}


def event_catalog():
    return [{"key": key, "group": group, "label": label}
            for key, (group, label) in AUDIT_EVENTS.items()]
