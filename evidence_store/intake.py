"""Intake: GitHub issues and comments to S3 records.

An issue opened with one of the store's forms (`forms.py`) becomes a record: a review (an acceptance,
a problem or a question), a challenge (a proposed test), a test, or a named result. Each later
comment on it becomes a record too:

* a **command** on its first line becomes a ``status`` of the issue's record, if the commenter may
  set it:

  | command | on | who | state |
  |---|---|---|---|
  | ``/withdraw`` | any record | its author | ``withdrawn`` |
  | ``/fixed [commit]`` | a problem | its reporter, a maintainer | ``fixed`` |
  | ``/intended``, ``/invalid`` | a problem | its reporter, a maintainer | ``intended``, ``invalid`` |
  | ``/answered`` | a question | its asker, a maintainer | ``answered`` |
  | ``/met <declaration>`` | a challenge | its author, a maintainer | ``met`` |
  | ``/failed``, ``/declined`` | a challenge | its author, a maintainer | ``failed``, ``declined`` |
  | ``/reopen`` | a problem, question or challenge | its author, a maintainer | ``reopened`` |

  Anything after the command is the status's note;
* anything else becomes a ``comment`` replying to it: the discussion, and the answers to questions.

**Closing or reopening** the issue by hand is a status too, as GitHub users expect: a problem closed
as completed is ``fixed``, as not planned ``invalid``; a question closed is ``answered``; a challenge
closed as completed is ``met``, as not planned ``declined``; reopening is ``reopened``. Only the
issue's author and the repository's maintainers can close an issue, which is the same rule. Closes
by the intake bot itself (after a command) are not read again.

An issue opened with the **status** form asks for one of those changes to a record named by its id,
under the same rules; it is how a page offers "Withdraw" or "Mark fixed" as a button. The change is
also said, and done, on the record's own issue.

The **bulk issue** (labelled ``evidence:bulk``) takes lines in its comments, one record each, in the
syntax of Reviewed-by, so that many can be given at once:
``Reviewed-by: <declaration> — <what was checked>``, ``Test: <declaration> — <test> — <what it
checks>``, ``Named: <declaration> — <name> — <a sentence>``, ``Challenge: <declaration> — <the
property>``.

Every record is by the GitHub account that wrote the issue or comment. An AI agent says so in the
form ("Written by: an AI agent") or with a line ``<!-- agent: tool=…; model=…; session=… -->`` (or
Reviewed-by's ``<!--reviewed-by:v1 {"agent": "…"}-->``) in a comment; an account of type ``Bot`` is
an agent too.

Intake is **idempotent**: a record's ``origin`` names the issue, comment, line or event it came from,
and one already in the store is not made again. So it can run on every event and also sweep recent
issues, which catches events whose runs were dropped.

``process`` is pure: it reads an issue, its comments and its events (as the GitHub API returns
them), the store and a dataset, and returns the records to add and what to say and do on GitHub
(an ``Outcome``).
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from evidence_core import Dataset
from evidence_core import records as rec
from evidence_core.rubric import OTHER
from evidence_core.store import Store

from . import forms

MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
BOTS = ("github-actions[bot]",)
AGENT_MARK = re.compile(r"<!--\s*agent:(.*?)-->", re.S)
#: Reviewed-by's marker: <!--reviewed-by:v1 {"agent": "Claude Code, Opus, session 12"}-->
REVIEWED_BY_MARK = re.compile(r"<!--\s*reviewed-by:v1\s+(\{.*?\})\s*-->", re.S)
COMMANDS = ("withdraw", "fixed", "intended", "invalid", "answered", "met", "failed", "declined", "reopen")
BULK_LABEL = "evidence:bulk"


class DatasetMissing(LookupError):
    """No dataset of the library at a commit: typically one not built yet. Intake tries again later."""

    def __init__(self, commit: str):
        super().__init__(commit)
        self.commit = commit


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
    """``<!-- agent: tool=Claude Code; model=claude-opus-5-5 -->`` as ``{tool, model}``; also
    Reviewed-by's ``<!--reviewed-by:v1 {"agent": "Claude Code, Opus 5, session 12"}-->``."""
    rb = REVIEWED_BY_MARK.search(text or "")
    if rb:
        try:
            label = json.loads(rb.group(1)).get("agent", "")
        except ValueError:
            label = ""
        return rec.parse_agent(label or "unknown agent")
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

def lookup(ctx: Context, name: str, commit: str):
    """(dataset, declaration or None) for a name at a commit (the latest for ``""``)."""
    try:
        ds = ctx.dataset(commit)
    except Exception as e:  # no dataset for that commit, or not yet
        raise DatasetMissing(commit) from e
    return ds, ds.by_name.get(name)


def not_found(name: str, ds) -> str:
    close = difflib.get_close_matches(name, [d.name for d in ds.decls if d.is_project], n=3)
    hint = f" Did you mean {', '.join(f'`{c}`' for c in close)}?" if close else ""
    return f"`{name}` is not a declaration of the library at `{ds.commit[:12]}`.{hint}"


def clean(text: str | None) -> str:
    return (text or "").strip().strip("`").strip()


def record_for(kind: str, answers: dict, by: dict, at: str, origin: dict, ctx: Context
               ) -> tuple[dict | None, list[str]]:
    """The record a form's answers (or a bulk line) ask for, or the reasons it cannot be made."""
    name, commit = clean(answers.get("decl")), clean(answers.get("commit"))
    if not name:
        return None, ["the declaration is missing"]
    ds, decl = lookup(ctx, name, commit)
    if decl is None:
        return None, [not_found(name, ds)]
    rubric = ctx.store.rubric
    base = {"schema": rec.SCHEMA, "subject": rec.subject_from_decl(decl, ds),
            "by": by, "at": at, "origin": origin}
    errs: list[str] = []
    if kind in ("review", "problem", "question"):
        r = {**base, "kind": "review"}
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
            axes = "|".join(re.escape(n) for n in sorted(rubric.names + [OTHER], key=len, reverse=True))
            for line in (answers.get("caveats") or "").split("\n"):
                m = re.match(rf"^\s*[-*]?\s*({axes})\s*[:—-]\s*(.+)$", line)
                if m:
                    caveats.append({"category": m.group(1), "note": m.group(2).strip()})
                elif line.strip():
                    caveats.append({"category": "other", "note": line.strip()})
            if caveats:
                r["caveats"] = caveats
            if answers.get("rationale"):
                r["text"] = answers["rationale"]
        elif kind == "problem":
            r["verdict"] = "problem"
            r["category"] = answers.get("category") or "other"
            r["text"] = answers.get("rationale", "")
            if answers.get("fix"):
                r["fix"] = answers["fix"]
        else:
            r["verdict"] = "question"
            r["text"] = answers.get("question", "")
        # The reviewer's current view: a new acceptance or problem supersedes their latest earlier
        # acceptance of the same declaration.
        if r["verdict"] in ("accept", "problem"):
            earlier = [x for x in ctx.store.records
                       if x.get("kind") == "review" and x.get("verdict") == "accept"
                       and (x.get("subject") or {}).get("name") == name and rec.same_reviewer(x.get("by", {}), by)]
            if earlier:
                r["links"] = {"supersedes": max(earlier, key=lambda x: x.get("at", ""))["id"]}
    elif kind == "challenge":
        r = {**base, "kind": "challenge", "text": (answers.get("property") or "").strip()}
        for k in ("statement", "catches"):
            if (answers.get(k) or "").strip():
                r[k] = answers[k].strip()
        modes = [code for code, on in (answers.get("modes") or {}).items() if on]
        if modes:
            r["modes"] = modes
    elif kind == "test":
        tname = clean(answers.get("test"))
        if not tname:
            return None, ["the declaration that tests it is missing"]
        tdecl = ds.by_name.get(tname)
        if tdecl is None:
            return None, ["the test: " + not_found(tname, ds)]
        key = rec.subject_from_decl(tdecl, ds)
        r = {**base, "kind": "test", "test": {k: key[k] for k in ("name", "module", "commit", "hashes") if k in key}}
        if (answers.get("checks") or "").strip():
            r["text"] = answers["checks"].strip()
    elif kind == "named":
        r = {**base, "kind": "named", "name": (answers.get("name") or "").strip(),
             "what": answers.get("what") or "result"}
        if (answers.get("about") or "").strip():
            r["text"] = answers["about"].strip()
        if (answers.get("source") or "").strip():
            src = answers["source"].strip()
            r["reference"] = {"url": src} if re.match(r"^https?://\S+$", src) else {"text": src}
    else:
        return None, [f"`{kind}` is not a kind of record"]
    if any(k in r for k in ("category", "checked", "caveats", "modes")):
        r["rubric"] = rubric.name
    r = rec.with_id(r)
    errs += rec.validate(r, {rubric.name: rubric})
    return (None, errs) if errs else (r, [])


def review_record(kind: str, answers: dict, issue: dict, ctx: Context) -> tuple[dict | None, list[str]]:
    """The record an issue opened with form ``kind`` asks for, or the reasons it cannot be made."""
    agent = None
    if answers.get("who") == "agent" or answers.get("agent"):
        agent = rec.parse_agent(answers.get("agent") or "unknown agent")
    by = author_of(issue.get("user", {}), agent, answers.get("involvement") or "unknown")
    return record_for(kind, answers, by, iso(issue.get("created_at", "")),
                      {"kind": "issue", "ref": f"{ctx.repo}#{issue['number']}"}, ctx)


def describe(r: dict) -> str:
    s = r["subject"]
    if r["kind"] == "review":
        what = {"accept": "review", "problem": f"problem ({r.get('category')})",
                "question": "question"}[r["verdict"]]
    else:
        what = {"challenge": "challenge (a proposed test)", "test": f"test `{r.get('test', {}).get('name')}`",
                "named": f"name \u201c{r.get('name')}\u201d"}[r["kind"]]
    by = rec.who(r["by"])
    return (f"Recorded {what} `{r['id']}` of `{s['name']}` at `{s['commit'][:12]}` by {by} "
            f"(meaning hash `{s.get('hashes', {}).get('meaning', '?')}`).")


FOOTER = {
    "accept": "It counts from now on. If `{name}`, or anything it rests on, changes, it goes stale, "
              "and the page says so. Comment `/withdraw` here to take it back.",
    "problem": "It stays open until you or a maintainer comment `/fixed <commit>`, `/intended` or "
               "`/invalid`, or close this issue (as completed: fixed; as not planned: not a problem). "
               "Comments here are recorded as replies.",
    "question": "Answers are comments here, and are recorded. You or a maintainer close it with "
                "`/answered`.",
    "challenge": "It stays open until someone proves it in the library and comments `/met <the declaration "
                 "that proves it>`: the page then lists that declaration as a test, and checks at every "
                 "commit that it is still there without `sorry`. `/failed` says `{name}` does not have the "
                 "property (then report the problem); you or a maintainer can also comment `/declined`.",
    "test": "Lean checks it at every commit: the page shows it as passing while it is there without "
            "`sorry`. Comment `/withdraw` here to take it back.",
    "named": "The page shows the name beside `{name}`. Comment `/withdraw` here to take it back.",
}


def kind_key(r: dict) -> str:
    return r["verdict"] if r["kind"] == "review" else r["kind"]


def process_issue(issue: dict, ctx: Context, announce: bool = True) -> tuple[Outcome, dict | None]:
    """The record an issue asks for, unless the store has it already. Returns the outcome and the
    issue's record (new or existing), if any."""
    out = Outcome()
    kind = forms.kind_of(issue.get("labels", []))
    if kind is None or issue.get("pull_request"):
        return out, None
    if kind == "status":
        return process_status_issue(issue, ctx, announce), None
    ref = f"{ctx.repo}#{issue['number']}"
    existing = [r for r in ctx.store.from_origin(ref) if r.get("kind") in ("review", "challenge", "test", "named")]
    if existing:
        return out, existing[0]
    answers = forms.parse(kind, issue.get("body", ""), ctx.store.rubric)
    n = issue["number"]
    try:
        r, errs = review_record(kind, answers, issue, ctx)
    except DatasetMissing as e:
        # Not the author's mistake: the dataset of a new commit is built after it. The next run, or
        # the next sweep, reads the issue again.
        if announce:
            out.replies.append((n, f"There is no dataset of the library at `{e.commit[:12] or 'its latest commit'}` "
                                   "yet (one is built after each commit). This will be recorded as soon as it is."))
        return out, None
    if r is None:
        if announce:
            out.replies.append((n, "Not recorded yet:\n\n" + "\n".join(f"- {e}" for e in errs) +
                                "\n\nEdit the issue to fix it, and it will be read again."))
            out.labels.append((n, ["evidence:needs-fix"], []))
        return out, None
    out.records.append(r)
    k = kind_key(r)
    footer = FOOTER[k].format(name=r["subject"]["name"])
    if k == "accept" and r["by"]["kind"] == "agent":
        footer = footer.replace("It counts from now on.", "It is recorded as an AI agent's review: each reader "
                                "chooses whether those count.")
    out.replies.append((n, describe(r) + "\n\n" + footer))
    if k in ("accept", "test", "named"):
        out.labels.append((n, [], ["evidence:needs-fix"]))
        out.close.append((n, "completed"))
    else:
        out.labels.append((n, ["evidence:open"], ["evidence:needs-fix"]))
    return out, r


# --- comments ----------------------------------------------------------------------------------

def command_of(text: str) -> tuple[str, list[str], str] | None:
    """``/fixed abc123 the note`` as ("fixed", ["abc123"], "the note"): the command on the first
    non-empty line, and what follows. ``/met <declaration>`` takes the declaration as its argument."""
    lines = [l for l in REVIEWED_BY_MARK.sub("", AGENT_MARK.sub("", text or "")).strip().splitlines()]
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
    elif m.group(1) == "met":
        cm = re.match(r"^`?([^\s`]+)`?\s*(.*)$", rest)
        if cm:
            args, rest = [cm.group(1)], cm.group(2)
    note = "\n".join([rest.lstrip("—-: ").strip()] + lines[1:]).strip()
    return m.group(1), args, note


#: The state a command sets, and what the reply calls it.
STATE_OF = {"withdraw": "withdrawn", "fixed": "fixed", "intended": "intended", "invalid": "invalid",
            "answered": "answered", "met": "met", "failed": "failed", "declined": "declined",
            "reopen": "reopened"}
LABEL_OF = {"withdrawn": "withdrawn", "fixed": "fixed", "intended": "intended as it is", "invalid": "invalid",
            "answered": "answered", "met": "met", "failed": "failed: the declaration does not have the property",
            "declined": "declined", "reopened": "reopened"}
ALIASES = {"withdrawn": "withdraw", "fix": "fixed", "answer": "answered", "reopened": "reopen",
           "not a problem": "invalid", "intended as it is": "intended", "decline": "declined", "fail": "failed"}
WHAT_OF = {"accept": "a review", "problem": "a problem", "question": "a question", "challenge": "a challenge",
           "test": "a test", "named": "a name"}


def decide(ctx: Context, target: dict, command: str, login: str, association: str) -> tuple[str | None, str | None]:
    """The state ``command`` sets on ``target`` when ``login`` asks, or why it may not."""
    state = STATE_OF.get(command)
    if state is None:
        return None, f"`{command}` is not a change intake knows"
    what = rec.target_kind(target)
    if what not in rec.STATE_TARGETS[state]:
        allowed = ", ".join(WHAT_OF.get(k, k) for k in rec.STATE_TARGETS[state])
        return None, f"`/{command}` is for {allowed}, and this is {WHAT_OF.get(what, what)}"
    author = (target.get("by", {}).get("identity") or {}).get("id") == login
    maintainer = association in MAINTAINER_ASSOCIATIONS or login in ctx.maintainers
    if state == "withdrawn":
        return (state, None) if author else (None, f"only the author of {WHAT_OF.get(what, what)} can withdraw it")
    return (state, None) if author or maintainer else \
        (None, f"only the author of {WHAT_OF.get(what, what)} or a maintainer can mark it {LABEL_OF[state]}")


def met_test(ctx: Context, name: str) -> tuple[dict | None, str | None]:
    """The key of the declaration that meets a challenge, from the latest dataset, or why not."""
    try:
        ds = ctx.dataset("")
    except Exception:
        return None, "there is no dataset of the library yet to find the declaration in"
    d = ds.by_name.get(name)
    if d is None:
        return None, not_found(name, ds)
    key = rec.subject_from_decl(d, ds)
    return {k: key[k] for k in ("name", "module", "commit", "hashes") if k in key}, None


def issue_number(target: dict, repo: str) -> int | None:
    """The number of the issue a record came from, if it came from one of ``repo``'s issues."""
    ref = (target.get("origin") or {}).get("ref", "")
    if (target.get("origin") or {}).get("kind") == "issue" and ref.startswith(repo + "#"):
        n = ref.split("#", 1)[1]
        return int(n) if n.isdigit() else None
    return None


def on_target_issue(out: "Outcome", n: int, state: str) -> None:
    """What a status does to the record's own issue: reopen it, or close it."""
    if state == "reopened":
        out.reopen.append(n)
        out.labels.append((n, ["evidence:open"], []))
    else:
        out.labels.append((n, [], ["evidence:open"]))
        out.close.append((n, "completed" if state in ("fixed", "answered", "met") else "not planned"))


def status_record(target: dict, state: str, by: dict, at: str, origin: dict, note: str = "",
                  commit: str = "", test: dict | None = None) -> dict:
    r = {"schema": rec.SCHEMA, "kind": "status", "target": target["id"], "state": state, "by": by,
         "at": at, "origin": origin}
    if note:
        r["text"] = note
    if commit and state == "fixed":
        r["commit"] = commit
    if test and state == "met":
        r["test"] = test
    return rec.with_id(r)


def said(state: str, r: dict) -> str:
    extra = f" (`{r['commit'][:12]}`)" if r.get("commit") else (f" by `{r['test']['name']}`" if r.get("test") else "")
    return f"**{LABEL_OF[state]}**{extra}"


def process_status_issue(issue: dict, ctx: Context, announce: bool = True) -> Outcome:
    """An issue opened with the status form: the change it asks for, if its author may make it."""
    out = Outcome()
    ref = f"{ctx.repo}#{issue['number']}"
    if ctx.store.from_origin(ref):
        return out
    n = issue["number"]
    a = forms.parse("status", issue.get("body", ""))
    rid = clean(a.get("record"))
    command = clean(a.get("action")).lstrip("/").lower()
    command = ALIASES.get(command, command)
    target = next((r for r in ctx.store.records if r.get("id") == rid and r.get("kind") in
                   ("review", "challenge", "test", "named")), None)
    user = issue.get("user", {})
    test = None
    if target is None:
        state, refusal = None, f"no record has the id `{rid}` in this store"
    else:
        state, refusal = decide(ctx, target, command, user.get("login", ""), issue.get("author_association", ""))
        if state == "met" and clean(a.get("test")):
            test, refusal = met_test(ctx, clean(a.get("test")))
            if test is None:
                state = None
    if state is None:
        if announce:
            out.replies.append((n, f"Not recorded: {refusal}."))
            out.close.append((n, "not planned"))
        return out
    agent = rec.parse_agent(a["agent"]) if (a.get("who") == "agent" or a.get("agent")) else None
    by = author_of(user, agent)
    r = status_record(target, state, by, iso(issue.get("created_at", "")), {"kind": "issue", "ref": ref},
                      a.get("note", ""), clean(a.get("commit")), test)
    out.records.append(r)
    what = f"`{target['id']}` ({WHAT_OF.get(rec.target_kind(target), '')} of `{target['subject']['name']}`)"
    out.replies.append((n, f"Recorded: {what} is now {said(state, r)}, by {rec.who(by)}."))
    out.close.append((n, "completed"))
    home = issue_number(target, ctx.repo)
    if home is not None:
        out.replies.append((home, f"Now {said(state, r)}, by {rec.who(by)}, from #{n}."
                                  + (f" {r['text']}" if r.get("text") else "")))
        on_target_issue(out, home, state)
    return out


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
    at = iso(comment.get("created_at", ""))
    origin = {"kind": "comment", "ref": ref}
    n = issue["number"]
    cmd = command_of(text)
    if cmd is not None:
        name, args, note = cmd
        state, refusal = decide(ctx, target, name, user.get("login", ""), comment.get("author_association", ""))
        test = None
        if state == "met" and args:
            test, refusal = met_test(ctx, args[0])
            if test is None:
                state = None
        if state is not None:
            r = status_record(target, state, by, at, origin, note, args[0] if name == "fixed" and args else "", test)
            out.records.append(r)
            out.replies.append((n, f"Recorded: `{target['id']}` is now {said(state, r)}, by {rec.who(by)}."))
            on_target_issue(out, n, state)
            return out
        if announce:
            out.replies.append((n, f"Not recorded as a status: {refusal}. The comment is kept as a reply."))
    # A reply: the discussion, and answers to questions.
    body = REVIEWED_BY_MARK.sub("", AGENT_MARK.sub("", text)).strip()
    if not body:
        return out
    r = rec.with_id({"schema": rec.SCHEMA, "by": by, "at": at, "origin": origin, "kind": "comment",
                     "text": body, "links": {"replies_to": target["id"]},
                     **({"subject": target["subject"]} if target.get("subject") else {})})
    out.records.append(r)
    if comment.get("id"):
        out.reactions.append((comment["id"], "eyes"))
    return out


# --- closing and reopening by hand ---------------------------------------------------------------

#: The state a close by hand means, by what the record is and how the issue was closed.
CLOSED_AS = {("problem", "completed"): "fixed", ("problem", "not_planned"): "invalid",
             ("problem", "duplicate"): "invalid", ("question", "completed"): "answered",
             ("question", "not_planned"): "answered", ("question", "duplicate"): "answered",
             ("challenge", "completed"): "met", ("challenge", "not_planned"): "declined",
             ("challenge", "duplicate"): "declined"}


def process_event(event: dict, issue: dict, target: dict, ctx: Context, state_now: str,
                  announce: bool = True) -> Outcome:
    """A close or reopen of the record's issue by a person (or an agent through its account), as a
    status. GitHub lets only the issue's author and the maintainers do either."""
    out = Outcome()
    actor = event.get("actor") or {}
    if event.get("event") not in ("closed", "reopened") or actor.get("login") in BOTS or \
            actor.get("login", "").endswith("[bot]"):
        return out
    ref = f"{ctx.repo}#{issue['number']}/event/{event.get('id')}"
    if ctx.store.from_origin(ref):
        return out
    what = rec.target_kind(target)
    if event["event"] == "reopened":
        state = "reopened" if what in rec.STATE_TARGETS["reopened"] else None
    else:
        state = CLOSED_AS.get((what, event.get("state_reason") or "completed"))
    if state is None or state == state_now or (state == "reopened" and state_now == "open"):
        return out  # nothing to say (an acceptance closes by itself), or already said by a command
    by = author_of(actor, None)
    note = "closed as a duplicate" if event.get("state_reason") == "duplicate" else ""
    r = status_record(target, state, by, iso(event.get("created_at", "")), {"kind": "event", "ref": ref}, note)
    out.records.append(r)
    if announce:
        out.replies.append((issue["number"], f"Recorded: `{target['id']}` is now {said(state, r)}, "
                                             f"by {rec.who(by)} ({'reopening' if state == 'reopened' else 'closing'} this issue)."))
    out.labels.append((issue["number"], ["evidence:open"] if state == "reopened" else [],
                       [] if state == "reopened" else ["evidence:open"]))
    return out


# --- the bulk issue ------------------------------------------------------------------------------

BULK_LINE = re.compile(r"^\s*(Reviewed-by|Test|Named|Challenge)\s*:\s*`?([^\s`]+)`?(?:\s+(?:—|–|--|-)\s+(.*?))?\s*$")
# A name may hold a dash of its own (Atkin–Lehner), so only a spaced em dash, or --, separates fields.
FIELD_SEP = re.compile(r"\s+(?:—|--)\s+")


def bulk_lines(text: str) -> list[tuple[int, str, str, str]]:
    """The record lines of a comment: (line number from 1, keyword, declaration, the rest)."""
    out = []
    for k, line in enumerate((text or "").splitlines(), 1):
        m = BULK_LINE.match(line)
        if m:
            out.append((k, m.group(1), m.group(2), (m.group(3) or "").strip()))
    return out


def bulk_answers(keyword: str, decl: str, rest: str) -> tuple[str, dict]:
    parts = FIELD_SEP.split(rest) if rest else []
    if keyword == "Reviewed-by":
        return "review", {"decl": decl, "rationale": rest}
    if keyword == "Test":
        test = parts[0].strip("`") if parts else ""
        return "test", {"decl": decl, "test": test, "checks": " — ".join(parts[1:])}
    if keyword == "Named":
        return "named", {"decl": decl, "name": parts[0] if parts else "", "about": " — ".join(parts[1:])}
    return "challenge", {"decl": decl, "property": rest}


def process_bulk_comment(comment: dict, issue: dict, ctx: Context, announce: bool = True) -> Outcome:
    """One record per line of a comment on the bulk issue; one reply listing what was recorded."""
    out = Outcome()
    user = comment.get("user", {})
    if user.get("login") in BOTS:
        return out
    base_ref = comment.get("html_url") or f"{ctx.repo}#{issue['number']}/comment/{comment.get('id')}"
    text = comment.get("body") or ""
    agent = parse_agent_mark(text)
    by = author_of(user, agent)
    lines = bulk_lines(text)
    done, refused = [], []
    for k, keyword, decl, rest in lines:
        ref = f"{base_ref}/line/{k}"
        if ctx.store.from_origin(ref):
            continue
        kind, answers = bulk_answers(keyword, decl, rest)
        try:
            r, errs = record_for(kind, answers, by, iso(comment.get("created_at", "")),
                                 {"kind": "comment", "ref": ref}, ctx)
        except DatasetMissing:
            refused.append(f"line {k}: there is no dataset of the library yet; it will be read again")
            continue
        if r is None:
            refused.append(f"line {k} (`{decl}`): " + "; ".join(errs))
            continue
        out.records.append(r)
        ctx.store.add([r])
        done.append(f"- {describe(r)}")
    if announce and (done or refused):
        msg = (f"Recorded {len(done)} of {len(lines)} lines:\n\n" + "\n".join(done)) if done else ""
        if refused:
            msg += ("\n\n" if msg else "") + "Not recorded:\n\n" + "\n".join(f"- {e}" for e in refused) + \
                "\n\nPost the corrected lines in a new comment."
        out.replies.append((issue["number"], msg))
    return out


def is_bulk(issue: dict) -> bool:
    return any((l.get("name") if isinstance(l, dict) else l) == BULK_LABEL for l in issue.get("labels", []))


def process(issue: dict, comments: list[dict], ctx: Context, announce: bool = True,
            events: list[dict] = ()) -> Outcome:
    """Everything an issue, its comments and its events ask for that the store does not have yet,
    in order. The records are added to the store as they are made, so that later comments see
    earlier ones."""
    if is_bulk(issue):
        out = Outcome()
        for c in sorted(comments, key=lambda c: c.get("created_at", "")):
            out.merge(process_bulk_comment(c, issue, ctx, announce))
        return out
    out, target = process_issue(issue, ctx, announce)
    ctx.store.add(out.records)
    if target is None:
        return out
    items = [("comment", c.get("created_at", ""), c) for c in comments] + \
        [("event", e.get("created_at", ""), e) for e in events if e.get("event") in ("closed", "reopened")]
    for what, _, item in sorted(items, key=lambda x: x[1]):
        if what == "comment":
            o = process_comment(item, issue, target, ctx, announce)
        else:
            o = process_event(item, issue, target, ctx, current_state(ctx, target), announce)
        ctx.store.add(o.records)
        out.merge(o)
    return out


def current_state(ctx: Context, target: dict) -> str:
    """A record's state from the statuses in the store: ``open`` for a problem, question or
    challenge with none (or reopened), ``stands`` otherwise."""
    statuses = sorted((r for r in ctx.store.records if r.get("kind") == "status" and r.get("target") == target["id"]),
                      key=lambda r: r.get("at", ""))
    if statuses and statuses[-1].get("state") != "reopened":
        return statuses[-1]["state"]
    return "open" if rec.target_kind(target) in ("problem", "question", "challenge") else "stands"
