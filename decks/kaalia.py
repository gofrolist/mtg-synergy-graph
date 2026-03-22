"""Kaalia of the Vast — RWB Angels/Demons/Dragons Cheating."""

COMMANDER = "Kaalia of the Vast"
EDHREC_SLUG = "kaalia-of-the-vast"
COLOR_IDENTITY = {"R", "W", "B"}

DECKLIST = [
    # Angels
    "Aegis Angel", "Akroma, Angel of Wrath", "Angel of Despair",
    "Angel of Serenity", "Angelic Arbiter", "Aurelia, the Law Above",
    "Aurelia, the Warleader", "Avacyn, Angel of Hope",
    "Bruna, the Fading Light", "Drana and Linvala",
    "Giada, Font of Hope", "Gisela, Blade of Goldnight",
    "Gisela, the Broken Blade", "Herald of Eternal Dawn",
    "Liesa, Forgotten Archangel", "Lyra Dawnbringer",
    "Reya Dawnbringer", "Sephara, Sky's Blade", "Serra's Emissary",
    "Tariel, Reckoner of Souls",
    # Demons
    "Archfiend of Depravity", "Archfiend of Despair",
    "Bloodgift Demon", "Burning-Rune Demon", "Lord of the Void",
    "Master of Cruelties", "Ob Nixilis, Unshackled",
    "Rakdos, Patron of Chaos", "Razaketh, the Foulblooded",
    "Rune-Scarred Demon", "Sower of Discord",
    "Valgavoth, Terror Eater", "Vilis, Broker of Blood",
    # Dragons
    "Ancient Brass Dragon", "Ancient Copper Dragon",
    "Balefire Dragon", "Blast-Furnace Hellkite",
    "Bloodthirster", "Drakuseth, Maw of Flames",
    "Hellkite Tyrant", "Neriv, Heart of the Storm",
    "Terror of the Peaks", "Twinflame Tyrant",
    # Support
    "Isshin, Two Heavens as One", "Kaalia, Zenith Seeker",
    "Dragon Tempest", "Demonic Tutor", "Demonic Counsel",
    "Reanimate", "Phyrexian Arena",
    # Protection / interaction
    "Boros Charm", "Flawless Maneuver", "Grand Abolisher",
    "Mother of Runes", "Heroic Intervention",
    "Lightning Greaves", "Swiftfoot Boots", "Whispersilk Cloak",
    "Path to Exile", "Terminate",
    # Ramp
    "Boros Signet", "Orzhov Signet", "Rakdos Signet", "Chromatic Lantern",
    # Lands (non-basic)
    "Arena of Glory", "Arid Mesa", "Blood Crypt", "Bloodstained Mire",
    "Boros Garrison", "Cavern of Souls", "City of Brass",
    "Command Beacon", "Godless Shrine", "Hall of the Bandit Lord",
    "Marsh Flats", "Maze of Ith", "Orzhov Basilica",
    "Reflecting Pool", "Reliquary Tower", "Rogue's Passage",
    "Sacred Foundry", "Seraph Sanctuary", "Slayers' Stronghold",
    "Wind-Scarred Crag",
]

DISTRACTORS = [
    "Iona, Shield of Emeria", "Doom Whisperer", "Griselbrand",
    "Utvara Hellkite", "Kokusho, the Evening Star", "Emeria Shepherd",
    "Baneslayer Angel", "Restoration Angel", "Crux of Fate",
]

SYNERGY_PAIRS = [
    ("Kaalia of the Vast", "Avacyn, Angel of Hope", "Kaalia cheats in Avacyn → board is indestructible"),
    ("Kaalia of the Vast", "Master of Cruelties", "Kaalia puts Master in attacking → opponent goes to 1 life → Kaalia's combat damage kills"),
    ("Kaalia of the Vast", "Gisela, Blade of Goldnight", "Kaalia cheats in Gisela → double damage, halve incoming"),
    ("Kaalia of the Vast", "Lightning Greaves", "Greaves protects Kaalia + gives haste to attack immediately"),
    ("Aurelia, the Warleader", "Kaalia of the Vast", "Aurelia's extra combat → Kaalia triggers again → two free creatures"),
    ("Terror of the Peaks", "Kaalia of the Vast", "Kaalia cheats in big creatures → Terror deals their power as damage"),
    ("Isshin, Two Heavens as One", "Kaalia of the Vast", "Isshin doubles Kaalia's attack trigger → put two creatures in"),
    ("Rune-Scarred Demon", "Kaalia of the Vast", "Kaalia cheats in Demon → tutor any card on ETB"),
    ("Bruna, the Fading Light", "Gisela, the Broken Blade", "Meld into Brisela — game-ending 9/10 flying, first strike, vigilance, lifelink"),
    ("Reya Dawnbringer", "Razaketh, the Foulblooded", "Razaketh tutors, Reya reanimates — value engine"),
    ("Archfiend of Despair", "Gisela, Blade of Goldnight", "double opponent damage + halve yours = 4x effective damage ratio"),
    ("Dragon Tempest", "Kaalia of the Vast", "Kaalia cheats in flyers → Dragon Tempest deals damage per dragon/flyer"),
    ("Vilis, Broker of Blood", "Phyrexian Arena", "Arena deals 1 damage to you = draw 1 from Vilis + draw 1 from Arena = 2 cards/turn"),
    ("Master of Cruelties", "Rogue's Passage", "make Master unblockable = opponent goes to 1 life"),
    ("Razaketh, the Foulblooded", "Kaalia of the Vast", "Kaalia cheats in Razaketh for free = tutor anything by sacrificing tokens"),
]
