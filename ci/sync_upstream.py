"""Verify/update the main project's OpenWrt architecture table and pinned apk builder."""

import argparse
import json
from pathlib import Path
import re
import tempfile

from common import download, sha256


def parse_contract(layout, apk_script):
    section = re.search(r"pub const ARCHES:.*?=\s*&\[(.*?)\n\];", layout, re.S)[1]
    arches = {target: re.findall(r'"([^"]+)"', values) for target, values in re.findall(r'\("([^"]+)",\s*&\[([^\]]+)\]\)', section)}
    section = re.search(r"pub const TARGET_PLATFORMS:.*?=\s*&\[(.*?)\n\];", layout, re.S)[1]
    platforms = dict(re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', section))
    formats = re.findall(r'"([^"]+)"', re.search(r"pub const FORMATS:.*?=\s*&\[(.*?)\];", layout, re.S)[1])
    image = re.search(r"^IMAGE=(\S+)$", apk_script, re.M)[1]
    if not arches or set(arches) != set(platforms):
        raise ValueError("Unsupported upstream architecture table")
    return arches, platforms, formats, image


def check_contract(config, layout, apk_script):
    arches, platforms, formats, image = parse_contract(layout, apk_script)
    if (arches != {row["target"]: row["arches"] for row in config["targets"]}
            or platforms != {row["target"]: row["platform"] for row in config["targets"]}
            or formats != config["formats"] or image != config["builder_image"]):
        raise ValueError("OpenWrt build contract differs from the main project")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Upstream ref, e.g. master")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).with_name("targets.json")
    config = json.loads(path.read_text())
    upstream = config["upstream"]
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        revision = upstream["revision"]
        if args.ref:
            download(f"https://api.github.com/repos/{upstream['repository']}/commits/{args.ref}", directory / "commit.json")
            revision = json.loads((directory / "commit.json").read_text())["sha"]
        texts = []
        for filename, field in (("tools/release/src/openwrt.rs", "layout_sha256"), ("tools/openwrt-apk.sh", "apk_script_sha256")):
            destination = directory / Path(filename).name
            download(f"https://raw.githubusercontent.com/{upstream['repository']}/{revision}/{filename}", destination, None if args.ref else upstream[field])
            texts.append(destination.read_text(encoding="utf-8"))
            if args.write:
                upstream[field] = sha256(destination)
        if args.write:
            arches, platforms, formats, image = parse_contract(*texts)
            by_target = {row["target"]: row for row in config["targets"]}
            if set(arches) != set(by_target):
                raise ValueError("New targets require compiler/smoke-test configuration")
            config["targets"] = [{**by_target[target], "platform": platforms[target], "arches": values} for target, values in arches.items()]
            config.update(builder_image=image, formats=formats)
            upstream["revision"] = revision
            path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        check_contract(config, *texts)
    print(f"OpenWrt contract matches {upstream['repository']}@{revision}")


if __name__ == "__main__":
    main()
