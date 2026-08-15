import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from lochy.store import (
    DEFAULT_STORE,
    FileStore,
    MissingObject,
    S3Store,
    create_store,
    resolve_store_uri,
)


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("LOCHY_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("LOCHY_S3_REGION", raising=False)


def test_file_store_round_trips(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    store.put("abc.loch", b"payload")
    assert store.get("abc.loch") == b"payload"
    assert store.describe() == str(tmp_path / "store")


def test_file_store_lists_by_prefix_and_deletes(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    store.put("bundles/abc.loch", b"a")
    store.put("index/branch/main/abc", b"b")
    store.put("index/branch/feature%2Ffoo/abc", b"c")

    assert store.list("") == [
        "bundles/abc.loch",
        "index/branch/feature%2Ffoo/abc",
        "index/branch/main/abc",
    ]
    assert store.list("index/branch/ma") == ["index/branch/main/abc"]
    assert store.list("nothing/") == []

    store.delete("index/branch/main/abc")
    store.delete("index/branch/main/abc")
    assert store.list("index/") == ["index/branch/feature%2Ffoo/abc"]


def test_file_store_lists_nothing_before_anything_is_written(tmp_path: Path) -> None:
    assert FileStore(str(tmp_path / "missing")).list("") == []


def test_file_store_reports_an_absent_object_as_missing_not_as_an_os_error(
    tmp_path: Path,
) -> None:
    store = FileStore(str(tmp_path / "store"))
    with pytest.raises(MissingObject):
        store.get("bundles/nope.loch")


def test_file_store_lets_an_unreachable_root_surface_as_itself(
    tmp_path: Path,
) -> None:
    """A store that can't answer is not a store answering "absent" — the
    caller retries one and gives up on the other."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError) as failure:
        FileStore(str(blocked)).get("bundles/nope.loch")
    assert not isinstance(failure.value, MissingObject)


def test_create_store_parses_uris(tmp_path: Path) -> None:
    assert isinstance(create_store(str(tmp_path)), FileStore)
    assert create_store(f"file://{tmp_path}").describe() == str(tmp_path)
    assert create_store("relative/path").describe() == os.path.abspath("relative/path")

    s3 = create_store("s3://bucket/sessions/")
    assert isinstance(s3, S3Store)
    assert s3.describe() == "s3://bucket/sessions"
    assert create_store("s3://bucket").describe() == "s3://bucket/"


def test_create_store_rejects_a_bucketless_s3_uri() -> None:
    with pytest.raises(ValueError, match="invalid S3 store URI"):
        create_store("s3:///sessions")


def test_resolve_store_uri_prefers_explicit_then_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCHY_STORE", raising=False)
    assert resolve_store_uri() == DEFAULT_STORE
    monkeypatch.setenv("LOCHY_STORE", "s3://from-env/x")
    assert resolve_store_uri() == "s3://from-env/x"
    assert resolve_store_uri("/explicit") == "/explicit"


@mock_aws
def test_s3_store_round_trips(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "bundles")
    store.put("abc.loch", b"payload")

    assert store.get("abc.loch") == b"payload"
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="sessions", Key="bundles/abc.loch"
    )
    assert stored["ContentType"] == "application/gzip"


@mock_aws
def test_s3_store_reports_an_absent_key_as_missing(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    with pytest.raises(MissingObject):
        S3Store("sessions", "bundles").get("abc.loch")


@mock_aws
def test_s3_store_lets_a_missing_bucket_surface_as_itself(
    aws_credentials: None,
) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        S3Store("no-such-bucket", "bundles").get("abc.loch")


@mock_aws
def test_s3_store_without_a_prefix(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "")
    store.put("abc.loch", b"payload")

    stored = boto3.client("s3", region_name="us-east-1").list_objects_v2(
        Bucket="sessions"
    )
    assert [item["Key"] for item in stored["Contents"]] == ["abc.loch"]


@mock_aws
def test_s3_store_lists_by_prefix_and_deletes(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "bundles")
    store.put("bundles/abc.loch", b"a")
    store.put("index/branch/main/abc", b"b")

    assert store.list("") == ["bundles/abc.loch", "index/branch/main/abc"]
    assert store.list("index/") == ["index/branch/main/abc"]

    store.delete("index/branch/main/abc")
    store.delete("index/branch/main/abc")
    assert store.list("") == ["bundles/abc.loch"]


@mock_aws
def test_s3_store_lists_past_the_first_page(aws_credentials: None) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="sessions")
    for number in range(1001):
        client.put_object(
            Bucket="sessions", Key=f"index/branch/main/{number:04d}", Body=b""
        )

    assert len(S3Store("sessions", "").list("index/")) == 1001


@mock_aws
def test_s3_store_honours_the_region_override(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCHY_S3_REGION", "us-east-2")
    boto3.client("s3", region_name="us-east-2").create_bucket(
        Bucket="sessions",
        CreateBucketConfiguration={"LocationConstraint": "us-east-2"},
    )

    store = S3Store("sessions", "")
    store.put("abc.loch", b"payload")
    assert store.get("abc.loch") == b"payload"
