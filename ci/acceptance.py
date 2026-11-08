"""Native opkg/apk install/remove acceptance in the main project's OpenWrt rootfs versions."""

import argparse
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, choices=("tinc", "frr"))
    parser.add_argument("--formats", default="ipk,apk")
    args = parser.parse_args()
    config = json.loads(Path("ci/targets.json").read_text())
    lock = json.loads(Path("ci/sources.lock.json").read_text())
    version = lock["packages"][args.package]["version"]
    script = r'''
set -eu
base="/pkgs/${PACKAGE}_${VERSION}_x86_64.${FORMAT}"
case "$FORMAT" in
  ipk) opkg install "$base"
       if [ "$PACKAGE" = frr ]; then opkg install /pkgs/frr-*_${VERSION}_x86_64.ipk; fi ;;
  apk) apk add --allow-untrusted --no-network "$base"
       if [ "$PACKAGE" = frr ]; then apk add --allow-untrusted --no-network /pkgs/frr-*_${VERSION}_x86_64.apk; fi ;;
esac
if [ "$PACKAGE" = tinc ]; then
  /usr/sbin/tinc --version
  /usr/sbin/tincd --version
  /usr/sbin/tinc -b -c /tmp/tinc-native-smoke init smoke
  test -s /tmp/tinc-native-smoke/ed25519_key.priv
else
  for bin in /usr/sbin/zebra /usr/sbin/mgmtd /usr/sbin/bgpd /usr/sbin/bfdd /usr/sbin/ospfd /usr/bin/vtysh; do
    "$bin" --version
  done
fi
names="$PACKAGE"
if [ "$PACKAGE" = frr ]; then
  names=""
  for file in /pkgs/frr-*_${VERSION}_x86_64.${FORMAT}; do
    name=${file##*/}; names="$names ${name%%_*}"
  done
  case "$FORMAT" in ipk) opkg remove $names ;; apk) apk del $names ;; esac
fi
case "$FORMAT" in ipk) opkg remove "$PACKAGE" ;; apk) apk del "$PACKAGE" ;; esac
if [ "$PACKAGE" = tinc ]; then test ! -e /usr/sbin/tincd; else test ! -e /usr/sbin/zebra; fi
'''
    for fmt in args.formats.split(","):
        if fmt not in config["formats"]:
            parser.error("Unknown package format")
        print(f"Native {fmt} acceptance: {config['acceptance_images'][fmt]}", flush=True)
        subprocess.run(["podman", "run", "--rm", "--network=none", "--platform", "linux/amd64",
                        "-v", f"{Path('dist').resolve()}:/pkgs:ro,Z", "-e", f"PACKAGE={args.package}",
                        "-e", f"VERSION={version}", "-e", f"FORMAT={fmt}",
                        config["acceptance_images"][fmt], "sh", "-euc", script], check=True, timeout=600)


if __name__ == "__main__":
    main()
