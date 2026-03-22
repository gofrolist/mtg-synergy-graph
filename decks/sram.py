"""Sram, Senior Edificer — Mono-White equipment / auras / voltron."""

COMMANDER = "Sram, Senior Edificer"
EDHREC_SLUG = "sram-senior-edificer"
COLOR_IDENTITY = {"W"}

DECKLIST = [
    # 0-1 cost equipment (draw engine with Sram)
    "Accorder's Shield", "Bone Saw", "Bonesplitter",
    "Cathar's Shield", "Cliffhaven Kitesail", "Colossus Hammer",
    "Explorer's Scope", "Kite Shield", "O-Naginata",
    "Paradise Mantle", "Prying Blade", "Spidersilk Net",
    # Power equipment
    "Basilisk Collar", "Blackblade Reforged", "Bloodforged Battle-Axe",
    "Commander's Plate", "Excalibur, Sword of Eden",
    "Fireshrieker", "Golem-Skin Gauntlets",
    "Hammer of Nazahn", "Loxodon Warhammer",
    "Mask of Memory", "Masterwork of Ingenuity",
    "Mithril Coat", "Nettlecyst", "Shadowspear",
    "Sword of Feast and Famine", "Sword of Fire and Ice",
    "Sword of Vengeance", "Sword of the Animist",
    "Swiftfoot Boots", "Thran Power Suit", "Trailblazer's Boots",
    # Auras
    "All That Glitters", "Armored Ascension", "Conviction",
    "Darksteel Mutation", "Ethereal Armor", "Flickering Ward",
    "Hyena Umbra", "Mantle of the Ancients",
    "Spirit Mantle", "Unquestioned Authority",
    # Equipment/aura support creatures
    "Ardenn, Intrepid Archaeologist", "Balan, Wandering Knight",
    "Cid, Freeflier Pilot", "Cloud, Midgar Mercenary",
    "Codsworth, Handy Helper", "Danitha Capashen, Paragon",
    "Foundry Inspector", "Kemba, Kha Regent",
    "Leonin Shikari", "Lion Sash",
    "Puresteel Paladin", "Starnheim Courser",
    "Stoneforge Mystic", "Stonehewer Giant",
    # Tutors
    "Open the Armory", "Steelshaper's Gift", "Sigarda's Aid",
    "Forge Anew", "Mystic Forge",
    # Removal
    "Dispatch", "Divine Reckoning", "Single Combat",
    "Winds of Rath",
    # Lands (non-basic)
    "Ancient Den", "Buried Ruin", "Darksteel Citadel",
    "Inventors' Fair", "Reliquary Tower", "Rogue's Passage",
    "Urza's Saga",
]

DISTRACTORS = [
    "Helm of the Host", "Argentum Armor", "Batterskull",
    "Umezawa's Jitte", "Teferi's Protection", "Smothering Tithe",
    "Enlightened Tutor", "Sun Titan", "Heliod, Sun-Crowned",
]

SYNERGY_PAIRS = [
    ("Sram, Senior Edificer", "Colossus Hammer", "Hammer costs 1 to cast → Sram draws → attach for +10/+10"),
    ("Sram, Senior Edificer", "Puresteel Paladin", "both draw on equipment/aura cast → double draw engine"),
    ("Sram, Senior Edificer", "Sigarda's Aid", "flash equipment + auto-attach → Sram draws + instant equip"),
    ("Puresteel Paladin", "Colossus Hammer", "metalcraft makes equip free → Hammer is free 10/10"),
    ("Hammer of Nazahn", "Colossus Hammer", "Hammer of Nazahn auto-attaches → Colossus Hammer attached for free"),
    ("Stoneforge Mystic", "Colossus Hammer", "tutor Hammer → put it in for 2 mana"),
    ("All That Glitters", "Ethereal Armor", "both scale with enchantment/artifact count → massive pump"),
    ("Sword of Feast and Famine", "Sram, Senior Edificer", "untap lands on hit → cast more equipment → Sram draws more"),
    ("Ardenn, Intrepid Archaeologist", "Colossus Hammer", "Ardenn attaches equipment for free on combat"),
    ("Kemba, Kha Regent", "Sram, Senior Edificer", "equipment triggers Sram draw + Kemba makes cat tokens"),
    ("Winds of Rath", "Ethereal Armor", "Winds destroys non-enchanted → your aura'd creature survives"),
    ("Masterwork of Ingenuity", "Sword of Fire and Ice", "copy the best equipment for 1 mana"),
    ("Mantle of the Ancients", "Sram, Senior Edificer", "reattach all auras/equipment from graveyard → massive draw burst"),
    ("Leonin Shikari", "Swiftfoot Boots", "flash-equip Boots for instant hexproof protection"),
    ("Bloodforged Battle-Axe", "Sram, Senior Edificer", "axe copies itself on hit → each copy triggers more equip casts"),
]
