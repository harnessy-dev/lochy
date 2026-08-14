import pytest

from lochy.bundle import (
    Bundle,
    BundleOrigin,
    BundleSession,
    bundle_ref,
    pack_bundle,
    unpack_bundle,
)


def sample_bundle(git_branch: str | None = "main") -> Bundle:
    return Bundle(
        version=1,
        agent="claude-code",
        created_at="2026-08-14T13:27:07.373Z",
        origin=BundleOrigin(home="/home/mike", platform="linux", hostname="box"),
        sessions=[
            BundleSession(
                session_id="abc",
                cwd="/home/mike/proj",
                git_branch=git_branch,
                claude_version="1.0.99",
                modified_at="2026-08-14T13:00:00.000Z",
                transcript='{"cwd":"/home/mike/proj"}\n',
            )
        ],
    )


def test_pack_round_trips() -> None:
    bundle = sample_bundle()
    assert unpack_bundle(pack_bundle(bundle)) == bundle


def test_pack_is_deterministic() -> None:
    assert pack_bundle(sample_bundle()) == pack_bundle(sample_bundle())


def test_ref_is_the_sha256_of_the_packed_bytes() -> None:
    import hashlib

    packed = pack_bundle(sample_bundle())
    assert bundle_ref(packed) == hashlib.sha256(packed).hexdigest()


def test_absent_metadata_stays_absent_rather_than_null() -> None:
    import gzip
    import json

    raw = json.loads(gzip.decompress(pack_bundle(sample_bundle(git_branch=None))))
    assert "gitBranch" not in raw["sessions"][0]
    assert (
        unpack_bundle(pack_bundle(sample_bundle(git_branch=None)))
        .sessions[0]
        .git_branch
        is None
    )


def test_rejects_an_unsupported_version() -> None:
    packed = pack_bundle(
        Bundle(
            version=2,
            agent="claude-code",
            created_at="",
            origin=BundleOrigin(home="", platform="", hostname=""),
            sessions=[],
        )
    )
    with pytest.raises(ValueError, match="unsupported bundle version 2"):
        unpack_bundle(packed)
