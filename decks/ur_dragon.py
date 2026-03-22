"""The Ur-Dragon — WUBRG Dragon Tribal."""

COMMANDER = "The Ur-Dragon"
EDHREC_SLUG = "the-ur-dragon"
COLOR_IDENTITY = {"W", "U", "B", "R", "G"}

DECKLIST = [
    # Dragon creatures
    "Ancient Brass Dragon", "Ancient Copper Dragon", "Ancient Gold Dragon",
    "Ancient Silver Dragon", "Atarka, World Render", "Balefire Dragon",
    "Betor, Kin to All", "Bladewing the Risen", "Dragonlord Dromoka",
    "Ganax, Astral Hunter", "Goldspan Dragon", "Hellkite Charger",
    "Hellkite Courser", "Hellkite Tyrant", "Klauth, Unrivaled Ancient",
    "Korlessa, Scale Singer", "Lathliss, Dragon Queen",
    "Miirym, Sentinel Wyrm", "Old Gnawbone", "Ramos, Dragon Engine",
    "Rith, Liberated Primeval", "Rivaz of the Claw",
    "Savage Ventmaw", "Scalelord Reckoner", "Scion of Draco",
    "Scion of the Ur-Dragon", "Scourge of Valkas",
    "Scourge of the Throne", "Silumgar, the Drifting Death",
    "Terror of the Peaks", "Thrakkus the Butcher", "Tiamat",
    "Twinflame Tyrant", "Two-Headed Hellkite",
    "Ureni of the Unwritten", "Utvara Hellkite",
    "Wrathful Red Dragon", "Zurgo and Ojutai",
    # Dragon tribal support
    "Morophon, the Boundless", "Roaming Throne",
    "Crucible of Fire", "Dragon Tempest", "Temur Ascendancy",
    "Herald's Horn", "Urza's Incubator", "Dragonlord's Servant",
    "Dragonspeaker Shaman",
    # Dragon-specific cards
    "Crux of Fate", "Sarkhan Unbroken", "Sarkhan, Soul Aflame",
    "Sarkhan's Triumph", "Orb of Dragonkind", "Carnelian Orb of Dragonkind",
    "Dragon's Hoard", "Dragonstorm Globe", "Call the Spirit Dragons",
    "Dracogenesis",
    # Support
    "Garruk's Uprising", "The Great Henge", "Rhythm of the Wild",
    "Heroic Intervention", "Farseek", "Nature's Lore", "Mox Jasper",
    # Lands (non-basic)
    "Arena of Glory", "Arid Mesa", "Blood Crypt", "Bloodstained Mire",
    "Breeding Pool", "Cavern of Souls", "Crucible of the Spirit Dragon",
    "Haven of the Spirit Dragon", "Jetmir's Garden", "Ketria Triome",
    "Maelstrom of the Spirit Dragon", "Overgrown Tomb",
    "Path of Ancestry", "Reflecting Pool", "Sacred Foundry",
    "Savai Triome", "Scalding Tarn", "Secluded Courtyard",
    "Steam Vents", "Stomping Ground", "Temple Garden",
    "Temple of the Dragon Queen", "Unclaimed Territory",
    "Verdant Catacombs", "Wooded Foothills", "Ziatora's Proving Ground",
]

DISTRACTORS = [
    "Dragonlord Silumgar", "Nicol Bolas, the Ravager",
    "Karrthus, Tyrant of Jund", "Drakuseth, Maw of Flames",
    "Dragon Broodmother", "Dragonmaster Outcast",
    "Kindred Discovery", "Reflections of Littjara", "Patriarch's Bidding",
]

SYNERGY_PAIRS = [
    ("The Ur-Dragon", "Terror of the Peaks", "Ur-Dragon's eminence makes dragons cheaper, Terror deals damage on each ETB"),
    ("The Ur-Dragon", "Scourge of Valkas", "each dragon entering deals damage equal to dragon count"),
    ("Miirym, Sentinel Wyrm", "Lathliss, Dragon Queen", "Miirym copies nonlegendary dragons, Lathliss makes tokens on each dragon ETB"),
    ("Terror of the Peaks", "Lathliss, Dragon Queen", "Lathliss creates 5/5 tokens, Terror deals 5 damage per token"),
    ("Savage Ventmaw", "Hellkite Charger", "Ventmaw generates RRRGGG on attack, Charger costs 5RR for extra combat = infinite combats"),
    ("Old Gnawbone", "Hellkite Charger", "Gnawbone creates treasures on combat damage, Charger uses them for extra combats"),
    ("Goldspan Dragon", "Klauth, Unrivaled Ancient", "both generate massive mana on attack"),
    ("Scion of the Ur-Dragon", "Bladewing the Risen", "Scion becomes Bladewing to reanimate dragons from graveyard"),
    ("Dragon Tempest", "Scourge of Valkas", "double damage triggers on each dragon ETB"),
    ("Tiamat", "The Ur-Dragon", "Tiamat tutors 5 dragons on ETB, Ur-Dragon makes them cheaper"),
    ("Crucible of Fire", "Atarka, World Render", "Crucible pumps +3/+3, Atarka gives double strike = lethal"),
    ("Temur Ascendancy", "Miirym, Sentinel Wyrm", "Miirym copies enter with haste from Ascendancy + draw cards"),
    ("Urza's Incubator", "Dragonlord's Servant", "cost reduction stacks with Ur-Dragon eminence"),
    ("Morophon, the Boundless", "The Ur-Dragon", "Morophon makes dragons cost WUBRG less + Ur-Dragon -1 = most free"),
    ("Utvara Hellkite", "Terror of the Peaks", "Utvara creates 6/6 tokens on attack, Terror deals 6 per token"),
]
