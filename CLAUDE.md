# lochy — repo overview for Claude

## What this is

A standalone CLI that makes agent coding sessions portable between
machines. Agent CLIs store transcripts in machine-local directories keyed
by the working directory they ran in, which strands a session on the
laptop that produced it. `lochy` packs those transcripts into a
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

**No single index object.** A shared `index.json` would need
read-modify-write on every save, so two machines saving concurrently would
silently clobber each other. Every index write is instead a new object at
a distinct key, which makes concurrent saves commutative and needs no
locking from a store that offers none.

**Index entries are derived, never authoritative.** The bundles are the
source of truth and every entry is reconstructible by listing `bundles/`
and unpacking. That is what makes `reindex` a repair command and what
makes a half-failed `save` leave a recoverable store rather than a corrupt
one. Nothing may end up knowable only from the index.

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
- **A session can hold more than one `cwd`, and more than one
  `gitBranch`.** When the agent moves — into a worktree, say — the cwd
  changes and every record from before the move stays in the file. The
  transcript is filed under the directory it *ends* in, so the first
  record's cwd can be stale, and stale by a long way: in the session this
  was found on, records 5–295 named the main repo on `main` and records
  300–1245 named the worktree on `feat/send-worktree-to-remote`. The runs
  are contiguous and the live one is last.
- `encode_path` of the live cwd equals the containing directory's name
  exactly, and the stale one's does not. **The directory is the
  disambiguator**, and the only one available — no field marks which cwd
  is current.
- `"cwd"` also appears *inside* tool inputs and results, naming some
  other process's directory. lochy's own `--json` output is one such
  source, so anything counting cwds has to parse each record and read the
  top-level field, not grep the text.
- Scale reference: 750MB across 428 project dirs on one active machine;
  largest single session 3.4MB. Per-branch bundles are a few MB gzipped.
  Parsing every line of that 3.4MB session costs ~11ms and listing its
  whole project directory (7 sessions) ~17ms.
- **Multi-cwd sessions are rare and concentrated where it hurts.** Across
  661 sessions belonging to one machine's repos, 7 (1.1%) held more than
  one cwd and exactly 1 had its live cwd past line 200. That one is the
  longest and most-compacted session in the set — 1245 lines, several
  context compactions — and it is the compaction preamble that pushes the
  real cwd down the file. So the defect concentrates in long-lived
  sessions, which are precisely the ones worth moving between machines,
  and it grows as sessions get longer. That distribution is the argument
  for the project-directory disambiguator over any larger window: a
  bigger window is a bet on a bound that the worst cases are the likeliest
  to break.

## Layout

Python 3.10 through 3.13, Poetry, boto3 behind an extra.

```
lochy/
├── cli.py       # entry point, arg parsing, list/save/restore/delete/reindex
├── claude.py    # Claude Code adapter: path encoding, session discovery, metadata
├── bundle.py    # bundle format, gzip, content-addressed ref
├── index.py     # store key layout, derived index entries, delete and reindex
├── git.py       # current branch of a checkout, for the list default
├── output.py    # result envelope, the two renderers, structured failures
├── redact.py    # secret scrubbing, applied on save before packing
├── rewrite.py   # path rewriting (the load-bearing logic)
└── store.py     # Store interface + local-fs and S3-compatible backends
test/
└── test_*.py    # one module per source module
```

The store layout is:

```
bundles/<ref>.loch                  # the artifact
index/<dimension>/<value>/<ref>     # derived entry, small JSON payload
```

`branch` is the only dimension implemented, but it is a *dimension*, not a
special case: `index/cwd/...` can be added later by passing a different
name, with no migration and no format change. Don't collapse the
dimension segment back into the path.

Values are percent-encoded (`feature/foo` → `feature%2Ffoo`) so a segment
never contains `/`. **Do not reuse Claude's `[^a-zA-Z0-9] -> -` scheme
here** — it is lossy, and it would file `feature/foo` and `feature-foo`
under the same segment. A session with no branch is indexed under `%00`,
which no real value can encode to since a ref name can't hold a control
character.

Branch is a property of each *session*, not of the bundle: a `save`
without `--branch` can pack sessions from several branches, so one bundle
gets one entry per branch it touches. An entry carries only display
metadata — session count, cwd, timestamp, packed size, Claude version —
enough to list a branch without fetching a single bundle.

Ordering matters in both directions and for the same reason. `save`
writes the bundle before its entries, so a failure leaves an
unindexed bundle that `reindex` can recover. `delete` removes the entries
before the bundle, since the entries are derived from it and losing the
bundle first would strand them. `Store.delete` on an absent key succeeds,
which is what lets either repair run.

`claude.py` decides **which** cwd a session gets, and everything in
`rewrite.py` is downstream of that answer: `SessionMeta.cwd` becomes the
bundle manifest's cwd and then `RewriteSpec.origin_cwd`, so one wrong
value poisons the whole rewrite. Because a session can hold several cwds
(see above), it is chosen by comparing `encode_path` of each against the
name of the directory the transcript is filed under. **The encoding is
only ever compared here, never decoded** — it is lossy, so `<repo>/sub`
and `<repo>-sub` produce the same slug, which makes a match strong
evidence and a mismatch conclusive. That asymmetry is the whole safety
argument. Nothing matching — a hand-copied transcript, a renamed
directory — falls back to the first cwd rather than dropping the session.

`gitBranch` is stale in exactly the same way and decides which index entry
a bundle is filed under, so branch and version are read from the records
carrying the *chosen* cwd rather than from whichever record came first. A
branch taken from a directory the session has left is the same defect
wearing a different hat: the real session was reported on `main` when it
had spent 726 of its records on `feat/send-worktree-to-remote`, which
filed it under a branch nobody would look for it on.

The scan for cwd is deliberately **unbounded** where the rest of the
metadata scan used to be capped at 200 lines. The cap was the bug: the
live cwd first appeared at record 300, so no window wide enough to be
correct is a window at all. The cwds a session left are kept as
`SessionMeta.other_cwds` and surfaced as `otherCwds`, since a session
spanning two directories is something to know before shipping it, not
after.

**That is a real cost and it was paid knowingly.** `list` now parses
every line of every session in a project directory. On the worst
directory on one machine — 5197 sessions, 133MB — it went from ~5.8s to
~6.5s best-of-three, with noisier runs nearer 8s; 10–45% depending on
cache state.

Three measurements say don't buy it back with a window. **The scan depth
is not where it went**: parsing all 5197 sessions whole takes 0.42s,
against 0.76s for a 200-line window of each — the window is *slower*,
because those sessions average 12 lines, it never binds, and slicing the
line list costs more than it saves. **The obvious optimisation does
nothing**: skipping `json.loads` on lines with no `"cwd"` substring
measured 0.43s against 0.42s, a wash, since 58% of lines carry one and
the search is paid on all of them. So the regression is per-file overhead
— stat, read, summary redaction — and the residue of building three dicts
where the old loop broke out after one line. **And the directory is a
pathological outlier**: the next largest holds 281 sessions.

Optimise the per-file path if this ever matters. Reinstating a cap would
buy back a fraction of a second on one anomalous directory in exchange
for being silently wrong again.

`rewrite.py` is where correctness actually lives. It does a **single
left-to-right pass** with longest-match-first alternation, so a
substitution's output can never be re-matched by a later pair. The hazard
it guards against: a cwd nested under a home directory getting
half-rewritten if the pairs were applied sequentially. Keep the
single-pass property if you touch it — one alternation regex, one
`re.sub`, never a loop of `str.replace` calls.

The second property is a **boundary rule**: a path is only substituted
where the match ends on a path-component boundary. Without it a sibling
directory that extends the origin's last component gets mangled —
`<repo>-worktrees/<branch>`, which is where Harness puts every worktree,
became the target path with its own tail duplicated. That failure was
**silent**, and that is the part worth remembering: the corrupted string
no longer contains the origin, so `residual_origin_paths` reported a clean
rewrite across 239 bad values in one restored session. The guards are
per-alternative zero-width lookaheads, which is what keeps them compatible
with the single pass — an alternative that fails its guard falls through
to a shorter one, so `/Users/mike/projector` still gets its home rewritten
after the cwd pair declines.

The asymmetry that sets the guards: **too strict fails loudly** (the path
survives and `residual_origin_paths` reports it), **too lenient fails
silently**. So the real-path guard rejects only what could continue a
filename (`[A-Za-z0-9._-]`) and allows every other follower, since a
transcript ends a path with `"`, `\`, `:`, `,`, a space or a newline far
more often than with `/`.

That "fails loudly" holds only for the failure it describes — a path that
*keeps* an origin marker. It does not hold in general, and assuming it did
cost a downstream consumer a restore that was 76% wrong while reporting
success. `residual_origin_paths` is a substring search for `origin_cwd`
and `origin_home`, so it is blind to any path that came out wrong *and*
well-formed. The way that happens: give the spec the wrong `origin_cwd`
and the records naming the right one decline the cwd pair (correctly — the
boundary guard is doing its job), fall through to the home pair, and
become a path on this machine that simply isn't there. The origin marker
is consumed on the way out, so the search comes back empty.

`foreign_cwds` is the check that isn't blind to it. It asks the rewritten
transcript what directory its records think they are in and reports
anything that isn't `target_cwd`, with counts — after a rewrite that
worked, every record names the target, so a clean result is real evidence
rather than the absence of one marker. It reads the top-level `cwd` field
per record, not a substring: `"cwd"` occurs inside tool output too, and a
detector that cries wolf gets ignored. **This is the field to gate on**;
`residual_origin_paths` remains necessary and is nowhere near sufficient.

Non-empty is not always a bug. A session that genuinely moved between
directories has records that no single target cwd can satisfy, since only
one directory is being restored into. It always means the restored
transcript is partly wrong about the filesystem, which is the thing a
caller needs to know, so it is reported rather than papered over: those
records keep whatever the home pair made of them (a plausible sibling on
the destination) and are counted in `foreignCwds`.

It reads the transcript **before** rewriting, and that is the whole
reason the entry is keyed on the origin path. A caller's next move is to
state a mapping for the directory that came out wrong, and the origin
path is that mapping's left-hand side — but it survives losslessly only
on the input. Recovering it from the restored value means assuming a
leading target home was rewritten from the origin's rather than having
been there all along: right most of the time, silently wrong otherwise,
undetectable when wrong. Handing a caller a guess to fix a guessing bug
would reproduce the defect this whole area exists to remove. `restored`
comes from putting each cwd through the same spec, so both facts are
given and neither is inferred.

**Leaving foreign cwds unrewritten is possible — it is just not better.**
The obvious objection, that it needs knowing which subtrees exist on the
destination, does not apply: lochy has the exact set of candidate cwds
already, having computed them to choose one, and those specific strings
are known to be session working directories rather than dotfile or config
references. So the rule "the home pair declines a value exactly equal to
a known non-chosen session cwd" is an exact match against a small known
set, needs nothing from the far side, and fits the single pass — an
identity pair sorts ahead of the shorter home pair by length, matches,
and consumes the string, so the home pair never sees it.
`_apply_replacements` filters identity pairs out via `source != dest` and
would have to admit them deliberately, which is a small change rather
than a fight with the design. It is not built because reporting is
sufficient and `--map` supersedes it: a mapping makes those records
*correct*, where declining the rewrite only makes them loudly wrong.
Recorded because "it can't be done" would read later as a closed
question, and it isn't one.

**`--map <origin>=<target>`, not built, is the intended shape.** A
repeatable flag on `restore` adding path pairs to the `RewriteSpec`
alongside the cwd and home ones, so a session that moved between
directories can have *every* one of them land somewhere real instead of
one landing right and the rest being counted. It is the natural
successor to `foreignCwds`: the report tells a caller which directories
came out wrong, and the flag is how they say what those should become.
Nothing in `rewrite.py` needs to change to accept it — pairs already
carry their own boundary guard and are already sorted longest-first, and
**that sort must keep being by length rather than by caller order**: the
single-pass guarantee depends on a nested path matching before the
shorter prefix that contains it, and caller order is arbitrary. This is
a "keep doing that" note, not a change.

What makes it safe is that **the caller states the mapping; lochy never
infers it.** The first consumer (Harness "send worktree to remote") can
name all four paths without a guess: the origin worktree is what the
user selected, the origin repo root comes from
`git rev-parse --path-format=absolute --git-common-dir` stripped of its
`.git` tail, the destination worktree is what `addWorktree` returns, and
the destination repo root is a required argument on the caller because
it is the one value nothing on either side can derive. Note the
destination worktree must be `realpath`'d: git reports canonical paths,
so `/tmp/...` comes back as `/private/tmp/...` and a lookup keyed on the
uncanonical form silently misses. That is the same symlink resolution
`claude.py` does, for the same reason.

The encoded slug takes a stricter rule for a reason that can't be fixed:
`encode_path` maps every non-alphanumeric to `-`, so `<repo>/sub` and
`<repo>-sub` encode to the *same string* and no lookahead can tell them
apart. It is therefore rewritten only as a complete component, which
declines `<encoded-repo>-worktrees-...` entirely. That is a known lossy
limitation, and the false negative is the deliberate half of it — don't
"fix" it by loosening the guard, that reintroduces the corruption. The
session-id pair is a UUID rather than a path and carries no guard.

`redact.py` scrubs secrets out of a transcript on `save`, before packing.
It is **best-effort pattern matching and nothing more.** It finds
credentials with distinctive structure (AWS/GitHub/Slack/Stripe/
Anthropic/OpenAI/Google keys, JWTs, PEM blocks) plus one context rule for
`UPPERCASE_NAME=value`, which is the only way to catch an AWS secret
access key — 40 chars of base64 with no prefix. It will miss anything
shaped like ordinary text, and lowercase JSON keys such as
`"api_key": "..."` are deliberately out of scope because matching them
fires on prose. **A redacted bundle is not a safe bundle: rotate any
credential an agent has read.** The rules stay narrow on purpose — a
scrubber that mangles prose is one people disable. No entropy heuristics;
they fire on every hash, UUID, and base64 blob in a transcript.

Three properties it has to keep:

- **The local transcript is never modified.** `cli.py` reads, redacts the
  in-memory copy, packs that. Redaction changes the packed bytes and
  therefore the ref; that is correct, the redacted bundle is the artifact.
- **Single pass, same discipline as `rewrite.py`** — one alternation of
  named groups, one `re.sub` — so no rule can match inside another rule's
  replacement. The `env-assignment` value carries a `(?!\[REDACTED:)`
  lookahead for the same reason: without it, re-scanning
  `TOKEN=[REDACTED:env-assignment]` matches all over again.
- **Redacted output is re-scanned, and a surviving match aborts the
  save.** A rule that doesn't remove what it matched is a leak, so it
  fails loudly instead of uploading. Two related constraints: the
  replacement token holds no quote, backslash, or newline, so a JSONL
  line still parses; and the PEM rule (the only multi-line one) excludes
  `"` from its body so a BEGIN and an END in two different records can't
  merge into one match that swallows everything between them.

Counts are reported per session by rule name. **Never print the match**,
not even truncated — `save`'s stdout lands in the next agent's transcript.

`save` is not the only path transcript content escapes on, so `claude.py`
scrubs the `list` summary too, in `_display_summary`. That summary is the
first user message, it reaches a picker row and a pasted bug report without
a `save` in between, and it is the only such field. Two ordering points:
redact *before* truncating, since a cut can leave a secret's tail matching
no rule; and a summary that raises `RedactionError` becomes `[REDACTED]`
whole, because failing a listing over a label would be the wrong trade.

Measured against 350MB of real transcripts: 37 matches, all from
`env-assignment` and all of them source code naming a secret rather than
a literal secret (`HARNESS_TOKEN: process.env.X`,
`RPC_TOKEN_TTL = timedelta(...)`). Distinguishing a reference from a
value needs a parser, so that failure mode stays — it mangles a line of
code in the packed copy, it doesn't leak. The prefixed-key rules match
nothing in that corpus, which is the point of the word boundaries.

Two smaller things `bundle.py` has to keep doing, because the ref is a
hash of the packed bytes and any drift changes a bundle's identity: JSON
is packed compactly (`separators=(",", ":")`, `ensure_ascii=False`), and
absent session metadata is omitted rather than serialized as `null`.
Packing is deterministic (`mtime=0`), so the same sessions always
produce the same ref.

## The JSON contract

`lochy` is meant to be embedded in other tools — Harness is the first — and
a caller that scrapes prose breaks the moment someone rewords a sentence.
So every command takes `--json`, accepted either before or after the
subcommand, and answers with exactly one document on stdout:

```json
{"schema": 1, "ok": true, "command": "save", "ref": "...", "bytes": 331, ...}
```

`schema` is the envelope version. It exists so a consumer can detect a
format change it doesn't understand; bump it when a field changes meaning or
disappears, not when one is added.

**Built once per command, rendered twice.** Each `command_*` returns a
`Result` — the payload, the prose, and any stderr warnings — and `main()`
hands it to one renderer or the other. No command contains `if args.json`,
and that is the point: a payload assembled anywhere other than beside the
prose that describes it will rot the first time someone edits the prose.

**Failures are documents, on stdout.**

```json
{"schema": 1, "ok": false, "command": "restore", "code": "nothing-restored",
 "error": "nothing restored", "sessions": [{"status": "skipped", ...}]}
```

Stdout rather than stderr so a caller parses one stream unconditionally and
reads the verdict off the exit status and `ok`. Splitting the two across
streams would force it to guess which stream to read before it knows which
outcome it got. In JSON mode stderr stays empty, and the prose warnings
(`restore`'s skips) are carried as per-item status instead.

**`code` is the contract; `error` is for humans.** Reword a message freely,
never a code. The set today:

| code | meaning |
| --- | --- |
| `no-sessions` | `save` matched nothing |
| `redaction-failed` | a secret survived redaction; nothing was uploaded |
| `missing-ref` | `restore`/`delete` called without a ref |
| `bundle-not-found` | the store answered and the ref isn't there |
| `store-unreachable` | the store didn't answer |
| `s3-extra-missing` | an `s3://` store on an install without the `s3` extra |
| `bundle-unreadable` | the bytes arrived and won't unpack |
| `nothing-restored` | every session in the bundle was skipped |
| `unknown-command` | no such subcommand |
| `usage` | argparse rejected the arguments (exit status 2) |
| `internal` | an unexpected exception |

The four store-facing codes — `bundle-not-found`, `store-unreachable`,
`s3-extra-missing`, `bundle-unreadable` — are one failure to a human and four
different affordances to a caller: don't retry, retry, reinstall, report
corruption. Telling them apart is why `store.py` raises typed
`MissingObject` and `MissingDependency` exceptions — a bare `except Exception`
around a store call can only produce the union, and `s3-extra-missing` was
`store-unreachable` until boto3 became optional, which told a caller to retry
a network problem that was really a missing package. The `store_errors` block
wraps the store call *alone*, since anything wider would relabel a bug in
this process as a failure of the store.

**Exactly one document per invocation, including the paths argparse owns.**
argparse answers `-h` and a bad flag itself, writing prose and exiting
before a command ever runs, so `_Parser` routes both back through `main()`.
An unexpected exception becomes `code: "internal"` in JSON mode and stays a
traceback without the flag, since a traceback is the most useful thing a
human can get and the one thing a consumer can't parse.

**Raw values, not renderings.** `format_bytes` and the fixed-width columns
of `describe`/`describe_entry` belong to the text mode alone: the payload
carries the integer and the ISO-8601 `...Z` timestamp. An empty result is
`{"sessions": []}` with `ok: true`, never an error.

The `list --remote` payload is deliberately *not* `index.py`'s stored entry
shape even though the fields line up. One is a storage format, the other is
a versioned wire contract, and they change for different reasons.

**A partly-wrong restore is reported, not failed.** `restore` carries
`foreignCwds` per session — `{"<path>": <count>}` of directories the
rewritten transcript still names other than the one it was restored into.
It is the field to gate on, because `residualOriginPaths` cannot see the
failure it exists for (above). It stays a warning rather than a `code`:
a session that genuinely moved between directories has records no target
cwd can satisfy, and refusing the restore would help nobody. The text
renderer prints both, and the prose warning is stderr-only as usual, so
JSON mode carries the same fact as a payload field and leaves stderr
empty. `save` and `list` carry the same signal ahead of time as
`otherCwds`.

**Never the matched text.** Redaction is reported as `{"<rule>": <count>}`
plus a `redacted` total, exactly as much as the prose summary carries and no
more — `save`'s stdout lands in the next agent's transcript. The one field
holding transcript content is `list`'s `summary` (the first 120 characters of
the first user message, which the text output already prints), and it is
scrubbed in `claude.py` before it ever becomes a `SessionMeta`. It should
stay the only such field.

## Packaging and release

The distribution path is `uv tool install <release wheel URL>`: uv is a static
binary that brings its own CPython, so a machine being bootstrapped over SSH
needs no Python toolchain and there is no PyPI account in the loop. That
constrains three things.

**Supported range is 3.10 through 3.13, with no upper bound.** The old
`>=3.12,<3.13` was pinning copied from an unrelated backend, and it is what
made `pipx install` fail on both ends — Ubuntu 22.04 ships 3.10, and a 3.13
box failed the cap. Never reintroduce a ceiling: the next Python breaks the
install before it breaks the code. 3.10 is the floor because PEP 604 `X | None`
unions are pervasive and 3.9 would mean rewriting every annotation. One line
actually needed 3.12 — a backslash inside an f-string expression, PEP 701
syntax — so keep interpolations backslash-free. `[tool.mypy] python_version`
is pinned to the floor rather than following the running interpreter, so a
3.11+ API fails on a 3.13 laptop and not only in CI.

**boto3 is an optional `[s3]` extra.** What makes that safe is the lazy import
in `S3Store._client`: the `file://` backend loads nothing third-party at
runtime, so the SDK was only ever an install-time cost — 8 packages and 27MB,
of which botocore alone is 24MB against lochy's own 172KB. A default install
is one package and 184KB. Keep the import inside the method, and keep any new
`botocore` import *after* the `_client()` call that would have failed first,
or the SDK's absence surfaces as a bare `ImportError` and gets mislabelled
`store-unreachable`. The dev group still carries boto3, moto and the stubs, so
the S3 backend stays fully covered by the suite.

**`lochy --version` reads package metadata**, so pyproject is the only place
the number is set. `importlib.metadata` raises `PackageNotFoundError` in a
source checkout that was never installed, which reports `"unknown"` — pyproject
can't be read as a fallback, since `tomllib` arrived in 3.11 and the floor is
3.10. It is a `Result` like every other command, so `--version --json` is an
envelope.

CI lives in `.github/workflows/`. `test.yml` runs ruff, mypy and pytest across
3.10/3.12/3.13 — the floor and the ceiling are where breakage appears — and
`release.yml` calls it via `workflow_call` rather than repeating it, so an
untested tag cannot publish. Releasing is `./release.sh <version>`, which bumps
pyproject, runs the same four checks, commits, tags and pushes. It refuses a
dirty tree, a branch behind origin, or an existing tag, because a bad tag is
expensive to undo: a tag-triggered run uses the workflow file *as of the tagged
commit*, so a broken workflow can't be repaired by re-running the tag — the tag
has to be deleted from the remote and re-cut. Actions must be pinned to versions
that exist; `setup-uv` stopped publishing floating major tags after `v7`, and
`@v10` failed the first release attempt. The workflow independently refuses a
tag that disagrees with pyproject, since the install path is a pinned asset URL
and a wheel named after the wrong version is not something to discover
afterwards. `uv build` emits a
`py3-none-any` wheel — universal, hence no platform matrix — plus an sdist, and
both are attached to the GitHub release. The build is byte-reproducible; don't
introduce anything timestamp-dependent into it. There is deliberately no PyPI
publishing.

## State

Working and verified end to end: a session was created on a feature
branch, saved, its bundle rewritten to look like it came from a
different machine (foreign home and repo path), restored into a
different directory with the original deleted so there was no fallback,
and resumed — it recalled content from the original conversation.

The multi-cwd fix is verified against the real session it was found on:
`save` now packs it under the worktree it ended in and indexes it under
`feat/send-worktree-to-remote` (it was packing the main repo and indexing
`main`), a cross-machine restore rewrites all 726 worktree records to the
target, and the 194 records from before the move are reported as
`foreignCwds` with `residualOriginPaths` still empty — which is precisely
the pair of facts the old output could not distinguish from success. The
regression tests fail against the previous selection rule, checked by
reverting it.

The same staleness had a second victim that was not in the brief:
`gitBranch`. It was read from the first record too, so that session
reported `main` and was filed under `index/branch/main` while 726 of its
records were on `feat/send-worktree-to-remote`. Branch and version are
now read from records carrying the *chosen* cwd. This is worth its own
release note rather than folding into the cwd fix, because the blast
radius is different: a consumer that carries the branch itself sees a
healthy-looking transfer while the lochy entry sits under a name nobody
would look for it on, and every already-saved bundle from a moved session
is filed wrong until re-saved or reindexed.

Those 194 records are the honest limit. They name a directory that really
is different, so they keep whatever the home pair made of them and are
counted rather than corrected. `--map` (above) is what would make them
correct rather than merely visible; nothing needs it yet.

`otherCwds` and `foreignCwds` are new payload fields, so no `schema` bump
— but the first consumer (Harness "send worktree to remote") was reading
`residualOriginPaths` as its success signal and needs telling that the
field it wants is `foreignCwds`. Its shape is
`{"<origin path>": {"count": N, "restored": "<path on this machine>"}}`,
keyed on the origin deliberately (above) — it was `{"<path>": <count>}`
briefly and was changed before anything consumed it.

Nothing currently pins the bundle format. The two constraints above —
compact separators and omitted-not-null metadata — are held by prose
rather than by a test, so a well-meaning refactor could change every
ref the format produces without failing the suite. Note that
`SessionMeta.other_cwds` is deliberately *not* packed into the bundle:
the transcript is the source of truth for what directories it names, and
adding a field would change the ref of every bundle for a value the
restore side can recompute.

**The S3 backend has still never run against a real bucket.** It has moto
coverage now, which is more than it had, but mocks agree with your
assumptions by construction. A live run failed on `Access Denied` (the
IAM user lacked `s3:PutObject`); the region mismatch that precedes that
failure surfaces as "The bucket you are attempting to access must be
addressed using the specified endpoint," so set `LOCHY_S3_REGION`
when the bucket's region differs from the AWS config default.

Listing and deleting need IAM actions the bucket policy may not grant
yet: `s3:ListBucket` is **bucket-level**, on `arn:aws:s3:::<bucket>`
rather than `arn:aws:s3:::<bucket>/*` where `s3:GetObject` and
`s3:PutObject` live, and `s3:DeleteObject` is object-level. A missing
`s3:ListBucket` makes `list --remote` and `reindex` fail while `save`
and `restore` keep working.

Redaction on `save` is in and on by default, with no opt-out flag — if
verbatim bundles turn out to be needed, add one deliberately. Its
recall is untested against a real leak: the corpus it was measured on
contained no live credential to catch.

Branch indexing, `delete`, and `reindex` are in, verified against the
file store and moto. The old flat `<ref>.loch` layout was dropped without
a migration: nothing had been stored in it beyond throwaway local
bundles, since every S3 save so far failed on `Access Denied`.

`--json` covers every command, including the empty results and every failure
path, and the suite pins the two invariants a consumer actually leans on:
stdout parses as a single document with nothing else in it, and a failing
command still produces a parseable document with a nonzero exit status. The
error taxonomy was reviewed by its first consumer, which is where the split
between `bundle-not-found` and `store-unreachable` came from, but nothing has
been wired up against it yet, so the field names have never survived contact
with a real integration.

The suite passes on 3.10.13 and 3.13 as well as 3.12, and a boto3-free
install saves and lists against a `file://` store unmodified. **Neither
workflow has ever run**, though — they were written against a repo with no
CI and no release, so the first tag push is also the first execution of the
release path. No release exists yet, so the wheel URL in the README points at
a `v0.1.0` that has to be cut before it resolves.

Also missing: `restore --map` (design intent recorded above, deliberately
not built on this branch), any other index dimension (`cwd` is the
obvious next one, and the layout takes it without a migration), any git
integration (no notes, no PR-level pointers), and no adapters beyond
Claude Code.
`restore` still needs a full 64-character ref — the index makes refs
discoverable, but nothing resolves a prefix.

## Conventions

- Verify before committing: `poetry run ruff check`,
  `poetry run ruff format --check`, `poetry run mypy .`, and
  `poetry run pytest`.
- Commit coherent changes as you go rather than batching.
- **Every command must be fully drivable by a program that never reads the
  human text.** A new command isn't finished until `--json` covers it —
  its result, its empty case, and its failures with a stable `code`. Build
  the payload where the prose is built, never in an `if args.json` beside a
  write call.
- Don't add comments that restate the code. The comments that exist mark
  non-obvious constraints (the single-pass rewrite, the lazy SDK import,
  symlink resolution) — preserve or update those rather than deleting.
- Don't write planning or design documents. Update this file instead.
- Treat transcripts as sensitive: they contain everything an agent read.
