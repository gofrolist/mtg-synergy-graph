"""Syr Konrad, the Grim — mono-B graveyard/aristocrats/mill."""

COMMANDER = "Syr Konrad, the Grim"
EDHREC_SLUG = "syr-konrad-the-grim"
COLOR_IDENTITY = {"B"}

DECKLIST = [
    # Self-mill / graveyard filling
    "Altar of Dementia", "Altar of the Brood", "Mesmeric Orb",
    "Stitcher's Supplier", "Eye Collector", "Millikin",
    "Mindwrack Harpy", "Mire Triton", "Stinkweed Imp",
    "Golgari Thug", "Dakmor Salvage", "Doom Whisperer",
    "Dread Summons", "Morality Shift", "Perpetual Timepiece",
    "Ghoulcaller's Bell", "Ripples of Undeath",
    "Corpse Churn", "Unseal the Necropolis",
    # Combo / synergy
    "Mindcrank", "Bloodchief Ascension", "Angel of Suffering",
    "Phyresis",
    # Death triggers / aristocrats
    "Blood Artist", "Zulaport Cutthroat", "Gray Merchant of Asphodel",
    "Ayara, First of Locthwain", "Dreadhound",
    "Pawn of Ulamog", "Tormod, the Desecrator",
    # Sacrifice outlets
    "Viscera Seer", "Carrion Feeder", "Woe Strider",
    # Sacrifice payoffs / edict effects
    "Braids, Arisen Nightmare", "Fleshbag Marauder",
    "Merciless Executioner", "Demon's Disciple",
    "Plaguecrafter", "Accursed Marauder",
    # Reanimation
    "Dread Return", "Victimize", "Living Death",
    "Breach the Multiverse", "Wake the Dead",
    "Reassembling Skeleton", "Undead Butler",
    # Recursion / graveyard manipulation
    "Tortured Existence", "Forever Young", "Footbottom Feast",
    "Gravepurge", "Returned Reveler",
    # Card draw / value
    "Grim Haruspex", "Midnight Reaper", "Fell Stinger",
    "Songs of the Damned",
    # Removal
    "Massacre Girl", "Massacre Wurm", "Ravenous Chupacabra",
    "Shriekmaw",
    # Ramp / mana
    "Burnished Hart", "Leaden Myr", "Ornithopter of Paradise",
    "Solemn Simulacrum",
    "Undercity Informer",
    # Lands (non-basic)
    "Bojuka Bog", "Crypt of Agadeem", "Memorial to Folly",
    "Mortuary Mire", "Scavenger Grounds", "Witch's Cottage",
]

DISTRACTORS = [
    "Necropotence", "Phyrexian Arena", "Bolas's Citadel",
    "Grave Titan", "Sheoldred, Whispering One", "Entomb",
    "Reanimate", "Exsanguinate", "Cabal Coffers",
]

SYNERGY_PAIRS = [
    ("Syr Konrad, the Grim", "Mindcrank", "Konrad deals damage when creatures enter/leave graveyard → Mindcrank mills → more creatures enter graveyard → infinite loop"),
    ("Syr Konrad, the Grim", "Mesmeric Orb", "every untap mills → creatures enter graveyard → Konrad deals damage"),
    ("Syr Konrad, the Grim", "Morality Shift", "swap library and graveyard → every creature changes zones → massive damage"),
    ("Syr Konrad, the Grim", "Altar of Dementia", "sacrifice creatures → mill yourself → Konrad triggers on death + mill"),
    ("Mindcrank", "Bloodchief Ascension", "infinite combo: opponent loses life → mill → cards enter graveyard → Ascension triggers → opponent loses life"),
    ("Blood Artist", "Viscera Seer", "sacrifice creatures for scry + drain"),
    ("Zulaport Cutthroat", "Living Death", "Living Death kills + reanimates → double drain triggers"),
    ("Gray Merchant of Asphodel", "Victimize", "reanimate Gary for repeated devotion drain"),
    ("Stinkweed Imp", "Tortured Existence", "discard Imp → dredge 5 → discard another creature → repeat"),
    ("Reassembling Skeleton", "Altar of Dementia", "sacrifice Skeleton → mill → reanimate Skeleton → repeat"),
    ("Grim Haruspex", "Fleshbag Marauder", "Marauder forces sacrifice → Haruspex draws on each death"),
    ("Dreadhound", "Syr Konrad, the Grim", "both trigger on creatures entering graveyard → double damage"),
    ("Angel of Suffering", "Syr Konrad, the Grim", "damage becomes mill → Konrad triggers on milled creatures"),
    ("Braids, Arisen Nightmare", "Pawn of Ulamog", "Braids sacrifices → Pawn makes tokens → sacrifice tokens"),
    ("Phyresis", "Syr Konrad, the Grim", "Konrad's damage becomes infect → 10 poison counters kills"),
]
