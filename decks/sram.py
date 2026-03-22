"""Sram, Senior Edificer — Mono-White equipment / auras / voltron."""

COMMANDER = "Sram, Senior Edificer"
EDHREC_SLUG = "sram-senior-edificer"
COLOR_IDENTITY = {"W"}

DECKLIST = [
    # Free/0-1 mana equipment (maximise Sram draw triggers)
    "Bone Saw", "Shuko", "Paradise Mantle", "Cathar's Shield", "Accorder's Shield",
    # Core equipment
    "Colossus Hammer", "Hammer of Nazahn", "Umezawa's Jitte",
    "Sword of Fire and Ice", "Sword of Feast and Famine",
    "Sword of the Animist", "Champion's Helm",
    "Argentum Armor",
    # Auras (also trigger Sram)
    "All That Glitters", "Ethereal Armor", "Daybreak Coronet",
    "Battle Mastery", "Hyena Umbra", "Flickering Ward",
    # Aura support
    "Helm of the Gods",
    # Equipment tutors and support
    "Sigarda's Aid", "Steelshaper's Gift", "Puresteel Paladin",
    "Stoneforge Mystic", "Stonehewer Giant", "Armored Skyhunter",
    "Leonin Shikari", "Auriok Steelshaper", "Kemba, Kha Regent",
    "Danitha Capashen, Paragon", "Magnetic Theft",
    # Aura tutors and draw
    "Kor Spiritdancer", "Mesa Enchantress", "Heliod's Pilgrim",
    "Sage's Reverie",
    # Voltron support creatures
    "Adorned Pouncer", "Esper Sentinel",
    # Aura recursion / mass recursion
    "Retether", "Open the Vaults",
    # Board wipe resilience / enchantment sweeper
    "Winds of Rath",
    # Protection
    "Mother of Runes", "Giver of Runes", "Teferi's Protection",
    # Removal
    "Swords to Plowshares", "Path to Exile", "Generous Gift",
    "Dispatch", "Unexpectedly Absent",
    # Ramp
    "Sol Ring", "Mana Crypt", "Pearl Medallion",
    "Arcane Signet",
    # Oswald artifact tutor (mono-white artifact toolbox)
    "Oswald Fiddlebender",
    # Lands
    "Command Tower", "Cavern of Souls", "Ancient Tomb",
    "Inventors' Fair", "Eiganjo, Seat of the Empire",
    "Flagstones of Trokair",
    "Plains", "Plains", "Plains", "Plains", "Plains",
    "Plains", "Plains", "Plains", "Plains", "Plains",
    "Plains", "Plains", "Plains", "Plains", "Plains",
    "Plains", "Plains", "Plains", "Plains", "Plains",
    "Plains", "Plains", "Plains", "Plains", "Plains",
    "Plains", "Plains", "Plains", "Plains",
]

DISTRACTORS = [
    "Paladin en-Vec",         # protection from colors but doesn't draw cards with Sram
    "Steelshaper Apprentice", # tutors equipment but costs 4 and doesn't synergize beyond that
    "Mana Tithe",             # counterspell effect in white — feels cute but slows the deck
    "Skull Shield",           # not in DB and irrelevant anyway
    "Sage's Reverie",         # already in deck — this represents cards that seem draw-happy
    "Konda's Banner",         # tribal banner, no tribal theme here
    "Loxodon Warhammer",      # decent equipment but not synergistic with the draw engine
    "Diviner's Wand",         # slow wizard equipment, Sram is a Dwarf
    "Masterwork of Ingenuity", # copies best equipment but enters tapped and creates a legendary copy problem
]

SYNERGY_PAIRS = [
    ("Sram, Senior Edificer", "Puresteel Paladin", "Paladin also draws on equipment cast — double draw engine"),
    ("Sram, Senior Edificer", "Kor Spiritdancer", "Spiritdancer draws on aura cast — redundant draw engine"),
    ("Sram, Senior Edificer", "Flickering Ward", "return and recast Ward each turn: Sram draws a card each time for free"),
    ("Sram, Senior Edificer", "Paradise Mantle", "0-cost equip: cast to draw, equip free with Metalcraft"),
    ("Sram, Senior Edificer", "Bone Saw", "0-cost equip: cast Bone Saw, draw with Sram, attack with commander"),
    ("Colossus Hammer", "Sigarda's Aid", "Aid lets you equip Hammer at flash speed for free the turn it enters"),
    ("Colossus Hammer", "Puresteel Paladin", "Paladin's metalcraft grants free equip — bypass Hammer's equip cost"),
    ("Colossus Hammer", "Hammer of Nazahn", "Nazahn auto-equips anything for free — attach Colossus Hammer immediately"),
    ("All That Glitters", "Ethereal Armor", "stacking enchantment-count auras — each other aura pumps both"),
    ("All That Glitters", "Daybreak Coronet", "Coronet grants first strike, vigilance; Glitters adds huge power"),
    ("Stonehewer Giant", "Argentum Armor", "Giant tutors and equips Armor directly — free destroy-a-permanent"),
    ("Kemba, Kha Regent", "Sword of Fire and Ice", "Kemba makes cats; Sword draws and protects; each attack is +3/+3 plus draw"),
    ("Winds of Rath", "Ethereal Armor", "Winds destroys unenchanted creatures — your aura'd commander survives"),
    ("Mesa Enchantress", "Flickering Ward", "Ward return loop draws cards with Enchantress just like Sram"),
    ("Armored Skyhunter", "Sword of Feast and Famine", "Skyhunter attacks, puts equipment from library onto battlefield"),
]

# Scryfall supplement filters for Sram
SUPPLEMENT_FILTERS = [
    {
        "check": lambda type_line, oracle_text: (
            "Equipment" in type_line and "equip" in oracle_text.lower()
        ),
        "reason": "equipment",
    },
    {
        "check": lambda type_line, oracle_text: (
            "Aura" in type_line and "enchant creature" in oracle_text.lower()
        ),
        "reason": "creature-aura",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"whenever.*equip|whenever.*equipment.*attach", oracle_text.lower()))
        ),
        "reason": "equip-trigger",
    },
    {
        "check": lambda type_line, oracle_text: (
            bool(__import__("re").search(r"equip {0}|equip—{0}|equip 0", oracle_text.lower()))
        ),
        "reason": "free-equip",
    },
]
