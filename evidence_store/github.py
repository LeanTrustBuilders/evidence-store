"""GitHub, through the `gh` command line: datasets from releases, issues and comments, and replies.

Everything here runs `gh`, which reads its token from ``GH_TOKEN`` (in Actions, the workflow's
token). Nothing else in the package talks to GitHub, so the logic stays testable without it.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

from evidence_core import Dataset

from .forms import FORMS
from .intake import BULK_LABEL, Outcome

#: Colours of the labels intake uses.
LABEL_COLOURS = {"evidence:review": "0e8a16", "evidence:problem": "d93f0b",
                 "evidence:question": "1d76db", "evidence:challenge": "5319e7", "evidence:test": "0052cc",
                 "evidence:named": "bfd4f2", "evidence:status": "c5def5", "evidence:bulk": "ededed",
                 "evidence:open": "fbca04", "evidence:needs-fix": "e4e669"}


def gh(*args: str, input: str | None = None, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}…: {r.stderr.strip()}")
    return r.stdout


def gh_json(*args: str):
    return json.loads(gh(*args) or "null")


class Datasets:
    """The library's datasets, from the releases the store's config names, cached per commit."""

    def __init__(self, config: dict, cache: Path | None = None, override: Path | None = None):
        self.config = config["datasets"]
        self.cache = cache or Path(tempfile.mkdtemp(prefix="evidence-datasets-"))
        self.override = override
        self.loaded: dict[str, Dataset] = {}

    def tags(self) -> list[str]:
        """Every release that is a dataset, oldest first."""
        prefix = self.config["tag"].split("{")[0]
        releases = gh_json("release", "list", "-R", self.config["repo"], "--limit", "1000",
                           "--json", "tagName,createdAt")
        return [r["tagName"] for r in sorted((r for r in releases or [] if r["tagName"].startswith(prefix)),
                                             key=lambda r: r["createdAt"])]

    def latest_tag(self) -> str:
        tags = self.tags()
        if not tags:
            raise LookupError(f"no release of {self.config['repo']} is a dataset "
                              f"({self.config['tag'].split('{')[0]}…)")
        return tags[-1]

    def tag(self, commit: str) -> str:
        """The release of a commit's dataset; the latest when ``commit`` is empty."""
        return self.config["tag"].replace("{commit12}", commit[:12]).replace("{commit}", commit) \
            if commit else self.latest_tag()

    def download(self, tag: str, where: Path) -> Path:
        """Downloads and unpacks a dataset release into ``where`` (unless it is there already)."""
        if not (where / "meta.json").exists():
            where.mkdir(parents=True, exist_ok=True)
            gh("release", "download", tag, "-R", self.config["repo"], "-p", self.config["asset"],
               "-D", str(where), "--clobber")
            with tarfile.open(where / self.config["asset"]) as t:
                t.extractall(where, filter="data")
            (where / self.config["asset"]).unlink()
        return where

    def __call__(self, commit: str) -> Dataset:
        if self.override is not None:
            return Dataset.load(self.override)
        tag = self.tag(commit)
        if tag in self.loaded:
            return self.loaded[tag]
        ds = Dataset.load(self.download(tag, self.cache / tag))
        self.loaded[tag] = ds
        return ds


def issue(repo: str, number: int) -> dict:
    return gh_json("api", f"repos/{repo}/issues/{number}")


def comments(repo: str, number: int) -> list[dict]:
    return gh_json("api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100") or []


def events(repo: str, number: int) -> list[dict]:
    """An issue's events (closed, reopened, labelled, …), oldest first."""
    return gh_json("api", "--paginate", f"repos/{repo}/issues/{number}/events?per_page=100") or []


def recent_issues(repo: str, since: str) -> list[dict]:
    """Issues of the store's forms, and the bulk issue, updated since ``since`` (RFC 3339), open or
    closed."""
    out, seen = [], set()
    for label in [f["label"] for f in FORMS.values()] + [BULK_LABEL]:
        for i in gh_json("api", "--paginate",
                         f"repos/{repo}/issues?state=all&labels={label}&since={since}&per_page=100") or []:
            if i["number"] not in seen and not i.get("pull_request"):
                seen.add(i["number"])
                out.append(i)
    return sorted(out, key=lambda i: i["number"])


def ensure_labels(repo: str) -> None:
    for name, colour in LABEL_COLOURS.items():
        gh("label", "create", name, "-R", repo, "--color", colour, "--force", check=False)


def apply(outcome: dict, repo: str) -> None:
    """Says and does on GitHub what intake decided (``Outcome.as_json``)."""
    if outcome.get("labels"):
        ensure_labels(repo)
    for n, text in outcome.get("replies", []):
        gh("issue", "comment", str(n), "-R", repo, "--body-file", "-", input=text)
    for n, add, remove in outcome.get("labels", []):
        args = [a for l in add for a in ("--add-label", l)] + [a for l in remove for a in ("--remove-label", l)]
        if args:
            gh("issue", "edit", str(n), "-R", repo, *args, check=False)
    for n, reason in outcome.get("close", []):
        gh("issue", "close", str(n), "-R", repo, "--reason", reason, check=False)
    for n in outcome.get("reopen", []):
        gh("issue", "reopen", str(n), "-R", repo, check=False)
    for cid, content in outcome.get("reactions", []):
        gh("api", "-X", "POST", f"repos/{repo}/issues/comments/{cid}/reactions", "-f",
           f"content={content}", check=False)


def create_issue(repo: str, title: str, body: str, label: str) -> str:
    """Opens an issue as a form would, and returns its URL."""
    ensure_labels(repo)
    return gh("issue", "create", "-R", repo, "--title", title, "--body-file", "-", "--label", label,
              input=body).strip()


def comment(repo: str, number: int, text: str) -> str:
    return gh("issue", "comment", str(number), "-R", repo, "--body-file", "-", input=text).strip()
