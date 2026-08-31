"""Define actionable errors raised by the media downloader application."""

from __future__ import annotations


class MediaDownloaderError(RuntimeError):
    """Indicate an expected media downloader failure."""


class StorageUnavailableError(MediaDownloaderError):
    """Indicate that the configured external storage is unavailable or unsafe."""


class HuggingFaceCommandError(MediaDownloaderError):
    """Indicate that the Hugging Face CLI did not complete successfully."""


class IntegrityError(MediaDownloaderError):
    """Indicate that a local artifact does not match its recorded checksums."""
