import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

DEFAULT_STORE = os.path.join(str(Path.home()), ".sessionport", "store")


class Store(ABC):
    @abstractmethod
    def describe(self) -> str: ...

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...


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
        return (Path(self._root) / key).read_bytes()


class S3Store(Store):
    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix

    def describe(self) -> str:
        return f"s3://{self._bucket}/{self._prefix}"

    def _key_for(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    # Imported lazily so the file backend never pays for loading the SDK.
    def _client(self) -> "S3Client":
        import boto3
        from botocore.config import Config

        endpoint = os.environ.get("SESSIONPORT_S3_ENDPOINT")
        region = os.environ.get("SESSIONPORT_S3_REGION")
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
        response = self._client().get_object(
            Bucket=self._bucket, Key=self._key_for(key)
        )
        body = response.get("Body")
        if body is None:
            raise ValueError(f"empty object at {self._key_for(key)}")
        data: bytes = body.read()
        return data


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
    return os.environ.get("SESSIONPORT_STORE", DEFAULT_STORE)
