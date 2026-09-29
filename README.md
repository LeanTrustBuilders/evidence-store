# evidence-store

An evidence store in a GitHub repository: the reviews, problems, questions and discussion about a
Lean library's declarations, as [S3](https://github.com/LeanTrustBuilders/specs/blob/main/S3-evidence.md)
records in git, written from GitHub issues and comments by the people and AI agents who made them.

```bash
pip install git+https://github.com/LeanTrustBuilders/evidence-store
evidence-store init --repo OWNER/NAME --root MyLib --name "My library" --labels     # in the repository's checkout
```

## What a repository gets

| path | what it is |
|---|---|
| `evidence/store.json` | the store: its name (`--name`), the library it is about, where its datasets are, its claims and maintainers, and its rubric if not the standard one |
| `evidence/records/*.jsonl` | the records, one file per month, append-only |
| `.github/ISSUE_TEMPLATE/evidence-*.yml` | issue forms: review a declaration, report a problem, ask a question, propose a test (a **challenge**), list a test, name a result, and change a record's state |
| `.github/workflows/evidence-intake.yml` | intake: turns those issues and their comments into records |
| `.github/workflows/evidence-check.yml` | checks every change to the store |

The forms ask what a review checked, what is wrong with a declaration and what a challenge would
catch in terms of the store's **rubric** (S3): `ltb-rubric/1` (`object`, `convention`, `edge-cases`,
`junk`, `vacuous`, `choice`, `generality`, `naming`) unless `store.json` gives another, as `{name,
axes: [{name, check, problem}]}`. Run `init` again after changing it, to rewrite the forms.

Intake keys each record by the reviewed declaration's hashes, so it needs the library's **datasets**
(S2): by default a release `dataset-<commit12>` with an asset `dataset.tar.gz` per commit, as the
[extractor](https://github.com/LeanTrustBuilders/extractor)'s `extract` action publishes them.

## Intake

- **An issue opened with a form** becomes a record about the declaration it names, at the commit it
  names (the latest dataset if none). The bot replies with the record's id. A review, a test or a name
  closes; a problem, a question or a challenge stays open.
- **A comment on such an issue** becomes a reply, or, with a command on its first line, a status, if
  the commenter may set it. Anything after the command is the status's text.

  | command | on | who |
  |---|---|---|
  | `/withdraw` | any record | its author |
  | `/fixed [commit]`, `/intended`, `/invalid` | a problem | its reporter, a maintainer |
  | `/answered` | a question | its asker, a maintainer |
  | `/met <declaration>` | a challenge | its author, a maintainer: the declaration that proves it, which pages then check as a test |
  | `/failed`, `/declined` | a challenge | its author, a maintainer |
  | `/reopen` | a problem, question or challenge | its author, a maintainer |

- **Closing or reopening the issue** by hand is a status too: a problem closed as completed is
  `fixed`, as not planned `invalid`; a question closed is `answered`; a challenge closed as completed
  is `met`, as not planned `declined`.
- **The status form** asks for one of those changes to a record named by its id, under the same
  rules; the bot records it and says so on the record's own issue. A static page's buttons open it,
  prefilled.
- **The bulk issue** (labelled `evidence:bulk`) takes one record per line of a comment:
  `Reviewed-by: <declaration> — <what you checked>`, `Test: <declaration> — <test> — <what it
  checks>`, `Named: <declaration> — <name> — <a sentence>`, `Challenge: <declaration> — <the
  property>`.
- **Maintainers** are the repository's owners, members and collaborators, and the logins listed in
  `store.json`.

Intake is idempotent: each record names the issue or comment it came from, and nothing is recorded
twice. Each run also rereads the evidence issues updated in the last two weeks, and a scheduled run
every six hours catches dropped events.

## Who wrote it

Every record is by the GitHub account that wrote the issue or comment; there are no anonymous
records. **AI agents** are recorded as agents, with the account they acted through: in a form,
"Written by: an AI agent, run by me" with the agent's tool and model; in a comment, a line
`<!-- agent: tool=Claude Code; model=claude-opus-5-5; session=… -->` (a `<!--reviewed-by:v1 …-->`
marker is read too); and an account of type `Bot` is an agent. An agent's review says why.

From a terminal, an agent writes exactly what the forms would:

```bash
evidence-store submit --repo OWNER/NAME --decl MyLib.foo --verdict accept \
  --checked object,convention,edge-cases --reference "Rudin, Principles §3.1" --rationale "…" \
  --agent "Claude Code, claude-opus-5-5"
evidence-store submit --repo OWNER/NAME --decl MyLib.foo --verdict problem --category edge-cases --rationale "…" --agent "…"
evidence-store comment --repo OWNER/NAME --issue 12 --text "It is 0: by rfl." --agent "…"
evidence-store status  --repo OWNER/NAME --record 41fe3cfb8d7af478 --action withdraw --agent "…"
evidence-store challenge --repo OWNER/NAME --decl MyLib.foo --property "foo 0 = 0" --statement "MyLib.foo 0 = 0" --modes edge-cases --agent "…"
evidence-store test    --repo OWNER/NAME --decl MyLib.foo --test MyLib.foo_zero --checks "the value at 0" --agent "…"
evidence-store name    --repo OWNER/NAME --decl MyLib.main --name "The main theorem" --about "…" --agent "…"
```

## Pull requests

Records can also be added by pull request. The check requires that the records it adds are valid and
**by the pull request's author**, and that no record was changed or removed. A push by a person is
checked the same way; the intake bot writes records for the accounts whose issues it read.
`evidence-store add FILE` puts the records of a file (a reader's export from a page, say) into the
store, each checked and given its id, for such a pull request.

## Command line

```
evidence-store init    --repo OWNER/NAME --root ROOT --name NAME [--pages-workflow FILE] [--claim NAME] [--labels]
evidence-store intake  --repo OWNER/NAME --outcome FILE [--event PATH --event-name NAME] [--sweep-days N]
evidence-store apply   --repo OWNER/NAME --outcome FILE
evidence-store check   [--base REV] [--author LOGIN]
evidence-store add     FILE [--store evidence]
evidence-store dataset [--commit SHA | --all] --out DIR [--store evidence] [--repo OWNER/NAME --tag 'dataset-{commit12}']
evidence-store fetch-imports --out DIR [--store evidence]
evidence-store submit | challenge | test | name | comment | status   (above)
```

`dataset` fetches datasets from their releases, as intake does: where `store.json` says, or, without
a store, where `--repo` and `--tag` say. `fetch-imports` fetches the stores `store.json` imports
(`imports`, S3's "Imported records") into a directory, each at the commit its `ref` names or its
default branch, with `imports.json` saying which; a view then reads them beside the store's own
records (evidence-core's `with_imports`, and `--imports DIR` for `referee-site`). The composite actions `LeanTrustBuilders/evidence-store/intake`
and `…/check` are what the generated workflows run. `intake` reads, commits the records, then replies
on the issues, so a reply never announces a record the store did not keep; with `pages-workflow`, it
then runs that workflow to rebuild a page.

The logic is in [evidence-core](https://github.com/LeanTrustBuilders/evidence-core) (records,
threads, stores and their checks) and in `evidence_store.intake`, which is pure: it reads issues and
comments as the GitHub API returns them. Only `evidence_store.github` talks to GitHub, through `gh`.

## Tests

```bash
pip install -e . && python3 -m unittest discover -s tests
```
