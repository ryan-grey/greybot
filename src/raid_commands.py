"""Raid command definitions registered by the Lambda entry point."""
def commands():
    common = [{"type": 7, "name": "channel", "description": "Where to publish", "channel_types": [0, 5]},
              {"type": 3, "name": "template", "description": "Signup choices", "choices": [
                  {"name": "Simple attendance", "value": "standard"}, {"name": "WoW classes and specs", "value": "wowretail1"},
                  {"name": "WoW combat roles", "value": "wowretail2"}]}]
    return [{"name": "create", "description": "Create a raid signup with a guided form", "default_member_permissions": "32", "dm_permission": False, "options": common},
            {"name": "quickcreate", "description": "Create a raid signup in one command", "default_member_permissions": "32", "dm_permission": False,
             "options": [{"type": 3, "name": "title", "description": "Event title", "required": True, "max_length": 200},
                         {"type": 3, "name": "when", "description": "Date with UTC offset, e.g. 2026-09-12T18:00-07:00", "required": True},
                         *common, {"type": 3, "name": "description", "description": "Event details", "max_length": 3500}]},
            {"name": "raid", "description": "Open raid signups, rosters and attendance", "dm_permission": False}]
