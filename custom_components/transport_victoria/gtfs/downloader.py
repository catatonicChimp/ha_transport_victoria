"""GTFS Schedule ZIP downloader.

Downloads the PTV GTFS schedule ZIP file and checks whether it has changed
since the last import. Change detection uses a two-stage approach:

1. HEAD request: compare ETag and Content-Length against stored values.
   If both match, skip the download entirely (~1 KB vs 282 MB).
2. SHA-256 digest: after download, compare against the stored SHA-256.
   If identical, skip the import step.

The download is async (uses aiohttp via the HA session). All file I/O
(mkdir, write, rename) runs in executor threads to keep the event loop free.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Chunk size for streaming the ZIP download
_CHUNK_SIZE = 1024 * 1024  # 1 MB


async def check_for_remote_changes(
    hass: HomeAssistant,
    url: str,
    stored_etag: str | None,
    stored_size: str | None,
) -> bool:
    """Return True if the remote file appears to have changed since last download.

    Uses a HEAD request to compare ETag and Content-Length headers against the
    values stored from the previous download. If either header is absent from
    the response, conservatively returns True (assume changed).

    Args:
        hass: HomeAssistant instance for the shared aiohttp session.
        url: URL of the GTFS ZIP to check.
        stored_etag: ETag stored from the last download, or None if unknown.
        stored_size: Content-Length stored from the last download, or None.

    Returns:
        True if the file should be re-downloaded, False if it appears unchanged.
    """
    if not stored_etag and not stored_size:
        return True  # No baseline — must download

    session = async_get_clientsession(hass)
    try:
        async with session.head(url, timeout=30, allow_redirects=True) as resp:
            remote_etag = resp.headers.get("ETag")
            remote_size = resp.headers.get("Content-Length")
    except Exception as exc:
        _LOGGER.warning("HEAD request for GTFS ZIP failed: %s — assuming changed", exc)
        return True

    if not remote_etag or not remote_size:
        _LOGGER.debug(
            "HEAD response missing ETag/Content-Length — assuming changed"
        )
        return True

    etag_match = (remote_etag == stored_etag) if stored_etag else True
    size_match = (remote_size == stored_size) if stored_size else True

    if etag_match and size_match:
        _LOGGER.debug(
            "HEAD check: ETag=%s, size=%s — no change detected", remote_etag, remote_size
        )
        return False

    _LOGGER.info(
        "HEAD check: remote changed (ETag %s→%s, size %s→%s)",
        stored_etag,
        remote_etag,
        stored_size,
        remote_size,
    )
    return True


async def download_zip(
    hass: HomeAssistant,
    url: str,
    dest_path: Path,
    *,
    skip_if_exists: bool = False,
) -> tuple[bool, str, str | None, str | None]:
    """Download the GTFS ZIP from url to dest_path.

    Streams the file in chunks over async HTTP, computes its SHA-256 digest
    during download, then writes to disk in an executor thread.

    Args:
        hass: The HomeAssistant instance (used to get the shared aiohttp session).
        url: Direct download URL for the GTFS ZIP file.
        dest_path: Where to save the downloaded ZIP.
        skip_if_exists: If True and dest_path already exists, return its SHA-256
            without downloading. Use this during setup when the file was just
            downloaded by a prior step. Weekly refreshes should leave this False.

    Returns:
        (changed, sha256_hex, etag, content_length) — True if the file was
        newly downloaded, the SHA-256 hex digest, and the ETag / Content-Length
        headers from the response (both None when the download was skipped).
    """
    # Create dest directory in executor (blocking I/O)
    await hass.async_add_executor_job(
        lambda: dest_path.parent.mkdir(parents=True, exist_ok=True)
    )

    # If caller says to skip when the file is already present, return cached SHA
    if skip_if_exists:
        exists = await hass.async_add_executor_job(dest_path.exists)
        if exists:
            _LOGGER.debug("ZIP already on disk, skipping download")
            sha256_hex = await hass.async_add_executor_job(_sha256_of_file, dest_path)
            return False, sha256_hex, None, None

    session = async_get_clientsession(hass)
    hasher = hashlib.sha256()

    _LOGGER.info("Downloading GTFS schedule from %s", url)

    tmp_path = dest_path.parent / (dest_path.name + ".tmp")

    try:
        # Stream HTTP response into memory, computing SHA-256 as we go
        chunks: list[bytes] = []
        total_bytes = 0
        log_threshold = 10 * 1024 * 1024  # log every 10 MB
        next_log_at = log_threshold
        etag: str | None = None
        content_length: str | None = None

        async with session.get(url, timeout=600) as response:
            response.raise_for_status()
            etag = response.headers.get("ETag")
            content_length = response.headers.get("Content-Length")
            total_expected_mb = int(content_length) / 1_048_576 if content_length else None

            async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                chunks.append(chunk)
                hasher.update(chunk)
                total_bytes += len(chunk)

                if total_bytes >= next_log_at:
                    if total_expected_mb:
                        _LOGGER.info(
                            "GTFS download: %.0f / %.0f MB (%.0f%%)",
                            total_bytes / 1_048_576,
                            total_expected_mb,
                            100 * total_bytes / int(content_length),
                        )
                    else:
                        _LOGGER.info(
                            "GTFS download: %.0f MB so far", total_bytes / 1_048_576
                        )
                    next_log_at += log_threshold

        sha256_hex = hasher.hexdigest()
        _LOGGER.info(
            "GTFS download complete: %.1f MB, SHA-256: %s",
            total_bytes / 1_048_576,
            sha256_hex,
        )

        # Check whether this matches the currently stored file (executor — blocking)
        dest_exists = await hass.async_add_executor_job(dest_path.exists)
        if dest_exists:
            existing_sha256 = await hass.async_add_executor_job(_sha256_of_file, dest_path)
            if existing_sha256 == sha256_hex:
                _LOGGER.debug("Downloaded ZIP matches existing file — no change")
                return False, sha256_hex, etag, content_length

        # Write buffered chunks to tmp file then atomically swap (executor — blocking)
        def _write_and_replace() -> None:
            with tmp_path.open("wb") as f:
                for chunk in chunks:
                    f.write(chunk)
            tmp_path.replace(dest_path)

        await hass.async_add_executor_job(_write_and_replace)
        return True, sha256_hex, etag, content_length

    except Exception:
        await hass.async_add_executor_job(
            lambda: tmp_path.unlink(missing_ok=True)
        )
        raise


def _sha256_of_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
