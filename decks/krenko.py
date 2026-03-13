"""Krenko, Mob Boss — Mono-Red Goblin tribal / combo."""

COMMANDER = "Krenko, Mob Boss"
EDHREC_SLUG = "krenko-mob-boss"
COLOR_IDENTITY = {"R"}

DECKLIST = [
    # Creatures — Goblins
    "Goblin Chirurgeon", "Goblin Lackey", "Skirk Prospector", "Goblin Recruiter",
    "Goblin Warchief", "Goblin Chieftain", "Goblin King", "Goblin Ringleader",
    "Goblin Matron", "Goblin Trashmaster", "Muxus, Goblin Grandee",
    "Siege-Gang Commander", "Goblin Rabblemaster", "Legion Warboss",
    "Pashalik Mons", "Mogg War Marshal", "Goblin Instigator",
    "Sling-Gang Lieutenant", "Rundvelt Hordemaster", "Hobgoblin Bandit Lord",
    "Krenko's Command",
    # Creatures — Non-Goblin utility
    "Dockside Extortionist", "Imperial Recruiter",
    # Combo pieces — Untap
    "Thornbite Staff", "Umbral Mantle", "Staff of Domination",
    "Sword of the Paruns", "Thousand-Year Elixir", "Magewright's Stone",
    # Combo pieces — Sacrifice outlets
    "Ashnod's Altar", "Phyrexian Altar", "Goblin Bombardment",
    # Combo pieces — Copy/extra
    "Kiki-Jiki, Mirror Breaker",
    # Haste enablers
    "Lightning Greaves", "Swiftfoot Boots", "Ogre Battledriver",
    # Damage amplifiers
    "Purphoros, God of the Forge", "Impact Tremors", "Shared Animosity",
    # Token doublers
    "Krenko's Buzz", "Doubling Cube",
    # Card advantage
    "Skullclamp", "Wheel of Fortune", "Reforge the Soul",
    "Herald's Horn", "Icon of Ancestry",
    # Removal / interaction
    "Chaos Warp", "Blasphemous Act", "Vandalblast",
    # Tutors
    "Gamble",
    # Ramp
    "Sol Ring", "Ruby Medallion", "Fire Diamond", "Arcane Signet",
    "Mana Crypt",
    # Utility
    "Blood Moon", "Goblin War Strike",
    # Lands
    "Snow-Covered Mountain", "Snow-Covered Mountain", "Snow-Covered Mountain",
    "Snow-Covered Mountain", "Snow-Covered Mountain", "Snow-Covered Mountain",
    "Snow-Covered Mountain", "Snow-Covered Mountain", "Snow-Covered Mountain",
    "Castle Embereth", "Valakut, the Molten Pinnacle",
    "Cavern of Souls", "Command Tower",
    "Ancient Tomb", "War Room", "Nykthos, Shrine to Nyx",
    "Goblin Burrows", "Skirk Ridge",
]

DISTRACTORS = [
    "Coat of Arms",           # tribal anthem but symmetric — helps opponents' tribes too
    "Door of Destinies",      # slow counter buildup, doesn't combo
    "Obelisk of Urd",         # convoke anthem but no combo potential
    "Eldrazi Monument",       # anthem + sac but doesn't synergize with Krenko's tap
    "Vanquisher's Banner",    # draw + anthem but 5 CMC, slow
    "Moggcatcher",            # tutor but too slow (tap + wait)
    "Brightstone Ritual",     # one-shot mana, no lasting value
    "Battle Hymn",            # one-shot mana from creatures
    "Empty the Warrens",      # storm tokens but no goblin synergy
]

SYNERGY_PAIRS = [
    ("Krenko, Mob Boss", "Thornbite Staff", "Staff untaps Krenko when a Goblin dies → infinite with sac outlet"),
    ("Krenko, Mob Boss", "Umbral Mantle", "Mantle untaps Krenko for {3} → infinite with enough Goblins"),
    ("Krenko, Mob Boss", "Staff of Domination", "Staff untaps Krenko for {3} → infinite tokens + life + cards"),
    ("Krenko, Mob Boss", "Skirk Prospector", "Prospector sacs Goblins for mana to fuel Krenko activations"),
    ("Krenko, Mob Boss", "Goblin Warchief", "Warchief gives haste so Krenko taps immediately"),
    ("Krenko, Mob Boss", "Purphoros, God of the Forge", "each Krenko activation deals 2 damage per token"),
    ("Krenko, Mob Boss", "Ashnod's Altar", "Altar sacs tokens for mana → untap combos"),
    ("Krenko, Mob Boss", "Phyrexian Altar", "Altar sacs tokens for colored mana"),
    ("Krenko, Mob Boss", "Goblin Bombardment", "Bombardment sacs tokens for direct damage"),
    ("Thornbite Staff", "Ashnod's Altar", "Altar sacs → Staff untaps → infinite loop"),
    ("Thornbite Staff", "Goblin Bombardment", "Bombardment sacs → Staff untaps → infinite damage"),
    ("Purphoros, God of the Forge", "Impact Tremors", "both deal damage on creature ETB — redundant wincons"),
    ("Goblin Chieftain", "Goblin Warchief", "both grant haste, stack lord effects"),
    ("Skirk Prospector", "Phyrexian Altar", "redundant sac-for-mana outlets"),
    ("Skullclamp", "Skirk Prospector", "Clamp on 1/1 Goblin tokens → draw 2"),
    ("Skullclamp", "Goblin Bombardment", "sac clamped creature to Bombardment for value"),
    ("Muxus, Goblin Grandee", "Goblin Recruiter", "Recruiter stacks top of library → Muxus hits everything"),
    ("Goblin Matron", "Goblin Recruiter", "both tutor Goblins — find combo pieces"),
    ("Shared Animosity", "Goblin Rabblemaster", "Rabblemaster's tokens attack with Animosity bonus"),
    ("Kiki-Jiki, Mirror Breaker", "Goblin Chieftain", "copy Chieftain → hasty token army"),
    ("Thousand-Year Elixir", "Krenko, Mob Boss", "Elixir gives Krenko pseudo-haste + extra untap"),
    ("Dockside Extortionist", "Kiki-Jiki, Mirror Breaker", "copy Dockside for repeated treasure generation"),
    ("Pashalik Mons", "Goblin Bombardment", "Mons triggers on Goblin death → sac for double damage"),
    ("Sling-Gang Lieutenant", "Ashnod's Altar", "Lieutenant sacs Goblins for drain + Altar gives mana"),
    ("Rundvelt Hordemaster", "Skirk Prospector", "Hordemaster exiles on Goblin death → sac for value chain"),
]

# Scryfall supplement filters for Krenko
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "Goblin" in type_line and "token" in oracle_text.lower()
        ),
        "reason": "goblin+tokens",
    },
    {
        "check": lambda type_line, oracle_text: (
            "Goblin" in type_line and ("haste" in oracle_text.lower() or "untap" in oracle_text.lower())
        ),
        "reason": "goblin+haste/untap",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"untap target creature", oracle_text.lower()))
            and "artifact" in type_line.lower()
        ),
        "reason": "creature-untapper",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"sacrifice.*creature.*:", oracle_text.lower()))
        ),
        "reason": "sac-outlet",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever.*creature.*enters|whenever.*token", oracle_text.lower()))
            and "damage" in oracle_text.lower()
        ),
        "reason": "etb-damage",
    },
]
