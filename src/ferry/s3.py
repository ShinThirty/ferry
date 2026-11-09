"""A hub in an S3 bucket: s3://<bucket>/<prefix>. boto3 comes with ferry's
s3 extra, and is loaded only for a hub in S3.

Each key is an object under the prefix. S3 shows an object whole or not at
all, and every reader sees it once its write returns, so a file needs no .part
beside it. A folder is an empty object named <folder>/, so a machine that has
pushed nothing yet is still on the hub. The claim is a write S3 makes only if
the object isn't there (If-None-Match), which lets one machine win.

A stat's sig is the object's size and ETag: LastModified is whole seconds, and
age seals the same text to different bytes every time, so the ETag moves with
every write.

Credentials, region and endpoint are boto3's own, as for the AWS CLI:
~/.aws/config and credentials, AWS_PROFILE, AWS_REGION, AWS_ENDPOINT_URL. A
hub that can't be reached raises ConnectionError, as a share that has gone
away does; a missing bucket, or credentials that won't do, a StoreError.
"""
from __future__ import annotations

import errno
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError, NoCredentialsError
from botocore.exceptions import ConnectionError as BotoConnectionError

from .store import Stat, Store, StoreError

# Seconds, not boto3's minutes: a session start waits on this, and away from
# the network it should give up and work from local state.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 3})
PERMISSIONS = "s3:ListBucket on the bucket, and s3:GetObject, s3:PutObject, s3:DeleteObject under the prefix"


def _code(e: ClientError) -> tuple[str, int]:
    return e.response.get("Error", {}).get("Code", ""), e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)


class S3Store(Store):
    mounted = False
    missing_hint = "is that the hub's prefix in the bucket?"

    def __init__(self, bucket: str, prefix: str, client=None):
        """prefix is "" for a whole bucket, or ends in /."""
        self.bucket = bucket
        self.prefix = prefix
        self.location = f"s3://{bucket}/{prefix}".rstrip("/")
        self._client = client

    @classmethod
    def at(cls, location: str) -> S3Store:
        bucket, _, prefix = location.removeprefix("s3://").partition("/")
        if not bucket:
            raise StoreError(f"no bucket in {location} — a hub in S3 is s3://BUCKET/PREFIX")
        prefix = prefix.strip("/")
        return cls(bucket, prefix + "/" if prefix else "")

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("s3", config=CLIENT_CONFIG)
        return self._client

    def _key(self, key: str) -> str:
        return self.prefix + key

    def _folder(self, key: str) -> str:
        return f"{self.prefix}{key}/" if key else self.prefix

    @contextmanager
    def _calls(self, key: str = "") -> Iterator[None]:
        """boto3's errors as ferry's: a missing object FileNotFoundError, a hub
        that can't be reached ConnectionError, the rest StoreError."""
        try:
            yield
        except ClientError as e:
            code, status = _code(e)
            msg = e.response.get("Error", {}).get("Message", "") or str(e)
            if code == "NoSuchBucket":
                raise StoreError(f"there's no bucket {self.bucket} — or not one these AWS credentials can see") from e
            if code in ("NoSuchKey", "404") or status == 404:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), f"{self.location}/{key}") from e
            if code == "AccessDenied" or status == 403:
                raise StoreError(f"access denied to {self.location} — ferry needs {PERMISSIONS} ({msg})") from e
            raise StoreError(f"{self.location}: {code or status} — {msg}") from e
        except S3UploadFailedError as e:
            raise StoreError(f"can't write {self.location}/{key} — {e}") from e
        except (BotoConnectionError, HTTPClientError) as e:
            raise ConnectionError(f"can't reach {self.location} — {e}") from e
        except NoCredentialsError as e:
            raise StoreError(f"no AWS credentials for {self.location} — aws configure sets them up, "
                             "or AWS_PROFILE names a profile") from e
        except BotoCoreError as e:
            raise StoreError(f"{self.location}: {e}") from e

    def _listing(self, key: str) -> tuple[set[str], set[str]]:
        """The folders and the files directly in folder key."""
        folder, folders, files = self._folder(key), set(), set()
        with self._calls(key):
            for page in self.client.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=folder, Delimiter="/"):
                folders |= {p["Prefix"][len(folder):-1] for p in page.get("CommonPrefixes", [])}
                files |= {o["Key"][len(folder):] for o in page.get("Contents", [])}
        files.discard("")                   # the folder's own marker
        return folders, files

    def check_vacant(self) -> None:
        with self._calls():
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except ClientError as e:
                if _code(e)[1] == 404:
                    raise StoreError(f"there's no bucket {self.bucket} — make it first, or check the name") from e
                raise
            found = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        if found.get("KeyCount"):
            raise StoreError(f"{self.location} isn't empty — a hub starts where nothing is yet")

    def mkdir(self, key: str) -> None:
        if folder := self._folder(key):
            with self._calls(key):
                self.client.put_object(Bucket=self.bucket, Key=folder, Body=b"")

    def is_folder(self, key: str) -> bool:
        with self._calls(key):
            return bool(self.client.list_objects_v2(Bucket=self.bucket, Prefix=self._folder(key), MaxKeys=1)
                        .get("KeyCount"))

    def folders(self, key: str) -> set[str]:
        return self._listing(key)[0]

    def files(self, key: str) -> set[str]:
        return self._listing(key)[1]

    def stat(self, key: str) -> Stat | None:
        try:
            with self._calls(key):
                head = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except FileNotFoundError:
            return None
        return Stat(f"{head['ContentLength']} {head['ETag']}", head["LastModified"].timestamp())

    @contextmanager
    def reading(self, key: str) -> Iterator[Path]:
        fd, name = tempfile.mkstemp(prefix="ferry.")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as f, self._calls(key):
                body = self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"]
                shutil.copyfileobj(body, f, 1 << 20)
            yield path
        finally:
            path.unlink(missing_ok=True)

    @contextmanager
    def writing(self, key: str) -> Iterator[Path]:
        """Uploaded once filled; one bigger than a part goes up in parts, and
        appears only once the last one has."""
        fd, name = tempfile.mkstemp(prefix="ferry.")
        os.close(fd)
        path = Path(name)
        try:
            yield path
            with self._calls(key):
                self.client.upload_file(name, self.bucket, self._key(key))
        finally:
            path.unlink(missing_ok=True)

    def claim(self, key: str, data: bytes) -> bool:
        with self._calls(key):
            try:
                self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, IfNoneMatch="*")
            except ClientError as e:
                # 409: another machine's claim of it is under way at this moment
                if _code(e)[0] in ("PreconditionFailed", "ConditionalRequestConflict"):
                    return False
                raise
        return True

    def delete(self, key: str) -> None:
        with self._calls(key):
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def delete_folder(self, key: str) -> None:
        with self._calls(key):
            for page in self.client.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=self._folder(key)):
                if objects := [{"Key": o["Key"]} for o in page.get("Contents", [])]:
                    done = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True})
                    if err := (done.get("Errors") or [None])[0]:
                        raise StoreError(f"can't delete {self.location}/{key} — {err.get('Code')}: {err.get('Message')}")

    def clean(self, key: str) -> None:
        """Uploads in parts that never finished: S3 keeps, and bills, their parts
        until they're aborted. Only this machine writes in its slot, one run at a
        time, so any upload there is one that died."""
        folder = self._folder(key)
        try:
            with self._calls(key):
                for page in self.client.get_paginator("list_multipart_uploads").paginate(
                        Bucket=self.bucket, Prefix=folder):
                    for u in page.get("Uploads", []):
                        if "/" not in u["Key"][len(folder):]:
                            self.client.abort_multipart_upload(Bucket=self.bucket, Key=u["Key"], UploadId=u["UploadId"])
        except (StoreError, OSError):
            pass
