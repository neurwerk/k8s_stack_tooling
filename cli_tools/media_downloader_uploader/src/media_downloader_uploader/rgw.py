"""Publish immutable PII bundles to a Rook Ceph RGW object store."""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from subprocess import Popen
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from media_downloader_uploader.bundle import PiiBundle
from media_downloader_uploader.config import Settings
from media_downloader_uploader.errors import MediaDownloaderError
from media_downloader_uploader.integrity import sha256_file


class RgwUploadError(MediaDownloaderError):
    """Indicate that the RGW upload could not be completed."""


class KubectlUnavailableError(RgwUploadError):
    """Indicate that kubectl or local port forwarding is unavailable."""


class S3Client(Protocol):
    """Describe the subset of the boto3 S3 client used by the publisher."""

    def upload_file(self, **kwargs: object) -> None:
        """Upload a local file."""

    def head_bucket(self, **kwargs: object) -> dict[str, object]:
        """Check that a bucket is reachable."""

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        """List objects below a prefix."""

    def head_object(self, **kwargs: object) -> dict[str, object]:
        """Read one object metadata record."""

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        """Delete a batch of objects."""

    def put_object(self, **kwargs: object) -> dict[str, object]:
        """Write a small object, optionally with an atomic precondition."""


@dataclass(frozen=True)
class _PublicationLock:
    """Identify one publisher's ownership of a version prefix."""

    key: str
    owner: str
    etag: str


@dataclass(frozen=True)
class _ObservedLock:
    """Contain the state used to decide and perform a stale lock takeover."""

    etag: str
    state: str
    modified: datetime


_LOCK_NAME = ".publication-lock"
_PUBLISHER_METADATA = "publisher-id"


class RgwPortForward:
    """Manage a short-lived local kubectl port-forward process."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and prepare the child-process handle."""
        self._settings = settings
        self._process: Popen[str] | None = None
        self.local_port: int | None = None

    def __enter__(self) -> RgwPortForward:
        """Start port forwarding and wait until localhost accepts connections."""
        self._check_kubectl()
        local_port = _available_port()
        command = [
            *_kubectl_command(self._settings),
            "-n",
            self._settings.rgw_namespace,
            "port-forward",
            f"service/{self._settings.rgw_service}",
            f"{local_port}:{self._settings.rgw_remote_port}",
        ]
        try:
            self._process = subprocess.Popen(  # noqa: S603 -- binary is an explicit local setting.
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise KubectlUnavailableError(_port_forward_message()) from error
        deadline = time.monotonic() + self._settings.rgw_port_forward_timeout_seconds
        while time.monotonic() < deadline:
            if _local_port_open(local_port):
                self.local_port = local_port
                return self
            if self._process.poll() is not None:
                detail = self._process.stderr.read().strip() if self._process.stderr else ""
                self._stop()
                raise KubectlUnavailableError(f"{_port_forward_message()} {detail}".strip())
            time.sleep(0.1)
        self._stop()
        raise KubectlUnavailableError(_port_forward_message())

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Terminate the port-forward process even after upload failures."""
        self._stop()

    def _stop(self) -> None:
        """Terminate the child process if it was started."""
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def _check_kubectl(self) -> None:
        """Confirm the selected kubectl context is usable before port-forwarding."""
        check_kubernetes(self._settings)


class RgwPublisher:
    """Upload a complete PII bundle through an automatically managed port-forward."""

    def __init__(
        self,
        settings: Settings,
        confirm_prefix_cleanup: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        """Store RGW connection settings."""
        self._settings = settings
        self._confirm_prefix_cleanup = confirm_prefix_cleanup

    def publish(self, bundle: PiiBundle) -> str:
        """Publish one immutable bundle and return its object prefix."""
        with RgwPortForward(self._settings) as forwarding:
            if forwarding.local_port is None:
                raise RgwUploadError("RGW port-forward did not provide a local port")
            access_key, secret_key = _read_credentials(self._settings)
            client: S3Client = boto3.client(
                "s3",
                endpoint_url=f"http://127.0.0.1:{forwarding.local_port}",
                region_name=self._settings.rgw_region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 5, "mode": "standard"},
                    s3={"addressing_style": "path"},
                ),
            )
            prefix = f"{bundle.version}/"
            context = _required_kube_context(self._settings)
            _ensure_bucket(client, self._settings.rgw_bucket)
            publication_lock = _acquire_publication_lock(
                client,
                self._settings.rgw_bucket,
                prefix,
                context,
                self._confirm_prefix_cleanup,
                self._settings.rgw_publication_lock_stale_seconds,
            )
            attempted_keys: list[str] = []
            try:
                for item in bundle.checksums:
                    publication_lock = _refresh_publication_lock(
                        client, self._settings.rgw_bucket, publication_lock
                    )
                    key = prefix + item.path
                    attempted_keys.append(key)
                    self._upload_file(
                        client,
                        bundle,
                        item.path,
                        key,
                        item.sha256,
                        publication_lock.owner,
                    )
                publication_lock = _refresh_publication_lock(
                    client, self._settings.rgw_bucket, publication_lock
                )
                checksum_key = prefix + "checksums.sha256"
                attempted_keys.append(checksum_key)
                self._upload_file(
                    client,
                    bundle,
                    "checksums.sha256",
                    checksum_key,
                    None,
                    publication_lock.owner,
                )
                # The manifest is the completion marker and is intentionally last.
                publication_lock = _refresh_publication_lock(
                    client, self._settings.rgw_bucket, publication_lock
                )
                manifest_key = prefix + "manifest.yaml"
                attempted_keys.append(manifest_key)
                self._upload_file(
                    client,
                    bundle,
                    "manifest.yaml",
                    manifest_key,
                    None,
                    publication_lock.owner,
                )
                _verify_uploaded_files(
                    client,
                    self._settings.rgw_bucket,
                    bundle,
                    prefix,
                    publication_lock.owner,
                )
            except Exception:
                _cleanup_owned_objects(
                    client,
                    self._settings.rgw_bucket,
                    attempted_keys,
                    publication_lock.owner,
                )
                _mark_publication_failed(client, self._settings.rgw_bucket, publication_lock)
                raise
        return prefix

    def _upload_file(
        self,
        client: S3Client,
        bundle: PiiBundle,
        local_name: str,
        key: str,
        sha256: str | None,
        publisher_id: str,
    ) -> None:
        """Upload one file with an optional checksum metadata header."""
        digest = sha256 or sha256_file(bundle.root / local_name)
        client.upload_file(
            Filename=str(bundle.root / local_name),
            Bucket=self._settings.rgw_bucket,
            Key=key,
            ExtraArgs={"Metadata": {"sha256": digest, _PUBLISHER_METADATA: publisher_id}},
        )


def _read_credentials(settings: Settings) -> tuple[str, str]:
    """Read the generated Rook user Secret without writing credentials to disk."""
    try:
        access = subprocess.run(  # noqa: S603 -- binary is an explicit local setting.
            [
                *_kubectl_command(settings),
                "-n",
                settings.rgw_namespace,
                "get",
                "secret",
                settings.rgw_credential_secret,
                "-o",
                "jsonpath={.data.AccessKey}",
            ],
            check=True,
            capture_output=True,
        ).stdout
        secret = subprocess.run(  # noqa: S603 -- binary is an explicit local setting.
            [
                *_kubectl_command(settings),
                "-n",
                settings.rgw_namespace,
                "get",
                "secret",
                settings.rgw_credential_secret,
                "-o",
                "jsonpath={.data.SecretKey}",
            ],
            check=True,
            capture_output=True,
        ).stdout
        access_key = base64.b64decode(access).decode("utf-8")
        secret_key = base64.b64decode(secret).decode("utf-8")
    except (OSError, subprocess.CalledProcessError, ValueError, UnicodeDecodeError) as error:
        raise RgwUploadError(
            "Could not read RGW publisher credentials from Secret "
            f"{settings.rgw_credential_secret}."
        ) from error
    if not access_key or not secret_key:
        raise RgwUploadError("RGW publisher Secret contains empty credentials")
    return access_key, secret_key


def check_kubernetes(settings: Settings) -> None:
    """Confirm the selected kubectl context can reach its Kubernetes API.

    Args:
        settings: RGW and kubectl configuration.

    Raises:
        KubectlUnavailableError: If the context is missing, unexpected, or unreachable.
    """
    _required_kube_context(settings)
    try:
        subprocess.run(  # noqa: S603 -- binary is an explicit local setting.
            [*_kubectl_command(settings), "get", "--raw=/version"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise KubectlUnavailableError(_port_forward_message()) from error


def _ensure_bucket(client: S3Client, bucket: str) -> None:
    """Confirm the configured bucket exists."""
    try:
        client.head_bucket(Bucket=bucket)
    except Exception as error:
        raise RgwUploadError(f"RGW bucket is unavailable: {bucket}") from error


def _acquire_publication_lock(
    client: S3Client,
    bucket: str,
    prefix: str,
    context: str,
    confirm_cleanup: Callable[[str, str, str], bool] | None,
    stale_seconds: float,
) -> _PublicationLock:
    """Atomically acquire a version lock and recover confirmed stale content."""
    keys = _list_keys(client, bucket, prefix)
    if prefix + "manifest.yaml" in keys:
        raise RgwUploadError(f"PII bundle version already exists: {prefix[:-1]}")
    lock_key = prefix + _LOCK_NAME
    owner = uuid.uuid4().hex
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=lock_key,
            IfNoneMatch="*",
            **_lock_payload(owner, "publishing"),
        )
    except ClientError as error:
        if not _is_conditional_conflict(error):
            raise RgwUploadError(f"Could not acquire publication lock for {prefix[:-1]}") from error
        existing = _read_publication_lock(client, bucket, lock_key)
        if not _lock_is_recoverable(existing, stale_seconds):
            raise RgwUploadError(f"PII bundle version is being published: {prefix[:-1]}") from error
        try:
            response = client.put_object(
                Bucket=bucket,
                Key=lock_key,
                IfMatch=existing.etag,
                **_lock_payload(owner, "publishing"),
            )
        except ClientError as takeover_error:
            if _is_conditional_conflict(takeover_error):
                raise RgwUploadError(
                    f"Publication lock changed during recovery: {prefix[:-1]}"
                ) from takeover_error
            raise RgwUploadError(
                f"Could not recover publication lock for {prefix[:-1]}"
            ) from takeover_error

    lock = _lock_from_response(lock_key, owner, response)
    current_keys = [key for key in _list_keys(client, bucket, prefix) if key != lock_key]
    if prefix + "manifest.yaml" in current_keys:
        _mark_publication_failed(client, bucket, lock)
        raise RgwUploadError(f"PII bundle version already exists: {prefix[:-1]}")
    try:
        _confirm_stale_cleanup(context, bucket, prefix, current_keys, confirm_cleanup)
        _delete_keys(client, bucket, current_keys)
    except Exception:
        _mark_publication_failed(client, bucket, lock)
        raise
    return lock


def _confirm_stale_cleanup(
    context: str,
    bucket: str,
    prefix: str,
    keys: list[str],
    confirm_cleanup: Callable[[str, str, str], bool] | None,
) -> None:
    """Require context-bound confirmation before deleting incomplete objects."""
    if keys and (confirm_cleanup is None or not confirm_cleanup(context, bucket, prefix)):
        raise RgwUploadError(
            f"Incomplete prefix cleanup was not confirmed for Kubernetes context {context!r}"
        )


def _lock_payload(owner: str, state: str) -> dict[str, object]:
    """Build the body and metadata shared by conditional lock writes."""
    updated_at = datetime.now(UTC).isoformat()
    return {
        "Body": json.dumps({"owner": owner, "state": state, "updatedAt": updated_at}).encode(),
        "ContentType": "application/json",
        "Metadata": {
            _PUBLISHER_METADATA: owner,
            "state": state,
            "updated-at": updated_at,
        },
    }


def _lock_from_response(key: str, owner: str, response: dict[str, object]) -> _PublicationLock:
    """Require the S3-compatible conditional write to return an ETag."""
    etag = response.get("ETag")
    if not isinstance(etag, str) or not etag:
        raise RgwUploadError("RGW publication lock response did not contain an ETag")
    return _PublicationLock(key=key, owner=owner, etag=etag)


def _read_publication_lock(client: S3Client, bucket: str, key: str) -> _ObservedLock:
    """Read the lock metadata needed for atomic stale takeover."""
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        raise RgwUploadError("Publication lock disappeared during acquisition") from error
    metadata = response.get("Metadata")
    etag = response.get("ETag")
    modified = response.get("LastModified")
    if (
        not isinstance(metadata, dict)
        or not isinstance(etag, str)
        or not etag
        or not isinstance(modified, datetime)
    ):
        raise RgwUploadError("Existing publication lock has invalid metadata")
    state = metadata.get("state")
    if not isinstance(state, str):
        raise RgwUploadError("Existing publication lock has invalid metadata")
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    return _ObservedLock(etag=etag, state=state, modified=modified)


def _lock_is_recoverable(lock: _ObservedLock, stale_seconds: float) -> bool:
    """Allow takeover only for explicitly failed or sufficiently old locks."""
    if lock.state == "failed":
        return True
    return (datetime.now(UTC) - lock.modified).total_seconds() >= stale_seconds


def _refresh_publication_lock(
    client: S3Client, bucket: str, lock: _PublicationLock
) -> _PublicationLock:
    """Refresh the lock lease and prove ownership before the next upload."""
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=lock.key,
            IfMatch=lock.etag,
            **_lock_payload(lock.owner, "publishing"),
        )
    except ClientError as error:
        raise RgwUploadError("Publication lock ownership was lost") from error
    return _lock_from_response(lock.key, lock.owner, response)


def _mark_publication_failed(client: S3Client, bucket: str, lock: _PublicationLock) -> None:
    """Make a still-owned failed lock immediately recoverable."""
    try:
        client.put_object(
            Bucket=bucket,
            Key=lock.key,
            IfMatch=lock.etag,
            **_lock_payload(lock.owner, "failed"),
        )
    except ClientError:
        return


def _is_conditional_conflict(error: ClientError) -> bool:
    """Recognize AWS S3 and Ceph RGW conditional-write conflicts."""
    metadata = error.response.get("ResponseMetadata")
    details = error.response.get("Error")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return status in {409, 412} or code in {
        "ConditionalRequestConflict",
        "PreconditionFailed",
    }


def _verify_uploaded_files(
    client: S3Client, bucket: str, bundle: PiiBundle, prefix: str, publisher_id: str
) -> None:
    """Verify object sizes and publisher-provided SHA-256 metadata."""
    for item in bundle.checksums:
        response = client.head_object(Bucket=bucket, Key=prefix + item.path)
        metadata = response.get("Metadata")
        uploaded_sha = metadata.get("sha256") if isinstance(metadata, dict) else None
        uploaded_by = metadata.get(_PUBLISHER_METADATA) if isinstance(metadata, dict) else None
        if (
            response.get("ContentLength") != item.size
            or uploaded_sha != item.sha256
            or uploaded_by != publisher_id
        ):
            raise RgwUploadError(f"Uploaded object failed verification: {prefix}{item.path}")
    for name in ("checksums.sha256", "manifest.yaml"):
        response = client.head_object(Bucket=bucket, Key=prefix + name)
        metadata = response.get("Metadata")
        uploaded_sha = metadata.get("sha256") if isinstance(metadata, dict) else None
        uploaded_by = metadata.get(_PUBLISHER_METADATA) if isinstance(metadata, dict) else None
        if uploaded_sha != sha256_file(bundle.root / name) or uploaded_by != publisher_id:
            raise RgwUploadError(f"Uploaded object failed verification: {prefix}{name}")


def _cleanup_owned_objects(
    client: S3Client, bucket: str, attempted_keys: list[str], publisher_id: str
) -> None:
    """Delete only attempted objects whose metadata still names this publisher."""
    owned: list[str] = []
    for key in attempted_keys:
        try:
            response = client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            continue
        metadata = response.get("Metadata")
        if isinstance(metadata, dict) and metadata.get(_PUBLISHER_METADATA) == publisher_id:
            owned.append(key)
    _delete_keys(client, bucket, owned)


def _delete_keys(client: S3Client, bucket: str, keys: list[str]) -> None:
    """Delete an already observed set of object keys without re-listing it."""
    for start in range(0, len(keys), 1000):
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]},
        )


def _list_keys(client: S3Client, bucket: str, prefix: str) -> list[str]:
    """List every object key below a prefix, including paginated results."""
    keys: list[str] = []
    token: str | None = None
    while True:
        if token:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, ContinuationToken=token)
        else:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise RgwUploadError(f"RGW returned invalid object contents for {prefix}")
        for item in contents:
            if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                raise RgwUploadError(f"RGW returned invalid object metadata for {prefix}")
            keys.append(item["Key"])
        if response.get("IsTruncated") is not True:
            return keys
        next_token = response.get("NextContinuationToken")
        if not isinstance(next_token, str) or not next_token:
            raise RgwUploadError(f"RGW returned an invalid pagination response for {prefix}")
        token = next_token


def _available_port() -> int:
    """Reserve a currently unused local TCP port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _local_port_open(port: int) -> bool:
    """Check whether localhost accepts a TCP connection on a forwarded port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _required_kube_context(settings: Settings) -> str:
    """Return the explicit upload context or stop before invoking kubectl."""
    context = settings.kube_context.strip() if settings.kube_context else ""
    if not context:
        raise KubectlUnavailableError(
            "MEDIA_DOWNLOADER_UPLOADER_KUBE_CONTEXT is required for uploads."
        )
    return context


def _kubectl_command(settings: Settings) -> list[str]:
    """Build the context-bound prefix shared by every kubectl invocation."""
    return [settings.kubectl_binary, "--context", _required_kube_context(settings)]


def _port_forward_message() -> str:
    """Return the actionable kubectl/port-forward failure guidance."""
    return (
        "kubectl cannot reach the selected cluster or RGW on 127.0.0.1. "
        "Please enable port-forwarding and retry the upload."
    )
