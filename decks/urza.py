"""Urza, Lord High Artificer — Mono-Blue artifacts / combo."""

COMMANDER = "Urza, Lord High Artificer"
EDHREC_SLUG = "urza-lord-high-artificer"
COLOR_IDENTITY = {"U"}

DECKLIST = [
    # Mana rocks — power pieces (tap with Urza ability)
    "Sol Ring", "Mana Crypt", "Mana Vault", "Grim Monolith",
    "Basalt Monolith", "Chrome Mox", "Mox Opal", "Mox Diamond",
    "Arcane Signet", "Sapphire Medallion",
    # Combo pieces
    "Isochron Scepter", "Dramatic Reversal",   # infinite mana combo
    "Power Artifact",                           # + Basalt/Grim Monolith = infinite mana
    # Untap / key artifacts
    "Voltaic Key", "Manifold Key",
    # Stax pieces (tap with Urza so only you have mana)
    "Winter Orb", "Static Orb", "Trinisphere",
    # Artifact tutors
    "Fabricate", "Whir of Invention", "Transmute Artifact", "Reshape", "Tinker",
    "Tezzeret the Seeker",
    # Win conditions
    "Thassa's Oracle", "Staff of Domination",
    # Artifact synergy creatures
    "Emry, Lurker of the Loch", "Sai, Master Thopterist",
    "Vedalken Archmage", "Riddlesmith",
    # Counterspells
    "Counterspell", "Force of Will", "Pact of Negation", "Mana Drain",
    "Swan Song", "Negate", "Spell Pierce", "Mental Misstep",
    "Flusterstorm", "Spell Snare",
    # Draw engines
    "Rhystic Study", "Mystic Remora", "Sensei's Divining Top",
    # Cantrips / library manipulation
    "Brainstorm", "Ponder", "Thoughtcast",
    # Tutors
    "Mystical Tutor",
    # Artifact recursion
    "Academy Ruins",
    # Extra artifacts for Urza's token and mana generation
    "Mycosynth Lattice", "Karn, the Great Creator",
    "Paradox Engine",
    # Bounce / interaction
    "Cyclonic Rift", "Chain of Vapor", "Hurkyl's Recall",
    "Snap",
    # Other utility artifacts
    "Phyrexian Metamorph",
    # Lands
    "Tolarian Academy", "Seat of the Synod",
    "Urza's Saga", "Ancient Den", "Darksteel Citadel",
    "Inventors' Fair", "Blinkmoth Well",
    "Command Tower",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island", "Island",
]

DISTRACTORS = [
    "Goblin Engineer",         # only in mono-U shell, but Goblin is a red card and off-color
    "Sly Requisitioner",       # needs historic theme; not enough synergy density here
    "Baleful Strix",           # blue/black card — off color identity
    "Time Walk",               # powerful but this deck wins before needing extra turns
    "Timetwister",             # symmetric reset — hurts your combo more than helps
    "Riddlesmith",             # draw only when casting artifacts — marginal vs. Vedalken Archmage
    "Mirran Spy",              # untaps target — needs specific setup, too narrow
    "Seat of the Synod",       # already in deck — represents generic blue artifact land
    "Turnabout",               # mana untap ritual, but Isochron+Reversal already handles this
]

SYNERGY_PAIRS = [
    ("Urza, Lord High Artificer", "Winter Orb", "tap Winter Orb with Urza's ability to turn it off on your turn while it locks opponents"),
    ("Urza, Lord High Artificer", "Static Orb", "tap Static Orb with Urza — opponents untap only two permanents, you untap freely"),
    ("Urza, Lord High Artificer", "Grim Monolith", "tap Monolith with Urza for mana, Power Artifact makes it produce infinite mana"),
    ("Isochron Scepter", "Dramatic Reversal", "imprint Reversal: tap rocks for mana, Reversal untaps them, repeat for infinite mana"),
    ("Basalt Monolith", "Power Artifact", "Power Artifact reduces Monolith's untap cost to 0 → infinite colorless mana"),
    ("Thassa's Oracle", "Dramatic Reversal", "generate infinite mana via Scepter+Reversal, then tutor Oracle to win"),
    ("Mox Opal", "Mycosynth Lattice", "Lattice makes all permanents artifacts → Opal always has metalcraft"),
    ("Winter Orb", "Voltaic Key", "untap Winter Orb in response to its trigger so yours is tapped during your upkeep"),
    ("Emry, Lurker of the Loch", "Isochron Scepter", "Emry recurs Scepter from graveyard if it gets destroyed"),
    ("Sai, Master Thopterist", "Mox Opal", "Sai makes a thopter whenever you cast an artifact — each Mox is free + token"),
    ("Vedalken Archmage", "Isochron Scepter", "each Scepter activation draws a card via Archmage"),
    ("Whir of Invention", "Winter Orb", "instant-speed find Winter Orb at end of opponent's turn"),
    ("Karn, the Great Creator", "Mycosynth Lattice", "Karn + Lattice: opponents can't activate any mana abilities"),
    ("Academy Ruins", "Isochron Scepter", "Ruins returns destroyed combo pieces to top of library"),
    ("Staff of Domination", "Urza, Lord High Artificer", "Staff untaps Urza; tap Urza for mana; with infinite mana, draw your deck and win"),
]

# Scryfall supplement filters for Urza
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "Artifact" in type_line and bool(__import__("re").search(r"tap.*add.*mana|: add", oracle_text.lower()))
        ),
        "reason": "mana-rock",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever you cast an artifact", oracle_text.lower()))
        ),
        "reason": "artifact-cast-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"untap.*artifact|artifact.*untap", oracle_text.lower()))
        ),
        "reason": "artifact-untapper",
    },
    {
        "check": lambda type_line, oracle_text: (
            "Artifact" in type_line and "imprint" in oracle_text.lower()
        ),
        "reason": "imprint-artifact",
    },
]
