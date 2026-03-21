"""Atraxa, Praetors' Voice — WUBG Proliferate / Infect / Superfriends."""

COMMANDER = "Atraxa, Praetors' Voice"
EDHREC_SLUG = "atraxa-praetors-voice"
COLOR_IDENTITY = {"W", "U", "B", "G"}

DECKLIST = [
    # Planeswalkers
    "Ajani, Sleeper Agent", "Tamiyo, Field Researcher", "Vraska, Betrayal's Sting",
    # Infect creatures
    "Blightbelly Rat", "Blighted Agent", "Bloated Contaminator", "Contaminant Grafter",
    "Ichor Rats", "Plague Stinger", "Skithiryx, the Blight Dragon", "Tainted Observer",
    "Venerated Rotpriest", "Voidwing Hybrid",
    # Proliferate enablers
    "Contagion Clasp", "Contagion Engine", "Deepglow Skate", "Evolution Sage",
    "Flux Channeler", "Grateful Apparition", "Inexorable Tide", "Tekuthal, Inquiry Dominus",
    "Thrummingbird", "Norn's Choirmaster",
    # Infect/poison support
    "Phyrexian Swarmlord", "Vishgraz, the Doomhive", "Ixhel, Scion of Atraxa",
    "Skrelv, Defector Mite", "Metastatic Evangel", "Dreamtide Whale",
    # Counter synergy
    "Doubling Season", "Vorinclex, Monstrous Raider", "Brokers Ascendancy",
    "Sword of Truth and Justice", "Innkeeper's Talent", "Norn's Decree",
    "Shalai, Voice of Plenty", "Ichormoon Gauntlet",
    # Interaction
    "Counterspell", "Atomize", "Cankerbloom", "Drown in Ichor", "Infectious Bite",
    "Phyresis Outbreak", "Vraska's Fall",
    # Card advantage
    "Contentious Plan", "Experimental Augury", "Infectious Inquiry",
    "Prologue to Phyresis", "Radstorm", "Staff of Compleation",
    "Tezzeret's Gambit", "Oath of Teferi",
    # Ramp / mana
    "Astral Cornucopia", "Chromatic Lantern", "Cultivate", "Everflowing Chalice",
    "Farseek", "Glistening Sphere", "Replicating Ring", "Thirsting Roots",
    "Unnatural Restoration",
    # Equipment
    "Lightning Greaves",
    # Lands (non-basic)
    "Arcane Sanctum", "Canopy Vista", "Dreadship Reef", "Evolving Wilds",
    "Gavony Township", "Indatha Triome", "Inkmoth Nexus", "Interplanar Beacon",
    "Karn's Bastion", "Nesting Grounds", "Opulent Palace", "Path of Ancestry",
    "Raffine's Tower", "Reflecting Pool", "Rejuvenating Springs", "Rogue's Passage",
    "Saltcrusted Steppe", "Sandsteppe Citadel", "Seaside Citadel",
    "Spara's Headquarters", "Terramorphic Expanse", "The Seedcore",
    "Zagoth Triome",
]

DISTRACTORS = [
    "Atraxa, Grand Unifier", "Triumph of the Hordes", "Grafted Exoskeleton",
    "Phyrexian Juggernaut", "Hand of the Praetors", "Corrupted Conscience",
    "Tainted Strike", "Blightsteel Colossus", "Glistener Elf",
]

SYNERGY_PAIRS = [
    ("Atraxa, Praetors' Voice", "Contagion Engine", "Atraxa proliferates, Engine proliferates twice"),
    ("Atraxa, Praetors' Voice", "Inexorable Tide", "every spell + Atraxa = double proliferate per turn"),
    ("Atraxa, Praetors' Voice", "Tekuthal, Inquiry Dominus", "Tekuthal doubles Atraxa's proliferate"),
    ("Doubling Season", "Vorinclex, Monstrous Raider", "both double counters placed"),
    ("Doubling Season", "Deepglow Skate", "Skate doubles, Season doubles again = 4x"),
    ("Contagion Engine", "Ichor Rats", "Rats give poison, Engine proliferates it"),
    ("Flux Channeler", "Tezzeret's Gambit", "Gambit proliferates + Channeler proliferates = 2"),
    ("Evolution Sage", "Farseek", "land enters → proliferate"),
    ("Thrummingbird", "Sword of Truth and Justice", "both proliferate on combat damage"),
    ("Skithiryx, the Blight Dragon", "Rogue's Passage", "unblockable infect = 2-hit kill"),
    ("Vorinclex, Monstrous Raider", "Atraxa, Praetors' Voice", "Vorinclex doubles Atraxa's proliferate counters"),
    ("Blighted Agent", "Inkmoth Nexus", "two unblockable infect sources"),
    ("Ichormoon Gauntlet", "Atraxa, Praetors' Voice", "proliferate ticks up planeswalker loyalty"),
    ("Norn's Choirmaster", "Atraxa, Praetors' Voice", "Choirmaster adds counters when Atraxa deals combat damage"),
    ("Karn's Bastion", "Atraxa, Praetors' Voice", "land-based proliferate backup"),
]
