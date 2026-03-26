import sqlite3
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_spellbook_confirmed_combo(tmp_db):
    """Deck containing all cards from a Spellbook combo should be labeled infinite-confirmed."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Two cards that form a Spellbook combo
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-a', 'Card A')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-b', 'Card B')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-a', 'tag-x')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-b', 'tag-x')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-b', 'tag-y')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-a', 'tag-y')")

    # Spellbook entry
    conn.execute("""INSERT INTO spellbook_combos (combo_id, card_oracle_ids, card_names, result, prerequisites, card_count)
                    VALUES ('combo-1', '["oid-a","oid-b"]', '["Card A","Card B"]', 'Infinite damage', '', 2)""")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-a')")
    conn.execute("INSERT INTO spellbook_combo_cards (combo_id, oracle_id) VALUES ('combo-1', 'oid-b')")
    conn.commit()
    conn.close()

    deck_oids = {"oid-a", "oid-b"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    confirmed = [c for c in combos if c["tier"] == "infinite-confirmed"]
    assert len(confirmed) >= 1
    assert "Infinite damage" in confirmed[0]["result"]


def test_trigger_chain_combo_likely(tmp_db):
    """Cards with circular trigger chains should be labeled combo-likely."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Sanguine Bond + Exquisite Blood pattern:
    # Card C: "Whenever you gain life, target opponent loses that much life"
    #   trigger_tags: ["life-gain"], effect_tags: ["life-drain"]
    # Card D: "Whenever an opponent loses life, you gain that much life"
    #   trigger_tags: ["life-drain"], effect_tags: ["life-gain"]
    # Chain: C triggers on life-gain -> drains life -> D triggers on life-drain -> gains life -> C triggers again

    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-c', 'Sanguine Bond')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-c', 'life-drain')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-c', 'life-gain')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-c', 0, 'triggered', 'Whenever you gain life',
                    '["life-gain"]', 'target opponent loses that much life', '["life-drain"]')""")

    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-d', 'Exquisite Blood')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-d', 'life-gain')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-d', 'life-drain')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-d', 0, 'triggered', 'Whenever an opponent loses life',
                    '["life-drain"]', 'you gain that much life', '["life-gain"]')""")

    conn.commit()
    conn.close()

    deck_oids = {"oid-c", "oid-d"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    likely = [c for c in combos if c["tier"] == "combo-likely"]
    # C's effect_tags {life-drain} ∩ D's trigger_tags {life-drain} = {life-drain} ✓
    # D's effect_tags {life-gain} ∩ C's trigger_tags {life-gain} = {life-gain} ✓
    assert len(likely) == 1


def test_synergy_tier_fallback(tmp_db):
    """Pairs with provides->wants cycle are detected as combo-likely or synergy tier."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-e', 'Card E')")
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-f', 'Card F')")
    # One-directional provides->wants: E provides what F wants, but not vice versa.
    # This means a provides cycle is absent so the pair is NOT detected at all.
    # For a synergy-tier result we need a bidirectional cycle with non-bridge tags.
    # Since provides/wants are now used as effect_tags/trigger_tags respectively,
    # a bidirectional cycle (E provides tag-m / F wants tag-m AND F provides tag-n /
    # E wants tag-n) will produce a combo-likely result (tags cross-match).
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-e', 'tag-m')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-f', 'tag-m')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-f', 'tag-n')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-e', 'tag-n')")
    # No spellbook entry
    conn.commit()
    conn.close()

    deck_oids = {"oid-e", "oid-f"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    # With provides/wants as ability source, a bidirectional cycle resolves to combo-likely.
    detected = [c for c in combos if c["tier"] in ("synergy", "combo-likely")]
    assert len(detected) >= 1


def test_trigger_chain_with_bridges(tmp_db):
    """Token creation should bridge to enters-battlefield for trigger chain detection."""
    from synergy_graph import find_combos_tiered

    conn = sqlite3.connect(tmp_db)
    # Card A: triggers on dies, creates tokens.
    # Provides token and wants sacrifice-outlet (direct cycle with card B).
    # Trigger chain: effect_tag "token" bridges → enters-battlefield (card B's trigger_tag).
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-tokener', 'Token Death')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-tokener', 'token')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-tokener', 'sacrifice-outlet')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-tokener', 0, 'triggered', 'Whenever a creature dies',
                    '["dies"]', 'create a 1/1 token', '["token"]')""")

    # Card B: triggers on enters-battlefield, sacrifices a creature.
    # Provides sacrifice-outlet and wants token (direct cycle with card A).
    # Trigger chain: effect_tag "sacrifice-outlet" bridges → dies (card A's trigger_tag).
    conn.execute("INSERT INTO cards (oracle_id, name) VALUES ('oid-saccer', 'ETB Sac')")
    conn.execute("INSERT INTO provides (oracle_id, tag) VALUES ('oid-saccer', 'sacrifice-outlet')")
    conn.execute("INSERT INTO wants (oracle_id, tag) VALUES ('oid-saccer', 'token')")
    conn.execute("""INSERT INTO abilities (oracle_id, ability_index, ability_type, trigger_condition,
                    trigger_tags, effect, effect_tags)
                    VALUES ('oid-saccer', 0, 'triggered', 'Whenever a creature enters',
                    '["enters-battlefield"]', 'sacrifice a creature', '["sacrifice-outlet"]')""")

    conn.commit()
    conn.close()

    deck_oids = {"oid-tokener", "oid-saccer"}
    combos = find_combos_tiered(deck_oids, tmp_db)

    likely = [c for c in combos if c["tier"] == "combo-likely"]
    # token bridges to enters-battlefield, sacrifice-outlet bridges to dies
    assert len(likely) >= 1, f"Expected combo-likely, got: {[c['tier'] for c in combos]}"
