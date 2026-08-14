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

Requires Python 3.12.

## Use

```sh
# what's on this machine for the current repo?
lochy list
lochy list --branch feature/checkout-flow

# pack them up and push to a store
lochy save --branch feature/checkout-flow --store s3://my-bucket/sessions
# -> ref 10482276de745032...

# on another machine, in a checkout of the same branch
lochy restore 10482276de745032... --store s3://my-bucket/sessions
# -> cd /path/to/repo && claude --resume e71cdcb1-7c2c-410b-8c73-91cdf0cba4b8
```

`save` bundles every session matching the filter, so a branch with several
sessions produces a single ref.

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
- **No redaction.** Transcripts contain verbatim tool output — file contents,
  command output, API responses. Anything an agent read is in the bundle.
  Treat a store as being as sensitive as the repo it came from, and prefer a
  bucket you can delete from over anything append-only.

## License

MIT
