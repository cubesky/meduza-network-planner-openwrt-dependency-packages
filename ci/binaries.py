"""Fingerprint, verify and locate compiled outputs independently of OpenWrt packaging."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from common import download, sha256


MANIFEST = "compiled-build.json"
PAYLOAD_DIRS = ("bin", "licenses", "share")
PAYLOAD_FILES = ("runtime.json", "sources.lock.json")


def compile_fingerprint(repo, config, lock, package, target):
    # Package release numbers, OpenWrt architecture aliases, package scripts,
    # acceptance tests and release metadata do not change compiled binaries.
    paths = ["build/Containerfile", "build/build.sh", "ci/binaries.py", "ci/elf.py"]
    if package == "frr":
        paths.append("build/musl-startup-smoke.c")
    paths.extend(path.relative_to(repo).as_posix() for path in sorted((repo / "build/patches" / package).glob("*.patch")))
    sources = {row["name"]: row for row in lock["sources"]}
    inputs = {
        "schema": 1, "package": package, "target": target["target"],
        "platform": target["platform"], "machine": target["machine"],
        "interpreter": target["interpreter"], "builder": config["builder_image"],
        "sources": [sources[name] for name in lock["packages"][package]["sources"]],
        "recipe": {path: sha256(repo / path) for path in paths},
    }
    import hashlib
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def payload_files(tree):
    files = {}
    for directory in PAYLOAD_DIRS:
        root = tree / directory
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"Missing compiled output directory: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Unexpected symlink in compiled output: {path}")
            if path.is_file():
                files[path.relative_to(tree).as_posix()] = sha256(path)
    for name in PAYLOAD_FILES:
        path = tree / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing compiled output file: {path}")
        files[name] = sha256(path)
    if not any(name.startswith("bin/") for name in files):
        raise ValueError("No compiled binaries")
    return files


def save_manifest(tree, fingerprint, commit):
    manifest = {"schema": 1, "fingerprint": fingerprint, "commit": commit, "files": payload_files(tree)}
    (tree / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_manifest(tree, fingerprint):
    manifest = json.loads((tree / MANIFEST).read_text())
    if manifest.get("schema") != 1 or manifest.get("fingerprint") != fingerprint:
        raise ValueError("Compiled artifact does not match the current compilation inputs")
    # ZIP artifacts omit empty directories (e.g. tinc's share/ directory).
    for directory in PAYLOAD_DIRS:
        (tree / directory).mkdir(exist_ok=True)
    if manifest.get("files") != payload_files(tree):
        raise ValueError("Compiled artifact SHA256/file list mismatch")
    # upload/download-artifact does not preserve executable permissions.
    for path in (tree / "bin").iterdir():
        if path.is_file():
            path.chmod(0o755)
    return manifest


def compile_binaries(repo, config, lock, package, target, tree):
    downloads = repo / "downloads"
    downloads.mkdir(exist_ok=True)
    sources = {row["name"]: row for row in lock["sources"]}
    for name in lock["packages"][package]["sources"]:
        source = sources[name]
        archive = downloads / f"{name}.tar.gz"
        if not archive.exists() or sha256(archive) != source["sha256"]:
            download(source["url"], archive, source["sha256"])
    command = ["podman", "build", "--platform", target["platform"], "-f", "build/Containerfile",
               "--build-arg", f"BUILDER_IMAGE={config['builder_image']}", "--build-arg", f"PACKAGE={package}",
               "--build-arg", f"TARGETPLATFORM={target['platform']}", "--build-arg", "BUILD_JOBS=2",
               "--output", f"type=local,dest={tree}", "."]
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=repo, check=True)


def prepare_binaries(repo, config, lock, package, target, tree, reuse=False):
    fingerprint = compile_fingerprint(repo, config, lock, package, target)
    if reuse:
        manifest = verify_manifest(tree, fingerprint)
        print(f"Reusing verified binaries: {package} / {target['platform']} / {fingerprint}", flush=True)
        return manifest
    if tree.exists():
        raise ValueError(f"Refusing to reuse an existing build output without --reuse-binaries: {tree}")
    tree.parent.mkdir(parents=True, exist_ok=True)
    compile_binaries(repo, config, lock, package, target, tree)
    return save_manifest(tree, fingerprint, os.getenv("GITHUB_SHA", "local"))


def find_artifact(repo, name):
    # Repository artifacts are accessible across tag runs, unlike tag-scoped
    # dependency caches. Only use outputs from this workflow in this repository.
    response = json.loads(subprocess.check_output(
        ["gh", "api", f"repos/{repo}/actions/artifacts?name={quote(name)}&per_page=100"], text=True))
    for artifact in sorted(response["artifacts"], key=lambda row: row["id"], reverse=True):
        run = artifact.get("workflow_run", {})
        if (artifact["name"] != name or artifact.get("expired") or not run.get("id")
                or run.get("repository_id") != run.get("head_repository_id")):
            continue
        details = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/actions/runs/{run['id']}"], text=True))
        if (details.get("path") == ".github/workflows/build-packages.yml"
                and details.get("event") in ("push", "workflow_dispatch")):
            return {"artifact_id": str(artifact["id"]), "run_id": str(run["id"])}
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=("tinc", "frr"), required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--mode", choices=("auto", "repackage", "rebuild"), default="auto")
    args = parser.parse_args()
    repo = Path.cwd()
    config = json.loads((repo / "ci/targets.json").read_text())
    lock = json.loads((repo / "ci/sources.lock.json").read_text())
    target = next(row for row in config["targets"] if row["target"] == args.target)
    fingerprint = compile_fingerprint(repo, config, lock, args.package, target)
    name = f"compiled-{args.package}-{target['platform'].replace('/', '-')}-{fingerprint}"
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    found = {} if args.mode == "rebuild" else find_artifact(repository, name)
    if args.mode == "repackage" and not found:
        parser.error("Repackage-only requested, but no matching compiled artifact exists. Run auto/rebuild once to seed it.")
    outputs = {"fingerprint": fingerprint, "name": name, "hit": str(bool(found)).lower(), **found,
               "path": f"out/{args.package}/{target['platform'].replace('/', '-')}"}
    print(json.dumps(outputs), flush=True)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in outputs.items():
            output.write(f"{key}={value}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        action = "Repackage verified binaries" if found else "Compile (no matching reusable artifact, or rebuild requested)"
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(f"- {args.package} / {target['platform']}: **{action}**; fingerprint {fingerprint}\n")


if __name__ == "__main__":
    main()
