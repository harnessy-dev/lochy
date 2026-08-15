# lochy

Save and restore agent coding sessions across machines.

Agent CLIs keep their transcripts in machine-local directories keyed by the
working directory they ran in. That makes a session effectively stuck to the
laptop that produced it — you can't hand a teammate the conversation that
produced a branch, and you can't pick one up on another machine.

`lochy` packs those transcripts into a portable, content-addressed
bundle, stores it anywhere S3-compatible, and unpacks it on another machine
with the paths rewritten so the session actually resumes.

Currently supports Claude Code.

## Install

```sh
poetry install
poetry run lochy --help

pipx install .    # optional, puts `lochy` on PATH
```

Requires Python 3.12 — pass `--python python3.12` to `pipx` if that isn't the
interpreter it picks by default.

## Use

```sh
# what's on this machine for the current repo?
lochy list
lochy list --branch feature/checkout-flow

# pack them up and push to a store
lochy save --branch feature/checkout-flow --store s3://my-bucket/sessions
# -> packed e71cdcb1... [feature/checkout-flow] — redacted 2 secrets
# -> ref 10482276de745032...

# on another machine: what's been saved for the branch I'm on?
lochy list --remote --store s3://my-bucket/sessions
lochy list --remote --branch feature/checkout-flow
lochy list --all            # every branch in the store

lochy restore 10482276de745032... --store s3://my-bucket/sessions
# -> cd /path/to/repo && claude --resume e71cdcb1-7c2c-410b-8c73-91cdf0cba4b8

# a transcript that shouldn't be there any more
lochy delete 10482276de745032...

# rebuild the index from the bundles, if a save was interrupted
lochy reindex
```

`save` bundles every session matching the filter, so a branch with several
sessions produces a single ref.

## Layout of a store

```
bundles/<ref>.loch                 the bundle, named by the hash of its bytes
index/branch/<branch>/<ref>        a small entry per branch the bundle touches
```

`lochy list --remote` reads only the index, so listing a branch never
downloads a bundle. Entries are derived from the bundles and hold nothing
that isn't already in them — `lochy reindex` rebuilds the whole index by
scanning `bundles/`, and drops entries no bundle backs.

Branch names are percent-encoded, so `feature/foo` stays one path segment
and doesn't collide with `feature-foo`. Every index write goes to its own
key, so two machines saving at the same time can't overwrite each other's
entries.

An S3 store needs `s3:GetObject` and `s3:PutObject` on
`arn:aws:s3:::<bucket>/*`, plus `s3:ListBucket` on `arn:aws:s3:::<bucket>`
itself for `list`/`reindex` and `s3:DeleteObject` for `delete`.

## Stores

| URI | Backend |
| --- | --- |
| `/some/path` or `file:///some/path` | local directory |
| `s3://bucket/prefix` | any S3-compatible endpoint |

| Variable | Purpose |
| --- | --- |
| `LOCHY_STORE` | default store URI (else `~/.lochy/store`) |
| `LOCHY_S3_ENDPOINT` | custom endpoint for R2, MinIO, Backblaze, ... |
| `LOCHY_S3_REGION` | region override |

Credentials come from the standard AWS chain, so `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, a shared profile, or an instance role all work.

## How it works

Claude Code stores a session at
`~/.claude/projects/<cwd with every non-alphanumeric character replaced by ->/<session-id>.jsonl`,
using the symlink-resolved cwd (so `/tmp` is really `/private/tmp` on macOS).

A bundle is gzipped JSON holding each transcript verbatim plus a manifest
recording the origin machine's home directory, platform, and the cwd each
session ran in. The ref is the SHA-256 of the packed bytes.

On restore, the origin's paths are rewritten to local ones — the repo path,
the home directory, and the encoded project-directory form — in a single
left-to-right pass, longest match first, so a cwd nested under a home
directory can't be half-rewritten. The result is written to the local
project directory for the target cwd.

`--new-id` mints a fresh session id, rewriting the `sessionId` field on every
line to match the new filename. Claude Code keys off both, and a file where
they disagree resumes into a hybrid transcript.

## Limitations

- **Claude Code only.** Codex and Cursor are the intended next adapters;
  Cursor keeps chat state in a SQLite workspace database rather than files,
  so it needs a different capture strategy.
- **Same-agent resume only.** A Claude transcript can't be resumed by Codex —
  the tool-call schemas differ. Cross-agent handoff would mean injecting a
  normalized transcript as context, which is a different feature.
- **Path rewriting is textual.** Absolute paths from the origin machine are
  substituted, but a session that referenced files outside the repo will
  still point at paths that don't exist locally. `restore` warns when origin
  paths survive the rewrite.
- **Redaction is best-effort.** `save` scrubs credentials with distinctive
  structure — AWS, GitHub, Slack, Stripe, Anthropic, OpenAI and Google keys,
  JWTs, PEM blocks — plus `UPPERCASE_NAME=value` assignments, which is the
  only way to catch an AWS secret access key. It will miss anything shaped
  like ordinary text. **A redacted bundle is not a safe bundle: rotate any
  credential an agent has read.**
- **Transcripts are sensitive regardless.** They hold verbatim tool output —
  file contents, command output, API responses. Anything an agent read is in
  the bundle. Treat a store as being as sensitive as the repo it came from,
  and prefer a bucket you can delete from over anything append-only.

## License

MIT
