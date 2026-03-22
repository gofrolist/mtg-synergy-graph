"""Pantlaza, Sun-Favored — RGW Dinosaur Tribal / Discover."""

COMMANDER = "Pantlaza, Sun-Favored"
EDHREC_SLUG = "pantlaza-sun-favored"
COLOR_IDENTITY = {"R", "G", "W"}

DECKLIST = [
    # Dinosaurs
    "Apex Altisaur", "Bonehoard Dracosaur", "Bronzebeak Foragers",
    "Curious Altisaur", "Earthshaker Dreadmaw", "Etali, Primal Storm",
    "Ghalta and Mavren", "Ghalta, Primal Hunger", "Ghalta, Stampede Tyrant",
    "Gishath, Sun's Avatar", "Hulking Raptor", "Hunting Velociraptor",
    "Itzquinth, Firstborn of Gishath", "Kinjalli's Sunwing",
    "Kogla and Yidaro", "Marauding Raptor", "Palani's Hatcher",
    "Quartzwood Crasher", "Rampaging Brontodon", "Ranging Raptors",
    "Ravenous Tyrannosaurus", "Regal Behemoth", "Regisaur Alpha",
    "Ripjaw Raptor", "Runic Armasaur", "Scion of Calamity",
    "Sunfrill Imitator", "Temple Altisaur", "Thrashing Brontodon",
    "Topiary Stomper", "Trumpeting Carnosaur", "Vaultborn Tyrant",
    "Verdant Sun's Avatar", "Wakening Sun's Avatar",
    "Wayward Swordtooth", "Wrathful Raptors",
    "Zetalpa, Primal Dawn",
    # Dinosaur support
    "Atzocan Seer", "Drover of the Mighty", "Intrepid Paleontologist",
    "Ixalli's Lorekeeper", "Otepec Huntmaster",
    "Dinosaurs on a Spaceship", "From the Rubble",
    "Progenitor's Icon", "Savage Order",
    "Descendants' Path", "Urza's Incubator",
    # Power/value
    "Xenagos, God of Revels", "The Skullspore Nexus",
    # Spells
    "Akroma's Will", "Chandra's Ignition", "Ephemerate",
    "Garruk's Uprising", "Return of the Wildspeaker",
    "Rishkar's Expertise", "Rhythm of the Wild",
    # Ramp
    "Cultivate", "Farseek", "Migration Path",
    "Rampant Growth", "Thunderherd Migration",
    # Lands (non-basic)
    "Arch of Orazca", "Canopy Vista", "Cavern of Souls",
    "Cinder Glade", "Clifftop Retreat", "Exotic Orchard",
    "Fortified Village", "Furycalm Snarl", "Game Trail",
    "Jungle Shrine", "Kessig Wolf Run", "Mosswort Bridge",
    "Myriad Landscape", "Path of Ancestry", "Rogue's Passage",
    "Secluded Courtyard", "Temple of the False God",
    "Thriving Bluff", "Thriving Grove", "Thriving Heath",
    "Unclaimed Territory",
]

DISTRACTORS = [
    "Zacama, Primal Calamity", "Polyraptor", "Silverclad Ferocidons",
    "Carnage Tyrant", "Gigantosaurus", "Commune with Dinosaurs",
    "Huatli, Warrior Poet", "Overwhelming Stampede", "Craterhoof Behemoth",
]

SYNERGY_PAIRS = [
    ("Pantlaza, Sun-Favored", "Gishath, Sun's Avatar", "both discover/reveal dinosaurs on attack — massive value"),
    ("Pantlaza, Sun-Favored", "Regisaur Alpha", "Alpha gives haste + creates 3/3 token, Pantlaza discovers 7"),
    ("Gishath, Sun's Avatar", "Xenagos, God of Revels", "Xenagos doubles Gishath's power = reveal twice as many dinos"),
    ("Marauding Raptor", "Polyraptor", "Raptor deals 2 to each creature entering → Polyraptor infinite tokens"),
    ("Etali, Primal Storm", "Rhythm of the Wild", "Etali with haste attacks immediately → free spells from opponents"),
    ("Ghalta, Stampede Tyrant", "Terror of the Peaks", "Ghalta dumps hand of dinos onto battlefield → Terror deals massive damage"),
    ("Ripjaw Raptor", "Marauding Raptor", "Marauding deals 2 to Ripjaw on ETB → draw a card"),
    ("Garruk's Uprising", "Pantlaza, Sun-Favored", "every dinosaur ETB draws a card + trample"),
    ("Chandra's Ignition", "Ghalta, Primal Hunger", "deal 12 damage to all opponents and their creatures"),
    ("Wrathful Raptors", "Marauding Raptor", "damage to your dinos triggers Wrathful → damage to opponents"),
    ("Quartzwood Crasher", "Xenagos, God of Revels", "doubled trample creature creates massive token"),
    ("Descendants' Path", "Pantlaza, Sun-Favored", "free dinosaur every upkeep + Pantlaza discovers"),
    ("Rishkar's Expertise", "Ghalta, Primal Hunger", "draw 12 cards + cast 5 CMC free"),
    ("Temple Altisaur", "Marauding Raptor", "Altisaur prevents Raptor from killing small dinos"),
    ("Regal Behemoth", "Pantlaza, Sun-Favored", "monarch draws cards + double mana = cast more dinos = more discovers"),
]
