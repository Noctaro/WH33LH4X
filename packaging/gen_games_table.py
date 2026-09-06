r"""
gen_games_table.py: rewrite GAMES.md's "Status at a glance" table from games.json.

WHY THIS EXISTS
---------------
The same three facts, what works, whether setup is needed and when it was last checked, were
about to live in two places: GAMES.md for people, games.json for the GUI. Two copies of a fact
drift, and this project has already been bitten by that more than once: a breakaway figure that
was wrong in GAMES.md, a stiction constant that was never measured, a memory entry describing a
fix that does not exist in the tree.

So the direction is DATA -> PROSE, never the reverse. games.json is the source; this rewrites
one table between two markers and touches nothing else. The long-form sections stay
hand-written, because prose is what they are for.

Parsing GAMES.md to build the JSON would be the other direction, and it means writing a
Markdown parser that breaks the first time somebody reformats a table.

Usage, from the repo root:
    .\.venv\Scripts\python.exe packaging\gen_games_table.py
    .\.venv\Scripts\python.exe packaging\gen_games_table.py --check   # CI: fail if stale
"""

import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_JSON = os.path.join(ROOT, "games.json")
GAMES_MD = os.path.join(ROOT, "GAMES.md")

BEGIN = "<!-- BEGIN generated from games.json -- edit games.json, not this table -->"
END = "<!-- END generated -->"

# The four states GAMES.md documents, and the badge each one prints.
BADGES = {
    "works": "\U0001F7E2 Works",
    "playable": "\U0001F7E1 Playable",
    "broken": "\U0001F534 Broken",
    "untested": "⚪ Untested",
}


def anchor(docs, name):
    """`GAMES.md#dirt-4` -> `#dirt-4`, so the link works from inside GAMES.md itself."""
    if docs and "#" in docs:
        return "#" + docs.split("#", 1)[1]
    return "#" + name.lower().replace(" ", "-")


def render(games):
    rows = ["| Game | Status | Needs setup? | Verified |", "|---|---|---|---|"]
    for g in games:
        badge = BADGES.get(g["status"], g["status"])
        status = badge if g["status"] == "untested" else "%s: %s" % (badge, g["summary"])
        rows.append("| [%s](%s) | %s | %s | %s |" % (
            g["name"], anchor(g.get("docs"), g["name"]), status,
            g.get("setup_summary") or "Unknown", g.get("verified") or "—"))
    return "\n".join(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true",
                   help="do not write; exit 1 if GAMES.md is out of date with games.json")
    args = p.parse_args()

    with io.open(GAMES_JSON, encoding="utf-8") as fh:
        games = json.load(fh)["games"]
    with io.open(GAMES_MD, encoding="utf-8") as fh:
        md = fh.read()

    if BEGIN not in md or END not in md:
        print("GAMES.md is missing the generated-table markers:")
        print("  %s" % BEGIN)
        print("  %s" % END)
        return 1

    head, rest = md.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    new = head + BEGIN + "\n" + render(games) + "\n" + END + tail

    if new == md:
        print("GAMES.md table is up to date (%d games)." % len(games))
        return 0
    if args.check:
        print("GAMES.md table is STALE. Run: python packaging\\gen_games_table.py")
        return 1
    with io.open(GAMES_MD, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new)
    print("GAMES.md table rewritten from games.json (%d games)." % len(games))
    return 0


if __name__ == "__main__":
    sys.exit(main())
