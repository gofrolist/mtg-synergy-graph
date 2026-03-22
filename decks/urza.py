"""Urza, Lord High Artificer — Mono-Blue artifacts / stax / combo."""

COMMANDER = "Urza, Lord High Artificer"
EDHREC_SLUG = "urza-lord-high-artificer"
COLOR_IDENTITY = {"U"}

DECKLIST = [
    # 0-cost artifacts (tap for mana with Urza)
    "Ornithopter", "Mox Opal", "Lotus Petal", "Jeweled Amulet",
    "Mishra's Bauble", "Urza's Bauble", "Welding Jar",
    "Tormod's Crypt", "Spellbook", "Everflowing Chalice",
    # Mana rocks
    "Mana Vault", "Basalt Monolith", "Voltaic Key",
    "Manifold Key",
    # Stax pieces (tap with Urza for mana while locking opponents)
    "Winter Orb", "Static Orb", "Trinisphere",
    "Back to Basics", "Grafdigger's Cage",
    # Combo pieces
    "Isochron Scepter", "Dramatic Reversal",  # Infinite mana
    "Power Artifact",  # + Basalt Monolith = infinite colorless
    "Rings of Brighthearth",  # + Basalt Monolith = infinite colorless
    "Walking Ballista",  # Win con with infinite mana
    "Codex Shredder",  # Loop with infinite mana
    "Hullbreaker Horror",  # Bounce loop
    "Displacer Kitten",  # Flicker loop
    # Artifact synergy creatures
    "Sai, Master Thopterist", "Etherium Sculptor",
    "Foundry Inspector", "Emry, Lurker of the Loch",
    "Padeem, Consul of Innovation", "Forensic Gadgeteer",
    "Shimmer Myr", "Vedalken Archmage",
    "Phyrexian Metamorph", "Thought Monitor",
    "The Reality Chip", "Spellskite",
    "Uthros, Titanic Godcore",
    # Artifact support
    "Efficient Construction", "Unwinding Clock",
    "Copy Artifact", "The Mycosynth Gardens",
    "Mystic Forge", "Sensei's Divining Top",
    "Howling Mine", "The One Ring", "Aether Spellbomb",
    "Fomori Vault",
    # Tutors
    "Fabricate", "Whir of Invention", "Reshape",
    "Tezzeret the Seeker", "Mystical Tutor",
    "Karn, the Great Creator",
    # Counterspells
    "Mana Drain", "Force of Negation", "Pact of Negation",
    "Swan Song", "Flusterstorm", "Mental Misstep",
    "Mindbreak Trap", "An Offer You Can't Refuse",
    # Card draw
    "Mystic Remora", "Thoughtcast",
    # Lands (non-basic)
    "Academy Ruins", "Ancient Tomb", "Buried Ruin",
    "Darksteel Citadel", "Emergence Zone", "Flooded Strand",
    "Gemstone Caverns", "Inventors' Fair", "Misty Rainforest",
    "Otawara, Soaring City", "Polluted Delta", "Prismatic Vista",
    "Scalding Tarn", "Seat of the Synod",
    "Urza's Mine", "Urza's Power Plant", "Urza's Tower",
    "Urza's Saga",
]

DISTRACTORS = [
    "Paradox Engine", "Myr Battlesphere", "Blightsteel Colossus",
    "Metalworker", "Tinker", "Time Spiral",
    "Cyclonic Rift", "Rhystic Study", "Consecrated Sphinx",
]

SYNERGY_PAIRS = [
    ("Urza, Lord High Artificer", "Winter Orb", "tap Winter Orb with Urza for mana → your lands untap, opponents' don't"),
    ("Urza, Lord High Artificer", "Static Orb", "same trick — tap Orb with Urza, you untap normally"),
    ("Isochron Scepter", "Dramatic Reversal", "infinite mana: Scepter casts Reversal → untap all nonland permanents → retap for mana → repeat"),
    ("Basalt Monolith", "Power Artifact", "infinite colorless mana: reduce untap cost by 2 → tap for 3, untap for 1 → net +2"),
    ("Basalt Monolith", "Rings of Brighthearth", "infinite colorless: copy untap ability → untap twice for 5 mana, tap twice for 6"),
    ("Walking Ballista", "Urza, Lord High Artificer", "infinite mana → infinite Ballista damage"),
    ("Sai, Master Thopterist", "Urza, Lord High Artificer", "every artifact cast → thopter token → tap thopter for mana with Urza"),
    ("Sensei's Divining Top", "Mystic Forge", "Top draws + Forge casts from top → filter through entire deck"),
    ("Unwinding Clock", "Winter Orb", "your artifacts untap on everyone's turn → you have mana, opponents don't"),
    ("Voltaic Key", "Mana Vault", "untap Vault → tap for 3 → untap with Key → tap for 3 → 6 mana per turn"),
    ("Emry, Lurker of the Loch", "Aether Spellbomb", "cast Spellbomb from graveyard → sacrifice → recast with Emry → repeat"),
    ("Etherium Sculptor", "Foundry Inspector", "double cost reduction → most artifacts are free"),
    ("Hullbreaker Horror", "Mox Opal", "cast 0-cost artifact → bounce it → recast → infinite storm/ETB"),
    ("Tezzeret the Seeker", "Isochron Scepter", "Tezzeret untaps Scepter or tutors combo pieces"),
    ("Displacer Kitten", "Sensei's Divining Top", "cast noncreature → flicker Top → draw a card → recast Top → infinite draw"),
]
