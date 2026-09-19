"""Spec artwork for a card, by class and spec.

IDs ONLY, deliberately. These name greyBot's own Discord application emojis, whose images
live on Discord's CDN; the art itself is never committed here. `recap_page` already refuses
to redistribute Blizzard's role textures for exactly this reason, and forty spec icons in a
public repository would be a much larger version of the same thing. A card that wants them
fetches them at render time, the way the roll call card fetches member avatars.

Generated on 2026-09-19 from two things that already existed: the raid signup template's
`classes[].specs[]`, which pairs a spec name with the source emoji it was imported with,
and `control/greybot_control/raid_emojis.EMOJIS`, which maps that source id to the
application emoji greyBot re-uploaded. Forty specs across all thirteen classes, no gaps.

The key is lowercase `class|spec`. Warcraft Logs spells classes without spaces
("DeathKnight"), and the signup template abbreviates two of them, so `key()` reconciles
both. Trailing digits are stripped from spec names: the template carries "Frost1" and
"Holy1" only to keep Frost the death knight distinct from Frost the mage inside one
template, and that distinction is already carried by the class here.
"""
import re

# The signup template's two abbreviations, against Warcraft Logs' spelling.
ALIASES = {"dk": "deathknight", "dh": "demonhunter"}

ICONS = {
    # deathknight
    "deathknight|blood": "1546964102373441687",
    "deathknight|frost": "1546964109684244570",
    "deathknight|unholy": "1546964110296490094",
    # demonhunter
    "demonhunter|devourer": "1546964128629784667",
    "demonhunter|havoc": "1546964110665588767",
    "demonhunter|vengeance": "1546964103010984106",
    # druid
    "druid|balance": "1546964123861000364",
    "druid|feral": "1546964112079331338",
    "druid|guardian": "1546964104382783748",
    "druid|restoration": "1546964131414933504",
    # evoker
    "evoker|augmentation": "1546964128306827436",
    "evoker|devastation": "1546964126905933916",
    "evoker|preservation": "1546964133058973789",
    # hunter
    "hunter|beastmastery": "1546964122602578041",
    "hunter|marksmanship": "1546964123030397039",
    "hunter|survival": "1546964112578183201",
    # mage
    "mage|arcane": "1546964118102085642",
    "mage|fire": "1546964118697811990",
    "mage|frost": "1546964119394058310",
    # monk
    "monk|brewmaster": "1546964103740784640",
    "monk|mistweaver": "1546964130496258071",
    "monk|windwalker": "1546964111433273425",
    # paladin
    "paladin|holy": "1546964131989553243",
    "paladin|protection": "1546964104667996284",
    "paladin|retribution": "1546964113509322895",
    # priest
    "priest|discipline": "1546964129556729897",
    "priest|holy": "1546964130160836658",
    "priest|shadow": "1546964124473368617",
    # rogue
    "rogue|assassination": "1546964107767324764",
    "rogue|outlaw": "1546964108337872956",
    "rogue|subtlety": "1546964109063491647",
    # shaman
    "shaman|elemental": "1546964125991567393",
    "shaman|enhancement": "1546964116990722149",
    "shaman|restoration": "1546964132547268760",
    # warlock
    "warlock|affliction": "1546964121780617297",
    "warlock|demonology": "1546964120723792024",
    "warlock|destruction": "1546964120094380113",
    # warrior
    "warrior|arms": "1546964106127482941",
    "warrior|fury": "1546964107213676574",
    "warrior|protection": "1546964101513748550",
}

CDN = "https://cdn.discordapp.com/emojis/{}.png?size=64"


def _fold(value):
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def key(class_name, spec_name):
    cls = _fold(class_name)
    cls = ALIASES.get(cls, cls)
    # "Beast Mastery" and "BeastMastery" both fold to the same thing; the trailing digit
    # the template uses for disambiguation is not part of the spec's name.
    return f"{cls}|{_fold(spec_name)}" if cls and spec_name else ""


def emoji_id(class_name, spec_name):
    """The application emoji id for this spec, or None. None is an ordinary answer: a brand
    new spec, or a parse with no spec recorded, simply draws without an icon."""
    return ICONS.get(key(class_name, spec_name))


def url(class_name, spec_name):
    found = emoji_id(class_name, spec_name)
    return CDN.format(found) if found else ""
