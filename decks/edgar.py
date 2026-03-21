"""Edgar Markov — RWB (Mardu) Vampires / Aristocrats / Lifedrain."""

COMMANDER = "Edgar Markov"
EDHREC_SLUG = "edgar-markov"
COLOR_IDENTITY = {"R", "W", "B"}

DECKLIST = [
    # Vampire lords / anthems
    "Captivating Vampire", "Legion Lieutenant", "Stromkirk Captain",
    "Drana, Liberator of Malakir", "Rakish Heir", "Vampire Socialite",
    "Shared Animosity", "Stensia Masquerade", "Vanquisher's Banner",
    "Patchwork Banner",
    # Aristocrats / sacrifice
    "Blood Artist", "Cordial Vampire", "Cruel Celebrant", "Viscera Seer",
    "Yahenni, Undying Partisan", "Bartolomé del Presidio",
    "Elenda, the Dusk Rose", "Indulgent Aristocrat",
    "Butcher of Malakir", "Village Rites",
    # Lifedrain
    "Exquisite Blood", "Sanguine Bond", "Vito, Thorn of the Dusk Rose",
    "Marauding Blight-Priest", "Sanctum Seeker", "Bloodletter of Aclazotz",
    "Twilight Prophet", "Malakir Bloodwitch",
    # Vampire value
    "Champion of Dusk", "Welcoming Vampire", "Dusk Legion Zealot",
    "Forerunner of the Legion", "Mavren Fein, Dusk Apostle",
    "Charismatic Conqueror", "Preacher of the Schism",
    "Elenda's Hierophant", "Crossway Troublemakers",
    "Oathsworn Vampire", "Florian, Voldaren Scion",
    "Baron Bertram Graywater", "Markov Baron",
    # Vampire threats
    "Anowon, the Ruin Sage", "Knight of the Ebon Legion",
    "Olivia Voldaren", "Olivia, Crimson Bride",
    "Patron of the Vein", "Sorin, Imperious Bloodlord",
    "Bloodthirsty Conqueror", "Falkenrath Pit Fighter",
    "Vampire Nighthawk", "Vampire of the Dire Moon",
    "Vampire Gourmand", "Vengeful Bloodwitch",
    # Interaction
    "New Blood", "Olivia's Wrath", "Pact of the Serpent",
    "Ruthless Lawbringer", "Master of Dark Rites",
    # Equipment / support
    "Blade of the Bloodchief", "Herald's Horn", "Skullclamp",
    # Ramp
    "Dark Ritual", "Phyrexian Tower",
    # Lands (non-basic)
    "Accursed Duneyard", "Blazemire Verge", "Blood Crypt", "Bloodfell Caves",
    "Bojuka Bog", "Cabal Coffers", "Castle Locthwain", "Cavern of Souls",
    "Haunted Ridge", "Orzhov Basilica", "Path of Ancestry",
    "Scoured Barrens", "Secluded Courtyard", "Shattered Sanctum",
    "Tainted Field", "Three Tree City", "Unclaimed Territory",
    "Urborg, Tomb of Yawgmoth", "Vault of the Archangel", "Voldaren Estate",
]

DISTRACTORS = [
    "Anointed Procession", "Bolas's Citadel", "Exsanguinate",
    "Torment of Hailfire", "Coat of Arms", "Door of Destinies",
    "Bloodline Keeper", "Kalitas, Traitor of Ghet", "Sorin, Vengeful Bloodlord",
]

SYNERGY_PAIRS = [
    ("Edgar Markov", "Legion Lieutenant", "Edgar makes vampire tokens, Lieutenant pumps all vampires"),
    ("Edgar Markov", "Shared Animosity", "Edgar floods board, Animosity makes them lethal"),
    ("Exquisite Blood", "Sanguine Bond", "infinite combo: opponent loses life → you gain → they lose more"),
    ("Exquisite Blood", "Vito, Thorn of the Dusk Rose", "same infinite loop as Sanguine Bond"),
    ("Blood Artist", "Viscera Seer", "sac outlet + drain trigger on each death"),
    ("Blood Artist", "Elenda, the Dusk Rose", "Elenda grows from deaths, Artist drains"),
    ("Cordial Vampire", "Viscera Seer", "sac triggers Cordial's +1/+1 counter on all vampires"),
    ("Skullclamp", "Edgar Markov", "clamp the 1/1 vampire tokens for card draw"),
    ("Sanctum Seeker", "Shared Animosity", "wide vampire board = massive drain + damage"),
    ("Blade of the Bloodchief", "Viscera Seer", "each sacrifice puts 2 counters on equipped creature"),
    ("Captivating Vampire", "Edgar Markov", "Edgar makes tokens to fuel Captivating's steal ability"),
    ("Welcoming Vampire", "Edgar Markov", "each vampire cast triggers Edgar token + Welcoming draw"),
    ("Twilight Prophet", "Drana, Liberator of Malakir", "both benefit from vampire board presence"),
    ("Cruel Celebrant", "Yahenni, Undying Partisan", "Yahenni sacs, Celebrant drains"),
    ("Mavren Fein, Dusk Apostle", "Shared Animosity", "Mavren makes tokens on attack, Animosity pumps"),
]
