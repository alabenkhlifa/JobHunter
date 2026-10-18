"""Job-scoped, expiring, single-use controls for the owner's resume picker."""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import sqlite3
import time

import resume_blocks

TTL = 86400


class ChoiceError(ValueError):
    pass


def digest(profile):
    return hashlib.sha256(json.dumps(resume_blocks.catalog(profile), sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS resume_choice_sessions (
            job_id TEXT PRIMARY KEY, selection TEXT NOT NULL, step TEXT NOT NULL,
            revision INTEGER NOT NULL, catalog_sha TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resume_choice_tokens (
            token TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
            action TEXT NOT NULL, value TEXT NOT NULL, expires REAL NOT NULL
        );
    """)


def start(conn, job_id, profile):
    _schema(conn)
    with conn:
        conn.execute("DELETE FROM resume_choice_tokens WHERE job_id=? OR expires<?", (job_id, time.time()))
        conn.execute("INSERT INTO resume_choice_sessions VALUES (?, ?, 'flavor', 0, ?, ?) "
                     "ON CONFLICT(job_id) DO UPDATE SET selection=excluded.selection, step='flavor', "
                     "revision=resume_choice_sessions.revision+1, catalog_sha=excluded.catalog_sha, expires=excluded.expires",
                     (job_id, '{}', digest(profile), time.time() + TTL))
    return card(conn, job_id, profile)


def _session(conn, job_id, profile):
    row = conn.execute("SELECT selection,step,revision,catalog_sha,expires FROM resume_choice_sessions "
                       "WHERE job_id=?", (job_id,)).fetchone()
    if not row or row[4] < time.time() or row[3] != digest(profile):
        raise ChoiceError("This resume choice expired or its approved content changed. Choose Apply again.")
    return json.loads(row[0]), row[1], row[2]


def card(conn, job_id, profile):
    catalog = resume_blocks.catalog(profile)
    selection, step, revision = _session(conn, job_id, profile)
    rows = []

    def button(label, action, value=""):
        token = secrets.token_hex(16)
        conn.execute("INSERT INTO resume_choice_tokens VALUES (?,?,?,?,?,?)",
                     (token, job_id, revision, action, value, time.time() + TTL))
        rows.append([{"text": label, "callback_data": f"resume_choice:{token}"}])

    if step == "flavor":
        message = "🎨 <b>Choose your resume flavor</b>\n\nSoftware Engineer covers Senior Backend Engineer and Senior Software Engineer roles.\n\n"
        for flavor in catalog["flavors"]:
            if flavor["confirmation"] == "candidate-confirmed":
                button(flavor["label"], "flavor", flavor["id"])
            else:
                message += f"🕓 {html.escape(flavor['label'])}: wording awaiting approval.\n"
        message += "\nNext: choose the length and projects."
    elif step == "variant":
        message = "📐 <b>Choose the resume length</b>\n\nSelected projects and their bullets stay intact. A two-page option can still fit on one page."
        for variant in catalog["variants"]:
            if variant["flavor_id"] == selection["flavor_id"] and variant["confirmation"] == "candidate-confirmed":
                button(variant["label"], "variant", variant["id"])
        button("↩️ Change flavor", "back")
    elif step == "projects":
        variant = next(v for v in catalog["variants"] if v["id"] == selection["variant_id"])
        chosen = selection["project_ids"]
        message = ("🧩 <b>Choose your projects</b>\n\nTap a project to include or remove its complete approved block. "
                   "The summary and bullet wording stay fixed.\n\n"
                   f"<b>{len(chosen)} selected</b> · {html.escape(variant['label'])}\n"
                   "I’ll measure page two after rendering and flag it if it is mostly empty.")
        ordered = variant["default_project_ids"] + [identifier for identifier in variant["project_ids"]
                                                    if identifier not in variant["default_project_ids"]]
        for identifier in ordered:
            project = resume_blocks.project_for_variant(catalog, variant, identifier)
            if project["confirmation"] == "candidate-confirmed":
                button(("✅ " if project["id"] in chosen else "⬜ ") + project["label"], "toggle", project["id"])
        if chosen:
            button("📄 Generate resume", "generate")
        button("↩️ Change length", "length")
    else:
        raise ChoiceError("This choice is already being processed. Check the latest resume message.")
    conn.commit()
    return message, {"inline_keyboard": rows}


def choose(conn, token, profile):
    """Atomically consume this card, returning (job_id, generate_selection_or_None)."""
    _schema(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT job_id,revision,action,value,expires FROM resume_choice_tokens WHERE token=?",
                           (token,)).fetchone()
        if not row or row[4] < time.time():
            raise ChoiceError("This button was already used or expired. Use the latest resume card.")
        job_id, revision, action, value, _ = row
        selection, step, current_revision = _session(conn, job_id, profile)
        if revision != current_revision:
            raise ChoiceError("This card was replaced. Use the latest resume card.")
        catalog = resume_blocks.catalog(profile)
        if action == "flavor" and step == "flavor":
            if not any(f["id"] == value and f["confirmation"] == "candidate-confirmed" for f in catalog["flavors"]):
                raise ChoiceError("This flavor is no longer approved. Choose Apply again.")
            selection, step = {"flavor_id": value}, "variant"
        elif action == "variant" and step == "variant":
            variant = next((v for v in catalog["variants"] if v["id"] == value and
                            v["flavor_id"] == selection["flavor_id"] and v["confirmation"] == "candidate-confirmed"), None)
            if not variant:
                raise ChoiceError("This length is no longer approved. Choose Apply again.")
            selection.update(variant_id=value, project_ids=list(variant["default_project_ids"]))
            step = "projects"
        elif action == "toggle" and step == "projects":
            variant = next(v for v in catalog["variants"] if v["id"] == selection["variant_id"])
            if value not in variant["project_ids"]:
                raise ChoiceError("This project is unavailable. Choose Apply again.")
            if resume_blocks.project_for_variant(catalog, variant, value)["confirmation"] != "candidate-confirmed":
                raise ChoiceError("This project is awaiting approval. Choose Apply again.")
            picked = set(selection["project_ids"])
            picked.symmetric_difference_update({value})
            ordered = variant["default_project_ids"] + [identifier for identifier in variant["project_ids"]
                                                        if identifier not in variant["default_project_ids"]]
            selection["project_ids"] = [p for p in ordered if p in picked]
        elif action == "back" and step == "variant":
            selection, step = {}, "flavor"
        elif action == "length" and step == "projects":
            selection, step = {"flavor_id": selection["flavor_id"]}, "variant"
        elif action == "generate" and step == "projects":
            resume_blocks.build(profile, selection)
            step = "generating"
        else:
            raise ChoiceError("This card was replaced. Use the latest resume card.")
        conn.execute("DELETE FROM resume_choice_tokens WHERE job_id=?", (job_id,))
        conn.execute("UPDATE resume_choice_sessions SET selection=?,step=?,revision=revision+1 WHERE job_id=?",
                     (json.dumps(selection), step, job_id))
        conn.commit()
        return job_id, selection if step == "generating" else None
    except Exception:
        conn.rollback()
        raise


def retry(conn, job_id, profile):
    """Issue new controls after generation/delivery failure; old tokens remain spent."""
    selection, step, _ = _session(conn, job_id, profile)
    if step != "generating":
        raise ChoiceError("Choose Apply again to restart resume selection.")
    with conn:
        conn.execute("UPDATE resume_choice_sessions SET step='projects',revision=revision+1 WHERE job_id=?", (job_id,))
    return card(conn, job_id, profile)
