# sessionport — repo overview for Claude

## What this is

A standalone CLI that makes agent coding sessions portable between
machines. Agent CLIs store transcripts in machine-local directories keyed
by the working directory they ran in, which strands a session on the
laptop that produced it. `sessionport` packs those transcripts into a
content-addressed bundle, stores it anywhere S3-compatible, and unpacks
it elsewhere with paths rewritten so the session actually resumes.

Currently supports Claude Code. Codex and Cursor are the intended next
adapters.

## Why it exists

The driving use case is **PR review**: a reviewer should be able to pull
down the sessions that produced a branch, read them, and *talk to them* —
ask the agent that wrote the code why it made a choice, with all its
original context intact. That last part is why plain transcript viewing
isn't enough and `restore` has to actually work.

The longer-term goal is prompts/sessions being first-class artifacts that
travel with the code they produced.

## Deliberately not part of this project

- **Not a Harness feature.** Harness is one consumer among many. Anything
  that only makes sense inside Harness belongs in Harness.
- **Not a normalized cross-agent schema.** Resume is same-agent only —
  a Claude transcript can't be resumed by Codex, the tool-call schemas
  differ. Native blobs round-trip; normalization would be lossy for the
  one thing that matters. A normalized projection may come later for
  review rendering, but it is not the source of truth.
- **Not git-backed storage.** See below.

## Design decisions and their reasoning

**Object storage, not git.** The deciding factor was deletability, not
size. Transcripts capture verbatim tool output, so a session may need
purging later. Git's content-addressed immutability is a feature for code
and a bug for logs — a leaked key in `refs/sessions/*` means
`git filter-repo` across every clone. S3 lets you `DELETE`. Targeting the
S3 *API* (not AWS) means R2, B2, MinIO, and Garage all work, and users
can self-host. The local-filesystem backend behind the same interface
covers the no-infra case.

**Content-addressed refs.** The ref is the SHA-256 of the packed bytes,
so the storage URI is just where to fetch from. Swapping backends — or
migrating to git/LFS if object storage ever proves onerous — doesn't
change the identity of a bundle.

**Bundles hold N sessions.** A branch usually has several sessions, and
a reviewer wants one ref to pull. `save` filters by cwd/branch/session
and packs everything matching.

**Link at branch level, not per commit.** A reviewer asks "what produced
this PR," not "what produced commit abc123." Commit-level pointers would
drag in squash-merge, rebase, and `post-rewrite` handling for no benefit.
(Nothing writes git pointers yet — this is the intended shape.)

## Empirical findings about Claude Code's storage

These were verified by experiment, not documentation, and are not stable
public API. Re-verify if something breaks.

- Sessions live at
  `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
- The encoding is `re.sub(r"[^a-zA-Z0-9]", "-", cwd)` — every
  non-alphanumeric character, not just slashes.
- The cwd is **symlink-resolved first**, so `/tmp/x` is stored as
  `-private-tmp-x` on macOS. Missing this breaks lookups.
- Records carry `cwd`, `gitBranch`, `sessionId`, `version`, and a
  `parentUuid` chain. `gitBranch` on nearly every line is what makes
  branch filtering possible without any hook instrumentation.
- **The `sessionId` field on every line must agree with the filename.**
  On `--resume` the CLI appends lines stamped with the filename's id, so
  a file whose existing lines disagree becomes a hybrid transcript. When
  minting a new id, rewrite both.
- Absolute paths are pervasive: a real session had 1440 occurrences of
  the origin home directory across 64% of its lines, inside tool inputs
  and results. Rewriting is a whole-transcript operation.
- Scale reference: 750MB across 428 project dirs on one active machine;
  largest single session 1.8MB. Per-branch bundles are a few MB gzipped.

## Layout

Python 3.12, Poetry, boto3. Ported from an earlier TypeScript
implementation; the CLI surface and bundle format are unchanged.

```
sessionport/
├── cli.py       # entry point, arg parsing, list/save/restore commands
├── claude.py    # Claude Code adapter: path encoding, session discovery, metadata
├── bundle.py    # bundle format, gzip, content-addressed ref
├── rewrite.py   # path rewriting (the load-bearing logic)
└── store.py     # Store interface + local-fs and S3-compatible backends
test/
├── fixtures/    # a bundle written by the TypeScript build, and its restore
└── test_*.py    # one module per source module
```

`rewrite.py` is where correctness actually lives. It does a **single
left-to-right pass** with longest-match-first alternation, so a
substitution's output can never be re-matched by a later pair. The hazard
it guards against: a cwd nested under a home directory getting
half-rewritten if the pairs were applied sequentially. Keep the
single-pass property if you touch it — one alternation regex, one
`re.sub`, never a loop of `str.replace` calls.

Two smaller things the port has to keep doing, both for byte-compatibility
with bundles the TypeScript build wrote: JSON is packed compactly
(`separators=(",", ":")`, `ensure_ascii=False`), and absent session
metadata is omitted rather than serialized as `null`.

The gzip wrapper is not byte-identical to Node's — it differs in the OS
header byte — so the same sessions packed by each implementation produce
different refs. That only matters if bundles are ever produced by both.
Packing is deterministic within this implementation (`mtime=0`).

## State

Working and verified end to end, twice — once before the port and once
after. Both times: a session was created on a feature branch, saved, its
bundle rewritten to look like it came from a different machine (foreign
home and repo path), restored into a different directory with the
original deleted so there was no fallback, and resumed — it recalled
content from the original conversation.

The port is also pinned to the format it inherited. `test/fixtures` holds
a bundle written by the TypeScript build and the transcript that build
restored from it; the suite asserts the Python restore is byte-identical.
Both are synthetic — do not regenerate them from a real session.

**The S3 backend has still never run against a real bucket.** It has moto
coverage now, which is more than it had, but mocks agree with your
assumptions by construction. A live run failed on `Access Denied` (the
IAM user lacked `s3:PutObject`); the region mismatch that precedes that
failure surfaces as "The bucket you are attempting to access must be
addressed using the specified endpoint," so set `SESSIONPORT_S3_REGION`
when the bucket's region differs from the AWS config default.

Also missing: `delete`/`list` on the Store interface (a real gap, given
deletability is the stated reason for choosing object storage), any git
integration (no notes, no PR-level pointers), no redaction of secrets in
transcripts, no adapters beyond Claude Code, and no published package or
remote.

## Conventions

- Verify before committing: `poetry run ruff check`,
  `poetry run ruff format --check`, `poetry run mypy .`, and
  `poetry run pytest`.
- Commit coherent changes as you go rather than batching.
- Don't add comments that restate the code. The comments that exist mark
  non-obvious constraints (the single-pass rewrite, the lazy SDK import,
  symlink resolution) — preserve or update those rather than deleting.
- Don't write planning or design documents. Update this file instead.
- Treat transcripts as sensitive: they contain everything an agent read.
