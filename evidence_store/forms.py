"""The store's issue forms: one definition, from which come the templates GitHub shows, the parser
for the issues they produce, and the body an agent writes to submit the same thing.

Three forms, one per verdict of a review (S3): **review** (an acceptance, with what was checked, the
reference and caveats), **problem** (with its failure mode) and **question**; and a fourth,
**status**, which changes the state of a record: withdraw a review, mark a problem fixed, intended or
not a problem, mark a question answered, reopen. Each labels its issue (`evidence:review`, …), which
is how intake knows what an issue is.

GitHub writes a form's answers into the issue body as ``### <label>`` sections, in form order, with
``_No response_`` for an empty field and ``- [X] …`` / ``- [ ] …`` for checkboxes. ``parse`` reads
that, and ``body`` writes it, so an issue opened from the command line reads the same as one opened
from the web.
"""
from __future__ import annotations

import json
import re

NO_RESPONSE = "_No response_"

#: The failure modes of trusting-definitions.md §2 that a reviewer can check, and naming.
CHECKS = [
    ("F1", "the intended object, not a different notion"),
    ("F2", "its conventions: normalization, indexing, signs"),
    ("F3", "its edge cases: degenerate or boundary inputs"),
    ("F4", "junk values: defaults outside the intended domain"),
    ("F5", "not vacuous, not trivial"),
    ("F6", "no arbitrary choice"),
    ("F7", "what it rests on: the definitions and instances underneath"),
    ("F9", "its generality, against the source's"),
    ("naming", "its name and docstring do not mislead"),
]

#: Problem categories (S3), with how the form shows them.
CATEGORIES = [
    ("F1", "F1 a different object"), ("F2", "F2 a different convention"),
    ("F3", "F3 different edge cases"), ("F4", "F4 a junk value"),
    ("F5", "F5 vacuous or trivial"), ("F6", "F6 an arbitrary choice"),
    ("F7", "F7 something wrong underneath"), ("F9", "F9 less general than the source"),
    ("naming", "a misleading name or docstring"), ("other", "something else"),
]

INVOLVEMENT = [("outsider", "an outsider to this library"), ("contributor", "a contributor to this library"),
               ("author", "the author of this declaration")]

WHO = [("person", "me, a person"), ("agent", "an AI agent, run by me")]

DECL = {"id": "decl", "type": "input", "label": "Declaration", "required": True,
        "description": "Its full Lean name, e.g. `MyLib.Foo.bar`."}
COMMIT = {"id": "commit", "type": "input", "label": "Commit",
          "description": "The library's commit you read it at. Empty: the latest."}
WHO_FIELD = {"id": "who", "type": "dropdown", "label": "Written by", "options": WHO,
             "description": "A review by an AI agent is shown as one, and counted apart."}
AGENT = {"id": "agent", "type": "input", "label": "Agent",
         "description": "If an AI agent wrote this: its tool and model, e.g. `Claude Code, claude-opus-5-5`."}
INVOLVED = {"id": "involvement", "type": "dropdown", "label": "You are", "options": INVOLVEMENT}

FORMS = {
    "review": {
        "file": "evidence-review.yml", "label": "evidence:review", "title": "Review: ",
        "name": "Review a declaration",
        "description": "Say that a declaration means what it should, and what you checked.",
        "intro": "A review is a judgement about **meaning**: does this declaration say what it is "
                 "supposed to say? It is recorded in the repository's evidence store under your "
                 "GitHub account, and goes stale by itself if the declaration, or anything it rests "
                 "on, changes.",
        "fields": [
            DECL, COMMIT,
            {"id": "reference", "type": "input", "label": "Compared with",
             "description": "What you compared it with: a book or paper and section, a URL, or "
                            "\"my own knowledge\"."},
            {"id": "checked", "type": "checkboxes", "label": "What you checked", "options": CHECKS,
             "description": "Tick what you checked. What is left unticked is shown as not checked."},
            {"id": "caveats", "type": "textarea", "label": "Caveats",
             "description": "One per line, starting with the failure mode: `F3: false at n = 0`."},
            {"id": "rationale", "type": "textarea", "label": "Why",
             "description": "Optional for people, required for AI agents."},
            INVOLVED, WHO_FIELD, AGENT,
        ],
    },
    "problem": {
        "file": "evidence-problem.yml", "label": "evidence:problem", "title": "Problem: ",
        "name": "Report a problem with a declaration",
        "description": "It does not mean what it should.",
        "intro": "A problem stays open until its reporter or a maintainer comments `/fixed <commit>`, "
                 "`/intended` or `/invalid`. Comments on this issue are recorded as replies.",
        "fields": [
            DECL, COMMIT,
            {"id": "category", "type": "dropdown", "label": "What is wrong", "options": CATEGORIES,
             "required": True},
            {"id": "rationale", "type": "textarea", "label": "Why", "required": True,
             "description": "The counterexample, the case, or the step that fails."},
            INVOLVED, WHO_FIELD, AGENT,
        ],
    },
    "question": {
        "file": "evidence-question.yml", "label": "evidence:question", "title": "Question: ",
        "name": "Ask a question about a declaration",
        "description": "What is it at 0? Why this convention?",
        "intro": "Answers are comments on this issue. The asker or a maintainer closes it with "
                 "`/answered`.",
        "fields": [
            DECL, COMMIT,
            {"id": "question", "type": "textarea", "label": "Question", "required": True},
            INVOLVED, WHO_FIELD, AGENT,
        ],
    },
    "status": {
        "file": "evidence-status.yml", "label": "evidence:status", "title": "Status: ",
        "name": "Change the state of a review",
        "description": "Withdraw your review; mark a problem fixed, intended or not a problem; mark a question answered; reopen.",
        "intro": "Usually opened from the buttons under a review, with its id filled in. It is recorded if your "
                 "account may make the change: the author of a review can withdraw it; the reporter of a problem, "
                 "the asker of a question, and the maintainers can resolve or reopen it. The same changes can be "
                 "made by commenting on the review's own issue (`/withdraw`, `/fixed <commit>`, …).",
        "fields": [
            {"id": "record", "type": "input", "label": "Record", "required": True,
             "description": "The id of the review, problem or question: 16 hexadecimal digits."},
            {"id": "action", "type": "input", "label": "Change", "required": True,
             "description": "One of: withdraw, fixed, intended, invalid, answered, reopen."},
            {"id": "commit", "type": "input", "label": "Fixed in",
             "description": "For `fixed`: the commit that fixed it."},
            {"id": "note", "type": "textarea", "label": "Note"},
            WHO_FIELD, AGENT,
        ],
    },
}

LABELS = {f["label"]: kind for kind, f in FORMS.items()}


def kind_of(labels: list) -> str | None:
    """The form an issue was opened with, from its labels (names or label objects)."""
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if name in LABELS:
            return LABELS[name]
    return None


# --- templates -------------------------------------------------------------------------------------

def _q(s: str) -> str:
    """A YAML double-quoted scalar: JSON's escaping is valid YAML."""
    return json.dumps(s, ensure_ascii=False)


def template(kind: str) -> str:
    """The issue form template (YAML) for ``kind``."""
    f = FORMS[kind]
    out = [f"# Generated by evidence-store (`evidence-store init`); edit the definitions in",
           f"# evidence_store/forms.py rather than this file.",
           f"name: {_q(f['name'])}", f"description: {_q(f['description'])}",
           f"title: {_q(f['title'])}", f"labels: [{_q(f['label'])}]", "body:",
           "  - type: markdown", "    attributes:", f"      value: {_q(f['intro'])}"]
    for field in f["fields"]:
        out += [f"  - type: {field['type']}", f"    id: {field['id']}", "    attributes:",
                f"      label: {_q(field['label'])}"]
        if field.get("description"):
            out.append(f"      description: {_q(field['description'])}")
        if field["type"] == "dropdown":
            out.append("      options:")
            out += [f"        - {_q(text)}" for _, text in field["options"]]
        if field["type"] == "checkboxes":
            out.append("      options:")
            out += [f"        - label: {_q(f'{code}: {text}')}" for code, text in field["options"]]
        if field.get("required"):
            out += ["    validations:", "      required: true"]
    return "\n".join(out) + "\n"


# --- parsing and writing bodies --------------------------------------------------------------------

def parse(kind: str, body: str) -> dict:
    """The answers of an issue body written from form ``kind``: field id → text (inputs,
    textareas), option code (dropdowns) or {code: checked} (checkboxes). Empty fields are left out."""
    fields = {f["label"]: f for f in FORMS[kind]["fields"]}
    sections: dict[str, list[str]] = {}
    current = None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = re.match(r"^###\s+(.*?)\s*$", line)
        if m and m.group(1) in fields:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    out: dict = {}
    for label, lines in sections.items():
        field = fields[label]
        text = "\n".join(lines).strip()
        if field["type"] == "checkboxes":
            ticked = {}
            for line in lines:
                cm = re.match(r"^\s*-\s*\[([ xX])\]\s*(.*)$", line)
                if cm:
                    code = cm.group(2).split(":", 1)[0].strip()
                    ticked[code] = cm.group(1).lower() == "x"
            out[field["id"]] = ticked
            continue
        if not text or text == NO_RESPONSE:
            continue
        if field["type"] == "dropdown":
            code = next((c for c, t in field["options"] if t == text or c == text), None)
            out[field["id"]] = code if code is not None else text
        else:
            out[field["id"]] = text
    return out


def body(kind: str, answers: dict) -> str:
    """The issue body GitHub would write for ``answers`` (as ``parse`` returns them)."""
    parts = []
    for field in FORMS[kind]["fields"]:
        value = answers.get(field["id"])
        if field["type"] == "checkboxes":
            value = value or {}
            text = "\n".join(f"- [{'X' if value.get(code) else ' '}] {code}: {label}"
                             for code, label in field["options"])
        elif field["type"] == "dropdown":
            text = dict(field["options"]).get(value, value) if value else NO_RESPONSE
        else:
            text = value if value else NO_RESPONSE
        parts.append(f"### {field['label']}\n\n{text}")
    return "\n\n".join(parts) + "\n"
