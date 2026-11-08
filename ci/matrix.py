"""Generate the package / package-format / architecture build matrix."""

import json
import os
from pathlib import Path


def build_matrix(config, package="all", package_format="all", architectures="all"):
    packages = ["tinc", "frr"] if package == "all" else [package]
    formats = list(config["releases"]) if package_format == "all" else [package_format]
    if any(value not in ("tinc", "frr") for value in packages):
        raise ValueError(f"Unsupported package: {package}")
    if any(value not in config["releases"] for value in formats):
        raise ValueError(f"Unsupported package format: {package_format}")

    targets = config["architectures"]
    if architectures.strip().lower() != "all":
        aliases = {
            name: target["arch"]
            for target in targets
            for name in [target["arch"], *target["aliases"]]
        }
        requested = [name.strip().lower() for name in architectures.split(",")]
        unknown = set(requested) - aliases.keys()
        if unknown:
            raise ValueError(f"Unsupported architectures: {', '.join(sorted(unknown))}")
        selected = {aliases[name] for name in requested}
        targets = [target for target in targets if target["arch"] in selected]

    return {"include": [
        {
            "package": name,
            "format": fmt,
            "release": config["releases"][fmt],
            "arch": target["arch"],
            "target": target["target"],
            "bundle": f"{name}-openwrt-{config['releases'][fmt]}-{target['arch']}-{fmt}",
        }
        for name in packages for fmt in formats for target in targets
    ]}


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("targets.json").read_text())
    matrix = build_matrix(
        config,
        os.getenv("INPUT_PACKAGE") or "all",
        os.getenv("INPUT_FORMAT") or "all",
        os.getenv("INPUT_ARCHITECTURES") or "all",
    )
    encoded = json.dumps(matrix, separators=(",", ":"))
    print(encoded)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"matrix={encoded}\n")
