"""Install, run and remove packages in checksum-pinned native OpenWrt rootfs images."""

import argparse
import json
from pathlib import Path
import subprocess

from common import download, sha256
from openwrt import FRR_BINS


SCRIPT = r'''
set -eu
# sh runs instead of init, so prepare OpenWrt's volatile /var -> /tmp paths.
mkdir -p /tmp /var/lock /var/run
base="/pkgs/${PACKAGE}_${VERSION}_${ARCH}.${FORMAT}"
case "$FORMAT" in
  ipk) opkg install "$base"
       if [ "$PACKAGE" = frr ]; then opkg install /pkgs/frr-*_${VERSION}_${ARCH}.ipk; fi ;;
  # Disposable rootfs containers can run from tmpfs without a persistent APK cache.
  apk) apk add --allow-untrusted --no-network --force-non-repository "$base"
       if [ "$PACKAGE" = frr ]; then apk add --allow-untrusted --no-network --force-non-repository /pkgs/frr-*_${VERSION}_${ARCH}.apk; fi ;;
esac
if [ "$PACKAGE" = frr ]; then
  # Meduza's FRR plan chowns both its pathspace and generated configuration.
  id frr
  mkdir -p /var/run/frr/meduza-smoke /tmp/frr-generation-smoke
  chown frr:frr /var/run/frr/meduza-smoke
  chown -R frr:frr /tmp/frr-generation-smoke
fi
for bin in $BINARIES; do
  case "$bin" in */vtysh) "$bin" --help ;; *) "$bin" --version ;; esac
done
if [ "$PACKAGE" = tinc ]; then
  /usr/sbin/tinc -b -c /tmp/tinc-native-smoke init smoke
  test -s /tmp/tinc-native-smoke/rsa_key.priv
  test -s /tmp/tinc-native-smoke/ed25519_key.priv
else
  /usr/bin/vtysh -C -f /etc/frr/frr.conf
fi
if [ "$PACKAGE" = frr ]; then
  names=""
  for file in /pkgs/frr-*_${VERSION}_${ARCH}.${FORMAT}; do
    name=${file##*/}; names="$names ${name%%_*}"
  done
  case "$FORMAT" in ipk) opkg remove $names ;; apk) apk del $names ;; esac
fi
case "$FORMAT" in ipk) opkg remove "$PACKAGE" ;; apk) apk del "$PACKAGE" ;; esac
if [ "$PACKAGE" = tinc ]; then test ! -e /usr/sbin/tincd; else test ! -e /usr/sbin/zebra; fi
'''


def prepare_rootfs(config, target, fmt, directory):
    release = config["acceptance_releases"][fmt]
    board = target["openwrt_target"]
    filename = f"openwrt-{release}-{board.replace('/', '-')}-rootfs.tar.gz"
    archive = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    expected = target["rootfs_sha256"][fmt]
    if not archive.exists() or sha256(archive) != expected:
        download(f"https://downloads.openwrt.org/releases/{release}/targets/{board}/{filename}", archive, expected)
    platform = target["platform"].split("/")
    image = f"localhost/meduza-openwrt-acceptance:{release}-{fmt}-{'-'.join(platform)}"
    command = ["podman", "import", "--os", platform[0], "--arch", platform[1]]
    if len(platform) == 3:
        command += ["--variant", platform[2]]
    # Official ARM container tags use OpenWrt package arch names as OCI arch
    # metadata. Import the verified rootfs with standard arm64 / arm/v7 instead.
    subprocess.run([*command, str(archive.resolve()), image], check=True, timeout=300)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, choices=("tinc", "frr"))
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
    version = lock["packages"][args.package]["version"]
    arch = target["acceptance_arch"]
    packages = repo / "dist" if arch in arches else repo / "out" / args.package / target["platform"].replace("/", "-") / "acceptance"
    bins = ["tinc", "tincd"] if args.package == "tinc" else FRR_BINS
    paths = [f"/usr/{'bin' if name == 'vtysh' else 'sbin'}/{name}" for name in bins]
    for fmt in formats:
        if not (packages / f"{args.package}_{version}_{arch}.{fmt}").is_file():
            parser.error(f"Missing native acceptance packages: {packages}")
        image = prepare_rootfs(config, target, fmt, repo / "out/acceptance-rootfs")
        print(f"Native {fmt} acceptance: {target['platform']} / {arch}", flush=True)
        subprocess.run(["podman", "run", "--rm", "--network=none", "--platform", target["platform"],
                        "-v", f"{packages.resolve()}:/pkgs:ro,Z", "-e", f"PACKAGE={args.package}",
                        "-e", f"VERSION={version}", "-e", f"FORMAT={fmt}", "-e", f"ARCH={arch}",
                        "-e", f"BINARIES={' '.join(paths)}", image, "sh", "-euc", SCRIPT], check=True, timeout=600)


if __name__ == "__main__":
    main()
