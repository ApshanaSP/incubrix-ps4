# Rules pasted by a person

Some communities do not let bots read their rules page (for example Discourse forums: their robots.txt line
`Disallow: /g` also covers `/guidelines`), or render it with JavaScript. A person may still read it.

To add one: open the community's rules page in a browser, copy the rules text, and save it here as
`<name>.txt`, where `<name>` is the destination's name in lower case with every run of other characters turned
into one `-` (for example "OpenAI Developer Community" becomes `openai-developer-community.txt`). The first line
may say where it came from:

    source: https://community.openai.com/guidelines
    <the rules text>

Then rebuild: `python -m tools.build_real_catalogue`. The AI reads the pasted text like a fetched page, every
quote is checked against it, and the row is marked `rules_source=ai_from_pasted_rules`.
`TODO.csv` lists the relevant destinations that still have no verified rules, with the file name to use.
