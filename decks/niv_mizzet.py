"""Niv-Mizzet, Parun — UR spellslinger / draw-damage combo."""

COMMANDER = "Niv-Mizzet, Parun"
EDHREC_SLUG = "niv-mizzet-parun"
COLOR_IDENTITY = {"U", "R"}

DECKLIST = [
    # Curiosity combo pieces (infinite draw-damage with Niv)
    "Curiosity", "Ophidian Eye", "Tandem Lookout",
    # Other Niv-Mizzets
    "Niv-Mizzet, the Firemind", "Niv-Mizzet, Dracogenius",
    "Niv-Mizzet, Visionary",
    # Draw-damage payoffs
    "Psychosis Crawler", "The Locust God", "Body of Knowledge",
    "Teferi's Ageless Insight", "Fiery Inscription",
    # Spellslinger payoffs
    "Guttersnipe", "Electrostatic Field", "Storm-Kiln Artist",
    "Archmage Emeritus", "Young Pyromancer", "Talrand, Sky Summoner",
    "Crackling Drake", "Harmonic Prodigy",
    # Damage multipliers
    "Torbran, Thane of Red Fell", "Vivi Ornitier",
    # Win conditions
    "Jace, Wielder of Mysteries", "Laboratory Maniac",
    # Cantrips
    "Brainstorm", "Ponder", "Preordain", "Opt", "Consider",
    "Serum Visions", "Gitaxian Probe", "Quick Study",
    # Card draw
    "Rhystic Study", "Mystic Remora", "Windfall",
    "Winds of Change", "Treasure Cruise", "Midnight Clock",
    "Faithless Looting", "Thrill of Possibility",
    "Frantic Search",
    # Counterspells
    "Counterspell", "Arcane Denial", "Negate", "Spell Pierce",
    "Dispel", "Rewind", "Ionize", "Misdirection",
    # Cost reduction
    "Baral, Chief of Compliance", "Goblin Electromancer",
    # Narset lock
    "Narset, Parter of Veils",
    # Removal
    "Abrade", "Lightning Bolt", "Pongify", "Rapid Hybridization",
    "Blasphemous Act", "Flame of Anor",
    # Tutors
    "Mystical Tutor",
    # Other
    "Clear the Mind", "Explore",
    # Ramp
    "Commander's Sphere", "Izzet Signet", "Izzet Locket",
    "Decanter of Endless Water",
    # Lands (non-basic)
    "Cephalid Coliseum", "Evolving Wilds", "Forbidden Orchard",
    "Forgotten Cave", "Highland Lake", "Izzet Boilerworks",
    "Izzet Guildgate", "Lonely Sandbar", "Mystic Sanctuary",
    "Reflecting Pool", "Reliquary Tower", "Swiftwater Cliffs",
    "Temple of Epiphany", "Terramorphic Expanse",
]

DISTRACTORS = [
    "Dramatic Reversal", "Isochron Scepter", "Thousand-Year Storm",
    "Expressive Iteration", "Hullbreaker Horror", "Cyclonic Rift",
    "Consecrated Sphinx", "Jin-Gitaxias, Core Augur", "Mana Drain",
]

SYNERGY_PAIRS = [
    ("Niv-Mizzet, Parun", "Curiosity", "infinite combo: Niv draws → deals damage → Curiosity draws → repeat until opponent dies"),
    ("Niv-Mizzet, Parun", "Ophidian Eye", "same infinite combo as Curiosity — draw-damage loop"),
    ("Niv-Mizzet, Parun", "Tandem Lookout", "soulbond version of Curiosity — infinite draw-damage"),
    ("Niv-Mizzet, Parun", "Windfall", "everyone draws 7 → Niv deals 7 damage → each spell opponent casts triggers Niv"),
    ("Psychosis Crawler", "Windfall", "draw 7 → Crawler deals 7 to each opponent"),
    ("The Locust God", "Windfall", "draw 7 → make 7 hasty 1/1 flyers"),
    ("Guttersnipe", "Harmonic Prodigy", "Prodigy doubles Guttersnipe's trigger → 4 damage per spell"),
    ("Narset, Parter of Veils", "Windfall", "opponents can only draw 1 while you draw 7 → opponents lose their hands"),
    ("Teferi's Ageless Insight", "Niv-Mizzet, Parun", "double all draws → Niv deals double damage"),
    ("Baral, Chief of Compliance", "Niv-Mizzet, Parun", "cheaper spells = more casts per turn = more Niv triggers"),
    ("Storm-Kiln Artist", "Frantic Search", "Frantic is free → Artist makes treasure → net positive mana"),
    ("Laboratory Maniac", "Niv-Mizzet, Parun", "if Curiosity combo draws entire library → Lab Man wins instead of losing"),
    ("Archmage Emeritus", "Niv-Mizzet, Parun", "every spell draws → Niv deals damage → chain of triggers"),
    ("Torbran, Thane of Red Fell", "Niv-Mizzet, Parun", "Niv deals 1+2=3 damage per draw instead of 1"),
    ("Electrostatic Field", "Guttersnipe", "both trigger on every spell → 4 damage total per instant/sorcery"),
]
