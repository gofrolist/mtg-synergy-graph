"""Syr Konrad, the Grim — Mono-Black graveyard / aristocrats."""

COMMANDER = "Syr Konrad, the Grim"
EDHREC_SLUG = "syr-konrad-the-grim"
COLOR_IDENTITY = {"B"}

DECKLIST = [
    # Self-mill engines
    "Mesmeric Orb", "Altar of Dementia", "Stitcher's Supplier",
    "Corpse Connoisseur", "Gravebreaker Lamia",
    # Reanimation
    "Animate Dead", "Reanimate", "Exhume", "Stitch Together",
    "Living Death", "Victimize", "Whip of Erebos", "Champion of Stray Souls",
    "Phyrexian Reclamation", "Rise of the Dark Realms",
    # Sacrifice outlets
    "Viscera Seer", "Carrion Feeder", "Ashnod's Altar", "Phyrexian Altar",
    "Altar of Dementia",
    # Death/graveyard triggers — the Konrad damage engine
    "Blood Artist", "Zulaport Cutthroat", "Midnight Reaper",
    "Pawn of Ulamog", "Grim Haruspex", "Desecrated Tomb",
    "Secrets of the Dead",
    # Aristocrats payoffs
    "Gray Merchant of Asphodel", "Dictate of Erebos", "Grave Pact",
    "Butcher of Malakir", "Fleshbag Marauder", "Merciless Executioner",
    "Plaguecrafter",
    # Recursion / graveyard support
    "Relentless Dead", "Tortured Existence", "Nim Deathmantle",
    "Mikaeus, the Unhallowed", "Coffin Queen",
    # Finishers / game-changers
    "Sheoldred, the Apocalypse", "Massacre Wurm", "Bolas's Citadel",
    "Liliana, Dreadhorde General",
    # Board wipes
    "Damnation", "Toxic Deluge",
    # Removal
    "Infernal Grasp", "Deadly Rollick", "Hero's Downfall",
    # Ramp
    "Sol Ring", "Arcane Signet", "Dark Ritual", "Crypt Ghast",
    # Card draw / advantage
    "Phyrexian Arena", "Read the Bones", "Necropotence", "Skullclamp",
    "Sign in Blood", "Night's Whisper", "Dark Prophecy", "Peer into the Abyss",
    # Tutors
    "Demonic Tutor", "Entomb", "Buried Alive",
    # Graveyard hate tech
    "Filth",
    # Lands
    "Cabal Coffers", "Cabal Stronghold", "Urborg, Tomb of Yawgmoth",
    "Phyrexian Tower", "Volrath's Stronghold", "Command Beacon",
    "Bojuka Bog", "Unholy Grotto", "Mortuary Mire", "Castle Locthwain",
    "Witch's Cottage",
    "Swamp", "Swamp", "Swamp", "Swamp", "Swamp",
    "Swamp", "Swamp", "Swamp", "Swamp", "Swamp",
    "Swamp", "Swamp", "Swamp", "Swamp", "Swamp",
    "Swamp", "Swamp", "Swamp", "Swamp", "Swamp",
    "Swamp", "Swamp", "Swamp", "Swamp", "Swamp",
]

DISTRACTORS = [
    "Haunting Voyage",     # only good if many creatures died this turn — inconsistent
    "Patriarch's Bidding", # symmetric reanimate — helps opponents too
    "Living End",          # requires cascade setup, awkward without it
    "Lolth, Spider Queen", # needs steady creature deaths but isn't a drain payoff
    "Dark Prophecy",       # already in deck — this is a distractor slot placeholder
    "Zombie Infestation",  # makes tokens but wastes cards in hand
    "Haakon, Stromgald Scourge",  # only recurs Knights, doesn't combo with Konrad
    "Death's Caress",      # overcosted removal with minor upside
    "Bitter Ordeal",       # win-more storm card — requires combo to be online already
]

SYNERGY_PAIRS = [
    ("Syr Konrad, the Grim", "Altar of Dementia", "mill yourself → every creature entering graveyard pings each opponent"),
    ("Syr Konrad, the Grim", "Mesmeric Orb", "Orb mills on every untap — chains with Konrad pings"),
    ("Syr Konrad, the Grim", "Living Death", "wipe + reanimate → every creature leaving and re-entering graveyard triggers Konrad"),
    ("Syr Konrad, the Grim", "Mikaeus, the Unhallowed", "Mikaeus undying keeps creatures looping back through graveyard for Konrad triggers"),
    ("Syr Konrad, the Grim", "Blood Artist", "creatures dying = double drain: Artist + Konrad"),
    ("Syr Konrad, the Grim", "Nim Deathmantle", "Deathmantle loops a dying creature repeatedly for repeated Konrad triggers"),
    ("Viscera Seer", "Blood Artist", "free sac outlet + drain on each death"),
    ("Ashnod's Altar", "Living Death", "sac board before Living Death → generate mana, then reanimate everything"),
    ("Dictate of Erebos", "Fleshbag Marauder", "Fleshbag + Dictate forces opponents to sac two creatures"),
    ("Grave Pact", "Plaguecrafter", "Plaguecrafter ETB clears boards via Grave Pact"),
    ("Bolas's Citadel", "Necropotence", "Citadel lets you cast from top; Necropotence refills hand as resource"),
    ("Cabal Coffers", "Urborg, Tomb of Yawgmoth", "Urborg makes all lands Swamps → Coffers generates massive mana"),
    ("Buried Alive", "Animate Dead", "Buried Alive places targets, Animate Dead brings them back immediately"),
    ("Entomb", "Reanimate", "instant-speed combo: Entomb + Reanimate at end of opponent's turn"),
    ("Midnight Reaper", "Dictate of Erebos", "each opponent sac → Reaper draws you a card per creature you sac in response"),
]

# Scryfall supplement filters for Syr Konrad
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "graveyard" in oracle_text.lower() and "damage" in oracle_text.lower()
        ),
        "reason": "graveyard-damage",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever.*creature.*dies", oracle_text.lower()))
        ),
        "reason": "creature-death-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"sacrifice.*creature.*:", oracle_text.lower()))
        ),
        "reason": "sac-outlet",
    },
    {
        "check": lambda type_line, oracle_text: (
            "mill" in oracle_text.lower() and ("you" in oracle_text.lower())
        ),
        "reason": "self-mill",
    },
]
