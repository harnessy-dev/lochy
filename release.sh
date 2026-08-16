#!/usr/bin/env bash
# Cut a release. Bumps the version, verifies, tags, and pushes; CI builds the
# wheel and publishes the GitHub release from the tag.
#
#   ./release.sh 0.2.0
#   DRY_RUN=1 ./release.sh 0.2.0     rehearse without committing or pushing
set -euo pipefail

BRANCH=main
DRY_RUN=${DRY_RUN:-}

die() { printf 'release: %s\n' "$1" >&2; exit 1; }
run() { if [ -n "$DRY_RUN" ]; then printf '  would run: %s\n' "$*"; else "$@"; fi; }

[ $# -eq 1 ] || die "usage: ./release.sh <version>   (e.g. ./release.sh 0.2.0)"
version=${1#v}
echo "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$' \
  || die "'$version' is not a version like 0.2.0"

cd "$(dirname "$0")"

tag="v$version"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || die "not on $BRANCH"
[ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit or stash first"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "$tag already exists locally"

git fetch --quiet origin "$BRANCH" --tags
git ls-remote --exit-code --tags origin "$tag" >/dev/null 2>&1 && die "$tag already exists on origin"
[ -z "$(git rev-list "HEAD..origin/$BRANCH")" ] || die "$BRANCH is behind origin; pull first"

# The tag has to point at a commit containing the workflow that builds it: a
# tag-triggered run uses the workflow file as of the tagged commit.
echo "==> setting version to $version"
python3 - "$version" <<'PY'
import pathlib, re, sys
path = pathlib.Path("pyproject.toml")
text = path.read_text()
new, count = re.subn(r'(?m)^version = "[^"]*"$', f'version = "{sys.argv[1]}"', text, count=1)
if count != 1:
    raise SystemExit("release: could not find a single version line in pyproject.toml")
path.write_text(new)
PY
grep -q "^version = \"$version\"$" pyproject.toml || die "version bump did not apply"

# Not asserting `lochy --version` here: it reads installed package metadata,
# which still holds the old number until the bumped project is reinstalled.
# The release workflow smoke-tests it against a freshly built wheel instead.
echo "==> verifying"
poetry run ruff check
poetry run ruff format --check
poetry run mypy .
poetry run pytest -q

echo "==> tagging $tag"
run git add pyproject.toml
run git commit -m "release: $tag"
run git tag -a "$tag" -m "$tag"
run git push origin "$BRANCH"
run git push origin "$tag"

if [ -n "$DRY_RUN" ]; then
  git checkout -- pyproject.toml
  echo "==> dry run: pyproject.toml restored, nothing pushed"
  exit 0
fi

slug=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
cat <<EOF

released $tag
  watch:   https://github.com/$slug/actions
  install: uv tool install https://github.com/$slug/releases/download/$tag/lochy-$version-py3-none-any.whl
EOF
