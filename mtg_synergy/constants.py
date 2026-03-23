"""Shared constants for the synergy graph system.

SEMANTIC_BRIDGES: Maps (provides_tag, wants_tag) → weight for cross-concept matching.
TRIGGER_EFFECT_BRIDGES: Maps effect_tags → trigger_tags for combo detection.
STAPLE_ROLES: Card roles that should never be cut from a deck.
"""

# Semantic bridges between provides and wants with different names.
# These capture relationships that can't be found by string matching.
SEMANTIC_BRIDGES = {
    # Counter placement provides what counter-placement-events wants
    ("counter-placement", "counter-placement-events"): 1.0,
    ("board-wide-counter-placement", "counter-placement-events"): 1.0,
    ("counter-distribution", "counter-placement-events"): 1.0,
    ("proliferate", "counter-placement-events"): 0.8,
    ("counter-amplification", "counter-amplification"): 1.0,

    # Token generation → creature ETB (tokens entering = creatures entering)
    ("token-generation", "creature-etb"): 0.8,
    ("token-doubling", "creature-etb"): 0.6,

    # Creature pump ↔ creature power
    ("creature-pump", "creature-power"): 0.8,
    ("board-wide-counter-placement", "creature-power"): 0.7,
    ("counter-placement", "creature-power"): 0.6,

    # Evasion/trample → combat & attack synergy
    ("trample-grant", "attack-events"): 0.5,
    ("evasion-grant", "attack-events"): 0.5,
    ("haste-grant", "attack-events"): 0.6,
    ("combat-enabler", "attack-events"): 0.7,
    ("combat-enabler", "combat-events"): 0.7,
    ("combat-trigger", "combat-events"): 0.8,
    ("combat-trigger", "attack-events"): 0.7,

    # Tribal connections
    ("human-tribal", "creature-type-selection"): 0.7,
    ("tribal-enabler", "creature-type-selection"): 0.8,
    ("creature-type-flexibility", "creature-type-selection"): 0.9,

    # Token generation → creature board & token events
    ("token-generation", "creature-board"): 0.6,
    ("token-generation", "token-events"): 0.8,
    ("token-doubling", "token-events"): 0.8,

    # Card draw → card draw events
    ("card-draw", "card-draw-events"): 0.8,
    ("top-of-library", "card-draw-events"): 0.5,

    # Removal ↔ targeted spells
    ("targeted-removal", "targeted-spells"): 0.6,
    ("artifact-enchantment-removal", "artifact-presence"): 0.4,

    # Protection synergies
    ("hexproof-grant", "creature-targeting"): 0.4,
    ("indestructible-grant", "creature-death"): 0.5,
    ("reactive-protection", "creature-targeting"): 0.4,
    ("board-protection", "creature-board"): 0.5,

    # Sacrifice payoff → sacrifice events
    ("sacrifice-payoff", "sacrifice-events"): 0.8,

    # Graveyard connections
    ("graveyard-recursion", "graveyard-filling"): 0.7,
    ("graveyard-hate", "graveyard-filling"): 0.4,

    # Mana acceleration → mana needs
    ("mana-acceleration", "mana-needs"): 0.8,
    ("mana-flexibility", "mana-needs"): 0.7,
    ("cost-reduction", "mana-needs"): 0.6,
    ("land-search", "land-density"): 0.9,

    # ETB payoff connections
    ("etb-payoff", "creature-etb"): 0.8,

    # Life gain
    ("life-gain", "life-gain-events"): 0.9,
    ("life-drain", "life-payment"): 0.5,

    # Trigger doubling wants triggered abilities
    ("trigger-doubling", "triggered-abilities"): 0.7,

    # Counter mover → counter placement events (moving = placing on new target)
    ("counter-mover", "counter-placement-events"): 0.7,

    # Graveyard recursion benefits from creature death
    ("graveyard-recursion", "creature-death"): 0.6,

    # Counter amplification provides counter-placement-events (amplified placement = placement)
    ("counter-amplification", "counter-placement-events"): 0.8,

    # Token generation → sacrifice events (tokens are fodder for sac outlets)
    ("token-generation", "sacrifice-events"): 0.8,
    ("token-generation", "creature-death"): 0.6,

    # Sacrifice payoff wants creature death / sacrifice events
    ("sacrifice-payoff", "creature-death"): 0.8,
    ("sacrifice-payoff", "sacrifice-events"): 0.9,
    ("sacrifice-payoff", "creature-etb"): 0.4,

    # Untap provides what tap-ability commanders need
    ("untap", "triggered-abilities"): 0.7,

    # Haste enables tap abilities
    ("haste-grant", "triggered-abilities"): 0.5,

    # Goblin tribal connections
    ("goblin-tribal", "goblin-tribal"): 1.0,
    ("token-generation", "goblin-tribal"): 0.5,
    ("cost-reduction", "goblin-tribal"): 0.5,
    ("tutor", "goblin-tribal"): 0.6,

    # ETB payoff wants creature ETB
    ("etb-payoff", "creature-board"): 0.5,
    ("etb-payoff", "token-events"): 0.6,

    # Damage dealing provides what creature board/tokens want
    ("damage-dealing", "creature-etb"): 0.4,

    # Sacrifice payoff → mana needs (sac outlets produce mana)
    ("sacrifice-payoff", "mana-needs"): 0.5,

    # Creature pump → attack events (pumped creatures want to attack)
    ("creature-pump", "attack-events"): 0.5,

    # Counter-related provides → counter-placement-events
    # (evasion/pump payoffs that care about counters)
    ("evasion-grant", "counter-placement-events"): 0.5,

    # Artifact/enchantment removal benefits from creature ETB (Aura Shards pattern)
    ("artifact-enchantment-removal", "creature-etb"): 0.4,

    # Counter placement → creature-board (placing counters means having creatures)
    ("counter-placement", "creature-board"): 0.4,
    ("board-wide-counter-placement", "creature-board"): 0.5,

    # ── Poison / Infect / Toxic / Proliferate bridges ──
    # Poison counters ARE counters — proliferate ticks them up
    ("poison-counter-placement", "counter-placement-events"): 0.9,
    ("infect", "counter-placement-events"): 0.8,
    ("toxic-ability", "counter-placement-events"): 0.8,
    ("toxic-1", "counter-placement-events"): 0.8,
    # Proliferate provides what poison-counter cards want
    ("proliferate", "poison-counter-placement"): 0.9,
    ("proliferate", "poison-counter-presence"): 0.9,
    ("proliferate", "poison-counter-synergy"): 0.9,
    ("proliferate", "poison-counter-proliferation"): 1.0,
    ("proliferate", "poison-counter-accumulation"): 0.9,
    ("proliferate", "poison-counter-payoff"): 0.8,
    ("proliferate", "opponent-poison-counters"): 0.9,
    ("proliferate", "opponent-poisoning"): 0.8,
    # Poison placement provides what proliferate cards want
    ("poison-counter-placement", "board-threats"): 0.5,
    # Infect provides what counter-synergy wants
    ("infect", "creature-board"): 0.4,

    # ── Untap / Mana loop bridges ──
    # Untap effects enable cards that tap for value (mana, abilities)
    ("untap", "tapped-creatures"): 0.8,
    ("untap", "mana-needs"): 0.6,
    ("untap", "spell-casting"): 0.5,
    # Mana acceleration enables tap-based combos
    ("mana-acceleration", "tapped-creatures"): 0.6,
    # Haste enables tap abilities immediately (Krenko + haste = combo)
    ("haste-grant", "tapped-creatures"): 0.7,
    # Spell copying enables spell-based combos
    ("spell-copying", "spell-casting"): 0.8,
    ("spell-copying", "instant-sorcery-casting"): 0.8,

    # ── Refined creature-board sub-tag bridges ──
    # These are SPECIFIC versions of creature-board, enabling precise connections

    # Sacrifice fodder: token generation provides disposable creatures
    ("token-generation", "sacrifice-fodder"): 0.9,
    ("graveyard-recursion", "sacrifice-fodder"): 0.7,
    ("creature-pump", "sacrifice-fodder"): 0.3,  # Low — pumped creatures aren't great sac targets

    # Death payoff: sacrifice outlets and removal cause deaths
    ("sacrifice-outlet", "creature-death-payoff"): 0.9,
    ("token-generation", "creature-death-payoff"): 0.7,  # Tokens to sacrifice
    ("spot-removal", "creature-death-payoff"): 0.4,

    # ETB payoff: anything that puts creatures onto the battlefield
    ("token-generation", "creature-etb-payoff"): 0.9,
    ("graveyard-recursion", "creature-etb-payoff"): 0.8,
    ("blink", "creature-etb-payoff"): 0.9,

    # Equipment target: token generation provides bodies to equip
    ("token-generation", "equip-target"): 0.6,
    ("haste-grant", "equip-target"): 0.5,

    # Power matters: counter placement and creature pump increase power
    ("counter-placement", "creature-power-matters"): 0.8,
    ("creature-pump", "creature-power-matters"): 0.9,
    ("board-wide-counter-placement", "creature-power-matters"): 0.8,

    # Creature count matters: token generation creates many creatures
    ("token-generation", "creature-count-matters"): 0.9,
    ("graveyard-recursion", "creature-count-matters"): 0.6,

    # Tap ability creatures: untap effects let them tap again
    ("untap", "tap-ability-creatures"): 0.9,
    ("haste-grant", "tap-ability-creatures"): 0.8,  # Haste lets them tap immediately
    ("vigilance-grant", "tap-ability-creatures"): 0.6,

    # Combat attackers: haste/evasion/pump help attackers
    ("haste-grant", "combat-attackers"): 0.8,
    ("evasion-grant", "combat-attackers"): 0.7,
    ("creature-pump", "combat-attackers"): 0.7,
    ("token-generation", "combat-attackers"): 0.6,  # More bodies to attack with

    # Creature type matters: changeling fits all types
    ("tribal-enabler", "creature-type-matters"): 0.8,
    ("creature-type-flexibility", "creature-type-matters"): 0.9,

    # Copy target: graveyard recursion provides targets
    ("graveyard-recursion", "creature-copy-target"): 0.6,
    ("token-generation", "creature-copy-target"): 0.5,

    # General creature-board bridges (kept but lower weight since generic)
    ("token-generation", "creature-board"): 0.5,
    ("graveyard-recursion", "creature-board"): 0.4,
    ("board-protection", "creature-board"): 0.4,
    ("combat-enabler", "combat-events"): 0.7,

    # ── Bidirectional combat/mana loops ──
    ("untap", "combat-events"): 0.5,           # Untap after combat enables re-use
    ("combat-enabler", "combat-attackers"): 0.8,
    ("creature-pump", "combat-attackers"): 0.7,  # Already have this, reinforce

    # ── Counter/ETB cycles ──
    ("counter-placement", "counter-placement-events"): 1.0,  # Already have, ensure both directions
    ("creature-etb-payoff", "token-generation"): 0.7,  # ETB payoff benefits from tokens entering

    # ── Graveyard filling ↔ recursion ──
    ("graveyard-filling", "graveyard-recursion"): 0.7,  # Fill enables recursion (NOT same as recursion→filling)

    # ── Equipment synergies ──
    ("equipment-synergy", "equip-target"): 0.8,
    ("equipment-synergy", "equipment-presence"): 0.8,  # Equipment cards want other equipment around
    ("creature-pump", "equip-target"): 0.5,
    ("untap", "equip-target"): 0.4,
    ("haste-grant", "equipment-presence"): 0.5,   # Haste equipment pairs with other equipment
    ("hexproof-grant", "equipment-presence"): 0.5, # Protection equipment pairs with other equipment
    ("protection-grant", "equipment-presence"): 0.5,
    ("ability-copying", "equip-target"): 0.6,     # Copy equipment → needs equip target
    ("ability-copying", "equipment-presence"): 0.7, # Copy effects want equipment to copy

    # ── Infect/grant synergies ──
    ("infect-grant", "damage-dealing"): 0.8,      # Infect enchantment on a damage-dealer = poison kills
    ("poison-counter-placement", "damage-dealing"): 0.6,

    # ── Triggered ability → ETB ──
    ("triggered-abilities", "creature-etb"): 0.4,  # Triggered abilities often involve ETB

    # ── Mana → spell casting ──
    ("mana-flexibility", "spell-casting"): 0.4,   # Mana enables casting spells
    ("mana-acceleration", "spell-casting"): 0.4,

    # ── Life gain loops ──
    ("life-gain", "life-gain-events"): 0.9,    # Already have but ensure
    ("life-drain", "life-gain"): 0.8,          # Draining opponents = gaining life (Exquisite Blood)

    # ── Spell synergies ──
    ("spell-cost-reduction", "spell-casting"): 0.7,
    ("card-draw", "spell-casting"): 0.5,       # Drawing gives more spells to cast

    # ── Mana/untap → specific needs ──
    ("mana-acceleration", "creature-etb"): 0.4,     # Mana enables casting creatures (ETB)
    ("mana-acceleration", "counter-placement-events"): 0.4,  # Mana enables counter-placing cards
    ("mana-acceleration", "equip-target"): 0.4,      # Mana to cast+equip
    ("mana-acceleration", "spell-casting"): 0.5,     # Mana enables spells
    ("mana-acceleration", "tap-ability-creatures"): 0.5,  # Mana rocks that tap = combo with untappers
    ("untap", "counter-placement-events"): 0.5,      # Untap counter-placing permanents
    ("untap", "creature-etb"): 0.4,                  # Untap bounce/blink sources
    ("mana-flexibility", "equip-target"): 0.4,

    # ── Counter → sacrifice/mana (Persist/Undying loops) ──
    ("counter-placement", "sacrifice-events"): 0.6,   # Counters reset persist/undying
    ("counter-placement", "mana-needs"): 0.4,         # Counter-based mana (Devoted Druid)

    # ── Creature type → ETB (Changeling/type matters) ──
    ("creature-type-flexibility", "creature-etb"): 0.5,
    ("tribal-enabler", "creature-etb"): 0.4,

    # ── Land synergies (land-density = wants many lands) ──
    ("land-search", "land-density"): 0.9,        # Tutoring lands
    ("land-animation", "land-density"): 0.7,     # Animated lands care about land count
    ("mana-acceleration", "land-density"): 0.3,  # Low — mana rocks aren't lands

    # ── Artifact synergies (artifact-presence = wants artifacts on board) ──
    ("artifact-enabler", "artifact-presence"): 0.8,
    ("treasure-generation", "artifact-presence"): 0.7,  # Treasures are artifacts
    ("mana-acceleration", "artifact-presence"): 0.5,    # Mana rocks are artifacts
    ("equipment-synergy", "artifact-presence"): 0.6,    # Equipment are artifacts

    # ── Protection / prevention → life gain events ──
    ("board-protection", "life-gain-events"): 0.3,  # Low — indirect connection
    ("damage-prevention", "life-gain-events"): 0.5,  # Preventing damage ≈ gaining life

    # ── Combat enabler → equip target ──
    ("combat-enabler", "equip-target"): 0.5,  # Combat enablers work well equipped

    # ── Targeting synergies ──
    ("targeting-bonus", "creature-targeting"): 0.8,

    # ── Redundancy synergies (cards doing same thing reinforce each other) ──
    ("etb-payoff", "creature-etb-payoff"): 0.7,     # Two ETB payoffs = twice the damage
    ("damage-dealing", "creature-etb-payoff"): 0.7,  # Damage dealers that trigger on ETB (Purphoros + Impact Tremors)
    ("life-drain", "creature-etb-payoff"): 0.6,      # Drain on ETB (Blood Artist pattern)
    ("creature-pump", "creature-etb-payoff"): 0.5,   # Pump + ETB payoff = growing board
    ("evasion-grant", "combat-events"): 0.6,         # Evasion enables combat (Blighted Agent + Inkmoth)
    ("infect", "combat-events"): 0.6,                # Infect creatures want combat
    ("indestructible-grant", "creature-etb-payoff"): 0.4, # Indestructible payoff stays on board

    # ── Tax / stax synergy ──
    ("stax-tax", "opponent-spell-casting"): 0.8,     # Tax effects compound (Rhystic + Quandary)
    ("reactive-protection", "opponent-spell-casting"): 0.6,  # Protection against opponent spells
    ("reactive-protection", "mana-needs"): 0.4,      # Protection enables safe big plays
    ("board-protection", "spell-casting"): 0.4,      # Protection enables safe big spells
    ("life-drain", "opponent-spell-casting"): 0.5,   # Drain punishes opponent casting

    # ── Wheel / draw triggers ──
    ("card-discard", "opponent-spell-casting"): 0.5,  # Wheel effects disrupt opponents
    ("card-draw", "opponent-spell-casting"): 0.3,     # Drawing more = having answers
    ("wheel", "card-draw-events"): 0.8,

    # ── Mill synergies ──
    ("self-mill", "mill-triggers"): 0.9,         # Mill triggers from mill effects
    ("card-discard", "graveyard-filling"): 0.7,  # Discarding fills graveyard
    ("card-mill", "graveyard-filling"): 0.9,     # Milling fills graveyard
    ("counter-amplification", "mill-triggers"): 0.7,  # Doubling mill (Bruvac)

    # ── Planeswalker synergies ──
    ("planeswalker-activation", "planeswalker-presence"): 0.9,
    ("untap", "planeswalker-presence"): 0.4,     # Untap can reset chain veil etc
    ("proliferate", "planeswalker-presence"): 0.8,  # Proliferate adds loyalty

    # ── Blink/bounce → spell cast ──
    ("blink", "spell-casting"): 0.5,             # Blinking is re-casting
    ("cost-reduction", "creature-etb-payoff"): 0.6,  # Cheaper creatures = more ETBs

    # ── Board wipe → death payoff ──
    ("board-wipe", "creature-death-payoff"): 0.7,  # Wipes cause mass death
    ("board-wipe", "sacrifice-events"): 0.5,

    # ── Graveyard recursion → mana ──
    ("graveyard-recursion", "mana-needs"): 0.4,  # Recurring cheap cards = mana efficiency

    # ── Life gain → artifact presence (artifact-based lifegain combos) ──
    ("life-gain", "artifact-presence"): 0.3,

    # ── Slow/game-ending → counter/death (combos with endgame pieces) ──
    ("slow-triggered-abilities", "counter-placement-events"): 0.5,
    ("slow-triggered-abilities", "creature-death"): 0.5,
    ("game-ending", "counter-placement-events"): 0.5,
    ("game-ending", "creature-death"): 0.5,

    # ── More interaction bridges ──
    ("targeted-removal", "life-gain-events"): 0.4,   # Removal triggers life-gain (Swords to Plowshares)
    ("targeted-removal", "spell-casting"): 0.4,       # Removal IS a spell
    ("board-wipe", "creature-death"): 0.7,            # Wipes cause mass death
    ("ability-copying", "creature-death"): 0.4,       # Copy death triggers
    ("ability-copying", "planeswalker-presence"): 0.5, # Copy planeswalker abilities
    ("spell-copying", "artifact-presence"): 0.4,
    ("graveyard-hate", "spell-casting"): 0.4,         # Graveyard hate as spell interaction
    ("spell-casting", "opponent-spell-casting"): 0.4,  # Casting triggers opponent-casting effects
    ("card-draw-payoff", "opponent-spell-casting"): 0.4,
    ("stax", "card-draw-events"): 0.4,                # Stax + draw = maintaining lock
    ("card-draw", "life-payment"): 0.4,               # Draw costs life (Necropotence pattern)
    ("life-drain", "creature-etb"): 0.4,              # Drain on ETB (Blood Artist)

    # ── Final push: 3-5 combo bridges ──
    ("mana-acceleration", "creature-death"): 0.3,     # Mana + death triggers (Ashnod's Altar patterns)
    ("evasion-grant", "mana-needs"): 0.3,             # Evasion creatures need mana to cast
    ("combat-enabler", "mana-needs"): 0.3,
    ("combat-enabler", "creature-etb"): 0.4,          # Combat enablers trigger ETB
    ("board-pressure", "equip-target"): 0.4,          # Pressure cards benefit from equipment
    ("board-protection", "creature-etb"): 0.4,        # Protected creatures keep triggering
    ("board-protection", "creature-type-selection"): 0.4,
    ("damage-prevention", "spell-casting"): 0.4,
    ("extra-turn", "artifact-presence"): 0.4,         # Extra turns with artifact combos
    ("blink", "triggered-abilities"): 0.6,            # Blink retriggers abilities
    ("evasion-grant", "triggered-abilities"): 0.4,    # Evasion enables combat triggers
    ("combat-trigger", "triggered-abilities"): 0.7,   # Combat triggers ARE triggered abilities
    ("graveyard-filling", "card-draw-events"): 0.4,   # Graveyard fill triggers draw effects
    ("targeted-removal", "life-payment"): 0.3,        # Removal that costs life
    ("self-mill", "creature-power"): 0.4,             # Mill fuels power-from-graveyard
    ("sacrifice-payoff", "targeted-spells"): 0.4,     # Sacrifice payoff on targeted spells

    # ── Equipment/Enchantment/Artifact interaction ──
    ("board-wipe", "equip-target"): 0.5,              # Wipe spares equipped/enchanted creatures
    ("board-wipe", "enchantment-presence"): 0.4,      # Wipe + enchantment protection (Winds of Rath)
    ("stax", "artifact-presence"): 0.6,               # Stax pieces ARE artifacts (Winter Orb)
    ("mana-flexibility", "artifact-casting"): 0.5,    # Mana sources enable artifact casting (Mox → Sai)
    ("mana-acceleration", "artifact-casting"): 0.5,
    ("tutor", "artifact-presence"): 0.5,              # Tutor finds artifacts (Whir → Winter Orb)
    ("tutor", "enchantment-presence"): 0.4,

    # ── Absolute final: 3-4 combo bridges ──
    ("top-of-library", "counter-placement-events"): 0.4,
    ("extra-turn", "artifact-presence"): 0.4,
    ("mana-flexibility", "tap-ability-creatures"): 0.5,
    ("creature-type-flexibility", "enchantment-presence"): 0.4,
    ("board-pressure", "creature-count-matters"): 0.4,
    ("passive-permanent", "legendary-presence"): 0.5,
    ("mana-acceleration", "creature-type-selection"): 0.3,
    ("targeted-removal", "mana-needs"): 0.3,
    ("mana-acceleration", "creature-targeting"): 0.3,
    ("untap", "permanent-color-diversity"): 0.3,

    # ── Deeper connections (5-8 combos each) ──
    ("cost-reduction", "etb-payoff"): 0.5,           # Cheaper spells = more ETB triggers
    ("extra-turn", "graveyard-filling"): 0.5,        # Extra turns = more mill/discard
    ("extra-turn", "enchantment-presence"): 0.4,     # Extra turns with enchantment synergy (Omniscience)
    ("graveyard-recursion", "low-life"): 0.5,        # Recursion from graveyard when at low life
    ("land-animation", "creature-death"): 0.6,       # Animated lands die = death trigger
    ("board-pressure", "creature-death"): 0.4,       # Pressure causes blocks/deaths
    ("damage-prevention", "creature-death"): 0.5,    # Prevention + sacrifice = safe deaths
    ("damage-prevention", "counter-placement-events"): 0.4,
    ("tutor", "spell-casting"): 0.4,                 # Tutoring for spells to cast
    ("tutor", "top-of-library"): 0.6,                # Tutor to top = topdeck synergy
    ("bounce-utility", "spell-casting"): 0.6,        # Bounce + recast = more spell casts
    ("bounce-utility", "creature-etb"): 0.7,         # Bounce + recast = ETB again
    ("creature-type-flexibility", "tapped-creatures"): 0.5, # Changeling taps for tribal abilities
    ("stax", "sacrifice-events"): 0.5,               # Stax pieces get sacrificed
    ("damage-dealing", "counter-placement-events"): 0.4,

    # ── Last batch: diminishing returns but still safe ──
    ("board-wipe", "creature-count-matters"): 0.5,  # Wipe resets counts, enables rebuild
    ("board-wipe", "graveyard-filling"): 0.6,        # Wipe fills graveyard
    ("board-wipe", "creature-etb"): 0.4,             # Wipe + recursion = re-ETB
    ("ability-copying", "artifact-presence"): 0.5,    # Copy artifact abilities
    ("ability-copying", "mana-needs"): 0.4,
    ("targeted-removal", "artifact-presence"): 0.4,   # Artifact removal = interaction
    ("targeted-removal", "enchantment-presence"): 0.4,
    ("cost-reduction", "creature-etb-payoff"): 0.6,   # Already added above but reinforce
    ("damage-dealing", "sacrifice-events"): 0.5,      # Damage + sacrifice patterns
    ("mana-flexibility", "permanent-recursion"): 0.4,  # Mana to fuel recursion
    ("mana-flexibility", "life-gain-events"): 0.3,
    ("evasion-grant", "life-gain-events"): 0.4,  # Evasion + lifelink = life gain

    # ── Extra turn / combat bridges ──
    ("extra-turn", "spell-casting"): 0.6,       # Extra turns let you cast more spells
    ("extra-turn", "combat-events"): 0.7,        # Extra turns = extra combat steps
    ("extra-turn", "card-draw-events"): 0.6,     # Extra turns = extra draw steps
    ("extra-turn", "creature-etb"): 0.5,         # Extra turns = more creatures played

    # ── Damage bridges ──
    ("damage-dealing", "combat-events"): 0.6,    # Damage dealers benefit from combat
    ("damage-dealing", "life-gain-events"): 0.7, # Damage to opponents = life loss events
    ("damage-dealing", "board-threats"): 0.5,    # Damage dealers ARE threats
    ("damage-dealing", "opponent-threats"): 0.5,

    # ── Graveyard / recursion bridges ──
    ("graveyard-recursion", "creature-etb"): 0.7,  # Recurring creatures trigger ETB
    ("self-mill", "graveyard-filling"): 0.9,     # Self-mill IS graveyard filling
    ("self-mill", "graveyard-events"): 0.8,

    # ── Copy / clone bridges ──
    ("ability-copying", "card-draw-events"): 0.6,  # Copy draw abilities = more draws
    ("ability-copying", "creature-etb"): 0.6,      # Copy ETB abilities
    ("spell-copying", "creature-etb"): 0.5,        # Copy creature spells = more ETBs
    ("flash-grant", "creature-etb"): 0.5,          # Flash enables surprise ETBs
    ("token-generation", "spell-casting"): 0.4,    # Token makers that trigger on cast

    # ── Spellslinger / Drain bridges ──

    # Life drain → life gain events (draining opponents = gaining life for payoffs)
    ("life-drain", "life-gain-events"): 0.9,
    ("life-gain", "life-gain-events"): 0.9,
    ("life-drain", "life-payment"): 0.5,

    # Card discard → discard events (Windfall → Bloodchief Ascension)
    ("card-discard", "discard-events"): 0.9,

    # Flash grant → spell casting (flash enables casting on every turn)
    ("flash-grant", "spell-casting"): 0.8,

    # Stax/tax → opponent spell casting (taxing opponents when they cast)
    ("stax-tax", "opponent-spell-casting"): 0.8,
    ("stax", "opponent-spell-casting"): 0.7,

    # Card draw payoff → card draw events
    ("card-draw-payoff", "card-draw-events"): 0.9,
    ("card-draw", "card-draw-events"): 0.8,

    # Graveyard casting → spell casting (casting from GY = casting spells)
    ("graveyard-casting", "spell-casting"): 0.7,

    # Blink → creature ETB (blinking = re-entering)
    ("blink", "creature-etb"): 0.8,

    # Board protection ↔ board threats
    ("board-protection", "board-threats"): 0.5,

    # Tutor → opponent search (Opposition Agent pattern — both care about searches)
    ("tutor", "opponent-spell-casting"): 0.3,

    # Cost reduction → spell casting (cheaper spells = more spells cast)
    ("cost-reduction", "spell-casting"): 0.6,
    ("cost-reduction", "opponent-spell-casting"): 0.3,

    # Card discard → card draw events (discard wheels trigger draw payoffs)
    ("card-discard", "card-draw-events"): 0.5,

    # Reactive protection → spell casting (protects key spells)
    ("reactive-protection", "spell-casting"): 0.3,

    # Life gain ↔ life gain events (same concept, different field)
    ("life-drain", "spell-casting"): 0.3,

    # Stax tax ↔ card draw events (Rhystic Study pattern)
    ("stax-tax", "card-draw-events"): 0.6,

    # Damage dealing ↔ token events (Impact Tremors + Krenko pattern)
    ("damage-dealing", "token-events"): 0.7,
    ("direct-damage", "token-events"): 0.6,
    ("group-damage", "token-events"): 0.6,

    # Token generation → sacrifice enablement
    ("token-generation", "sacrifice-outlet"): 0.6,

    # Sacrifice → graveyard filling + creature death
    ("sacrifice-outlet", "graveyard-filling"): 0.7,
    ("sacrifice-outlet", "creature-death"): 0.9,

    # Mana/untap → tap-combo (for commanders with tap abilities)
    ("untap", "tap-combo"): 0.9,
    ("mana-acceleration", "tap-combo"): 0.5,

    # Combat enabler → board-wide effects
    ("combat-enabler", "wide-board"): 0.6,
    ("haste-grant", "wide-board"): 0.5,

    # Board pump → attack events
    ("board-wide-pump", "attack-events"): 0.7,

    # Mill → graveyard filling
    ("mill", "graveyard-filling"): 0.8,
}


def _provides_satisfies_want(provide_tag: str, want_tag: str) -> float:
    """Check if a provides tag satisfies a wants tag. Returns weight 0.0-1.0.

    With normalized vocabulary, uses exact match + semantic bridges.
    Semantic bridges connect provides concepts to wants concepts that
    the vocabulary normalization can't capture (different word roots).
    """
    # Exact match (most common after normalization)
    if provide_tag == want_tag:
        return 1.0

    # Semantic bridges: provides → wants connections with different names
    # Each entry: (provides_tag, wants_tag) → weight
    pair = (provide_tag, want_tag)
    return SEMANTIC_BRIDGES.get(pair, 0.0)


# Maps effect tags to the trigger tags they would cause in-game.
# Used by find_combos_tiered to expand effect_tags before intersection,
# so that e.g. token-generation (effect) can match creature-etb (trigger).
TRIGGER_EFFECT_BRIDGES = {
    # Creating tokens/creatures triggers ETB
    "token-generation": {"creature-etb"},
    "graveyard-recursion": {"creature-etb"},
    "copy-effect": {"creature-etb"},

    # Removal/sacrifice causes death triggers
    "spot-removal": {"creature-death"},
    "sacrifice-outlet": {"creature-death"},
    "exile-removal": {"leaves-battlefield"},

    # Damage triggers life-loss which can trigger life-gain (Exquisite Blood pattern)
    "life-drain": {"life-gain"},   # Sanguine Bond: drain → opponent loses life → you gain
    "life-gain": {"life-drain"},   # Exquisite Blood: gain → opponent loses life

    # Direct damage can trigger damage events
    "direct-damage": {"combat-damage-events"},
    "group-damage": {"combat-damage-events"},

    # Card draw triggers draw events
    "card-draw": {"draw-events"},

    # Counter placement triggers counter events
    "counter-placement": {"counter-placement-events"},

    # Mana can enable untap loops
    "mana-acceleration": {"untap"},
    "untap": {"tap-cost"},

    # Discard triggers discard events
    "discard": {"discard-events"},

    # Mill triggers graveyard events
    "mill": {"graveyard-events"},

    # Creature pump with wide board triggers attack events
    "creature-pump": {"attack-events"},
}

STAPLE_ROLES = {"ramp", "draw", "removal", "protection", "land"}
