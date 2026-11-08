"""Static musl build + native OpenWrt ipk/apk packaging, matching the main project."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from common import download, sha256
from openwrt import FRR_BINS, descriptor, package_files, write_apk_stage, write_ipk


def run(*command, **kwargs):
    print("+ " + " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def check_binaries(tree, target, package):
    names = ["tinc", "tincd"] if package == "tinc" else FRR_BINS
    for name in names:
        binary = tree / "bin" / name
        header = subprocess.check_output(["readelf", "-h", str(binary)], text=True)
        if not any(line.split(":", 1)[-1].strip() == target["machine"] for line in header.splitlines() if "Machine:" in line):
            raise ValueError(f"Wrong ELF machine: {binary}")
        program = subprocess.check_output(["readelf", "-l", str(binary)], text=True)
        dynamic = subprocess.check_output(["readelf", "-d", str(binary)], text=True)
        if "INTERP" in program or "(NEEDED)" in dynamic:
            raise ValueError(f"Dynamic executable is not portable across OpenWrt architectures: {binary}")
        command = [target["qemu"]] if target["qemu"] else []
        run(*command, binary, "--version", timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=("tinc", "frr"), required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arches", required=True)
    parser.add_argument("--formats", default="ipk,apk")
    args = parser.parse_args()
    repo = Path.cwd()
    config = json.loads((repo / "ci/targets.json").read_text())
    lock = json.loads((repo / "ci/sources.lock.json").read_text())
    target = next((row for row in config["targets"] if row["target"] == args.target), None)
    arches, formats = args.arches.split(","), args.formats.split(",")
    if target is None or not set(arches) <= set(target["arches"]) or not set(formats) <= set(config["formats"]):
        parser.error("Unsupported target/architecture/format combination")
    sources = [row for row in lock["sources"] if row["name"] in lock["packages"][args.package]["sources"]]
    downloads = repo / "downloads"
    downloads.mkdir(exist_ok=True)
    for source in sources:
        cached = downloads / f"{source['name']}.tar.gz"
        if not cached.exists() or sha256(cached) != source["sha256"]:
            download(source["url"], cached, source["sha256"])
    # One isolated native-musl build per platform, emulated by QEMU for ARM.
    tree = repo / "out" / args.package / target["platform"].replace("/", "-")
    tree.parent.mkdir(parents=True, exist_ok=True)
    if tree.exists():
        raise ValueError(f"Refusing to reuse an existing build output: {tree}")
    run("podman", "build", "--platform", target["platform"], "-f", "build/Containerfile",
        "--build-arg", f"BUILDER_IMAGE={config['builder_image']}", "--build-arg", f"PACKAGE={args.package}",
        "--build-arg", f"TARGETPLATFORM={target['platform']}", "--build-arg", "BUILD_JOBS=2",
        "--output", f"type=local,dest={tree}", ".")
    check_binaries(tree, target, args.package)
    provenance = {"buildInfoVersion": 1, "package": args.package, "target": args.target, "platform": target["platform"],
                  "static": True, "builder": config["builder_image"], "sources": sources,
                  "upstream": config["upstream"], "commit": os.getenv("GITHUB_SHA", "local"),
                  "binaries": {path.name: sha256(path) for path in sorted((tree / "bin").iterdir())}}
    packages = package_files(tree, args.package, provenance, repo)
    version = lock["packages"][args.package]["version"]
    dist = repo / "dist"
    dist.mkdir(exist_ok=True)
    for arch in arches:
        for name, (files, depends) in packages.items():
            for fmt in formats:
                output = dist / f"{name}_{version}_{arch}.{fmt}"
                if fmt == "ipk":
                    write_ipk(files, name, version, arch, depends, output)
                    descriptor(output, name, version, arch, fmt)
                else:
                    write_apk_stage(files, name, version, arch, depends, output)
    stages = sorted(dist.glob("*.apk.stage"))
    if stages:
        run("podman", "run", "--rm", "--network=none", "--platform", "linux/amd64", "-v", f"{dist}:/w:Z",
            config["builder_image"], "sh", "-euc",
            'for s in /w/*.apk.stage; do (cd "$s" && sh ./mkpkg.sh); done; chown -R "$(stat -c %u:%g /w)" /w')
        for stage in stages:
            info = json.loads((stage / "stage.json").read_text())
            descriptor(dist / info["file"], info["package"], version, info["platform"].split("/", 1)[1], "apk")
            shutil.rmtree(stage)
    for fmt in formats:
        for arch in arches:
            for name in packages:
                if not (dist / f"{name}_{version}_{arch}.{fmt}").is_file():
                    raise ValueError(f"Missing output: {name}/{arch}/{fmt}")
    print(f"Built {len(packages)} packages x {len(arches)} OpenWrt architectures x {len(formats)} formats")


if __name__ == "__main__":
    main()
