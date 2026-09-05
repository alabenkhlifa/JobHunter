"""One-off: re-extract tech_required/tech_nice_to_have for every stored
description, using the current extractor. Run once, after the AI stack
ring (Task 1 of this plan) is merged -- not before, so the "did this
help" measurement below reflects the shipped ring, not a guess.

Needed because commit f27d322 improved scraper.extract_tech_keywords to
recognize AI/LLM vocabulary, but the 4,580 rows already in data/jobs.db
were extracted before that commit landed -- their stored tech fields
don't carry the AI terms even where the description does.
"""
import sqlite3
import sys
sys.path.insert(0, ".")
import scraper

DB = "data/jobs.db"


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, description FROM jobs WHERE description != ''").fetchall()

    changed = 0
    for row in rows:
        required, nice = scraper.extract_tech_keywords(row["description"])
        req_str, nice_str = ", ".join(required), ", ".join(nice)
        current = conn.execute(
            "SELECT tech_required, tech_nice_to_have FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        if current["tech_required"] != req_str or current["tech_nice_to_have"] != nice_str:
            conn.execute(
                "UPDATE jobs SET tech_required = ?, tech_nice_to_have = ? WHERE id = ?",
                (req_str, nice_str, row["id"]),
            )
            changed += 1
    conn.commit()
    print(f"backfilled {changed} of {len(rows)} rows")


if __name__ == "__main__":
    main()
