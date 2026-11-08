"""Check selected SDK availability and architectures without downloading SDK archives."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile

from build import DOWNLOADS, download, read_checksums, sdk_name, verify


def check(row):
    url = f"{DOWNLOADS}/{row['release']}/targets/{row['target']}"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        download(f"{url}/sha256sums", directory / "sha256sums")
        sums = read_checksums(directory / "sha256sums")
        name = sdk_name(sums, row["release"], row["target"])
        for filename in ("profiles.json", "feeds.buildinfo"):
            download(f"{url}/{filename}", directory / filename)
            verify(directory / filename, sums[filename])
        actual = json.loads((directory / "profiles.json").read_text())["arch_packages"]
        if actual != row["arch"]:
            raise ValueError(f"{row['release']}/{row['target']}: expected {row['arch']}, got {actual}")
        print(f"OK {row['format']} {actual}: {name}", flush=True)


if __name__ == "__main__":
    import os
    from matrix import build_matrix

    config = json.loads(Path(__file__).with_name("targets.json").read_text())
    matrix = build_matrix(config, "tinc", os.getenv("INPUT_FORMAT") or "all",
                          os.getenv("INPUT_ARCHITECTURES") or "all")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(check, matrix["include"]))
