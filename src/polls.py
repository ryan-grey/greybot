"""Immediate native Discord polls; votes and expiry are managed by Discord.

API contract: https://docs.discord.com/developers/resources/poll
Callback contract: https://docs.discord.com/developers/interactions/receiving-and-responding
"""

SEND_POLLS = 1 << 49
ADMINISTRATOR = 1 << 3
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
SEND_MESSAGES_IN_THREADS = 1 << 38

COMMAND = {
    "name": "poll", "description": "Create a poll in this channel", "type": 1,
    "dm_permission": False, "default_member_permissions": str(SEND_POLLS),
    "options": [
        {"name": "question", "description": "What should members vote on?",
         "type": 3, "required": True, "min_length": 1, "max_length": 300},
        *[{"name": f"answer{i}", "description": f"Answer choice {i}",
           "type": 3, "required": i <= 2, "min_length": 1, "max_length": 55}
          for i in range(1, 11)],
        {"name": "duration", "description": "How long voting stays open (default: 24 hours)",
         "type": 4, "choices": [
             {"name": label, "value": hours} for label, hours in
             [("1 hour", 1), ("4 hours", 4), ("8 hours", 8),
              ("24 hours", 24), ("3 days", 72), ("7 days", 168)]]},
        {"name": "multiple", "description": "Allow voting for more than one answer",
         "type": 5},
    ],
}


def response(body, guild_id):
    """Accept only signed guild interactions after the handler verifies them."""
    import interactions

    def error(text):
        return interactions.message({"description": text, "color": 0x4493F8})

    member = body.get("member") or {}
    if not guild_id or body.get("guild_id") != guild_id or not member:
        return error("Use `/poll` inside this server.")
    if member.get("pending"):
        return error("Finish server verification before creating a poll.")
    thread = (body.get("channel") or {}).get("type") in (10, 11, 12)
    required = VIEW_CHANNEL | SEND_POLLS | (SEND_MESSAGES_IN_THREADS if thread else SEND_MESSAGES)
    for raw, who in [(member.get("permissions"), "You"),
                     (body.get("app_permissions"), "greyBot")]:
        try:
            permissions = int(raw or "0")
        except (TypeError, ValueError):
            permissions = 0
        if permissions < 0 or not (permissions & ADMINISTRATOR or permissions & required == required):
            return error(f"{who} need permission to view this channel, send messages and create polls.")

    opts = interactions.command_options(body)
    question = opts.get("question")
    question = question.strip() if isinstance(question, str) else ""
    if not 1 <= len(question) <= 300:
        return error("Enter a question between 1 and 300 characters.")
    answers = []
    for i in range(1, 11):
        value = opts.get(f"answer{i}")
        if value is None and i > 2:
            continue
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 55:
            return error("Provide at least two answers, each between 1 and 55 characters.")
        answers.append(value.strip())
    if len({a.casefold() for a in answers}) != len(answers):
        return error("Each answer must be different.")
    duration = opts.get("duration", 24)
    multiple = opts.get("multiple", False)
    if type(duration) is not int or duration not in (1, 4, 8, 24, 72, 168) or type(multiple) is not bool:
        return error("Choose a listed duration and a valid multiple-answer setting.")
    return {"type": interactions.CHANNEL_MESSAGE_WITH_SOURCE, "data": {
        "allowed_mentions": {"parse": []},
        "poll": {"question": {"text": question},
                 "answers": [{"poll_media": {"text": value}} for value in answers],
                 "duration": duration, "allow_multiselect": multiple, "layout_type": 1},
    }}
