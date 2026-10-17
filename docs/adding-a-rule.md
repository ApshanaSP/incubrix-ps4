# Adding a posting rule (test first)

All posting rules live in one function, `check()` in `app/rules.py`, and run before every post (and in
`validate`, the recommender and dry runs). A rule adds a violation that either **blocks** the post with a reason
or **reschedules** it to a time. Example: a community bans some words (for example 'crypto').

## 1. Write the test first (`tests/test_rules.py`)

```python
def test_banned_word_blocks(client):
    dest = make_destination(client, "lemmy", config={"banned_words": ["crypto"]})
    r = publish(client, [dest["id"]], content={"title": "Crypto tips for video creators", "body": "Short guide."})
    res = r["result"]["results"][0]
    assert res["state"] == "blocked" and any(v["rule"] == "banned_words" for v in res["violations"])
```

Run it and watch it fail: `python -m pytest tests/test_rules.py -k banned_word`

## 2. Add the rule in `check()` (`app/rules.py`)

Next to the other rules, before the final `return`:

```python
    # banned words
    text_lower = f"{rendition.title} {rendition.body}".lower()
    found = [w for w in destination.config.get("banned_words") or [] if re.search(rf"\b{re.escape(w.lower())}\b", text_lower)]
    if found:
        block("banned_words", "this community does not allow posts about: " + ", ".join(found))
```

`block(rule, message)` blocks with `DEST_RULE_VIOLATION`. For a time-based rule, add a violation with
`"action": "reschedule"` and a `reschedule_at` time, as the frequency cap does.

Rules see the post after tailoring (the rendition each destination really gets), so a rule about something
tailoring changes, such as hashtags on forums, must allow for that.

## 3. Add it to the rule test set

In `tools/make_rule_test_set.py`, add a case that should be blocked and one that should pass, then:

```bash
python -m tools.make_rule_test_set
python -m tools.measure            # rule_safety must stay "missed: 0, false_blocks: 0"
```

## 4. Run everything

```bash
python -m pytest
python -m tools.batch_runner --api http://localhost:8080 --input batch_inputs --out outputs
```

If the rule needs a new catalogue column, add it to the importer's validation (`app/catalogue/importer.py`) and to
the README's posting-rules table.
