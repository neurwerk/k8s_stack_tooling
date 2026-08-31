import base64
import hashlib
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from media_downloader_uploader.bundle import PiiBundle
from media_downloader_uploader.config import Settings
from media_downloader_uploader.models import FileChecksum
from media_downloader_uploader.rgw import (
    KubectlUnavailableError,
    RgwPortForward,
    RgwPublisher,
    RgwUploadError,
    _acquire_publication_lock,
    _port_forward_message,
    _read_credentials,
    check_kubernetes,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_root=tmp_path,
        hf_home=tmp_path / "hf",
        kube_context="example-cluster",
    )


def _bundle(root: Path) -> PiiBundle:
    return PiiBundle(
        version="0.1.2",
        root=root,
        checksums=(
            FileChecksum(path="model.bin", sha256=hashlib.sha256(b"model").hexdigest(), size=5),
        ),
    )


def _mock_publisher_connections(monkeypatch, client: object) -> None:
    forwarding = Mock(local_port=12345)
    forwarding.__enter__ = Mock(return_value=forwarding)
    forwarding.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(
        "media_downloader_uploader.rgw._read_credentials", lambda settings: ("a", "b")
    )
    monkeypatch.setattr(
        "media_downloader_uploader.rgw.boto3.client", lambda *args, **kwargs: client
    )
    monkeypatch.setattr("media_downloader_uploader.rgw.RgwPortForward", lambda settings: forwarding)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.uploaded_keys: list[str] = []
        self.deleted_keys: list[str] = []
        self.fail_upload_key: str | None = None
        self.failure_hook = None
        self.upload_started = threading.Event()
        self.continue_upload = threading.Event()
        self.block_first_upload = False
        self._etag = 0
        self._mutex = threading.Lock()

    def head_bucket(self, **kwargs: object) -> dict[str, object]:
        return {}

    def put_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        with self._mutex:
            current = self.objects.get(key)
            if kwargs.get("IfNoneMatch") == "*" and current is not None:
                raise self._conditional_error()
            if "IfMatch" in kwargs and (current is None or current["ETag"] != kwargs["IfMatch"]):
                raise self._conditional_error()
            body = kwargs.get("Body", b"")
            assert isinstance(body, bytes)
            metadata = kwargs.get("Metadata", {})
            assert isinstance(metadata, dict)
            self._etag += 1
            etag = f'"etag-{self._etag}"'
            self.objects[key] = {
                "Body": body,
                "ContentLength": len(body),
                "Metadata": dict(metadata),
                "ETag": etag,
                "LastModified": datetime.now(UTC),
            }
            return {"ETag": etag}

    def upload_file(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        if self.block_first_upload and key.endswith("model.bin"):
            self.upload_started.set()
            assert self.continue_upload.wait(timeout=5)
        if key == self.fail_upload_key:
            if self.failure_hook is not None:
                self.failure_hook()
            raise OSError("network")
        path = Path(str(kwargs["Filename"]))
        extra_args = kwargs["ExtraArgs"]
        assert isinstance(extra_args, dict)
        metadata = extra_args["Metadata"]
        assert isinstance(metadata, dict)
        payload = path.read_bytes()
        with self._mutex:
            self._etag += 1
            self.objects[key] = {
                "Body": payload,
                "ContentLength": len(payload),
                "Metadata": dict(metadata),
                "ETag": f'"etag-{self._etag}"',
                "LastModified": datetime.now(UTC),
            }
            self.uploaded_keys.append(key)

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        with self._mutex:
            item = self.objects.get(key)
            if item is None:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchKey"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )
            return dict(item)

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = str(kwargs["Prefix"])
        with self._mutex:
            keys = sorted(key for key in self.objects if key.startswith(prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def delete_objects(self, **kwargs: object) -> dict[str, object]:
        deletion = kwargs["Delete"]
        assert isinstance(deletion, dict)
        objects = deletion["Objects"]
        assert isinstance(objects, list)
        with self._mutex:
            for item in objects:
                assert isinstance(item, dict)
                key = str(item["Key"])
                self.objects.pop(key, None)
                self.deleted_keys.append(key)
        return {}

    def add_object(
        self,
        key: str,
        *,
        metadata: dict[str, str] | None = None,
        modified: datetime | None = None,
        body: bytes = b"old",
    ) -> None:
        with self._mutex:
            self._etag += 1
            self.objects[key] = {
                "Body": body,
                "ContentLength": len(body),
                "Metadata": metadata or {},
                "ETag": f'"etag-{self._etag}"',
                "LastModified": modified or datetime.now(UTC),
            }

    @staticmethod
    def _conditional_error() -> ClientError:
        return ClientError(
            {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            },
            "PutObject",
        )


def _materialized_bundle(tmp_path: Path) -> PiiBundle:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "model.bin").write_bytes(b"model")
    (root / "checksums.sha256").write_text("checksum", encoding="utf-8")
    (root / "manifest.yaml").write_text("manifest", encoding="utf-8")
    return _bundle(root)


def test_port_forward_reports_actionable_kubectl_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "media_downloader_uploader.rgw.subprocess.run",
        Mock(side_effect=OSError("missing kubectl")),
    )

    with pytest.raises(KubectlUnavailableError, match="enable port-forwarding"):
        RgwPortForward(_settings(tmp_path)).__enter__()

    assert "127.0.0.1" in _port_forward_message()


def test_kubernetes_preflight_requires_explicit_context(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "hf")
    run = Mock()
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.run", run)

    with pytest.raises(KubectlUnavailableError, match="KUBE_CONTEXT is required"):
        check_kubernetes(settings)

    run.assert_not_called()


def test_kubernetes_preflight_passes_context_to_api_probe(monkeypatch, tmp_path: Path) -> None:
    run = Mock(return_value=Mock(stdout="{}"))
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.run", run)

    check_kubernetes(_settings(tmp_path))

    assert run.call_args.args[0] == [
        "kubectl",
        "--context",
        "example-cluster",
        "get",
        "--raw=/version",
    ]


def test_publisher_uploads_manifest_last_and_verifies_objects(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    _mock_publisher_connections(monkeypatch, client)

    assert RgwPublisher(_settings(tmp_path)).publish(bundle) == "0.1.2/"
    assert client.uploaded_keys == [
        "0.1.2/model.bin",
        "0.1.2/checksums.sha256",
        "0.1.2/manifest.yaml",
    ]
    assert "0.1.2/.publication-lock" in client.objects
    publishers = {
        item["Metadata"]["publisher-id"]
        for key, item in client.objects.items()
        if key != "0.1.2/.publication-lock"
    }
    assert len(publishers) == 1


def test_read_credentials_decodes_secret_with_explicit_context(monkeypatch, tmp_path: Path) -> None:
    completed = [
        Mock(stdout=base64.b64encode(b"access")),
        Mock(stdout=base64.b64encode(b"secret")),
    ]
    run = Mock(side_effect=completed)
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.run", run)

    assert _read_credentials(_settings(tmp_path)) == ("access", "secret")
    for call in run.call_args_list:
        assert call.args[0][:3] == ["kubectl", "--context", "example-cluster"]


def test_read_credentials_reports_missing_secret(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "media_downloader_uploader.rgw.subprocess.run",
        Mock(side_effect=subprocess.CalledProcessError(1, "kubectl")),
    )

    with pytest.raises(RgwUploadError, match="publisher credentials"):
        _read_credentials(_settings(tmp_path))


def test_port_forward_passes_context_and_stops_failed_child(monkeypatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = 1
    process.stderr.read.return_value = "service missing"
    popen = Mock(return_value=process)
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.run", Mock())
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.Popen", popen)
    monkeypatch.setattr("media_downloader_uploader.rgw._available_port", lambda: 12345)
    monkeypatch.setattr("media_downloader_uploader.rgw._local_port_open", lambda port: False)

    with pytest.raises(KubectlUnavailableError, match="service missing"):
        RgwPortForward(_settings(tmp_path)).__enter__()

    assert popen.call_args.args[0][:3] == ["kubectl", "--context", "example-cluster"]
    process.terminate.assert_not_called()


def test_port_forward_terminates_successful_child(monkeypatch, tmp_path: Path) -> None:
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.run", Mock())
    monkeypatch.setattr("media_downloader_uploader.rgw.subprocess.Popen", popen)
    monkeypatch.setattr("media_downloader_uploader.rgw._available_port", lambda: 12345)
    monkeypatch.setattr("media_downloader_uploader.rgw._local_port_open", lambda port: True)

    with RgwPortForward(_settings(tmp_path)) as forwarding:
        assert forwarding.local_port == 12345

    process.terminate.assert_called_once()
    assert popen.call_args.args[0][:3] == ["kubectl", "--context", "example-cluster"]


def test_publisher_refuses_complete_existing_version(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.add_object("0.1.2/manifest.yaml")
    _mock_publisher_connections(monkeypatch, client)

    with pytest.raises(RgwUploadError, match="already exists"):
        RgwPublisher(_settings(tmp_path)).publish(bundle)


def test_publisher_cleans_current_attempt_after_upload_failure(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.fail_upload_key = "0.1.2/checksums.sha256"
    _mock_publisher_connections(monkeypatch, client)

    with pytest.raises(OSError, match="network"):
        RgwPublisher(_settings(tmp_path)).publish(bundle)

    assert "0.1.2/model.bin" not in client.objects
    lock = client.objects["0.1.2/.publication-lock"]
    assert lock["Metadata"]["state"] == "failed"


def test_publisher_requires_context_bound_confirmation_for_incomplete_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.add_object("0.1.2/old.bin")
    confirmation = Mock(return_value=False)
    _mock_publisher_connections(monkeypatch, client)

    with pytest.raises(RgwUploadError, match="example-cluster"):
        RgwPublisher(_settings(tmp_path), confirmation).publish(bundle)

    confirmation.assert_called_once_with("example-cluster", "pii-models", "0.1.2/")
    assert "0.1.2/old.bin" in client.objects
    assert client.objects["0.1.2/.publication-lock"]["Metadata"]["state"] == "failed"


def test_stale_incomplete_publication_is_atomically_recovered(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.add_object(
        "0.1.2/.publication-lock",
        metadata={"publisher-id": "old", "state": "publishing"},
        modified=datetime.now(UTC) - timedelta(seconds=10),
    )
    client.add_object("0.1.2/old.bin", metadata={"publisher-id": "old"})
    confirmation = Mock(return_value=True)
    settings = _settings(tmp_path).model_copy(update={"rgw_publication_lock_stale_seconds": 1.0})
    _mock_publisher_connections(monkeypatch, client)

    assert RgwPublisher(settings, confirmation).publish(bundle) == "0.1.2/"

    confirmation.assert_called_once_with("example-cluster", "pii-models", "0.1.2/")
    assert "0.1.2/old.bin" not in client.objects
    assert "0.1.2/manifest.yaml" in client.objects


def test_active_publication_lock_rejects_concurrent_publisher(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.block_first_upload = True
    _mock_publisher_connections(monkeypatch, client)
    first_error: list[Exception] = []

    def publish_first() -> None:
        try:
            RgwPublisher(_settings(tmp_path)).publish(bundle)
        except (OSError, RgwUploadError) as error:
            first_error.append(error)

    thread = threading.Thread(target=publish_first)
    thread.start()
    assert client.upload_started.wait(timeout=5)
    try:
        with pytest.raises(RgwUploadError, match="being published"):
            RgwPublisher(_settings(tmp_path)).publish(bundle)
    finally:
        client.continue_upload.set()
        thread.join(timeout=5)

    assert not first_error
    assert not thread.is_alive()
    assert "0.1.2/manifest.yaml" in client.objects


def test_failure_cleanup_does_not_delete_object_owned_by_another_publisher(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.fail_upload_key = "0.1.2/checksums.sha256"

    def transfer_model_ownership() -> None:
        client.objects["0.1.2/model.bin"]["Metadata"] = {
            "sha256": hashlib.sha256(b"model").hexdigest(),
            "publisher-id": "successor",
        }

    client.failure_hook = transfer_model_ownership
    _mock_publisher_connections(monkeypatch, client)

    with pytest.raises(OSError, match="network"):
        RgwPublisher(_settings(tmp_path)).publish(bundle)

    assert "0.1.2/model.bin" in client.objects
    assert "0.1.2/model.bin" not in client.deleted_keys


def test_failed_lock_can_be_recovered_without_waiting(monkeypatch, tmp_path: Path) -> None:
    bundle = _materialized_bundle(tmp_path)
    client = FakeS3()
    client.fail_upload_key = "0.1.2/checksums.sha256"
    _mock_publisher_connections(monkeypatch, client)

    with pytest.raises(OSError):
        RgwPublisher(_settings(tmp_path)).publish(bundle)
    client.fail_upload_key = None

    assert RgwPublisher(_settings(tmp_path)).publish(bundle) == "0.1.2/"
    assert "0.1.2/manifest.yaml" in client.objects


def test_conditional_create_allows_only_one_version_owner(tmp_path: Path) -> None:
    client = FakeS3()
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def acquire() -> None:
        start.wait()
        try:
            lock = _acquire_publication_lock(
                client,
                "pii-models",
                "0.1.2/",
                "example-cluster",
                None,
                86400,
            )
            outcomes.append(lock.owner)
        except RgwUploadError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count("rejected") == 1
    assert len(set(outcomes) - {"rejected"}) == 1
