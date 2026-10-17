from threadroute.feedback import store as fb
from threadroute.feedback.report import build_report_data, build_report_markdown

RULE = "Never reroute suits by road during monsoon; use rail or wait for the road to reopen."


def test_learn_creates_rule_with_tags():
    res = fb.learn_guidance(RULE, "exceptions")
    assert res["created"]
    g = fb.list_guidance()[0]
    assert "suits" in g["tags"] and "monsoon" in g["tags"] and "road" in g["tags"]


def test_duplicate_rules_reinforce_instead_of_duplicating():
    fb.learn_guidance(RULE, "exceptions")
    again = fb.learn_guidance("Never reroute suits by road in the monsoon, use rail or wait for roads to reopen.", "exceptions")
    assert again.get("reinforced") and again["weight"] == 2.0
    assert len(fb.list_guidance()) == 1


def test_too_short_rule_is_ignored():
    assert fb.learn_guidance("ok", "global")["created"] is False


def test_retrieval_is_relevant_and_scoped():
    fb.learn_guidance(RULE, "exceptions")
    fb.learn_guidance("Always email the store manager before moving more than 30 units out of a store.", "allocation")
    hits = fb.retrieve(["exceptions"], "Should we reroute the suit shipment by road during the monsoon?")
    assert hits and "monsoon" in hits[0]["text"]
    assert all("store manager" not in h["text"] for h in hits)
    assert fb.retrieve(["returns"], "grade this jacket") == []
    alloc = fb.retrieve(["allocation"], "move 40 shirts out of the Hyderabad store")
    assert alloc and "store manager" in alloc[0]["text"]


def test_global_rules_apply_everywhere():
    fb.learn_guidance("Always quote values in INR and state the source of the data you used.", "global")
    assert fb.retrieve(["returns"], "any question about money values in INR")


def test_outcomes_adjust_weight_and_retire_bad_rules():
    gid = fb.learn_guidance(RULE, "exceptions")["id"]
    fb.record_outcome([gid], True)
    assert fb.list_guidance()[0]["weight"] == 1.2 and fb.list_guidance()[0]["helpful"] == 1
    for _ in range(3):
        fb.record_outcome([gid], False)
    assert fb.list_guidance() == []  # retired
    assert fb.list_guidance(active_only=False)[0]["unhelpful"] == 3


def test_mark_applied_counts_uses():
    gid = fb.learn_guidance(RULE, "exceptions")["id"]
    fb.mark_applied([gid, gid])
    assert fb.list_guidance()[0]["uses"] == 2


def test_directive_detection():
    assert fb.looks_directive("Always use Chennai first for Bengaluru")
    assert not fb.looks_directive("Good call, thanks")


def test_format_block_and_retire():
    gid = fb.learn_guidance(RULE, "exceptions")["id"]
    block = fb.format_guidance_block(fb.list_guidance())
    assert "LEARNED OPERATING GUIDANCE" in block and RULE in block
    assert fb.format_guidance_block([]) == ""
    assert fb.delete_guidance(gid) and fb.list_guidance() == []


def test_report_builds_with_and_without_data():
    assert "No guidance learned yet" in build_report_markdown()
    fb.learn_guidance(RULE, "exceptions")
    fb.record_feedback("thumb", 1, None, "s", "message", "m1", "tracking_agent")
    data = build_report_data()
    assert data["thumbs"]["up"] == 1 and len(data["guidance"]) == 1
    assert "Learned guidance" in build_report_markdown()
