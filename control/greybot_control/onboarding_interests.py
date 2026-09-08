"""Build native onboarding using permission-free interest roles only."""
from copy import deepcopy

from .discord_api import Denied


def interest_payload(onboarding, roles, channels, option_roles, verified_role):
    """Keep choices and channel preferences, but never assign an access role.

    Role mappings must cover every option exactly. Neutral roles carry zero
    guild permissions and have no channel overwrites of either kind; combining
    all choices therefore cannot grant any permission to a new member.
    This changes onboarding configuration, never existing member assignments.
    """
    options = [o for p in onboarding["prompts"] for o in p["options"]]
    if set(option_roles) != {o["id"] for o in options}:
        raise Denied("Map every onboarding choice to a harmless interest role")
    by_id = {r["id"]: r for r in roles}
    for rid in option_roles.values():
        role = by_id.get(rid)
        if (not role or rid in {verified_role, onboarding["guild_id"]}
                or role.get("managed") or int(role["permissions"]) != 0
                or any(o["id"] == rid for c in channels for o in c.get("permission_overwrites", []))):
            raise Denied("Onboarding interest roles must have no permissions or channel overrides")
    prompts = deepcopy(onboarding["prompts"])
    for prompt in prompts:
        for option in prompt["options"]:
            option["role_ids"] = [option_roles[option["id"]]]
            emoji = option.pop("emoji", None)
            if emoji and emoji.get("id"):
                option["emoji_id"] = emoji["id"]
            elif emoji and emoji.get("name"):
                option["emoji_name"] = emoji["name"]
    return {"prompts": prompts, **{k: onboarding[k] for k in ("default_channel_ids", "enabled", "mode")}}
