# Complement rules reference

Catalogue of every complement rule in `src/mtg_synergy_graph/complement_rules/`
and the IDF / per-rule weighting applied in `universal_scorer.py`.

For the high-level scoring architecture, see [CLAUDE.md](../CLAUDE.md#scoring-architecture--universal-port-matcher).
For the rule-addition workflow, see [RULE_PLANNING.md](RULE_PLANNING.md).
For dated rule-by-rule audit history, see [RULE_HISTORY.md](RULE_HISTORY.md).

## Rule catalogue

Each rule wraps an existing mechanical map and declares when a
(commander_port, candidate_port) pair creates a synergy.

| Rule | Commander Port | Candidate Port | Source |
|------|---------------|---------------|--------|
| trigger_effect | trigger | effect | EVENT_MATCH_MAP |
| cost_feeds_trigger | trigger | cost | COST_FEEDS_TRIGGER |
| trigger_resonance | trigger | trigger | shared event axis |
| effect_resonance | effect | effect | resonant effects |
| replacement_resonance | effect | replacement | doubler matching |
| replacement_producer | replacement | effect | producer matching |
| replacement_blocks | trigger | replacement | anti-synergy |
| sacrifice_cluster | trigger | trigger | death-axis clustering |
| effect_feeds_trigger | effect | trigger | inverted EVENT_MATCH_MAP |
| lord | card subtypes | static | tribal lord matching |
| etb_self | trigger filter | card identity | ETB/dies self-matching |
| scaling | scales_with | card type | Aura/Equipment density |
| spell_density | SpellCast filter | card type | spell-type density |
| tribal_density | token subtypes (gated) | card subtype | tribal density (suppressed for Conspire) |
| zone_resonance | ChangesZone filter | ChangesZone trigger | landfall resonance |
| sacrifice_outlets | ChangesZone death | sacrifice cost | sac outlets for death cmdr |
| panharmonicon | Panharmonicon static | doubled trigger type | Yarok/Isshin/Teysa |
| graveyard_filler | GY-reanimate/cast | self-mill, discard | Meren/Karador/Kess |
| scales_with_density | scales_with port | density contributors | Phenax/Ezuri/Rakdos |
| extra_land_plays | AdjustLandPlays static | landfall triggers | Azusa |
| flicker_synergy | self-ETB trigger | exile+return effects | Gonti |
| cost_payoff | typed discard cost | graveyard return, Retrace/Dredge | Borborygmos |
| opponent_forcing | opp-facing trigger | opponent-targeting effects | Tergrid/Nekusar |
| token_producer | ChangesZone Creature trigger | Token effects | Purphoros |
| go_wide | Continuous creature pump | Token effects | Jetmir |
| voltron | Hexproof/Exalted keyword | Auras and Equipment | Sigarda/Rafiq |
| combat_enhancer | DamageDone trigger OR Attacks-Self trigger w/ engine effect (AddPhase/Dig/Play/Mana/Token/DealDamage/Discard/Mill, or 2+ value effects) | AddPhase + Double Strike | Saskia / Etali / Scourge of the Throne |
| wheel_synergy | Drawn trigger (opp-facing OR self+Token/Counter payoff) | Mode:Hand Discard + Draw | Nekusar, Locust God |
| monarch_synergy | BecomeMonarch effect | BecomeMonarch + CantAttackUnless pillowfort | Queen Marchesa |
| counter_target_payoff | scales_with XP + PutCounter P1P1 on Creature.Other | trigger CounterAdded / scales_with CardCounters.P1P1 | Ezuri |
| creature_untap_engine | cost=tap (plain) + effect=Mana | Untap Creature / unfiltered Untap | Selvala |
| populate_stack | CopyPermanent with Populate:True in raw | Other Populate CopyPermanent cards | Ghired |
| artifact_recursion | GY→BF Artifact effect | self-sac artifacts, ETB artifacts | Osgir/Daretti |
| copy_synergy | CopyPermanent effect | populate targets, ETB creatures | Ghired/Riku |
| panharmonicon (reverse) | cmdr subtype match | candidates doubling cmdr triggers | Kykar+Harmonic Prodigy |
| panharmonicon (stack) | cmdr Panharmonicon | other Panharmonicon statics | Yarok+Panharmonicon |
| evasion | DamageDone combat (non-tribal) | self-unblockable creatures | Saskia/Derevi |
| cheat_cmc | ChangeZone hand→BF with ChangeType | CMC-bucketed type matches (6+, 4-5) | Kaalia |
| cost_reduction_target | ReduceCost static | high-CMC creatures (6+) | Rakdos/Animar |
| toughness_synergy | scales_with CardToughness | Defender creatures | Phenax |
| cascade_value | Cascade keyword | high-CMC spells with valuable effects | Maelstrom Wanderer |
| subject_zone_feeder | ChangesZone BF→GY trigger w/ non-Creature subject (Land, Artifact, Enchantment) | effect=Sacrifice SacValid=<subject> + effect=ChangeZoneAll mass return from Graveyard. Scope-filtered (reject Defined=Opponent). | Titania (Land); any future subject-specific death-trigger commander |
| counter_axis_feeder | any cmdr port valid_filter contains `counters_GE_<TYPE>` on non-Self scope | counter_axis_payoff > counter_producer (self-sac-only cards dropped) > etb_counter_keyword > self_recur_keyword (P1P1 only) | Marchesa (trigger), Hamza (scales_with); any future counters_GE commander |
| modified_axis_feeder | any cmdr port carries the `modified` qualifier on a non-Self axis (rejecting `TargetsValid` / description clauses) | modified_p1p1_doubler > modified_p1p1_producer > modified_self_grower (creatures only) > modified_proliferate > modified_etb_keyword (etbCounter:P1P1 + Modular) | Kodama of the West Tree, Red XIII, SP//dr, Sephiroth, Chishiro, Goro-Goro, Silver Sable |
| damage_doubler_synergy | replacement.DamageDone with amp `replacement_result` (DmgTwice/DmgTriple/DmgPlus*) targeting opponent; rejects Prevent / self-target / DmgMinus | damage_amp_stack (other replacement doublers) > damage_pinger (non-combat repeating trigger + DealDamage opponent) | Torbran, Gisela, Solphim, Tor Wauki, Raphael, Wolverine, Neriv, Ojer Axonil, Absorbing Man and Titania |
| creatures_as_lands_landfall | static with Affected=Creature and AddType=Land | LandPlayed triggers + ChangesZone Land ETB triggers (landfall payoffs) | Ashaya, Soul of the Wild |
| cardpower_axis_feeder | `scales_with.CardPower` port (`SVar:X:Count$CardPower`) | cardpower_big_attachment (Equipment/Aura w/ static AddPower ≥ 3 or AddPower=X/Y/Z, ~220) > cardpower_p1p1_producer (PutCounter P1P1 on Creature target, self-sac-only excluded, ~400) | Combustion Man, Krenko (Tin Street Kingpin), Alesha, Carmen, Agatha, Inferno of the Star Mounts, Raubahn, Ian the Reckless |
| tap_type_feeder | `cost.tap_type` port (`tapXType<N/SUBJECT>`). Subject classified as creature / artifact / permanent via `_classify_tap_type_axis` | tap_type_sustained_untap (static.UntapOtherPlayer matching the axis, non-Self, ~10 cards per axis) > tap_type_phase_untap (trigger.Phase + effect.UntapAll matching the axis, ~10 cards) | Azami, Urza (Lord High Artificer), Aryel, Kumena, Lathril, Apothecary White, Baylen, Caparocti |
| hand_size_feeder | `scales_with ValidHand Card.YouOwn` port AND `_is_big_hand_commander` returns True (no `LE0`/`LE1`/`EQ0`/`GE2`/`GE3` SVarCompare on the hand-binding SVar) | hand_size_no_max (static.Continuous with `SetMaxHandSize: 'Unlimited'`, ~46 cards) | Alandra, Damia, Kefnet, Tishana, Soramaro, Kagemaro, Syr Elenora, Alrund, Jin-Gitaxias, Kozilek, Doctor Octopus, Duggan, Mr. Foxglove, Krang, Leonardo da Vinci |
| gy_fuel_feeder | `cost.exile_from_grave` port with `cost_target='any'` (excludes self-escape: Wilson, Symbiote Spider-Man, Tocasia, Venom, Morbius, Spider-Slayer, Beetle) | gy_fuel_self_mill (effect.Mill with `Defined: 'You'`, `NumCards >= 3` or scaling `X`/`Y`/`Z`, rejecting Opponent/EachPlayer, ~100 cards) | Araumi, Aphemia, Ashnod, Drivnod, Egon, Gorex, Ishkanah, Kethis, Osgir, Ultimecia, Varina, Winter |
| lifegain_feeder | `scales_with LifeYouGainedThisTurn` port. Axis is monotonic-positive (no bidirectional filter) | lifegain_amp (replacement.GainLife, `ValidPlayer: 'You'`, non-Prevent, amp ReplaceWith — `GainDouble`/`GainLife`/`ReplaceGain`, ~12 cards) > lifegain_etb_trigger (trigger.ChangesZone with Creature filter + Destination Battlefield + effect.GainLife, ~45 soul sisters) | Celestine, Aerith, Astarion, Bre of Clan Stoutarm, Haliya, Hope Estheim, Frodo, Gollum, Gwaihir, Lathiel, Licia, Saint Elenda, Sorin of House Markov, Willowdusk |
| life_total_feeder | `scales_with YourLifeTotal` port AND up-biased lifegain signal: `replacement.GainLife` amp on self OR `static.Continuous` raw_line with `SVarCompare` starting `GT`/`GE`. Narrow gate excludes query-variable cmdrs (Ayli / Bane / Beza / Cecil / Jerren / Linvala — life used as exile-power cap / indestructible threshold / flip condition) | life_total_peer (other cards with `scales_with.YourLifeTotal` that ALSO have Lifelink / effect.GainLife on `You` / replacement.GainLife, symmetric positive-bias filter, ~27 cards: Angel of Vitality, Blood Baron of Vizkopa, Divinity of Pride, Honor Troll, Path of Bravery, Righteous Valkyrie, Serra Ascendant, Sigarda's Splendor, Speaker of the Heavens, Leyline of Hope) | Bilbo, Birthday Celebrant; Elenda, Saint of Dusk |
| land_bounce_feeder | `cost.return` port with `cost_subtype` `<N>/Land*` AND `cost_target='any'`. Excludes cmdrs with `scales_with.ValidHand` (Soramaro — big-hand payoff is primary axis) or `scales_with.xPaid` (Tameshi — X-cost flicker is primary axis) | land_bounce_extra_drops (static.Continuous w/ AdjustLandPlays — Azusa, Exploration, Oracle of Mul Daya, Dryad of the Ilysian Grove, Fastbond, ~38 cards) > land_bounce_gy_recur (effect.ChangeZone Graveyard-origin with Land filter, non-opponent — Crucible of Worlds, Ramunap Excavator, Splendid Reclamation, Life from the Loam, Lord Windgrace, ~56 cards) | Meloku the Clouded Mirror, Mina and Denn Wildborn, Multani Yavimaya's Avatar, Sutina Speaker of the Tajuru, Uyo Silent Prophet |
| etb_tapped_stax_feeder | `replacement.Moved` port w/ `replacement_result='ETBTapped'` whose `valid_filter` does NOT contain `Card.Self` (external stax, not a self-ETBTapped drawback). Excludes Grimgrin / Ebondeath / Alirios / Taeko (self-flavor covered by sacrifice_outlets / graveyard_filler / etb_self) | etb_tapped_stax_peer (~24 cards with the same mechanical shape: Authority of the Consuls, Kismet, Blind Obedience, Loxodon Gatekeeper, Kinjalli's Sunwing, Imposing Sovereign, Manglehorn, Dauntless Dismantler, Archon of Emeria, Phyrexian Censor, Frozen Aether, Orb of Dreams, Root Maze, False Floor, Radiant Grace, Ashling's Prerogative) | Reidane, Spider-Woman, Thalia and The Gitrog Monster, Thalia Heretic Cathar, Urabrask, Zhao, Archelos |
| party_feeder | `scales_with.Party` port — `Count$Party` returns 1-4 = distinct count of Cleric/Rogue/Warrior/Wizard creatures you control (Zendikar Rising / Baldur's Gate / Final Fantasy Party mechanic) | party_peer (~34 cards with identical `scales_with.Party`: Acquisitions Expert, Allied Assault, Archpriest of Iona, Ardent Electromancer, Cascade Seer, Coveted Prize, Deadly Alliance, Drana's Silencer, Emeria Captain, Grotag Bug-Catcher, Journey to Oblivion, Kabira Outrider, Malakir Blood-Priest, Multiclass Baldric, Nimble Trapfinder, Ravager's Mace, Sea Gate Colossus, Seafloor Stalker, Spoils of Adventure, Squad Commander, Synchronized Spellcraft, Thundering Sparkmage, Thwart the Grave, Veteran Adventurer) | Burakos, Linvala Shield of Sea Gate, Nalia de'Arnise, Tazri Beacon of Unity, The Destined Black Mage / Thief / Warrior / White Mage, Zagras |
| creature_died_feeder | `scales_with` port whose `event_class` starts with `ThisTurnEntered_Graveyard_from_Battlefield_Creature` (covers all Forge filter variants: plain / `.YouCtrl` / `.YouOwn` / `.!token` / `.!namedSelf` / `.!token+YouCtrl`). Aristocrats-archetype scaler | creature_died_peer (~49 non-legendary cards with the same prefix: Feast of the Victorious Dead, Fresh Meat, Caller of the Claw, Deathreap Ritual, Grizzly Ghoul, Khabál Ghoul, Tallyman of Nurgle, Liliana's Devotee / Scrounger / Standard Bearer, Warlock Class, Ichor Shade, Rise of the Dread Marn, Osai Vultures, Vile Redeemer, Spoils of Blood, Body Count, Spymaster's Vault, Séance Board, Season of Loss) | Asmira Holy Avenger, Bontu the Glorified, Denethor Ruling Steward, Ebondeath Dracolich, Faramir Field Commander, Gadrak Crown-Scourge, Gimli Mournful Avenger, Inga Rune-Eyes, Kuon Ogre Ascendant, Lagomos, Mahadi Emporium Master, Nevinyrral Urborg Tyrant, Shessra Death's Whisper, Sméagol, Tobias |

## IDF weighting (`universal_scorer.py`)

Each complement's value = `1 / log2(1 + N)` where N = number of candidates
sharing the same match tuple. Specific matches (Saproling lord: N=3, IDF≈0.50)
worth more than broad matches (sacrifice cost: N=2000, IDF≈0.09).

Density rules use flat weights to prevent IDF from penalizing broad-but-correct
matching: spell_density 0.3, scaling 0.3, tribal_density 0.5, etb_self 0.01,
token_producer 0.25, evasion 0.15, pan_density 0.10.

Multi-rule bonus: cards matching 3+ distinct rule_ids get +0.1 per extra rule
(rewards multi-axis synergy picks like Pitiless Plunderer).
