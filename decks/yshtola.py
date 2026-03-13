"""Y'shtola, Night's Blessed — Esper spellslinger / drain / control."""

COMMANDER = "Y'shtola, Night's Blessed"
EDHREC_SLUG = "yshtola-nights-blessed"
COLOR_IDENTITY = {"W", "U", "B"}

DECKLIST = [
    # Creatures — Drain/Lifegain payoffs
    "Sheoldred, the Apocalypse", "Vito, Thorn of the Dusk Rose",
    "Bloodthirsty Conqueror", "Enduring Tenacity",
    # Creatures — Card advantage / value
    "Baleful Strix", "Archmage Emeritus", "Esper Sentinel",
    "Lotho, Corrupt Shirriff", "Sygg, River Cutthroat",
    "Faerie Mastermind", "Torrential Gearhulk",
    # Creatures — Control/stax
    "Opposition Agent", "Drannith Magistrate", "Grand Abolisher",
    "Baral, Chief of Compliance", "Hullbreaker Horror",
    "Orcish Bowmasters", "Displacer Kitten",
    # Creatures — Utility
    "Spark Double", "Murderous Rider",
    # Instants — Counterspells
    "Counterspell", "Swan Song", "Dovin's Veto", "Arcane Denial",
    "Force of Negation", "Mana Drain", "Rewind",
    # Instants — Removal
    "Swords to Plowshares", "Path to Exile", "Generous Gift",
    "Anguished Unmaking", "Deadly Rollick", "Snuff Out",
    "Stroke of Midnight",
    # Instants — Value
    "Dig Through Time", "Vampiric Tutor", "Enlightened Tutor",
    "Dark Ritual", "Inkshield", "Sublime Epiphany",
    # Sorceries
    "Exsanguinate", "Toxic Deluge", "Treasure Cruise",
    "Windfall", "Syphon Mind",
    # Enchantments — Drain engine
    "Exquisite Blood", "Sanguine Bond",
    "Authority of the Consuls", "Blind Obedience",
    "Bloodchief Ascension", "Painful Quandary",
    # Enchantments — Card advantage
    "Black Market Connections", "Phyrexian Arena",
    "Mystic Remora", "Rhystic Study", "Trouble in Pairs",
    # Enchantments — Utility
    "Leyline of Anticipation", "Shark Typhoon", "Necropotence",
    # Artifacts — Ramp
    "Sol Ring", "Arcane Signet", "Talisman of Dominance",
    "Talisman of Progress", "Talisman of Hierarchy",
    "Thought Vessel",
    # Artifacts — Payoffs
    "Aetherflux Reservoir", "Alhammarret's Archive",
    # Artifacts — Utility
    "Sensei's Divining Top", "Lightning Greaves", "Swiftfoot Boots",
    # Planeswalkers
    "Teferi, Time Raveler", "Narset, Parter of Veils",
    # Lands
    "Command Tower", "Exotic Orchard", "Arcane Sanctum",
    "Raffine's Tower", "Ancient Tomb",
    "Polluted Delta", "Flooded Strand", "Marsh Flats",
    "Watery Grave", "Godless Shrine", "Hallowed Fountain",
    "Drowned Catacomb", "Glacial Fortress", "Isolated Chapel",
    "Morphic Pool", "Sea of Clouds", "Vault of Champions",
    "Reliquary Tower", "Urborg, Tomb of Yawgmoth",
    "Otawara, Soaring City", "Bojuka Bog",
    "City of Brass", "Mana Confluence",
    "Island", "Island", "Plains", "Swamp", "Swamp", "Swamp",
]

DISTRACTORS = [
    "Approach of the Second Sun",  # win condition but doesn't synergize with drain
    "Omniscience",                 # too expensive, win-more
    "Doomsday",                    # different combo axis
    "Ad Nauseam",                  # life payment conflicts with drain plan
    "Notion Thief",                # replaced by Narset + Bowmasters
]

SYNERGY_PAIRS = [
    # Drain combos
    ("Exquisite Blood", "Sanguine Bond", "infinite drain loop — each triggers the other"),
    ("Exquisite Blood", "Vito, Thorn of the Dusk Rose", "Vito replaces Sanguine Bond for infinite drain"),
    ("Vito, Thorn of the Dusk Rose", "Sheoldred, the Apocalypse", "Sheoldred draws → drains, Vito doubles the drain"),
    # Commander synergy
    ("Y'shtola, Night's Blessed", "Sheoldred, the Apocalypse", "Sheoldred ensures 4+ life loss for Y'shtola's draw trigger"),
    ("Y'shtola, Night's Blessed", "Leyline of Anticipation", "cast noncreature spells on each end step to trigger drain + draw"),
    ("Y'shtola, Night's Blessed", "Exquisite Blood", "Y'shtola drains opponents, Exquisite Blood gains life off it"),
    ("Y'shtola, Night's Blessed", "Aetherflux Reservoir", "repeated spell casting gains life, Reservoir converts to lethal"),
    # Spellslinger synergy
    ("Archmage Emeritus", "Leyline of Anticipation", "flash lets you cast spells on every turn for more Emeritus draws"),
    ("Baral, Chief of Compliance", "Counterspell", "Baral discounts counters and draws on counter"),
    ("Torrential Gearhulk", "Dig Through Time", "Gearhulk flashes back Dig for massive card selection"),
    # Lifegain payoffs
    ("Aetherflux Reservoir", "Authority of the Consuls", "Consuls gains life per creature, Reservoir accumulates to 50"),
    ("Alhammarret's Archive", "Phyrexian Arena", "Archive doubles Arena's draw and other card draw"),
    ("Alhammarret's Archive", "Sheoldred, the Apocalypse", "Archive doubles Sheoldred's draw trigger card draw"),
    # Control synergy
    ("Teferi, Time Raveler", "Counterspell", "Teferi stops opponent counters, your counters still work"),
    ("Narset, Parter of Veils", "Windfall", "opponents discard hand and draw 1, you refill"),
    ("Grand Abolisher", "Exsanguinate", "Abolisher protects big X spells from counters"),
    # Drain engines
    ("Bloodchief Ascension", "Windfall", "Windfall triggers Ascension for each card discarded"),
    ("Painful Quandary", "Rhystic Study", "opponents taxed on spells — pay life or cards, you draw"),
    # Card advantage
    ("Mystic Remora", "Baral, Chief of Compliance", "Baral keeps interaction cheap while Remora draws off opponents"),
    ("Trouble in Pairs", "Teferi, Time Raveler", "opponents can't flash in to avoid Trouble's trigger"),
    # Recursion / value
    ("Torrential Gearhulk", "Sublime Epiphany", "Gearhulk recasts Epiphany for counter + copy + draw"),
    ("Displacer Kitten", "Sol Ring", "Kitten blinks permanents on each noncreature spell"),
    # Stax
    ("Opposition Agent", "Vampiric Tutor", "Agent steals opponent searches while you tutor freely"),
    ("Orcish Bowmasters", "Windfall", "Bowmasters triggers for each card opponents draw"),
    ("Orcish Bowmasters", "Rhystic Study", "opponents either pay or you draw — Bowmasters punishes draw"),
]

# Scryfall supplement filters for Y'shtola
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "instant" in type_line.lower()
            and any(kw in oracle_text.lower() for kw in ["counter target", "exile target", "destroy target"])
        ),
        "reason": "instant-removal/counter",
    },
    {
        "check": lambda type_line, oracle_text: (
            any(kw in oracle_text.lower() for kw in ["each opponent loses", "deals damage to each opponent"])
        ),
        "reason": "group-drain",
    },
    {
        "check": lambda type_line, oracle_text: (
            "whenever you gain life" in oracle_text.lower()
            or "whenever you cast a noncreature" in oracle_text.lower()
        ),
        "reason": "lifegain/spellcast-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            "you draw" in oracle_text.lower()
            and any(kw in oracle_text.lower() for kw in ["each", "whenever", "beginning"])
        ),
        "reason": "repeatable-draw",
    },
]
