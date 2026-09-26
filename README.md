# evidence-store

An evidence store in a GitHub repository: the reviews, problems, questions and discussion about a
Lean library's declarations, as [S3](https://github.com/LeanTrustBuilders/specs/blob/main/S3-evidence.md)
records in git, written from GitHub issues and comments by the people and AI agents who made them.

```bash
pip install git+https://github.com/LeanTrustBuilders/evidence-store
evidence-store init --repo OWNER/NAME --root MyLib --labels     # in the repository's checkout
```

## What a repository gets

`evidence-store init` writes:

| path | what it is |
|---|---|
| `evidence/store.json` | the store: the library it is about, where its datasets are, its claims and maintainers |
| `evidence/records/*.jsonl` | the records, one file per month, append-only |
| `.github/ISSUE_TEMPLATE/evidence-{review,problem,question}.yml` | three issue forms: review a declaration (with what was checked, the reference compared with, caveats), report a problem (with its failure mode, and a suggested fix), ask a question |
| `.github/ISSUE_TEMPLATE/evidence-challenge.yml` | propose a test (a **challenge**): a property the declaration should have, for someone to prove in the library |
| `.github/ISSUE_TEMPLATE/evidence-{test,named}.yml` | list a declaration of the library that tests another; name a result or notable definition |
| `.github/ISSUE_TEMPLATE/evidence-status.yml` | a last form, which changes a record's state (the same changes as the commands below): what a page's "Withdraw", "Mark fixed" or "Reopen" buttons open, prefilled |
| `.github/workflows/evidence-intake.yml` | intake: turns those issues and their comments into records |
| `.github/workflows/evidence-check.yml` | checks every change to the store |

Intake needs the library's **datasets** ([S2](https://github.com/LeanTrustBuilders/specs/blob/main/S2-dataset.md)),
to key each record by the reviewed declaration's hashes (S1): by default, a release
`dataset-<commit12>` with an asset `dataset.tar.gz` per commit, which a workflow running
[trust-extract](https://github.com/LeanTrustBuilders/extractor) publishes (see
[review-sandbox](https://github.com/LeanTrustBuilders/review-sandbox) for one).

## Intake

- **An issue opened with a form** becomes a record: an acceptance, a problem, a question, a
  challenge, a test or a name, about the declaration it names, at the commit it names (the latest
  dataset if none). The bot replies with the record's id. A review, a test or a name closes; a
  problem, a question or a challenge stays open.
- **A comment on such an issue** becomes a record too. A command on its first line becomes a status,
  if the commenter may set it:

  | command | on | who |
  |---|---|---|
  | `/withdraw` | any record | its author |
  | `/fixed [commit]`, `/intended`, `/invalid` | a problem | its reporter, a maintainer |
  | `/answered` | a question | its asker, a maintainer |
  | `/met <declaration>` | a challenge | its author, a maintainer: the declaration that proves it, which pages then check as a test |
  | `/failed`, `/declined` | a challenge | its author, a maintainer |
  | `/reopen` | a problem, question or challenge | its author, a maintainer |

  Anything after the command is kept as a note. Any other comment is recorded as a reply: the
  discussion, and the answers to questions.
- **Closing or reopening the issue** by hand is a status too: a problem closed as completed is
  `fixed`, as not planned `invalid`; a question closed is `answered`; a challenge closed as completed
  is `met`, as not planned `declined`; reopening is `reopened`. GitHub lets only the issue's author and
  the maintainers close or reopen it, which is the same rule.
- **The bulk issue** (any issue labelled `evidence:bulk`) takes many records at once, one per line of
  a comment, in Reviewed-by's syntax: `Reviewed-by: <declaration> — <what you checked>`,
  `Test: <declaration> — <test> — <what it checks>`, `Named: <declaration> — <name> — <a sentence>`,
  `Challenge: <declaration> — <the property>`. The bot answers with what it recorded and what it
  could not.
- **An issue opened with the status form** asks for one of those changes to a record named by its id,
  under the same rules. The bot records it, closes the form's issue, and says and does the change on
  the record's own issue too. This is what lets a static page offer the changes as buttons.
- **Maintainers** are the repository's owners, members and collaborators, and the logins listed in
  `store.json`.

Intake is idempotent: each record names the issue or comment it came from, and nothing is recorded
twice. Every run also reads the evidence issues updated in the last two weeks, and a scheduled run
every six hours catches events whose runs were dropped.

## Who wrote it

Every record is by the GitHub account that wrote the issue or comment. There are no anonymous
records.

**AI agents** are recorded as agents, with the account they acted through: in a form, "Written by: an
AI agent, run by me" and the agent's tool and model; in a comment, a line
`<!-- agent: tool=Claude Code; model=claude-opus-5-5; session=… -->`; and an account of type `Bot` is
an agent. An agent's review needs a rationale. From a terminal, an agent writes exactly what the
forms would:

```bash
evidence-store submit --repo OWNER/NAME --decl MyLib.foo --verdict accept \
  --checked F1,F2,F3 --reference "Rudin, Principles §3.1" --rationale "…" \
  --agent "Claude Code, claude-opus-5-5"
evidence-store submit --repo OWNER/NAME --decl MyLib.foo --verdict problem --category F3 --rationale "…" --agent "…"
evidence-store comment --repo OWNER/NAME --issue 12 --text "It is 0: by rfl." --agent "Claude Code, claude-opus-5-5"
evidence-store status  --repo OWNER/NAME --record 41fe3cfb8d7af478 --action withdraw --agent "Claude Code, claude-opus-5-5"
evidence-store challenge --repo OWNER/NAME --decl MyLib.foo --property "foo 0 = 0" --statement "MyLib.foo 0 = 0" --modes F3 --agent "…"
evidence-store test    --repo OWNER/NAME --decl MyLib.foo --test MyLib.foo_zero --checks "the value at 0" --agent "…"
evidence-store name    --repo OWNER/NAME --decl MyLib.main --name "The main theorem" --about "…" --agent "…"
```

Reviewed-by's marker `<!--reviewed-by:v1 {"agent": "Claude Code, claude-opus-5-5, session 12"}-->`
is read as well as evidence-store's own.

## Pull requests

Records can also be added by pull request (bulk reviews, an agent's batch). The check requires that
the records it adds are valid and **by the pull request's author**, and that no record was changed or
removed. A push by a person is checked the same way; the intake bot's pushes write records for the
accounts whose issues it read.

## Command line

```
evidence-store init    --repo OWNER/NAME --root ROOT [--pages-workflow FILE] [--claim NAME] [--labels]
evidence-store intake  --repo OWNER/NAME --outcome FILE [--event PATH --event-name NAME] [--sweep-days N]
evidence-store apply   --repo OWNER/NAME --outcome FILE
evidence-store check   [--base REV] [--author LOGIN]
evidence-store submit  --repo OWNER/NAME --decl NAME --verdict accept|problem|question …
evidence-store challenge --repo OWNER/NAME --decl NAME --property TEXT [--statement LEAN] [--catches TEXT] [--modes F1,…]
evidence-store test    --repo OWNER/NAME --decl NAME --test NAME [--checks TEXT] [--meets ID]
evidence-store name    --repo OWNER/NAME --decl NAME --name TEXT [--what result|definition] [--about TEXT] [--source TEXT]
evidence-store comment --repo OWNER/NAME --issue N --text TEXT [--agent "TOOL, MODEL"]
evidence-store status  --repo OWNER/NAME --record ID --action withdraw|fixed|intended|invalid|answered|met|failed|declined|reopen [--test NAME] …
evidence-store add     FILE [--store evidence]
evidence-store dataset [--commit SHA | --all] --out DIR [--store evidence] [--repo OWNER/NAME --tag 'dataset-{commit12}']
```

`add` puts the records of a file (a reader's export from a page, say) into the store, each checked and
given its id, for a pull request from its author. `dataset` fetches datasets from the releases that hold them, as intake does: where `store.json` says
(`datasets`), or, for a repository without a store, where `--repo` and `--tag` say. Pages and
workflows use it rather than downloading releases themselves.

The composite actions `LeanTrustBuilders/evidence-store/intake` and `…/check` are what the generated
workflows run. `intake` reads, then commits the records, then replies on the issues, so that a reply
never announces a record the store did not keep; with `pages-workflow`, it then runs that workflow to
rebuild a page.

The logic is in [evidence-core](https://github.com/LeanTrustBuilders/evidence-core) (records,
threads, stores and their checks) and in `evidence_store.intake`, which is pure: it reads issues and
comments as the GitHub API returns them. Only `evidence_store.github` talks to GitHub, through `gh`.

## Tests

```bash
pip install -e . && python3 -m unittest discover -s tests
```
