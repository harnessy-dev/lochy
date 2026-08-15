from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from lochy.bundle import (
    BUNDLE_VERSION,
    Bundle,
    BundleOrigin,
    BundleSession,
    bundle_ref,
    pack_bundle,
)
from lochy.index import (
    BRANCH,
    bundle_key,
    decode_segment,
    delete_bundle,
    dimension_prefix,
    encode_segment,
    entries_for,
    index_bundle,
    index_key,
    load_entries,
    ref_from_key,
    reindex,
    value_prefix,
)
from lochy.store import FileStore, S3Store


def session(session_id: str, branch: str | None, cwd: str = "/repo") -> BundleSession:
    return BundleSession(
        session_id=session_id,
        cwd=cwd,
        git_branch=branch,
        claude_version="1.0.99",
        modified_at="2026-08-15T12:00:00.000Z",
        transcript="{}\n",
    )


def make_bundle(
    *sessions: BundleSession, created_at: str = "2026-08-15T12:00:00.000Z"
) -> Bundle:
    return Bundle(
        version=BUNDLE_VERSION,
        agent="claude-code",
        created_at=created_at,
        origin=BundleOrigin(home="/home/origin", platform="linux", hostname="box"),
        sessions=list(sessions),
    )


def store_with(tmp_path: Path, bundle: Bundle) -> tuple[FileStore, str, int]:
    store = FileStore(str(tmp_path / "store"))
    packed = pack_bundle(bundle)
    ref = bundle_ref(packed)
    store.put(bundle_key(ref), packed)
    index_bundle(store, bundle, ref, len(packed))
    return store, ref, len(packed)


def test_encoding_keeps_a_slashed_branch_in_one_segment() -> None:
    assert encode_segment("feature/foo") == "feature%2Ffoo"
    assert "/" not in encode_segment("feature/foo/bar")
    assert decode_segment(encode_segment("feature/foo")) == "feature/foo"


def test_encoding_distinguishes_branches_claudes_scheme_would_collide() -> None:
    assert encode_segment("feature/foo") != encode_segment("feature-foo")
    assert encode_segment("feature-foo") == "feature-foo"


def test_encoding_round_trips_an_absent_value() -> None:
    assert decode_segment(encode_segment(None)) is None
    assert "/" not in encode_segment(None)


def test_keys_follow_the_dimension_layout() -> None:
    assert bundle_key("abc") == "bundles/abc.loch"
    assert ref_from_key(bundle_key("abc")) == "abc"
    assert index_key(BRANCH, "feature/foo", "abc") == "index/branch/feature%2Ffoo/abc"
    assert value_prefix(BRANCH, "main") == "index/branch/main/"
    assert dimension_prefix(BRANCH) == "index/branch/"


def test_a_bundle_spanning_branches_is_indexed_under_each() -> None:
    bundle = make_bundle(
        session("one", "main"),
        session("two", "feature/foo"),
        session("three", "feature/foo"),
    )
    entries = entries_for(bundle, "abc", 512)

    assert [(entry.value, entry.sessions) for entry in entries] == [
        ("feature/foo", 2),
        ("main", 1),
    ]
    assert {entry.ref for entry in entries} == {"abc"}
    assert entries[0].bytes == 512
    assert entries[0].cwd == "/repo"
    assert entries[0].claude_version == "1.0.99"
    assert entries[0].agent == "claude-code"


def test_a_branchless_session_still_gets_an_entry() -> None:
    entries = entries_for(make_bundle(session("one", None)), "abc", 1)
    assert entries[0].value is None
    assert index_key(BRANCH, None, "abc").count("/") == 3


def test_entries_are_readable_without_fetching_the_bundle(tmp_path: Path) -> None:
    bundle = make_bundle(session("one", "main"), session("two", "feature/foo"))
    store, ref, size = store_with(tmp_path, bundle)

    on_branch = load_entries(store, value_prefix(BRANCH, "feature/foo"))
    assert [entry.ref for entry in on_branch] == [ref]
    assert on_branch[0].sessions == 1
    assert on_branch[0].bytes == size

    assert len(load_entries(store, dimension_prefix(BRANCH))) == 2
    assert load_entries(store, value_prefix(BRANCH, "nope")) == []


def test_entries_come_back_newest_first(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    for created_at, session_id in (
        ("2026-08-01T00:00:00.000Z", "old"),
        ("2026-08-14T00:00:00.000Z", "new"),
    ):
        bundle = make_bundle(session(session_id, "main"), created_at=created_at)
        packed = pack_bundle(bundle)
        index_bundle(store, bundle, bundle_ref(packed), len(packed))

    assert [
        entry.created_at for entry in load_entries(store, dimension_prefix(BRANCH))
    ] == [
        "2026-08-14T00:00:00.000Z",
        "2026-08-01T00:00:00.000Z",
    ]


def test_delete_removes_the_bundle_and_every_entry_it_produced(tmp_path: Path) -> None:
    bundle = make_bundle(session("one", "main"), session("two", "feature/foo"))
    store, ref, _ = store_with(tmp_path, bundle)

    removed = delete_bundle(store, ref)

    assert bundle_key(ref) in removed
    assert index_key(BRANCH, "feature/foo", ref) in removed
    assert store.list("") == []


def test_delete_leaves_other_bundles_indexed(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    refs = []
    for session_id in ("one", "two"):
        bundle = make_bundle(session(session_id, "main"))
        packed = pack_bundle(bundle)
        ref = bundle_ref(packed)
        store.put(bundle_key(ref), packed)
        index_bundle(store, bundle, ref, len(packed))
        refs.append(ref)

    delete_bundle(store, refs[0])

    assert [entry.ref for entry in load_entries(store, dimension_prefix(BRANCH))] == [
        refs[1]
    ]


def test_delete_reports_a_missing_ref(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    with pytest.raises(FileNotFoundError):
        delete_bundle(store, "nope")


def test_reindex_rebuilds_the_index_from_the_bundles_alone(tmp_path: Path) -> None:
    bundle = make_bundle(session("one", "main"), session("two", "feature/foo"))
    store, ref, _ = store_with(tmp_path, bundle)
    before = load_entries(store, dimension_prefix(BRANCH))

    for key in store.list("index/"):
        store.delete(key)
    assert load_entries(store, dimension_prefix(BRANCH)) == []

    result = reindex(store)

    assert result.bundles == 1
    assert result.entries == 2
    assert load_entries(store, dimension_prefix(BRANCH)) == before


def test_reindex_drops_entries_no_bundle_backs(tmp_path: Path) -> None:
    bundle = make_bundle(session("one", "main"))
    store, ref, _ = store_with(tmp_path, bundle)
    orphan = index_key(BRANCH, "gone", "deadbeef")
    store.put(orphan, b'{"ref":"deadbeef","dimension":"branch","value":"gone"}')

    result = reindex(store)

    assert result.removed == 1
    assert orphan not in store.list("index/")
    assert store.list(value_prefix(BRANCH, "main")) == [index_key(BRANCH, "main", ref)]


@mock_aws
def test_an_encoded_branch_survives_a_round_trip_through_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("LOCHY_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("LOCHY_S3_REGION", raising=False)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "prefix")
    bundle = make_bundle(session("one", "feature/foo"))
    packed = pack_bundle(bundle)
    ref = bundle_ref(packed)
    store.put(bundle_key(ref), packed)
    index_bundle(store, bundle, ref, len(packed))

    entries = load_entries(store, value_prefix(BRANCH, "feature/foo"))
    assert [entry.value for entry in entries] == ["feature/foo"]

    delete_bundle(store, ref)
    assert store.list("") == []
