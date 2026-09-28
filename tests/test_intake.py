"""Intake, on the extractor's fixture (tests/vectors/fixture-b, a dataset of a small library).

Issues and comments are given as the GitHub API returns them; nothing here talks to GitHub.

Run with ``python3 -m unittest discover -s tests``.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from evidence_core import Dataset, Evidence, Policy
from evidence_core import store as sto

from evidence_store import forms
from evidence_store.cli import main as cli
from evidence_store.intake import Context, process

V = Path(__file__).parent / "vectors"
B = Dataset.load(V / "fixture-b")
F = "Fixture."
REPO = "owner/lib"


def user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def issue(number: int, kind: str, answers: dict, by: str = "alice", at: str = "2026-09-26T10:00:00Z",
          association: str = "NONE") -> dict:
    return {"number": number, "title": forms.FORMS[kind]["title"] + answers.get("decl", ""),
            "body": forms.body(kind, answers), "user": user(by), "created_at": at,
            "labels": [{"name": forms.FORMS[kind]["label"]}], "author_association": association}


def event(eid: int, what: str, by: str, at: str, reason: str | None = None) -> dict:
    return {"id": eid, "event": what, "actor": user(by, "Bot" if by.endswith("[bot]") else "User"),
            "created_at": at, "state_reason": reason}


def comment(cid: int, body: str, by: str = "bob", at: str = "2026-09-26T11:00:00Z",
            association: str = "NONE", kind: str = "User") -> dict:
    return {"id": cid, "html_url": f"https://github.com/{REPO}/issues/1#issuecomment-{cid}",
            "body": body, "user": user(by, kind), "created_at": at, "author_association": association}


class FormTests(unittest.TestCase):
    def test_round_trip(self):
        answers = {"decl": F + "double", "reference": "Knuth, TAOCP §1.2", "caveats": "F3: at 0",
                   "checked": {c: c in ("F1", "F3") for c, _ in forms.CHECKS},
                   "involvement": "outsider", "who": "person"}
        self.assertEqual(forms.parse("review", forms.body("review", answers)), answers)

    def test_what_github_writes(self):
        # An issue body as GitHub writes it from the web form: unanswered fields and checkboxes, ticked
        # ones as `[x]` (from a real submission).
        body = ("### Declaration\n\nFixture.double\n\n### Commit\n\n_No response_\n\n"
                "### Compared with\n\n_No response_\n\n### What you checked\n\n"
                "- [x] F1: the intended object, not a different notion\n- [ ] F2: its conventions: normalization, indexing, signs\n\n"
                "### Caveats\n\n_No response_\n\n### Why\n\nIt doubles.\n\n### You are\n\nan outsider to this library\n\n"
                "### Written by\n\nme, a person\n\n### Agent\n\n_No response_\n")
        self.assertEqual(forms.parse("review", body),
                         {"decl": F + "double", "checked": {"F1": True, "F2": False},
                          "rationale": "It doubles.", "involvement": "outsider", "who": "person"})

    def test_templates(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        for kind in forms.FORMS:
            t = yaml.safe_load(forms.template(kind))
            self.assertEqual(t["labels"], [forms.FORMS[kind]["label"]])
            ids = [f["id"] for f in t["body"] if "id" in f]
            self.assertEqual(ids, [f["id"] for f in forms.FORMS[kind]["fields"]])


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sto.Store.init(Path(self.tmp.name) / "evidence", sto.default_config(REPO, "Fixture"))
        self.ctx = Context(repo=REPO, store=self.store, dataset=lambda commit: B,
                           maintainers=frozenset({"maint"}))

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self) -> Evidence:
        return Evidence.resolve(self.store.records, B)

    def test_review(self):
        i = issue(1, "review", {"decl": F + "double", "reference": "see https://example.org/d.",
                                "checked": {"F1": True, "F4": False}, "caveats": "F3: odd at 0\nplain note",
                                "involvement": "outsider", "who": "person"})
        out = process(i, [], self.ctx)
        [r] = out.records
        self.assertEqual(r["by"], {"kind": "person", "identity": {"kind": "github", "id": "alice"},
                                   "involvement": "outsider"})
        self.assertEqual(r["verdict"], "accept")
        self.assertEqual(r["subject"]["hashes"]["meaning"], B.by_name[F + "double"].meaning)
        self.assertEqual(r["reference"]["url"], "https://example.org/d")
        # The form lists every failure mode: what is not ticked is recorded as not checked.
        self.assertEqual(r["checked"], {c: "checked" if c == "F1" else "unchecked" for c, _ in forms.CHECKS})
        self.assertEqual(r["caveats"], [{"category": "F3", "note": "odd at 0"},
                                        {"category": "other", "note": "plain note"}])
        self.assertEqual(r["origin"], {"kind": "issue", "ref": f"{REPO}#1"})
        self.assertEqual(out.close, [(1, "completed")])
        self.assertIn("Recorded review", out.replies[0][1])
        # Idempotent: the store has it, so reading the issue again adds nothing.
        self.assertEqual(process(i, [], self.ctx).records, [])
        self.assertEqual(len(sto.Store.load(self.store.root).records), 1)
        # Withdrawn by its author, and only by its author.
        out = process(i, [comment(10, "/withdraw", by="bob"), comment(11, "/withdraw — I misread it", by="alice",
                                                                        at="2026-09-26T12:00:00Z")], self.ctx)
        kinds = [(r["kind"], r.get("state")) for r in out.records]
        self.assertEqual(kinds, [("comment", None), ("status", "withdrawn")])
        self.assertEqual(out.records[1]["text"], "I misread it")
        self.assertEqual(self.ev().counting_accepts(F + "double", Policy()), [])

    def test_problem_lifecycle(self):
        i = issue(2, "problem", {"decl": F + "triple", "category": "F3", "rationale": "wrong at 0",
                                 "who": "person"}, by="carol")
        cs = [comment(20, "Are you sure? triple 0 = 0.", by="dave"),
              comment(21, "/fixed 0123abcd", by="dave", at="2026-09-26T12:00:00Z"),
              comment(22, "/fixed 0123abcd now requires n > 0", by="carol", at="2026-09-26T13:00:00Z"),
              comment(23, "/reopen still wrong", by="maint", at="2026-09-26T14:00:00Z"),
              comment(24, "/invalid", by="erin", association="COLLABORATOR", at="2026-09-26T15:00:00Z"),
              comment(25, "done", by="github-actions[bot]", kind="Bot", at="2026-09-26T16:00:00Z")]
        out = process(i, cs, self.ctx)
        seen = [(r["kind"], r.get("state"), r["by"]["identity"]["id"]) for r in out.records]
        self.assertEqual(seen, [("review", None, "carol"), ("comment", None, "dave"),
                                ("comment", None, "dave"),       # refused: kept as a reply
                                ("status", "fixed", "carol"), ("status", "reopened", "maint"),
                                ("status", "invalid", "erin")])
        fixed = out.records[3]
        self.assertEqual((fixed["commit"], fixed["text"]), ("0123abcd", "now requires n > 0"))
        self.assertTrue(any("only the author of a problem or a maintainer" in t for _, t in out.replies))
        self.assertEqual(out.labels[0], (2, ["evidence:open"], ["evidence:needs-fix"]))
        self.assertIn(2, out.reopen)
        problem = out.records[0]
        self.assertEqual(self.ev().state(problem["id"]), "invalid")
        self.assertEqual(self.ev().replies[problem["id"]][0]["text"], "Are you sure? triple 0 = 0.")

    def test_question_answered_by_an_agent_and_closed(self):
        i = issue(3, "question", {"decl": F + "double", "question": "What is `double 0`?", "who": "person"})
        mark = "<!-- agent: tool=Claude Code; model=claude-opus-5-5; session=s1 -->"
        cs = [comment(30, f"`double 0 = 0`, by `rfl`.\n\n{mark}", by="alice"),
              comment(31, "/answered", by="alice", at="2026-09-26T12:00:00Z")]
        out = process(i, cs, self.ctx)
        answer = out.records[1]
        self.assertEqual(answer["by"]["kind"], "agent")
        self.assertEqual(answer["by"]["agent"], {"tool": "Claude Code", "model": "claude-opus-5-5", "session": "s1"})
        self.assertEqual(answer["text"], "`double 0 = 0`, by `rfl`.")
        # The mark says an agent wrote the answer; the asker closing it is the asker.
        self.assertEqual(out.records[2]["by"]["kind"], "person")
        self.assertEqual(self.ev().open_questions(F + "double"), [])

    def test_agents_need_a_rationale_and_the_issue_can_be_fixed(self):
        answers = {"decl": F + "double", "who": "agent", "agent": "Claude Code, claude-opus-5-5"}
        out = process(issue(4, "review", answers), [], self.ctx)
        self.assertEqual(out.records, [])
        self.assertIn("needs its text", out.replies[0][1])
        self.assertEqual(out.labels, [(4, ["evidence:needs-fix"], [])])
        out = process(issue(4, "review", {**answers, "rationale": "It is n + n."}), [], self.ctx)
        [r] = out.records
        self.assertEqual((r["by"]["kind"], r["by"]["agent"]["model"]), ("agent", "claude-opus-5-5"))
        self.assertIn("each reader chooses whether those count", out.replies[0][1])
        self.assertEqual(r["by"]["identity"]["id"], "alice")

    def test_supersedes_and_disagreement(self):
        first = process(issue(5, "review", {"decl": F + "triple", "who": "person"}), [], self.ctx).records[0]
        second = process(issue(6, "review", {"decl": F + "triple", "who": "person"},
                               at="2026-09-27T10:00:00Z"), [], self.ctx).records[0]
        self.assertEqual(second["links"], {"supersedes": first["id"]})
        other = process(issue(7, "problem", {"decl": F + "triple", "category": "F1", "rationale": "no",
                                             "who": "person"}, by="zed"), [], self.ctx).records[0]
        self.assertNotIn("links", other)
        ev = self.ev()
        self.assertEqual(ev.superseded_by, {first["id"]: second["id"]})
        self.assertTrue(ev.disagreement(F + "triple"))

    def test_a_challenge_is_met_by_a_declaration_of_the_library(self):
        i = issue(40, "challenge", {"decl": F + "double", "property": "`double 0 = 0`",
                                    "statement": "double 0 = 0", "catches": "an offset",
                                    "modes": {"F3": True, "F1": False}, "who": "person"}, by="carol")
        cs = [comment(41, "/met Fixture.double_zero", by="dave"),           # not the author: refused
              comment(42, "/met Fixture.nope", by="carol", at="2026-09-26T12:00:00Z"),
              comment(43, "/met `Fixture.double_zero` by rfl", by="carol", at="2026-09-26T13:00:00Z")]
        out = process(i, cs, self.ctx)
        c = out.records[0]
        self.assertEqual((c["kind"], c["text"], c["statement"], c["modes"]),
                         ("challenge", "`double 0 = 0`", "double 0 = 0", ["F3"]))
        self.assertEqual(out.labels[0], (40, ["evidence:open"], ["evidence:needs-fix"]))
        statuses = [r for r in out.records if r["kind"] == "status"]
        self.assertEqual([(r["state"], r["test"]["name"], r.get("text")) for r in statuses],
                         [("met", F + "double_zero", "by rfl")])
        self.assertTrue(any("is not a declaration of the library" in t for _, t in out.replies))
        self.assertIn((40, "completed"), out.close)
        ev = self.ev()
        self.assertEqual(ev.challenges(F + "double"), [(c, "met")])
        self.assertEqual([(t["test"], t["result"]) for t in ev.tests(F + "double")], [(F + "double_zero", "passes")])

    def test_a_challenge_can_fail_or_be_declined(self):
        i = issue(44, "challenge", {"decl": F + "triple", "property": "triple 1 = 4", "who": "person"}, by="carol")
        out = process(i, [comment(45, "/failed triple 1 = 3", by="maint")], self.ctx)
        self.assertEqual([r.get("state") for r in out.records], [None, "failed"])
        self.assertIn((44, "not planned"), out.close)
        j = issue(46, "challenge", {"decl": F + "triple", "property": "p", "who": "person"}, by="carol")
        out = process(j, [comment(47, "/declined", by="erin", association="MEMBER")], self.ctx)
        self.assertEqual([r.get("state") for r in out.records], [None, "declined"])
        # /fixed is for problems
        out = process(j, [comment(48, "/fixed", by="carol", at="2026-09-26T12:00:00Z")], self.ctx)
        self.assertTrue(any("is for a problem, and this is a challenge" in t for _, t in out.replies))

    def test_tests_and_names(self):
        t = issue(50, "test", {"decl": F + "double", "test": "`Fixture.double_zero`", "checks": "the value at 0",
                               "who": "person"})
        out = process(t, [], self.ctx)
        [r] = out.records
        self.assertEqual((r["kind"], r["test"]["name"], r["text"]), ("test", F + "double_zero", "the value at 0"))
        self.assertEqual(r["test"]["hashes"]["meaning"], B.by_name[F + "double_zero"].meaning)
        self.assertEqual(out.close, [(50, "completed")])
        bad = issue(51, "test", {"decl": F + "double", "test": "Fixture.nope", "who": "person"})
        out = process(bad, [], self.ctx)
        self.assertEqual(out.records, [])
        self.assertIn("the test: `Fixture.nope` is not a declaration", out.replies[0][1])
        n = issue(52, "named", {"decl": F + "triple_pos", "name": "Positivity of triple", "what": "result",
                                "about": "It is positive.", "source": "https://example.org/roadmap", "who": "person"})
        [r] = process(n, [], self.ctx).records
        self.assertEqual((r["kind"], r["name"], r["what"], r["reference"]),
                         ("named", "Positivity of triple", "result", {"url": "https://example.org/roadmap"}))
        self.assertEqual(self.ev().named(F + "triple_pos"), [r])

    def test_a_problem_can_say_its_fix(self):
        i = issue(55, "problem", {"decl": F + "triple", "category": "F3", "rationale": "0", "fix": "def triple := 3 * n",
                                  "who": "person"})
        [r] = process(i, [], self.ctx).records
        self.assertEqual(r["fix"], "def triple := 3 * n")

    def test_closing_and_reopening_by_hand(self):
        i = issue(60, "problem", {"decl": F + "triple", "category": "F3", "rationale": "0", "who": "person"}, by="carol")
        evs = [event(1, "closed", "carol", "2026-09-26T12:00:00Z", "completed"),
               event(2, "reopened", "maint", "2026-09-26T13:00:00Z"),
               event(3, "closed", "github-actions[bot]", "2026-09-26T14:00:00Z", "completed"),
               event(4, "closed", "maint", "2026-09-26T15:00:00Z", "not_planned")]
        out = process(i, [], self.ctx, events=evs)
        self.assertEqual([(r.get("state"), r["by"]["identity"]["id"]) for r in out.records],
                         [(None, "carol"), ("fixed", "carol"), ("reopened", "maint"), ("invalid", "maint")])
        self.assertEqual(out.records[1]["origin"], {"kind": "event", "ref": f"{REPO}#60/event/1"})
        # Idempotent, and a close that follows a command (the bot closing after /fixed) says nothing new.
        self.assertEqual(process(i, [], self.ctx, events=evs).records, [])
        j = issue(61, "problem", {"decl": F + "triple", "category": "F3", "rationale": "0", "who": "person"}, by="carol")
        out = process(j, [comment(62, "/fixed", by="carol", at="2026-09-26T12:00:00Z")], self.ctx,
                      events=[event(5, "closed", "carol", "2026-09-26T12:00:05Z", "completed")])
        self.assertEqual([r.get("state") for r in out.records if r["kind"] == "status"], ["fixed"])
        k = issue(63, "challenge", {"decl": F + "triple", "property": "p", "who": "person"}, by="carol")
        out = process(k, [], self.ctx, events=[event(6, "closed", "carol", "2026-09-26T12:00:00Z", "not_planned")])
        self.assertEqual([r.get("state") for r in out.records], [None, "declined"])

    def test_the_bulk_issue(self):
        bulk = {"number": 1, "title": "Reviews", "body": "", "user": user("maint"), "created_at": "2026-09-01T00:00:00Z",
                "labels": [{"name": "evidence:bulk"}], "author_association": "OWNER"}
        text = ("Some reviews:\n"
                "Reviewed-by: Fixture.double — the doubling map, checked at 0 and 1\n"
                "Test: `Fixture.double` — `Fixture.double_zero` — the value at 0\n"
                "Named: Fixture.triple_pos — Positivity of triple — Atkin–Lehner-free — it is positive\n"
                "Challenge: Fixture.triple — triple 2 = 6\n"
                "Reviewed-by: Fixture.nope — typo\n"
                '<!--reviewed-by:v1 {"agent": "Claude Code, claude-opus-5-5, session s9"}-->')
        out = process(bulk, [comment(70, text, by="op")], self.ctx)
        kinds = [(r["kind"], r["subject"]["name"]) for r in out.records]
        self.assertEqual(kinds, [("review", F + "double"), ("test", F + "double"), ("named", F + "triple_pos"),
                                 ("challenge", F + "triple")])
        self.assertTrue(all(r["by"]["kind"] == "agent" and r["by"]["agent"]["session"] == "s9" for r in out.records))
        self.assertEqual(out.records[2]["name"], "Positivity of triple")
        self.assertEqual(out.records[2]["text"], "Atkin–Lehner-free — it is positive")
        self.assertEqual(out.records[0]["origin"]["ref"],
                         f"https://github.com/{REPO}/issues/1#issuecomment-70/line/2")
        [(n, reply)] = out.replies
        self.assertIn("Recorded 4 of 5 lines", reply)
        self.assertIn("line 6 (`Fixture.nope`)", reply)
        self.assertEqual(process(bulk, [comment(70, text, by="op")], self.ctx).records, [])

    def test_status_form(self):
        review = process(issue(10, "review", {"decl": F + "double", "who": "person"}), [], self.ctx).records[0]
        problem = process(issue(11, "problem", {"decl": F + "triple", "category": "F3", "rationale": "at 0",
                                                "who": "person"}, by="carol"), [], self.ctx).records[0]
        # Someone else cannot withdraw alice's review; an unknown id is refused too.
        out = process(issue(12, "status", {"record": review["id"], "action": "withdraw"}, by="bob"), [], self.ctx)
        self.assertEqual(out.records, [])
        self.assertEqual(out.close, [(12, "not planned")])
        self.assertIn("only the author of a review can withdraw it", out.replies[0][1])
        out = process(issue(13, "status", {"record": "0" * 16, "action": "withdraw"}), [], self.ctx)
        self.assertIn("no record has the id", out.replies[0][1])
        # Its author can: the status is recorded, and said on the review's own issue.
        out = process(issue(14, "status", {"record": review["id"], "action": "withdraw", "note": "a test"},
                            at="2026-09-26T12:00:00Z"), [], self.ctx)
        [st] = out.records
        self.assertEqual((st["kind"], st["target"], st["state"], st["text"]), ("status", review["id"], "withdrawn", "a test"))
        self.assertEqual(st["origin"], {"kind": "issue", "ref": f"{REPO}#14"})
        self.assertEqual([n for n, _ in out.replies], [14, 10])
        self.assertIn("from #14", out.replies[1][1])
        self.assertEqual(out.close, [(14, "completed"), (10, "not planned")])
        self.assertEqual(self.ev().counting_accepts(F + "double", Policy()), [])
        self.assertEqual(process(issue(14, "status", {"record": review["id"], "action": "withdraw"}), [], self.ctx).records, [])
        # A maintainer marks the problem fixed, with the commit; "/fixed" as typed works too.
        out = process(issue(15, "status", {"record": problem["id"], "action": "/fixed", "commit": "0123abcd"},
                            by="erin", association="MEMBER", at="2026-09-26T13:00:00Z"), [], self.ctx)
        [st] = out.records
        self.assertEqual((st["state"], st["commit"], st["by"]["identity"]["id"]), ("fixed", "0123abcd", "erin"))
        self.assertEqual(out.close, [(15, "completed"), (11, "completed")])
        self.assertEqual(self.ev().state(problem["id"]), "fixed")

    def test_a_commit_without_a_dataset_yet(self):
        def missing(commit):
            raise LookupError("release not found")
        ctx = Context(repo=REPO, store=self.store, dataset=missing)
        out = process(issue(9, "review", {"decl": F + "double", "commit": "abcdef123456", "who": "person"}), [], ctx)
        self.assertEqual((out.records, out.labels), ([], []))
        self.assertIn("will be recorded as soon as it is", out.replies[0][1])
        self.assertEqual(len(process(issue(9, "review", {"decl": F + "double", "commit": "abcdef123456", "who": "person"}),
                                     [], self.ctx).records), 1)

    def test_unknown_declaration(self):
        out = process(issue(8, "review", {"decl": F + "doubel", "who": "person"}), [], self.ctx)
        self.assertEqual(out.records, [])
        self.assertIn("Did you mean `Fixture.double`", out.replies[0][1])
        # In a sweep, errors are not repeated.
        self.assertEqual(process(issue(8, "review", {"decl": F + "doubel"}), [], self.ctx,
                                 announce=False).replies, [])


class CliTests(unittest.TestCase):
    def test_init_and_submit(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli(["init", "--repo", REPO, "--root", "Fixture", "--dir", d,
                                      "--pages-workflow", "pages.yml", "--claim", F + "triple_pos"]), 0)
            root = Path(d)
            self.assertEqual(json.loads((root / "evidence" / "store.json").read_text())["claims"],
                             [F + "triple_pos"])
            self.assertTrue((root / ".github" / "ISSUE_TEMPLATE" / "evidence-review.yml").exists())
            wf = (root / ".github" / "workflows" / "evidence-intake.yml").read_text()
            self.assertIn("!github.event.issue.pull_request", wf)
            self.assertIn("types: [opened, edited, closed, reopened]", wf)
            self.assertIn("pages-workflow: 'pages.yml'", wf)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli(["submit", "--repo", REPO, "--decl", F + "double", "--verdict", "accept",
                     "--checked", "F1,F2", "--rationale", "n + n", "--agent", "Claude Code, claude-opus-5-5",
                     "--dry-run"])
            title, body = buf.getvalue().split("\n\n", 1)
            self.assertEqual(title, "Review: " + F + "double")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli(["status", "--repo", REPO, "--record", "0123456789abcdef", "--action", "withdraw", "--dry-run"])
            st = forms.parse("status", buf.getvalue().split("\n\n", 1)[1])
            self.assertEqual((st["record"], st["action"], st["who"]), ("0123456789abcdef", "withdraw", "person"))
            a = forms.parse("review", body)
            self.assertEqual((a["who"], a["agent"], a["checked"]["F2"], a["checked"]["F3"]),
                             ("agent", "Claude Code, claude-opus-5-5", True, False))


class AddCommandTests(unittest.TestCase):
    def test_a_readers_export_gets_its_ids_in_the_store(self):
        from evidence_core import records as rec
        ds = Dataset.load(Path(__file__).parent / "vectors" / "fixture-b")
        d = next(x for x in ds.decls if x.is_project)
        r = {"schema": rec.SCHEMA, "kind": "review", "subject": rec.subject_from_decl(d, ds), "verdict": "accept",
             "by": {"kind": "person", "identity": {"kind": "github", "id": "alice"}}, "at": "2026-09-26T10:00:00Z",
             "origin": {"kind": "site", "ref": "https://example.org/site/"}}
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "evidence"
            sto.Store.init(store, sto.default_config("o/lib", "Lib"))
            f = Path(tmp) / "audit.jsonl"
            f.write_text(json.dumps(r) + "\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli(["add", str(f), "--store", str(store)]), 0)
                self.assertEqual(cli(["add", str(f), "--store", str(store)]), 0)
            self.assertIn("0 records added", buf.getvalue())
            [stored] = sto.Store.load(store).records
            self.assertEqual(stored["id"], rec.record_id(stored))


class DatasetCommandTests(unittest.TestCase):
    """`evidence-store dataset`: which release it fetches, and where it puts it (gh is replaced)."""

    def setUp(self):
        from evidence_store import github
        self.github, self.calls = github, []
        self.saved = github.gh, github.gh_json
        releases = [{"tagName": "dataset-bbb", "createdAt": "2026-09-02"}, {"tagName": "other", "createdAt": "2026-09-03"},
                    {"tagName": "dataset-aaa", "createdAt": "2026-09-01"}]
        github.gh_json = lambda *a: releases

        def gh(*args, **kw):
            self.calls.append(args)
            where = Path(args[args.index("-D") + 1])
            with tempfile.TemporaryDirectory() as src:
                (Path(src) / "meta.json").write_text("{}")
                import tarfile
                with tarfile.open(where / "dataset.tar.gz", "w:gz") as t:
                    t.add(Path(src) / "meta.json", arcname="meta.json")
            return ""
        github.gh = gh
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "evidence"
        sto.Store.init(self.store, sto.default_config("o/lib", "Lib", "o/data"))

    def tearDown(self):
        self.github.gh, self.github.gh_json = self.saved
        self.tmp.cleanup()

    def run_cli(self, *args) -> list[str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli(["dataset", "--store", str(self.store), *args]), 0)
        return buf.getvalue().split()

    def test_a_commit_is_fetched_from_where_the_store_says(self):
        out = Path(self.tmp.name) / "ds"
        self.assertEqual(self.run_cli("--commit", "0123456789abcdef", "--out", str(out)), [str(out)])
        self.assertEqual(self.calls[0][:5], ("release", "download", "dataset-0123456789ab", "-R", "o/data"))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["meta.json"])

    def test_every_dataset_oldest_first(self):
        out = Path(self.tmp.name) / "all"
        self.assertEqual(self.run_cli("--all", "--out", str(out)), [str(out / "dataset-aaa"), str(out / "dataset-bbb")])

    def test_a_repository_without_a_store(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli(["dataset", "--store", "/nonexistent", "--repo", "o/site", "--tag", "lml-{commit12}",
                                  "--commit", "abcdef", "--out", str(Path(self.tmp.name) / "x")]), 0)
        self.assertEqual(self.calls[0][2:5], ("lml-abcdef", "-R", "o/site"))


if __name__ == "__main__":
    unittest.main()
