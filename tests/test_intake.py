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
        # An issue body as GitHub writes it from the web form: unanswered fields and checkboxes.
        body = ("### Declaration\n\nFixture.double\n\n### Commit\n\n_No response_\n\n"
                "### Compared with\n\n_No response_\n\n### What you checked\n\n"
                "- [X] F1: the intended object, not a different notion\n- [ ] F2: its conventions: normalization, indexing, signs\n\n"
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
        self.assertEqual(out.records[1]["note"], "I misread it")
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
        self.assertEqual((fixed["commit"], fixed["note"]), ("0123abcd", "now requires n > 0"))
        self.assertTrue(any("only the reporter or a maintainer" in t for _, t in out.replies))
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
        self.assertIn("needs a rationale", out.replies[0][1])
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
            self.assertIn("${{ !github.event.issue.pull_request }}", wf)
            self.assertIn("pages-workflow: 'pages.yml'", wf)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli(["submit", "--repo", REPO, "--decl", F + "double", "--verdict", "accept",
                     "--checked", "F1,F2", "--rationale", "n + n", "--agent", "Claude Code, claude-opus-5-5",
                     "--dry-run"])
            title, body = buf.getvalue().split("\n\n", 1)
            self.assertEqual(title, "Review: " + F + "double")
            a = forms.parse("review", body)
            self.assertEqual((a["who"], a["agent"], a["checked"]["F2"], a["checked"]["F3"]),
                             ("agent", "Claude Code, claude-opus-5-5", True, False))


if __name__ == "__main__":
    unittest.main()
