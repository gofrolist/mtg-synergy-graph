"""Niv-Mizzet, Parun — UR spellslinger / draw-damage combo."""

COMMANDER = "Niv-Mizzet, Parun"
EDHREC_SLUG = "niv-mizzet-parun"
COLOR_IDENTITY = {"U", "R"}

DECKLIST = [
    # Infinite combo enablers with Niv-Mizzet
    "Curiosity", "Ophidian Eye", "Tandem Lookout",
    # Cantrips — cheap spells to trigger Niv and draw more
    "Brainstorm", "Ponder", "Preordain", "Opt",
    "Manamorphose", "Faithless Looting", "Frantic Search",
    # Wheel effects — mass draw for Niv triggers
    "Wheel of Fortune", "Windfall", "Memory Jar",
    "Teferi's Puzzle Box",
    # Counterspells
    "Counterspell", "Swan Song", "Negate", "Force of Will",
    "Mana Drain", "Pact of Negation", "Flusterstorm",
    "Arcane Denial",
    # Draw engines
    "Rhystic Study", "Mystic Remora", "Archmage Emeritus",
    "Jori En, Ruin Diver",
    # Spellslinger payoffs
    "Guttersnipe", "Young Pyromancer", "Electrostatic Field",
    "Talrand, Sky Summoner", "Storm-Kiln Artist", "Mizzix of the Izmagnus",
    "Ral, Storm Conduit", "Melek, Izzet Paragon",
    # Copy / storm value
    "Bonus Round", "Reiterate", "Narset's Reversal", "Isochron Scepter",
    # Damage multipliers
    "Fiery Emancipation", "Gratuitous Violence",
    # Instant / sorcery tutors
    "Mystical Tutor", "Merchant Scroll", "Muddle the Mixture",
    "Solve the Equation",
    # Removal / interaction
    "Lightning Bolt", "Chaos Warp", "Cyclonic Rift",
    "Fire Prophecy", "Izzet Charm",
    # Fuel / mana rituals
    "Desperate Ritual", "Pyretic Ritual", "Seething Song",
    "Expressive Iteration", "Treasure Cruise", "Dig Through Time",
    # Ramp
    "Sol Ring", "Mana Crypt", "Arcane Signet", "Izzet Signet",
    # Protection
    "Lightning Greaves", "Swiftfoot Boots",
    # Lands
    "Command Tower", "Steam Vents", "Volcanic Island",
    "Shivan Reef", "Spirebluff Canal", "Sulfur Falls",
    "Fiery Islet", "Scalding Tarn",
    "Mountain", "Mountain", "Mountain", "Mountain", "Mountain",
    "Mountain", "Mountain", "Mountain", "Mountain",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island", "Island", "Island",
]

DISTRACTORS = [
    "Coat of Arms",           # tribal anthem — no tribal theme in this deck
    "Training Grounds",       # Niv can't be activated, Training Grounds does nothing here
    "Pteramander",            # tempo creature, doesn't synergize with the combo plan
    "Curiosity Crafter",      # tokens off instants/sorceries, but not a Curiosity for Niv
    "Kaza, Roil Chaser",      # cost reducer but needs Wizard tribal density
    "Jhoira, Weatherlight Captain",  # artifacts matter, this deck is spell-based
    "Turnabout",              # mana untap but this isn't a storm combo deck
    "Reality Spasm",          # parallel to Turnabout — not the right plan
    "Snap",                   # bounce + untap but high floor for what it does here
]

SYNERGY_PAIRS = [
    ("Niv-Mizzet, Parun", "Curiosity", "enchant Niv: drawing a card → deal 1 damage → draw a card → infinite loop"),
    ("Niv-Mizzet, Parun", "Ophidian Eye", "same infinite as Curiosity but flash-able at instant speed"),
    ("Niv-Mizzet, Parun", "Tandem Lookout", "soulbond Lookout: Niv pings → draws → infinite loop"),
    ("Niv-Mizzet, Parun", "Wheel of Fortune", "wheel draws 7 → 7 Niv pings → each ping draws → chain"),
    ("Niv-Mizzet, Parun", "Rhystic Study", "each opponent spell draws a card → Niv pings"),
    ("Niv-Mizzet, Parun", "Fiery Emancipation", "triples all damage — Niv's 1-damage pings become 3"),
    ("Archmage Emeritus", "Niv-Mizzet, Parun", "Emeritus draws on every instant/sorcery → more Niv pings"),
    ("Isochron Scepter", "Dramatic Reversal", "imprint Reversal → infinite mana with rocks → cast from Scepter repeatedly"),
    ("Guttersnipe", "Wheel of Fortune", "Wheel = one Guttersnipe trigger + Niv draws all seven"),
    ("Guttersnipe", "Young Pyromancer", "redundant payoffs — both trigger on each spell"),
    ("Storm-Kiln Artist", "Bonus Round", "Bonus Round copies spells → Artist makes double treasures"),
    ("Reiterate", "Seething Song", "buyback Reiterate on Seething Song to make infinite mana"),
    ("Ral, Storm Conduit", "Reiterate", "Ral pings on each copy; Reiterate with buyback = infinite pings"),
    ("Mizzix of the Izmagnus", "Treasure Cruise", "experience counters reduce delve cost to zero"),
    ("Narset's Reversal", "Wheel of Fortune", "copy the Wheel → both wheels resolve → 14 cards drawn"),
]

# Scryfall supplement filters for Niv-Mizzet
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever you draw a card", oracle_text.lower()))
        ),
        "reason": "draw-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever you cast an instant or sorcery", oracle_text.lower()))
        ),
        "reason": "spellslinger-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"deals damage.*draw|draw.*deals damage", oracle_text.lower()))
        ),
        "reason": "damage-draw-loop",
    },
    {
        "check": lambda type_line, oracle_text: (
            "enchanted creature" in oracle_text.lower() and "draw a card" in oracle_text.lower()
        ),
        "reason": "curiosity-type",
    },
]
