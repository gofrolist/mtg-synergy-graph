"""Tatyova, Benthic Druid — UG landfall / extra lands."""

COMMANDER = "Tatyova, Benthic Druid"
EDHREC_SLUG = "tatyova-benthic-druid"
COLOR_IDENTITY = {"U", "G"}

DECKLIST = [
    # Extra land plays
    "Azusa, Lost but Seeking", "Exploration", "Burgeoning",
    "Oracle of Mul Daya", "Courser of Kruphix", "Dryad of the Ilysian Grove",
    "Wayward Swordtooth", "Druid Class", "Spelunking",
    # Land-to-hand / land scouts
    "Sakura-Tribe Scout", "Llanowar Scout", "Skyshroud Ranger",
    "Walking Atlas", "Scaled Herbalist",
    # Landfall payoffs
    "Avenger of Zendikar", "Rampaging Baloths", "Scute Swarm",
    "Field of the Dead", "Roil Elemental", "Zendikar's Roil",
    "Sporemound", "Retreat to Coralhelm", "Retreat to Kazandu",
    "Tireless Tracker", "Tireless Provisioner", "Lotus Cobra",
    "Cultivator Colossus", "Mossborn Hydra",
    # Land recursion / extra value
    "Ramunap Excavator", "Crucible of Worlds", "Ancient Greenwarden",
    "Conduit of Worlds", "Splendid Reclamation", "Scapeshift",
    "Trade Routes", "Meloku the Clouded Mirror",
    # Ramp / land search
    "Cultivate", "Kodama's Reach", "Nature's Lore", "Rampant Growth",
    "Harrow", "Roiling Regrowth", "Sakura-Tribe Elder",
    "Springbloom Druid", "Khalni Heart Expedition",
    "Growth Spiral", "Explore", "Summer Bloom",
    "Crop Rotation", "Entish Restoration",
    # Value creatures
    "Aesi, Tyrant of Gyre Strait", "Ashaya, Soul of the Wild",
    "Bonny Pall, Clearcutter", "Coiling Oracle", "Eternal Witness",
    "Icetill Explorer", "Loot, Exuberant Explorer",
    "Traveling Chocobo",
    # Interaction
    "Broken Bond", "Deprive", "Negate", "Sylvan Safekeeper",
    # Extra turns
    "Walk the Aeons",
    # Lands (non-basic)
    "Blighted Woodland", "Brokers Hideout", "Evolving Wilds",
    "Fabled Passage", "Flooded Strand", "Guildless Commons",
    "Myriad Landscape", "Mystic Sanctuary",
    "Oboro, Palace in the Clouds", "Prismatic Vista",
    "Simic Growth Chamber", "Strip Mine",
    "Terramorphic Expanse", "Verdant Catacombs",
    "Windswept Heath", "Wooded Foothills",
]

DISTRACTORS = [
    "Craterhoof Behemoth", "Cyclonic Rift", "Omniscience",
    "Tooth and Nail", "Seedborn Muse", "Alchemist's Refuge",
    "Panharmonicon", "Thassa, Deep-Dwelling", "Risen Reef",
]

SYNERGY_PAIRS = [
    ("Tatyova, Benthic Druid", "Azusa, Lost but Seeking", "Azusa plays 3 lands/turn → Tatyova draws 3 cards + gains 3 life"),
    ("Tatyova, Benthic Druid", "Exploration", "extra land drop = extra Tatyova trigger every turn"),
    ("Retreat to Coralhelm", "Sakura-Tribe Scout", "Scout taps to play land → Retreat untaps Scout → infinite landfall with bounce land"),
    ("Scapeshift", "Avenger of Zendikar", "sacrifice 7 lands, search 7 → Avenger makes 7 tokens, each gets 7 +1/+1 counters"),
    ("Ancient Greenwarden", "Tatyova, Benthic Druid", "Greenwarden doubles landfall → Tatyova draws 2 per land"),
    ("Crucible of Worlds", "Strip Mine", "recur Strip Mine every turn to destroy a land"),
    ("Ramunap Excavator", "Evolving Wilds", "replay fetch lands from graveyard for repeated landfall"),
    ("Lotus Cobra", "Scapeshift", "each land entering makes mana → Scapeshift = massive mana burst"),
    ("Field of the Dead", "Scapeshift", "Scapeshift puts 7+ unique lands → 7+ zombie tokens"),
    ("Roil Elemental", "Azusa, Lost but Seeking", "3 lands/turn = steal 3 creatures per turn"),
    ("Splendid Reclamation", "Scapeshift", "Scapeshift sacs all lands → Reclamation returns them all → double triggers"),
    ("Trade Routes", "Tatyova, Benthic Druid", "bounce land to hand, replay = draw + gain life repeatedly"),
    ("Meloku the Clouded Mirror", "Tatyova, Benthic Druid", "bounce land → make token → replay land → Tatyova triggers"),
    ("Cultivator Colossus", "Tatyova, Benthic Druid", "Colossus puts all lands from hand onto battlefield → massive Tatyova triggers"),
    ("Tireless Provisioner", "Scapeshift", "each land = treasure or food → Scapeshift = 7+ treasures"),
]
