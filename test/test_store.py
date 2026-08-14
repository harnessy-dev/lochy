import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from sessionport.store import (
    DEFAULT_STORE,
    FileStore,
    S3Store,
    create_store,
    resolve_store_uri,
)


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("SESSIONPORT_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("SESSIONPORT_S3_REGION", raising=False)


def test_file_store_round_trips(tmp_path: Path) -> None:
    store = FileStore(str(tmp_path / "store"))
    store.put("abc.spb", b"payload")
    assert store.get("abc.spb") == b"payload"
    assert store.describe() == str(tmp_path / "store")


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
    monkeypatch.delenv("SESSIONPORT_STORE", raising=False)
    assert resolve_store_uri() == DEFAULT_STORE
    monkeypatch.setenv("SESSIONPORT_STORE", "s3://from-env/x")
    assert resolve_store_uri() == "s3://from-env/x"
    assert resolve_store_uri("/explicit") == "/explicit"


@mock_aws
def test_s3_store_round_trips(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "bundles")
    store.put("abc.spb", b"payload")

    assert store.get("abc.spb") == b"payload"
    stored = boto3.client("s3", region_name="us-east-1").get_object(
        Bucket="sessions", Key="bundles/abc.spb"
    )
    assert stored["ContentType"] == "application/gzip"


@mock_aws
def test_s3_store_without_a_prefix(aws_credentials: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="sessions")

    store = S3Store("sessions", "")
    store.put("abc.spb", b"payload")

    stored = boto3.client("s3", region_name="us-east-1").list_objects_v2(
        Bucket="sessions"
    )
    assert [item["Key"] for item in stored["Contents"]] == ["abc.spb"]


@mock_aws
def test_s3_store_honours_the_region_override(
    aws_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SESSIONPORT_S3_REGION", "us-east-2")
    boto3.client("s3", region_name="us-east-2").create_bucket(
        Bucket="sessions",
        CreateBucketConfiguration={"LocationConstraint": "us-east-2"},
    )

    store = S3Store("sessions", "")
    store.put("abc.spb", b"payload")
    assert store.get("abc.spb") == b"payload"
