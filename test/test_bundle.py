from pathlib import Path

import pytest

from lochy.bundle import (
    Bundle,
    BundleOrigin,
    BundleSession,
    bundle_ref,
    pack_bundle,
    unpack_bundle,
)
from lochy.rewrite import RewriteSpec, rewrite_transcript

FIXTURES = Path(__file__).parent / "fixtures"
TS_REF = "39200e4051294da38a5e6dff17cf8144e3013245ba42afd9f470cbeb794b6fd2"

# The machine the fixture bundle was captured on, and the one the TypeScript
# build restored it onto — both baked into restored-by-typescript.jsonl.
ORIGIN_HOME = "/private/tmp/sp-fixture/home"
ORIGIN_CWD = "/private/tmp/sp-fixture/home/proj"
TARGET_HOME = "/private/tmp/sp-fixture/target-home"
TARGET_CWD = "/private/tmp/sp-fixture/target-home/work/proj"


def ts_bundle_bytes() -> bytes:
    return (FIXTURES / f"{TS_REF}.loch").read_bytes()


def test_reads_a_bundle_written_by_the_typescript_implementation() -> None:
    bundle = unpack_bundle(ts_bundle_bytes())

    assert bundle.version == 1
    assert bundle.agent == "claude-code"
    assert bundle.origin.home == ORIGIN_HOME
    assert bundle.origin.platform == "darwin"
    assert len(bundle.sessions) == 1

    session = bundle.sessions[0]
    assert session.session_id == "11111111-2222-4333-8444-555555555555"
    assert session.cwd == ORIGIN_CWD
    assert session.git_branch == "feature/widget"
    assert session.claude_version == "1.0.99"


def test_ref_of_the_fixture_matches_its_filename() -> None:
    assert bundle_ref(ts_bundle_bytes()) == TS_REF


def test_restoring_the_typescript_bundle_is_byte_identical() -> None:
    session = unpack_bundle(ts_bundle_bytes()).sessions[0]

    restored = rewrite_transcript(
        session.transcript,
        RewriteSpec(
            origin_cwd=session.cwd,
            origin_home=ORIGIN_HOME,
            target_cwd=TARGET_CWD,
            target_home=TARGET_HOME,
            origin_session_id=session.session_id,
            target_session_id=session.session_id,
        ),
    )

    expected = (FIXTURES / "restored-by-typescript.jsonl").read_text(encoding="utf-8")
    assert restored == expected


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
