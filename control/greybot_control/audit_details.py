"""Explicitly supported Discord change values, without arbitrary audit payloads."""

# Values are captured only for actions where the field has a known meaning.
FIELDS = {
    1: {"name", "verification_level", "explicit_content_filter", "afk_channel_id", "afk_timeout", "system_channel_id", "rules_channel_id"},
    10: {"name", "type", "nsfw", "bitrate", "user_limit", "rate_limit_per_user", "parent_id"},
    11: {"name", "type", "nsfw", "bitrate", "user_limit", "rate_limit_per_user", "parent_id"},
    12: {"name", "type"},
    13: {"allow", "deny"}, 14: {"allow", "deny"}, 15: {"allow", "deny"},
    24: {"nick", "communication_disabled_until", "mute", "deaf"},
    30: {"name", "color", "hoist", "mentionable", "permissions", "position"},
    31: {"name", "color", "hoist", "mentionable", "permissions", "position"},
    32: {"name", "color", "hoist", "mentionable", "permissions", "position"},
}


def project(entry):
    action = entry.get("action_type")
    result = {}
    changes = []
    for change in entry.get("changes", []):
        key = change.get("key")
        if action == 25 and key in {"$add", "$remove"}:
            roles = change.get("new_value")
            if isinstance(roles, list):
                result["roles_added" if key == "$add" else "roles_removed"] = [
                    {"id": str(role["id"]), "name": role.get("name", "")[:100]}
                    for role in roles if isinstance(role, dict) and str(role.get("id", "")).isdecimal()
                    and isinstance(role.get("name", ""), str)]
        elif key in FIELDS.get(action, set()):
            item = {"field": key}
            for source, target in (("old_value", "before"), ("new_value", "after")):
                if source in change and (change[source] is None or isinstance(change[source], (str, bool, int))):
                    if key in {"permissions", "allow", "deny", "color", "type"} and change[source] is not None:
                        if not str(change[source]).isdecimal() or int(change[source]) >= 1 << 64:
                            continue
                    item[target] = change[source][:256] if isinstance(change[source], str) else change[source]
            if len(item) > 1:
                changes.append(item)
    if changes:
        result["changes"] = changes
    if action in {13, 14, 15}:
        options = entry.get("options") or {}
        if str(options.get("id", "")).isdecimal() and str(options.get("type")) in {"0", "1"}:
            result["overwrite_target"] = {"id": str(options["id"]), "type": int(options["type"])}
            if isinstance(options.get("role_name"), str):
                result["overwrite_target"]["name"] = options["role_name"][:100]
    eid = str(entry.get("id", ""))
    if eid.isdecimal() and len(eid) >= 16:
        result["occurred_at"] = ((int(eid) >> 22) + 1420070400000) / 1000
    return result
