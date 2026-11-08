"""Verified source downloads; incomplete or corrupt responses are retried, never installed."""

import hashlib
from http.client import HTTPException
from pathlib import Path
import shutil
import time
from urllib.request import Request, urlopen


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url, destination, expected_sha256=None, attempts=4):
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    for attempt in range(attempts):
        request_url = url if attempt == 0 else f"{url}{'&' if '?' in url else '?'}download_retry={time.time_ns()}"
        try:
            print(f"Downloading {request_url} (attempt {attempt + 1}/{attempts})", flush=True)
            request = Request(request_url, headers={
                "Accept-Encoding": "identity", "Cache-Control": "no-cache", "Pragma": "no-cache"})
            with urlopen(request, timeout=120) as response:
                length = response.headers.get("Content-Length")
                content_type = response.headers.get("Content-Type", "")
                print(f"HTTP {response.status}; Content-Length={length or 'unknown'}; Content-Type={content_type}", flush=True)
                if response.status != 200 or response.headers.get("Content-Range"):
                    raise ValueError(f"Expected complete HTTP 200; got {response.status}, Content-Range={response.headers.get('Content-Range')}")
                if "text/html" in content_type.lower():
                    raise ValueError("Server returned HTML instead of the requested file")
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            size = partial.stat().st_size
            if length is not None and size != int(length):
                raise ValueError(f"Incomplete download: expected {length} bytes, received {size}")
            if expected_sha256 is not None:
                actual = sha256(partial)
                if actual != expected_sha256:
                    raise ValueError(f"SHA256 mismatch: {destination.name} ({size} bytes); expected {expected_sha256}, actual {actual}")
                print(f"SHA256 verified: {actual} ({size} bytes)", flush=True)
            partial.replace(destination)
            return
        except (OSError, HTTPException, ValueError) as error:
            partial.unlink(missing_ok=True)
            print(f"Download attempt {attempt + 1}/{attempts} failed: {error}", flush=True)
            if attempt + 1 == attempts:
                raise RuntimeError(f"Download failed after {attempts} attempts: {url}; {error}") from error
            time.sleep(2 ** attempt)
