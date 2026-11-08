"""Build real OpenWrt packages using an official, release-matched Linux SDK."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
from urllib.request import urlopen


DOWNLOADS = "https://downloads.openwrt.org/releases"


def download(url, destination):
    for attempt in range(4):
        try:
            print(f"Downloading {url}", flush=True)
            with urlopen(url, timeout=120) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def read_checksums(path):
    entries = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line)
        if match:
            entries[match[2].removeprefix("./")] = match[1].lower()
    return entries


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify(path, expected):
    if sha256(path) != expected:
        raise ValueError(f"SHA256 mismatch: {path.name}")


def sdk_name(checksums, release, target):
    prefix = f"openwrt-sdk-{release}-{target.replace('/', '-')}"
    matches = [name for name in checksums if name.startswith(prefix + "_")
               and re.search(r"\.Linux-x86_64\.tar\.(xz|zst)$", name)]
    if len(matches) != 1:
        raise ValueError(f"Expected one Linux x86_64 SDK for {release}/{target}: {matches}")
    return matches[0]


def run(*command, cwd):
    print("+ " + " ".join(map(str, command)), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def write_feeds(sdk, release_feeds):
    match = re.search(r"^src-git(?:-full)?\s+packages\s+\S+\^([0-9a-f]{40})\s*$",
                      release_feeds, re.M)
    if not match:
        raise ValueError("The official release did not supply a pinned packages feed")
    packages_revision = match[1]
    # SDKs only ship toolchain/kernel recipes. The pinned base feed supplies
    # dependencies such as OpenSSL, zlib, ncurses and util-linux.
    defaults = (sdk / "feeds.conf.default").read_text()
    match = re.search(r"^src-git(?:-full)?\s+--root=package\s+base\s+\S+\^([0-9a-f]{40})\s*$",
                      defaults, re.M)
    if not match:
        raise ValueError("The SDK did not supply a pinned base feed")
    base_revision = match[1]
    (sdk / "feeds.conf").write_text(
        f"src-git --root=package base https://github.com/openwrt/openwrt.git^{base_revision}\n"
        f"src-git packages https://github.com/openwrt/packages.git^{packages_revision}\n"
    )
    return {"base": base_revision, "packages": packages_revision}


def prepare_sdk(work, release, target, arch):
    base_url = f"{DOWNLOADS}/{release}/targets/{target}"
    checksums_file = work / "sha256sums"
    download(f"{base_url}/sha256sums", checksums_file)
    checksums = read_checksums(checksums_file)
    name = sdk_name(checksums, release, target)
    for filename in ("profiles.json", "feeds.buildinfo"):
        download(f"{base_url}/{filename}", work / filename)
        verify(work / filename, checksums[filename])
    profile = json.loads((work / "profiles.json").read_text())
    if profile["arch_packages"] != arch:
        raise ValueError(f"SDK architecture {profile['arch_packages']} does not match {arch}")

    archive = work / name
    download(f"{base_url}/{name}", archive)
    verify(archive, checksums[name])
    sdk = work / "sdk"
    sdk.mkdir()
    run("tar", "-xf", str(archive), "--strip-components=1", "-C", str(sdk), cwd=work)
    archive.unlink()

    revisions = write_feeds(sdk, (work / "feeds.buildinfo").read_text())
    return sdk, {"sdk_url": f"{base_url}/{name}", "sdk_sha256": checksums[name],
                 "feed_revisions": revisions}


def package_names(makefile, package):
    if package == "tinc":
        return ["tinc"]
    # Read the pinned recipe, so its complete FRR daemon set is selected.
    names = set(re.findall(r"call BuildPackage,(frr(?:-[\w-]+)?)\)", makefile))
    names.update("frr-" + name for name in re.findall(r"call BuildDaemon,([\w-]+),", makefile))
    if not {"frr", "frr-zebra", "frr-watchfrr", "frr-bgpd", "frr-ospfd"} <= names:
        raise ValueError("Unexpected FRR recipe: required subpackages are missing")
    return sorted(names)


def configure(sdk, package, package_format):
    recipe = sdk / "feeds" / "packages" / "net" / package / "Makefile"
    selected = package_names(recipe.read_text(), package)
    overrides = {
        "ALL": "n", "ALL_NONSHARED": "n", "ALL_KMODS": "n",
        "AUTOREMOVE": "n", "IPV6": "y", "BUILD_LOG": "y",
        "SIGNED_PACKAGES": "n",
    }
    overrides.update({f"PACKAGE_{name}": "m" for name in selected})
    config = sdk / ".config"
    lines = config.read_text().splitlines() if config.exists() else []
    lines = [line for line in lines if not any(
        line.startswith(f"CONFIG_{key}=") or line == f"# CONFIG_{key} is not set"
        for key in overrides
    )]
    lines.extend(f"CONFIG_{key}={value}" if value != "n" else f"# CONFIG_{key} is not set"
                 for key, value in overrides.items())
    config.write_text("\n".join(lines) + "\n")
    run("make", "defconfig", cwd=sdk)
    actual = config.read_text()
    apk_enabled = "CONFIG_USE_APK=y" in actual.splitlines()
    if apk_enabled != (package_format == "apk"):
        raise ValueError(f"SDK package manager does not match requested format {package_format}")
    for name in selected:
        if not re.search(rf"^CONFIG_PACKAGE_{re.escape(name)}=[my]$", actual, re.M):
            raise ValueError(f"Package was disabled by SDK configuration: {name}")
    return selected, recipe


def collect(sdk, destination, package_format, selected):
    destination.mkdir(parents=True)
    found = set()
    payloads = {}
    for source in sorted((sdk / "bin").rglob(f"*.{package_format}")):
        if source.name.startswith("kmod-"):
            continue  # Kernel modules must come from the running firmware's exact kernel feed.
        if package_format == "ipk":
            name = source.name.split("_", 1)[0]
        else:
            match = re.match(r"^(.+?)-(?=[0-9])", source.name)
            if not match:
                raise ValueError(f"Unrecognized APK filename: {source.name}")
            name = match[1]
        if name in selected:
            found.add(name)
        elif name == "tinc" or name == "frr" or name.startswith("frr-"):
            raise ValueError(f"Unselected application package: {source.name}")
        folder = "packages" if name in selected else "dependencies"
        relative = f"{folder}/{source.name}"
        digest = sha256(source)
        if relative in payloads and payloads[relative] != digest:
            raise ValueError(f"Conflicting output packages: {relative}")
        payloads[relative] = digest
        output = destination / relative
        output.parent.mkdir(exist_ok=True)
        shutil.copy2(source, output)
    missing = set(selected) - found
    if missing:
        raise ValueError(f"Build did not produce required packages: {', '.join(sorted(missing))}")
    return payloads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, choices=("tinc", "frr"))
    parser.add_argument("--format", required=True, choices=("ipk", "apk"))
    parser.add_argument("--release", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--arch", required=True)
    args = parser.parse_args()
    # Accept only combinations tracked in the repository.
    config = json.loads(Path(__file__).with_name("targets.json").read_text())
    if config["releases"][args.format] != args.release or not any(
        row["arch"] == args.arch and row["target"] == args.target
        for row in config["architectures"]
    ):
        parser.error("Unknown release/format/architecture/target combination")

    root = Path.cwd()
    work = root / "build"
    work.mkdir()  # Refuse to reuse a potentially dirty SDK.
    sdk, provenance = prepare_sdk(work, args.release, args.target, args.arch)
    downloads = root / "downloads"
    downloads.mkdir(exist_ok=True)
    if (sdk / "dl").exists():
        shutil.copytree(sdk / "dl", downloads, dirs_exist_ok=True)
        shutil.rmtree(sdk / "dl")
    (sdk / "dl").symlink_to(downloads, target_is_directory=True)
    run("./scripts/feeds", "update", "base", "packages", cwd=sdk)
    for feed, expected in provenance["feed_revisions"].items():
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=sdk / "feeds" / feed, text=True
        ).strip()
        if revision != expected:
            raise ValueError(f"Checked-out {feed} feed does not match the official release")
        run("./scripts/feeds", "install", "-a", "-p", feed, cwd=sdk)
    selected, recipe = configure(sdk, args.package, args.format)
    jobs = str(min(os.cpu_count() or 2, 2))
    goal = f"package/feeds/packages/{args.package}"
    run("make", f"{goal}/download", f"-j{jobs}", "V=s", cwd=sdk)
    try:
        run("make", f"{goal}/compile", f"-j{jobs}", "V=s", cwd=sdk)
    except subprocess.CalledProcessError:
        print("Retrying sequentially with verbose diagnostics", flush=True)
        run("make", f"{goal}/compile", "-j1", "V=s", cwd=sdk)

    bundle = f"{args.package}-openwrt-{args.release}-{args.arch}-{args.format}"
    output = root / "output" / bundle
    payloads = collect(sdk, output, args.format, selected)
    provenance.update(vars(args), packages=selected,
                      repository_commit=os.getenv("GITHUB_SHA", "local"),
                      workflow_run=os.getenv("GITHUB_RUN_ID", "local"),
                      files=payloads)
    (output / "build-info.json").write_text(json.dumps(provenance, indent=2) + "\n")
    shutil.copy2(sdk / ".config", output / "sdk.config")
    shutil.copy2(recipe, output / "package.Makefile")
    shutil.copy2(root / "INSTALL.md", output / "INSTALL.md")
    with (output / "SHA256SUMS").open("w") as sums:
        for path in sorted(output.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                sums.write(f"{sha256(path)}  {path.relative_to(output).as_posix()}\n")
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    with tarfile.open(dist / f"{bundle}.tar.gz", "w:gz") as archive:
        archive.add(output, arcname=bundle)
    print(f"Built {bundle}: {len(selected)} application packages", flush=True)


if __name__ == "__main__":
    main()
