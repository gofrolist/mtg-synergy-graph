"""Tatyova, Benthic Druid — UG landfall / extra lands."""

COMMANDER = "Tatyova, Benthic Druid"
EDHREC_SLUG = "tatyova-benthic-druid"
COLOR_IDENTITY = {"U", "G"}

DECKLIST = [
    # Extra land drops — the engine
    "Exploration", "Burgeoning", "Azusa, Lost but Seeking", "Oracle of Mul Daya",
    "Wayward Swordtooth", "Dryad of the Ilysian Grove",
    # Landfall payoffs
    "Avenger of Zendikar", "Rampaging Baloths", "Scute Swarm",
    "Zendikar's Roil", "Roil Elemental", "Aesi, Tyrant of Gyre Strait",
    "Lotus Cobra", "Tireless Provisioner",
    # Land recursion / graveyard
    "Ramunap Excavator", "Crucible of Worlds", "Life from the Loam",
    "Splendid Reclamation", "World Shaper",
    # Land tutors / fetch chains
    "Crop Rotation", "Scapeshift", "Pir's Whim", "Harrow",
    "Sakura-Tribe Elder", "Farseek", "Nature's Lore", "Three Visits",
    "Cultivate", "Kodama's Reach",
    # Landfall enablers (bounce / extra-play tricks)
    "Simic Growth Chamber", "Ghost Town", "Oboro, Palace in the Clouds",
    "Walking Atlas", "Retreat to Coralhelm", "Trade Routes",
    # Draw / value
    "Growth Spiral", "Explore", "Mulch", "Abundance",
    "Tireless Tracker", "Courser of Kruphix",
    # Win conditions
    "Multani, Yavimaya's Avatar", "Ashaya, Soul of the Wild",
    "Sylvan Awakening", "Reshape the Earth",
    # Interaction
    "Cyclonic Rift", "Beast Within", "Arcane Denial", "Counterspell",
    # Ramp / mana
    "Sol Ring", "Arcane Signet",
    # Extra bounce land payoffs / field of the dead
    "Perilous Forays",
    # Lands
    "Field of the Dead",
    "Gaea's Cradle", "Yavimaya, Cradle of Growth",
    "Tolarian Academy",
    "Evolving Wilds", "Terramorphic Expanse", "Fabled Passage",
    "Strip Mine", "Wasteland",
    "Vesuva", "Thespian's Stage",
    "Command Tower",
    "Breeding Pool", "Hinterland Harbor",
    "Tropical Island",
    "Forest", "Forest", "Forest", "Forest", "Forest",
    "Forest", "Forest", "Forest", "Forest", "Forest",
    "Island", "Island", "Island", "Island", "Island",
    "Island", "Island",
]

DISTRACTORS = [
    "Nissa, Who Shakes the World",   # strong but mostly ramp, not landfall synergy
    "Retreat to Kazandu",            # life gain retreats don't advance the game plan
    "Ghostly Flicker",               # bounces permanents but Tatyova triggers on land drops, not ETBs
    "Displace",                      # similar to above — doesn't trigger landfall
    "Teferi, Master of Time",        # card draw but no land synergy
    "Bountiful Harvest",             # overcosted land payoff — Tatyova already draws
    "Horn of Greed",                 # symmetric draw — helps opponents equally
    "Canopy Vista",                  # tapland, fine fetch target but not exciting
    "Prairie Stream",                # wrong colors' synergy, just a land
]

SYNERGY_PAIRS = [
    ("Tatyova, Benthic Druid", "Exploration", "each extra land = card + 1 life via Tatyova"),
    ("Tatyova, Benthic Druid", "Azusa, Lost but Seeking", "Azusa provides 3 land drops → 3 Tatyova draws per turn"),
    ("Tatyova, Benthic Druid", "Simic Growth Chamber", "bounce land re-triggers Tatyova each time you replay it"),
    ("Tatyova, Benthic Druid", "Ghost Town", "return Ghost Town during opponent's turn, replay on yours for extra draw"),
    ("Tatyova, Benthic Druid", "Lotus Cobra", "every land drop adds mana and draws via Tatyova"),
    ("Tatyova, Benthic Druid", "Crucible of Worlds", "replay fetchlands from graveyard repeatedly for repeated Tatyova triggers"),
    ("Ramunap Excavator", "Crucible of Worlds", "redundant strip-mine recursion — both enable land replay loops"),
    ("Scute Swarm", "Avenger of Zendikar", "each subsequent land doubles Scute Swarm → exponential token flood"),
    ("Field of the Dead", "Scapeshift", "Scapeshift fetches 7+ differently named lands → instant Field zombie army"),
    ("Lotus Cobra", "Fabled Passage", "fetch triggers Cobra twice: cracking + basic ETB"),
    ("Life from the Loam", "Strip Mine", "dredge Loam → replay Strip Mine every turn"),
    ("Crop Rotation", "Field of the Dead", "instant-speed find Field of the Dead when you need the zombie trigger"),
    ("Retreat to Coralhelm", "Walking Atlas", "Atlas triggers Retreat's untap effect → tap Atlas again → infinite land drops"),
    ("Avenger of Zendikar", "Perilous Forays", "sac a plant token to find a land → re-trigger Avenger → infinite tokens"),
    ("Crucible of Worlds", "Evolving Wilds", "sacrifice and replay Wilds each turn for a free basic + Tatyova draw"),
]

# Scryfall supplement filters for Tatyova
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"landfall", oracle_text.lower()))
        ),
        "reason": "landfall",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"land.*enters.*battlefield|whenever.*land.*enter", oracle_text.lower()))
        ),
        "reason": "land-etb-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"play.*additional land|additional land.*each turn", oracle_text.lower()))
        ),
        "reason": "extra-land-drop",
    },
    {
        "check": lambda type_line, oracle_text: (
            "dredge" in oracle_text.lower() and "land" in oracle_text.lower()
        ),
        "reason": "land-dredge",
    },
]
