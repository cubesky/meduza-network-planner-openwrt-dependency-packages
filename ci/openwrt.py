"""OpenWrt layout matching meduza-network-planner-v2 tools/release/src/openwrt.rs.

IPK is gzip(tar(debian-binary, data.tar.gz, control.tar.gz)), with literal ./ paths.
APK is staged for apk-tools 3 mkpkg in the main project's pinned Alpine image.
"""

import gzip
import io
import json
from pathlib import Path
import shlex
import tarfile

from common import sha256


FRR_BINS = "babeld bfdd bgpd eigrpd fabricd isisd ldpd nhrpd ospfd ospf6d pathd pbrd pimd pim6d ripd ripngd staticd vrrpd watchfrr zebra mgmtd vtysh".split()
FRR_USERS = "network:network"


def frr_postinst():
    return (Path(__file__).resolve().parents[1] / "packaging/frr.postinst").read_bytes()


def tar_gz(files, directories=True):
    """files is an ordered mapping of literal relative names to (bytes, mode)."""
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            def append(name, data, mode, directory=False):
                info = tarfile.TarInfo(name)
                info.mode, info.uid, info.gid, info.mtime = mode, 0, 0, 0
                info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
                info.size = 0 if directory else len(data)
                archive.addfile(info, None if directory else io.BytesIO(data))
            if directories:
                append("./", b"", 0o755, True)
                parents = {str(parent).replace("\\", "/") for path in files for parent in Path(path).parents if str(parent) != "."}
                for parent in sorted(parents):
                    append(f"./{parent}/", b"", 0o755, True)
            for name, (data, mode) in files.items():
                append(f"./{name}", data, mode)
    return output.getvalue()


def control_text(name, version, arch, files, depends):
    text = f"Package: {name}\nVersion: {version}\n"
    if name == "frr":
        text += f"Require-User: {FRR_USERS}\n"
    if depends:
        text += "Depends: " + ", ".join(f"{dep} (= {version})" if dep == "frr" else dep for dep in depends) + "\n"
    return text + (f"License: GPL-2.0-or-later\nSection: net\nArchitecture: {arch}\n"
                   f"Installed-Size: {sum(len(data) for data, mode in files.values())}\n"
                   f"Maintainer: Meduza NP\nDescription: OpenWrt {name} for Meduza NP\n")


def write_ipk(files, name, version, arch, depends, output):
    control = {"control": (control_text(name, version, arch, files, depends).encode(), 0o644)}
    if name == "frr":
        control["postinst"] = (frr_postinst(), 0o755)
    configs = ["/" + path for path in files if path.startswith("etc/") and not path.startswith("etc/init.d/")]
    if configs:
        control["conffiles"] = (("\n".join(configs) + "\n").encode(), 0o644)
    outer = {"debian-binary": (b"2.0\n", 0o644), "data.tar.gz": (tar_gz(dict(sorted(files.items()))), 0o644),
             "control.tar.gz": (tar_gz(dict(sorted(control.items())), False), 0o644)}
    output.write_bytes(tar_gz(outer, False))


def write_apk_stage(files, name, version, arch, depends, output):
    stage = output.with_name(output.name + ".stage")
    root = stage / "root"
    root.mkdir(parents=True)
    for path, (data, mode) in sorted(files.items()):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(mode)
    info = [f"name:{name}", f"version:{version}", f"arch:{arch}", f"description:OpenWrt {name} for Meduza NP",
            "license:GPL-2.0-or-later", f"origin:{name}", "maintainer:Meduza NP"]
    if depends:
        info.append("depends:" + " ".join(f"{dep}={version}" if dep == "frr" else dep for dep in depends))
    command = "#!/bin/sh\nset -eu\nfind root -exec touch -h -d @0 {} +\nexec apk mkpkg"
    command += "".join(" \\\n  --info " + shlex.quote(value) for value in info)
    command += " \\\n  --files root --output " + shlex.quote("../" + output.name) + "\n"
    if name == "frr":
        users = root / "lib/apk/packages/frr.rusers"
        users.parent.mkdir(parents=True, exist_ok=True)
        users.write_text(FRR_USERS + "\n", newline="\n")
        (stage / "post-install").write_bytes(frr_postinst())
        (stage / "post-install").chmod(0o755)
        command = command.rstrip("\n") + " --script post-install:post-install --script post-upgrade:post-install\n"
    (stage / "mkpkg.sh").write_text(command)
    (stage / "stage.json").write_text(json.dumps({"package": name, "version": version, "file": output.name,
                                                 "platform": "openwrt/" + arch, "format": "apk"}) + "\n")
    return stage


def descriptor(output, name, version, arch, fmt):
    result = {"package": name, "version": version, "file": output.name,
              "artifact": {"platform": "openwrt/" + arch, "format": fmt, "url": "", "size": output.stat().st_size,
                           "sha256": sha256(output), "chunks": [], "oci": None}}
    output.with_name(output.name + ".json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def package_files(tree, package, provenance, repo):
    """Preserve independent tinc/FRR applications and the individual FRR daemon packages."""
    def file(path, mode=0o644):
        return path.read_bytes(), mode
    def add_tree(files, source, prefix):
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    files[prefix + path.relative_to(source).as_posix()] = file(path)
    common = {}
    system_depends = provenance.get("runtime", {}).get("systemPackages", [])
    add_tree(common, tree / "licenses", f"usr/share/licenses/{package}/")
    common[f"usr/share/meduza-np-dependencies/{package}/build-info.json"] = (json.dumps(provenance, indent=2).encode() + b"\n", 0o644)
    if package == "tinc":
        for name in ("tinc", "tincd"):
            common[f"usr/sbin/{name}"] = file(tree / "bin" / name, 0o755)
        common["etc/config/tinc"] = file(repo / "packaging/tinc.config")
        common["etc/init.d/tinc"] = file(repo / "packaging/tinc.init", 0o755)
        return {"tinc": (common, system_depends)}
    add_tree(common, tree / "share", "usr/share/")
    common["etc/frr/frr.conf"] = (b"frr defaults traditional\nservice integrated-vtysh-config\n", 0o600)
    common["etc/frr/daemons"] = (b"# Standalone service only; leave disabled when managed by Meduza NP.\nDAEMONS='mgmtd zebra staticd bgpd bfdd ospfd'\n", 0o644)
    common["etc/init.d/frr"] = file(repo / "packaging/frr.init", 0o755)
    common["usr/libexec/frr-prepare"] = file(repo / "packaging/frr.prepare", 0o755)
    result = {"frr": (common, system_depends)}
    for name in FRR_BINS:
        path = "usr/bin/vtysh" if name == "vtysh" else f"usr/sbin/{name}"
        result[f"frr-{name}"] = ({path: file(tree / "bin" / name, 0o755)}, ["frr"])
    return result
