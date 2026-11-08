"""Build once per musl platform, then package for its matching OpenWrt architectures."""

import json
import os
from pathlib import Path


def build_matrix(config, package="all", package_format="all", architectures="all"):
    packages = ["tinc", "frr"] if package == "all" else [package]
    formats = config["formats"] if package_format == "all" else [package_format]
    if any(value not in ("tinc", "frr") for value in packages):
        raise ValueError(f"Unsupported package: {package}")
    if any(value not in config["formats"] for value in formats):
        raise ValueError(f"Unsupported package format: {package_format}")
    all_arches = {arch for target in config["targets"] for arch in target["arches"]}
    aliases = {arch: {arch} for arch in all_arches}
    aliases.update({alias: {arch} for alias, arch in config["architecture_aliases"].items()})
    for target in config["targets"]:
        aliases.update({name: set(target["arches"]) for name in [target["target"], target["platform"], *target["aliases"]]})
    if architectures.strip().lower() == "all":
        selected = all_arches
    else:
        requested = [name.strip().lower() for name in architectures.split(",")]
        unknown = set(requested) - aliases.keys()
        if unknown:
            raise ValueError(f"Unsupported architectures: {', '.join(sorted(unknown))}")
        selected = set().union(*(aliases[name] for name in requested))
    return {"include": [
        {"package": name, "target": target["target"], "platform": target["platform"],
         "slug": target["platform"].replace("/", "-"), "machine": target["machine"], "qemu": target["qemu"],
         "arches": ",".join(arch for arch in target["arches"] if arch in selected), "formats": ",".join(formats),
         "artifact": f"dist-{name}-{target['platform'].replace('/', '-')}"}
        for name in packages for target in config["targets"] if set(target["arches"]) & selected
    ]}


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("targets.json").read_text())
    matrix = build_matrix(config, os.getenv("INPUT_PACKAGE") or "all", os.getenv("INPUT_FORMAT") or "all", os.getenv("INPUT_ARCHITECTURES") or "all")
    encoded = json.dumps(matrix, separators=(",", ":"))
    print(encoded)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"matrix={encoded}\n")
