"""Kyler, Sigardian Emissary — GW Humans / +1/+1 counters."""

COMMANDER = "Kyler, Sigardian Emissary"
EDHREC_SLUG = "kyler-sigardian-emissary"
COLOR_IDENTITY = {"G", "W"}

DECKLIST = [
    "Greymond, Avacyn's Stalwart", "Hardened Scales", "Luminarch Aspirant",
    "Mikaeus, the Lunarch", "Ozolith, the Shattered Spire", "Pir, Imaginative Rascal",
    "Roaming Throne", "Thalia's Lieutenant", "The Ozolith",
    "Augur of Autumn", "Esper Sentinel", "Garruk's Uprising", "Outcaster Trailblazer",
    "Realmwalker", "The Great Henge", "Tribute to the World Tree",
    "Abzan Falconer", "Champion of Lambholt", "Surrak and Goreclaw", "Tuskguard Captain",
    "Bastion Protector", "Boromir, Warden of the Tower", "Clever Concealment",
    "Everybody Lives!", "Flare of Fortitude", "Flawless Maneuver", "Grand Abolisher",
    "Heroic Intervention", "Mother of Runes", "Saffi Eriksdotter", "Saryth, the Viper's Fang",
    "Sigarda, Font of Blessings", "Slippery Bogbonder", "Spectacular Spider-Man",
    "Swiftfoot Boots", "Sylvan Safekeeper", "Teferi's Protection", "The Wandering Rescuer",
    "Zack Fair", "Wild Beastmaster",
    "Avacyn's Pilgrim", "Biophagus", "Cultivate", "Farseek", "Heronblade Elite",
    "Katilda, Dawnhart Prime", "Nature's Lore", "Rampant Growth", "Sol Ring",
    "Somberwald Sage", "Three Visits",
    "Aura Shards", "Damning Verdict", "Druid of Purification", "Loran of the Third Path",
    "Swords to Plowshares", "Witch Enchanter",
    "Adeline, Resplendent Cathar", "Call the Coppercoats", "Horn of Gondor",
    "Torens, Fist of the Angels", "Wyrm's Crossing Patrol",
    "Ranger-Captain of Eos", "Recruiter of the Guard",
    "Command Tower", "Cavern of Souls", "Gavony Township", "Plaza of Heroes",
    "Boseiju, Who Endures", "Windswept Heath", "Temple Garden",
]

DISTRACTORS = [
    "Panharmonicon", "Lithoform Engine", "Strionic Resonator",
    "Illusionist's Bracers", "Anointed Procession", "Mondrak, Glory Dominus",
    "Doubling Season", "Elesh Norn, Grand Cenobite", "Solidarity of Heroes",
]

SYNERGY_PAIRS = [
    ("Hardened Scales", "Ozolith, the Shattered Spire", "both double counter placement"),
    ("Hardened Scales", "The Ozolith", "both double counter placement"),
    ("Hardened Scales", "Pir, Imaginative Rascal", "stacks: Pir+1 then Scales+1 = +2 per trigger"),
    ("Hardened Scales", "Luminarch Aspirant", "each trigger gets extra counter"),
    ("Pir, Imaginative Rascal", "The Ozolith", "Ozolith preserves counters Pir amplified"),
    ("Pir, Imaginative Rascal", "Ozolith, the Shattered Spire", "Ozolith preserves counters Pir amplified"),
    ("Pir, Imaginative Rascal", "Luminarch Aspirant", "Aspirant triggers stack with Pir"),
    ("Kyler, Sigardian Emissary", "Thalia's Lieutenant", "both care about Humans entering"),
    ("Kyler, Sigardian Emissary", "Champion of Lambholt", "counters make Champion unblockable"),
    ("The Ozolith", "Saffi Eriksdotter", "Saffi dies → Ozolith saves counters"),
    ("The Ozolith", "Slippery Bogbonder", "Bogbonder moves counters, Ozolith catches them"),
    ("Gavony Township", "Champion of Lambholt", "Township pumps all, Champion becomes unblockable"),
    ("Roaming Throne", "Kyler, Sigardian Emissary", "Throne doubles Kyler's triggered ability"),
    ("Torens, Fist of the Angels", "Kyler, Sigardian Emissary", "Torens makes Humans, Kyler pumps them"),
    ("Adeline, Resplendent Cathar", "Kyler, Sigardian Emissary", "Adeline floods board with Humans"),
    ("Abzan Falconer", "Champion of Lambholt", "both make counter-matters creatures relevant in combat"),
    ("Surrak and Goreclaw", "Wild Beastmaster", "Surrak gives trample, Beastmaster pumps on attack"),
    ("Katilda, Dawnhart Prime", "Kyler, Sigardian Emissary", "Katilda taps Humans for mana"),
    ("Heronblade Elite", "Kyler, Sigardian Emissary", "Elite grows from Human count like Kyler"),
    ("Tribute to the World Tree", "Kyler, Sigardian Emissary", "draws cards when pumped Humans ETB"),
    ("The Great Henge", "Kyler, Sigardian Emissary", "Henge draws when pumped Humans ETB"),
    ("Aura Shards", "Kyler, Sigardian Emissary", "each Human entering destroys artifact/enchantment"),
    ("Roaming Throne", "Thalia's Lieutenant", "Throne doubles Thalia's trigger on each Human ETB"),
    ("Roaming Throne", "Adeline, Resplendent Cathar", "Throne doubles Adeline's attack trigger"),
    ("Roaming Throne", "Torens, Fist of the Angels", "Throne doubles Torens's cast trigger"),
]

# Scryfall supplement filters: cards EDHREC might miss
# Each filter: {"pattern": regex, "field": "type_line"|"oracle_text", "reason": str}
# Plus optional "requires": additional field check
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "Human" in type_line and "+1/+1 counter" in oracle_text
        ),
        "reason": "human+counter",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"\+1/\+1 counter[s]? on (?:each|all)", oracle_text))
        ),
        "reason": "board-counter",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"twice.*counter|double.*counter|plus one.*counter", oracle_text))
        ),
        "reason": "counter-doubler",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"trigger.*additional time|additional time", oracle_text))
        ),
        "reason": "trigger-doubler",
    },
]
