"""Writes catalogue/rule_test_set.json: labelled cases for measuring rule safety.

Each case is a destination's rules, optional earlier posts there, one new post and whether that post
breaks a rule. Labels come from the written rule definitions, case by case; they were not produced by
running the engine.

    python -m tools.make_rule_test_set
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "catalogue" / "rule_test_set.json"

NEUTRAL = {"title": "What we learned testing three caption workflows", "body": "We compared three ways to caption short videos and timed each one.", "promotional": None}


def c(title: str, body: str = "A body with enough words to be a real post.", **kw) -> dict:
    return {"title": title, "body": body, **kw}


def build() -> list[dict]:
    cases: list[dict] = []

    def add(cid: str, why: str, destination: dict, content: dict, violation: bool, prior: list[dict] | None = None):
        cases.append({"id": cid, "why": why, "destination": destination, "content": content,
                      "prior_posts": prior or [], "expect_violation": violation})

    # frequency caps
    for cap, n_prior, viol in [("1/7d", 1, True), ("1/7d", 0, False), ("2/7d", 1, False), ("2/7d", 2, True),
                               ("1/day", 1, True), ("3/30d", 2, False), ("3/30d", 3, True), ("1/week", 1, True)]:
        prior = [c(f"Earlier post number {i} about video editing", promotional=False) for i in range(n_prior)]
        add(f"freq_{cap.replace('/', '_')}_{n_prior}", f"cap {cap} with {n_prior} earlier post(s)",
            {"frequency_cap": cap}, {**NEUTRAL}, viol, prior)
    # self-promotion: explicit flags
    add("promo_flag_no", "promotional flag to a no-promotion community", {"self_promo_allowed": "no"},
        c("Our launch announcement this week", promotional=True), True)
    add("promo_flag_yes", "promotional flag where promotion is allowed", {"self_promo_allowed": "yes"},
        c("Our launch announcement this week", promotional=True), False)
    add("promo_neutral_no", "non-promotional post to a no-promotion community", {"self_promo_allowed": "no"},
        {**NEUTRAL, "promotional": False}, False)
    # self-promotion: detected from text
    phrases = ["Sign up today and get 30% off", "Start your free trial now", "Use code CREATOR20 at checkout",
               "Book a demo with our team", "Check out our new tool for editors", "Join our waitlist for early access",
               "Buy now while the discount lasts", "Upgrade now to unlock everything"]
    for i, ph in enumerate(phrases):
        add(f"promo_text_{i}", f"call to action '{ph}' to a no-promotion community", {"self_promo_allowed": "no"},
            c("A post about our video tool", f"{ph}. More inside."), True)
    add("promo_own_link_no", "link to incubrix.com in a no-promotion community", {"self_promo_allowed": "no"},
        c("A walkthrough of caption timing", "Details at https://incubrix.com/blog/captions"), True)
    add("promo_other_link_no", "link to a neutral site in a no-promotion community", {"self_promo_allowed": "no"},
        c("A walkthrough of caption timing", "Background reading: https://en.wikipedia.org/wiki/Closed_captioning"), False)
    add("promo_limited_first", "first promotional post where promotion is limited", {"self_promo_allowed": "limited"},
        c("Our launch announcement this week", promotional=True), False)
    add("promo_limited_second", "second promotional post inside the limited window", {"self_promo_allowed": "limited"},
        c("Another launch announcement here", promotional=True), True,
        [c("Our launch announcement last week", promotional=True)])
    # links
    add("links_none_with_link", "link where links are banned", {"link_policy": "none"},
        c("Five editing tips we use daily", "See https://example.org/tips for the list."), True)
    add("links_none_without", "no link where links are banned", {"link_policy": "none"}, {**NEUTRAL}, False)
    add("links_one_two", "two links where one is allowed", {"link_policy": "one_link"},
        c("Two resources on caption timing", "https://example.org/a and https://example.net/b"), True)
    add("links_one_one", "one link where one is allowed", {"link_policy": "one_link"},
        c("One resource on caption timing", "https://example.org/a"), False)
    for short in ["https://bit.ly/3xYz", "https://tinyurl.com/abc123", "https://t.co/xyz", "https://buff.ly/9q"]:
        add(f"links_short_{short.split('/')[2]}", f"shortener {short.split('/')[2]} where shorteners are banned",
            {"link_policy": "no_shorteners"}, c("A useful guide for editors here", f"Read it: {short}"), True)
    add("links_short_full_url", "full URL where shorteners are banned", {"link_policy": "no_shorteners"},
        c("A useful guide for editors here", "Read it: https://example.org/guide"), False)
    add("links_allowlist_bad", "link outside the allowlist", {"link_policy": "allowlist:github.com"},
        c("Our open-source caption aligner", "Code: https://gitlab.com/x/y"), True)
    add("links_allowlist_ok", "link inside the allowlist", {"link_policy": "allowlist:github.com"},
        c("Our open-source caption aligner", "Code: https://github.com/x/y"), False)
    add("links_link_field", "link given in the link field where links are banned", {"link_policy": "none"},
        c("Five editing tips we use daily", "No link in the text.", link="https://example.org/tips"), True)
    # AI disclosure (tailoring adds the line, so a disclosed post must pass)
    add("ai_disclosure_required", "AI-generated post where disclosure is required (tailoring adds the label)",
        {"disclosure_required": True}, c("Five AI editing tips", ai_generated=True), False)
    add("ai_disclosure_not_required", "AI-generated post where disclosure is not required",
        {"disclosure_required": False}, c("Five AI editing tips", ai_generated=True), False)
    # self-promotion: wording found by probing on 17 Oct (the first detector got the first four wrong)
    no_promo = {"self_promo_allowed": "no"}
    add("promo_subtle_launch", "launch post with no sales words: 'we just shipped', 'link in bio', own domain", no_promo,
        c("We just shipped v2 of our editor", "Would love feedback, link in bio. incubrix.com"), True)
    add("promo_affiliate", "affiliate link and commission", no_promo,
        c("My honest review of 5 caption tools", "I earn a commission if you use my link: https://amzn.to/x"), True)
    add("promo_negated_signup", "'no sign up needed' is not a call to action", no_promo,
        c("Free and open-source caption aligner", "No sign up needed, MIT licence, download from GitHub."), False)
    add("promo_discount_topic", "an essay about discounts is not an ad", no_promo,
        c("Why 50% off sales hurt small creators", "An analysis of discount culture."), False)
    add("promo_founder_story", "a founder's story with no pitch", no_promo,
        c("How I grew my SaaS to 1k users", "Lessons from year one, no pitch."), False)
    add("promo_discount_with_link", "discount words plus a link", no_promo,
        c("Big discount this month", "See https://example.com/deal"), True)
    # automation
    add("automation_banned", "native posting where automation is banned", {"automation_allowed": False}, {**NEUTRAL}, True)
    add("automation_allowed", "native posting where automation is allowed", {"automation_allowed": True}, {**NEUTRAL}, False)
    # language
    add("lang_strict_mismatch", "English post to a strict Tamil community",
        {"language": "ta", "config": {"language_strict": True}}, c("English title here please", language="en"), True)
    add("lang_strict_match", "Tamil post to a strict Tamil community",
        {"language": "ta", "config": {"language_strict": True}}, c("வீடியோ எடிட்டிங் குறிப்புகள் ஐந்து", "தமிழ் உரை இங்கே உள்ளது.", language="ta"), False)
    add("lang_loose_mismatch", "English post to a German community that accepts English (warning only)",
        {"language": "de", "config": {"accepted_languages": ["de", "en"]}}, c("English title here please", language="en"), False)
    add("lang_default_mismatch", "English post to a Japanese community with no translation",
        {"language": "ja"}, c("English title here please", language="en"), True)
    # combinations
    add("combo_cap_and_promo", "promotional post to a capped no-promotion community with an earlier post",
        {"frequency_cap": "1/7d", "self_promo_allowed": "no"}, c("Try our free trial today", promotional=True), True,
        [c("An earlier neutral post here", promotional=False)])
    add("combo_all_ok", "neutral post with one full link to a strict community",
        {"frequency_cap": "1/7d", "self_promo_allowed": "no", "link_policy": "one_link;no_shorteners"},
        c("How we time captions for short videos", "Method: https://example.org/method", promotional=False), False)
    return cases


def main() -> None:
    cases = build()
    OUT.write_text(json.dumps({"description": __doc__.strip().splitlines()[0], "cases": cases}, indent=2,
                              ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases ({sum(c['expect_violation'] for c in cases)} violations) to {OUT}")


if __name__ == "__main__":
    main()
