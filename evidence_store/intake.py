"""Intake: GitHub issues and comments to S3 records.

An issue opened with one of the store's forms (`forms.py`) becomes a review record: an acceptance, a
problem or a question. Each later comment on it becomes a record too:

* a **command** on its first line becomes a ``status`` of the issue's record, if the commenter may
  set it:

  | command | on | who | state |
  |---|---|---|---|
  | ``/withdraw`` | any review | its author | ``withdrawn`` |
  | ``/fixed [commit]`` | a problem | its reporter, a maintainer | ``fixed`` |
  | ``/intended``, ``/invalid`` | a problem | its reporter, a maintainer | ``intended``, ``invalid`` |
  | ``/answered`` | a question | its asker, a maintainer | ``answered`` |
  | ``/reopen`` | a problem or question | its author, a maintainer | ``reopened`` |

  Anything after the command is the status's note;
* anything else becomes a ``comment`` replying to it: the discussion, and the answers to questions.

Every record is by the GitHub account that wrote the issue or comment. An AI agent says so in the
form ("Written by: an AI agent") or with a line ``<!-- agent: tool=…; model=…; session=… -->`` in a
comment; an account of type ``Bot`` is an agent too.

Intake is **idempotent**: a record's ``origin`` names the issue or comment it came from, and one
already in the store is not made again. So it can run on every event and also sweep recent issues,
which catches events whose runs were dropped.

``process`` is pure: it reads an issue and its comments (as the GitHub API returns them), the store
and a dataset, and returns the records to add and what to say and do on GitHub (an ``Outcome``).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable

from evidence_core import Dataset
from evidence_core import records as rec
from evidence_core.store import Store

from . import forms

MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
BOTS = ("github-actions[bot]",)
AGENT_MARK = re.compile(r"<!--\s*agent:(.*?)-->", re.S)
COMMANDS = ("withdraw", "fixed", "intended", "invalid", "answered", "reopen")


@dataclass
class Context:
    #: The repository whose issues are read, ``owner/name``.
    repo: str
    store: Store
    #: A dataset of the library at a commit; ``""`` for the latest.
    dataset: Callable[[str], Dataset]
    maintainers: frozenset = frozenset()


@dataclass
class Outcome:
    records: list[dict] = field(default_factory=list)
    #: (issue number, Markdown)
    replies: list[tuple[int, str]] = field(default_factory=list)
    #: (issue number, "completed" or "not planned")
    close: list[tuple[int, str]] = field(default_factory=list)
    reopen: list[int] = field(default_factory=list)
    #: (issue number, labels to add, labels to remove)
    labels: list[tuple[int, list[str], list[str]]] = field(default_factory=list)
    #: (comment id, reaction), to acknowledge a comment recorded as a reply
    reactions: list[tuple[int, str]] = field(default_factory=list)

    def merge(self, other: "Outcome") -> None:
        for k in ("records", "replies", "close", "reopen", "labels", "reactions"):
            getattr(self, k).extend(getattr(other, k))

    def as_json(self) -> dict:
        return {"records": [r["id"] for r in self.records], "replies": self.replies, "close": self.close,
                "reopen": self.reopen, "labels": self.labels, "reactions": self.reactions}


# --- who wrote it ----------------------------------------------------------------------------------

def parse_agent_mark(text: str) -> dict | None:
    """``<!-- agent: tool=Claude Code; model=claude-opus-5-5 -->`` as ``{tool, model}``."""
    m = AGENT_MARK.search(text or "")
    if not m:
        return None
    out = {}
    for part in m.group(1).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() in ("tool", "model", "session") and v.strip():
                out[k.strip()] = v.strip()
    if not out.get("tool"):
        out["tool"] = m.group(1).strip() or "unknown agent"
        out = {k: v for k, v in out.items() if k in ("tool", "model", "session")}
    return out


def author_of(user: dict, agent: dict | None, involvement: str = "unknown") -> dict:
    login = user.get("login", "")
    by = {"kind": "person", "identity": {"kind": "github", "id": login}, "involvement": involvement}
    if agent is None and user.get("type") == "Bot":
        agent = {"tool": login.removesuffix("[bot]")}
    if agent is not None:
        by["kind"] = "agent"
        by["agent"] = agent
    return by


def is_maintainer(ctx: Context, item: dict) -> bool:
    return item.get("author_association") in MAINTAINER_ASSOCIATIONS or \
        item.get("user", {}).get("login") in ctx.maintainers


def iso(ts: str) -> str:
    """GitHub's timestamps (``2026-09-26T08:04:20Z``) are RFC 3339 already; keep them to the second."""
    return (ts or "").replace(".000", "")


# --- the issue -------------------------------------------------------------------------------------

def subject_kind_for(decl) -> str:
    return "instance" if decl.kind == "instance" else ("statement" if decl.is_prop else "definition")


def review_record(kind: str, answers: dict, issue: dict, ctx: Context) -> tuple[dict | None, list[str]]:
    """The review record an issue asks for, or the reasons it cannot be made."""
    errs = []
    name = (answers.get("decl") or "").strip().strip("`")
    commit = (answers.get("commit") or "").strip().strip("`")
    if not name:
        return None, ["the declaration is missing"]
    try:
        ds = ctx.dataset(commit)
    except Exception as e:  # no dataset for that commit
        return None, [f"no dataset of the library at `{commit or 'latest'}`: {e}"]
    decl = ds.by_name.get(name)
    if decl is None:
        close = difflib.get_close_matches(name, [d.name for d in ds.decls if d.is_project], n=3)
        hint = f" Did you mean {', '.join(f'`{c}`' for c in close)}?" if close else ""
        return None, [f"`{name}` is not a declaration of the library at `{ds.commit[:12]}`.{hint}"]
    agent = None
    if answers.get("who") == "agent" or answers.get("agent"):
        agent = rec.parse_agent(answers.get("agent") or "unknown agent")
    by = author_of(issue.get("user", {}), agent, answers.get("involvement") or "unknown")
    r = {"schema": rec.SCHEMA, "kind": "review",
         "subject": rec.subject_from_decl(decl, ds, subject_kind_for(decl)),
         "by": by, "at": iso(issue.get("created_at", "")),
         "origin": {"kind": "issue", "ref": f"{ctx.repo}#{issue['number']}"}}
    if kind == "review":
        r["verdict"] = "accept"
        if answers.get("reference"):
            text = answers["reference"]
            url = re.search(r"https?://\S+", text)
            r["reference"] = {"text": text, **({"url": url.group(0).rstrip(").,")} if url else {})}
        if answers.get("checked"):
            r["checked"] = {code: ("checked" if on else "unchecked")
                            for code, on in answers["checked"].items()}
        caveats = []
        for line in (answers.get("caveats") or "").splitlines():
            m = re.match(r"^\s*[-*]?\s*(F\d|naming|other)\s*[:—-]\s*(.+)$", line)
            if m:
                caveats.append({"category": m.group(1), "note": m.group(2).strip()})
            elif line.strip():
                caveats.append({"category": "other", "note": line.strip()})
        if caveats:
            r["caveats"] = caveats
        if answers.get("rationale"):
            r["rationale"] = answers["rationale"]
    elif kind == "problem":
        r["verdict"] = "problem"
        r["problem"] = {"category": answers.get("category") or "other"}
        r["rationale"] = answers.get("rationale", "")
    else:
        r["verdict"] = "question"
        r["rationale"] = answers.get("question", "")
    # The reviewer's current view: a new acceptance or problem supersedes their latest earlier
    # acceptance of the same declaration.
    if r["verdict"] in ("accept", "problem"):
        earlier = [x for x in ctx.store.records
                   if x.get("kind") == "review" and x.get("verdict") == "accept"
                   and (x.get("subject") or {}).get("name") == name and rec.same_reviewer(x.get("by", {}), by)]
        if earlier:
            r["links"] = {"supersedes": max(earlier, key=lambda x: x.get("at", ""))["id"]}
    r = rec.with_id(r)
    errs += rec.validate(r)
    return (None, errs) if errs else (r, [])


def describe(r: dict) -> str:
    s = r["subject"]
    what = {"accept": "review", "problem": f"problem ({r.get('problem', {}).get('category')})",
            "question": "question"}[r["verdict"]]
    by = rec.who(r["by"])
    return (f"Recorded {what} `{r['id']}` of `{s['name']}` at `{s['commit'][:12]}` by {by} "
            f"(meaning hash `{s.get('hashes', {}).get('meaning', '?')}`).")


FOOTER = {
    "accept": "It counts from now on. If `{name}`, or anything it rests on, changes, it goes stale, "
              "and the page says so. Comment `/withdraw` here to take it back.",
    "problem": "It stays open until you or a maintainer comment `/fixed <commit>`, `/intended` or "
               "`/invalid`. Comments here are recorded as replies.",
    "question": "Answers are comments here, and are recorded. You or a maintainer close it with "
                "`/answered`.",
}


def process_issue(issue: dict, ctx: Context, announce: bool = True) -> tuple[Outcome, dict | None]:
    """The record an issue asks for, unless the store has it already. Returns the outcome and the
    issue's record (new or existing), if any."""
    out = Outcome()
    kind = forms.kind_of(issue.get("labels", []))
    if kind is None or issue.get("pull_request"):
        return out, None
    ref = f"{ctx.repo}#{issue['number']}"
    existing = [r for r in ctx.store.from_origin(ref) if r.get("kind") == "review"]
    if existing:
        return out, existing[0]
    answers = forms.parse(kind, issue.get("body", ""))
    r, errs = review_record(kind, answers, issue, ctx)
    n = issue["number"]
    if r is None:
        if announce:
            out.replies.append((n, "Not recorded yet:\n\n" + "\n".join(f"- {e}" for e in errs) +
                                "\n\nEdit the issue to fix it, and it will be read again."))
            out.labels.append((n, ["evidence:needs-fix"], []))
        return out, None
    out.records.append(r)
    out.replies.append((n, describe(r) + "\n\n" + FOOTER[r["verdict"]].format(name=r["subject"]["name"])))
    if r["verdict"] == "accept":
        out.labels.append((n, [], ["evidence:needs-fix"]))
        out.close.append((n, "completed"))
    else:
        out.labels.append((n, ["evidence:open"], ["evidence:needs-fix"]))
    return out, r


# --- comments --------------------------------------------------------------------------------------

def command_of(text: str) -> tuple[str, list[str], str] | None:
    """``/fixed abc123 the note`` as ("fixed", ["abc123"], "the note"): the command on the first
    non-empty line, and what follows."""
    lines = [l for l in AGENT_MARK.sub("", text or "").strip().splitlines()]
    if not lines:
        return None
    m = re.match(r"^/(\w+)\b\s*(.*)$", lines[0].strip())
    if not m or m.group(1) not in COMMANDS:
        return None
    rest = m.group(2).strip()
    args = []
    if m.group(1) == "fixed":
        cm = re.match(r"^([0-9a-f]{7,40})\b\s*(.*)$", rest)
        if cm:
            args, rest = [cm.group(1)], cm.group(2)
    note = "\n".join([rest.lstrip("—-: ").strip()] + lines[1:]).strip()
    return m.group(1), args, note


def process_comment(comment: dict, issue: dict, target: dict, ctx: Context,
                    announce: bool = True) -> Outcome:
    out = Outcome()
    user = comment.get("user", {})
    if user.get("login") in BOTS:
        return out
    ref = comment.get("html_url") or f"{ctx.repo}#{issue['number']}/comment/{comment.get('id')}"
    if ctx.store.from_origin(ref):
        return out
    text = comment.get("body") or ""
    by = author_of(user, parse_agent_mark(text))
    base = {"schema": rec.SCHEMA, "by": by, "at": iso(comment.get("created_at", "")),
            "origin": {"kind": "comment", "ref": ref}}
    n = issue["number"]
    cmd = command_of(text)
    if cmd is not None:
        name, args, note = cmd
        verdict = target.get("verdict")
        author = (target.get("by", {}).get("identity") or {}).get("id") == user.get("login")
        maintainer = is_maintainer(ctx, comment)
        state, refusal = None, None
        if name == "withdraw":
            state = "withdrawn" if author else None
            refusal = None if author else "only the author of a review can withdraw it"
        elif name in ("fixed", "intended", "invalid"):
            if verdict != "problem":
                refusal = f"`/{name}` is for problems"
            elif author or maintainer:
                state = name
            else:
                refusal = "only the reporter or a maintainer can say how a problem was resolved"
        elif name == "answered":
            if verdict != "question":
                refusal = "`/answered` is for questions"
            elif author or maintainer:
                state = "answered"
            else:
                refusal = "only the asker or a maintainer can close a question"
        elif name == "reopen":
            if verdict not in ("problem", "question"):
                refusal = "only problems and questions can be reopened"
            elif author or maintainer:
                state = "reopened"
            else:
                refusal = "only the author or a maintainer can reopen it"
        if state is not None:
            r = {**base, "kind": "status", "target": target["id"], "state": state}
            if note:
                r["note"] = note
            if args:
                r["commit"] = args[0]
            r = rec.with_id(r)
            out.records.append(r)
            label = {"withdrawn": "withdrawn", "fixed": "fixed", "intended": "intended as it is",
                     "invalid": "invalid", "answered": "answered", "reopened": "reopened"}[state]
            out.replies.append((n, f"Recorded: `{target['id']}` is now **{label}**"
                                   + (f" (`{args[0]}`)" if args else "") + f", by {rec.who(by)}."))
            if state == "reopened":
                out.reopen.append(n)
                out.labels.append((n, ["evidence:open"], []))
            else:
                out.labels.append((n, [], ["evidence:open"]))
                out.close.append((n, "completed" if state in ("fixed", "answered") else "not planned"))
            return out
        if announce:
            out.replies.append((n, f"Not recorded as a status: {refusal}. The comment is kept as a reply."))
    # A reply: the discussion, and answers to questions.
    body = AGENT_MARK.sub("", text).strip()
    if not body:
        return out
    r = rec.with_id({**base, "kind": "comment", "text": body, "links": {"replies_to": target["id"]},
                     **({"subject": target["subject"]} if target.get("subject") else {})})
    out.records.append(r)
    if comment.get("id"):
        out.reactions.append((comment["id"], "eyes"))
    return out


def process(issue: dict, comments: list[dict], ctx: Context, announce: bool = True) -> Outcome:
    """Everything an issue and its comments ask for that the store does not have yet, in order. The
    records are added to the store as they are made, so that later comments see earlier ones."""
    out, target = process_issue(issue, ctx, announce)
    ctx.store.add(out.records)
    if target is None:
        return out
    for c in sorted(comments, key=lambda c: c.get("created_at", "")):
        o = process_comment(c, issue, target, ctx, announce)
        ctx.store.add(o.records)
        out.merge(o)
    return out
