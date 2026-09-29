"""`evidence-store fetch-imports`: the stores a store imports, fetched for its views.

The imported store is a local git repository, reached through EVIDENCE_STORE_GIT_BASE.

Run with ``python3 -m unittest discover -s tests``.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from evidence_core import store as sto
from evidence_store.cli import main as cli


def git(where: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class FetchImports(unittest.TestCase):
    def test_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            other = tmp / "hosts" / "other" / "lib"
            (other / "evidence").mkdir(parents=True)
            (other / "evidence" / "store.json").write_text(json.dumps(sto.default_config("other/lib", "Other")))
            (other / "evidence" / "r.jsonl").write_text('{"id": "a", "kind": "comment"}\n')
            git(other, "init", "-q")
            git(other, "add", "-A")
            git(other, "commit", "-q", "-m", "one record")
            git(other, "tag", "v1")
            first = git(other, "rev-parse", "HEAD")
            (other / "evidence" / "r.jsonl").write_text('{"id": "a", "kind": "comment"}\n{"id": "b", "kind": "comment"}\n')
            git(other, "commit", "-q", "-am", "two records")
            last = git(other, "rev-parse", "HEAD")

            mine = tmp / "mine" / "evidence"
            mine.mkdir(parents=True)
            config = sto.default_config("me/lib", "Mine")
            with mock.patch.dict(os.environ, {"EVIDENCE_STORE_GIT_BASE": f"file://{tmp}/hosts/"}):
                for imports, commit, n in (([{"repo": "other/lib"}], last, 2),
                                           ([{"repo": "other/lib", "ref": "v1"}], first, 1)):
                    (mine / "store.json").write_text(json.dumps({**config, "imports": imports}))
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(cli(["fetch-imports", "--store", str(mine), "--out", str(tmp / "cache")]), 0)
                    manifest = json.loads((tmp / "cache" / "imports.json").read_text())
                    self.assertEqual([(m["repo"], m["commit"]) for m in manifest], [("other/lib", commit)])
                    got = sto.with_imports(sto.Store.load(mine), tmp / "cache")
                    self.assertEqual((len(got.records), got.read[0]["commit"]), (n, commit))
                (mine / "store.json").write_text(json.dumps({**config, "imports": [{"repo": "no/such"}]}))
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                    self.assertEqual(cli(["fetch-imports", "--store", str(mine), "--out", str(tmp / "cache")]), 1)


if __name__ == "__main__":
    unittest.main()
