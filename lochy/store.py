import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

DEFAULT_STORE = os.path.join(str(Path.home()), ".lochy", "store")


class MissingObject(Exception):
    """The store answered and the object isn't there. Distinct from a store
    that didn't answer at all: one is terminal, the other worth retrying, and
    a caller has to be able to tell them apart."""


class MissingDependency(Exception):
    """A backend whose SDK isn't installed. Neither terminal nor worth
    retrying: the fix is an install, so a caller has to be able to tell this
    from a store that didn't answer."""


class Store(ABC):
    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]: ...

    # Deleting an absent key succeeds: index entries are derived, so a repair
    # pass has to be able to remove ones a half-finished save never wrote.
    @abstractmethod
    def delete(self, key: str) -> None: ...


class FileStore(Store):
    def __init__(self, root: str) -> None:
        self._root = root

    def describe(self) -> str:
        return self._root

    def put(self, key: str, data: bytes) -> None:
        path = Path(self._root) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return (Path(self._root) / key).read_bytes()
        except FileNotFoundError as error:
            raise MissingObject(f"no object at {key}") from error

    def list(self, prefix: str) -> list[str]:
        root = Path(self._root)
        if not root.is_dir():
            return []
        # String prefix rather than directory descent, so both backends answer
        # the same question for a prefix that stops mid-segment.
        return sorted(
            key
            for path in root.rglob("*")
            if path.is_file()
            and (key := path.relative_to(root).as_posix()).startswith(prefix)
        )

    def delete(self, key: str) -> None:
        (Path(self._root) / key).unlink(missing_ok=True)


class S3Store(Store):
    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix

    def describe(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    def _key_for(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _relative_key(self, key: str) -> str:
        return key[len(self._prefix) + 1 :] if self._prefix else key

    # Imported lazily so the file backend never pays for loading the SDK —
    # which is also what lets boto3 be an optional extra rather than a
    # dependency every install carries.
    def _client(self) -> "S3Client":
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise MissingDependency(
                "the S3 backend needs boto3, which this install doesn't have "
                "(reinstall as lochy[s3])"
            ) from error

        endpoint = os.environ.get("LOCHY_S3_ENDPOINT")
        region = os.environ.get("LOCHY_S3_REGION")
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            # A custom endpoint means MinIO/R2/Garage, which need path style.
            config=Config(s3={"addressing_style": "path"}) if endpoint else None,
        )

    def put(self, key: str, data: bytes) -> None:
        self._client().put_object(
            Bucket=self._bucket,
            Key=self._key_for(key),
            Body=data,
            ContentType="application/gzip",
        )

    def get(self, key: str) -> bytes:
        # Before the botocore import, so an install without the extra reports
        # the missing dependency rather than a bare ImportError.
        client = self._client()
        from botocore.exceptions import ClientError

        try:
            response = client.get_object(Bucket=self._bucket, Key=self._key_for(key))
        except ClientError as error:
            # A missing bucket stays a plain ClientError: it means the store
            # is misconfigured, not that this one object was deleted.
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise MissingObject(f"no object at {key}") from error
            raise
        body = response.get("Body")
        if body is None:
            raise ValueError(f"empty object at {self._key_for(key)}")
        data: bytes = body.read()
        return data

    def list(self, prefix: str) -> list[str]:
        # Paginated: list_objects_v2 truncates at 1000 keys.
        pages = self._client().get_paginator("list_objects_v2")
        return sorted(
            self._relative_key(item["Key"])
            for page in pages.paginate(
                Bucket=self._bucket, Prefix=self._key_for(prefix)
            )
            for item in page.get("Contents", [])
        )

    def delete(self, key: str) -> None:
        self._client().delete_object(Bucket=self._bucket, Key=self._key_for(key))


def create_store(uri: str) -> Store:
    if uri.startswith("s3://"):
        without_scheme = uri[len("s3://") :]
        slash = without_scheme.find("/")
        bucket = without_scheme if slash == -1 else without_scheme[:slash]
        prefix = "" if slash == -1 else re.sub(r"/$", "", without_scheme[slash + 1 :])
        if not bucket:
            raise ValueError(f"invalid S3 store URI: {uri}")
        return S3Store(bucket, prefix)

    if uri.startswith("file://"):
        return FileStore(unquote(urlparse(uri).path))
    return FileStore(uri if os.path.isabs(uri) else os.path.abspath(uri))


def resolve_store_uri(explicit: str | None = None) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get("LOCHY_STORE", DEFAULT_STORE)
