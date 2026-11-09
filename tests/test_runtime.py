"""OpenWrt runtime checks, including the three reported FRR ELF outputs."""
from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import acceptance
import build
import elf
import openwrt

CONFIG = json.loads((ROOT / "ci/targets.json").read_text())


class ElfTests(unittest.TestCase):
    def test_reported_frr_outputs_allow_the_openwrt_loader_on_all_three_platforms(self):
        for target, fixture in zip(CONFIG["targets"], ("amd64", "arm64", "armv7")):
            with self.subTest(platform=target["platform"]):
                report = (ROOT / f"tests/fixtures/frr-{fixture}.readelf.txt").read_text()
                info = elf.validate_elf(f"  Machine: {target['machine']}\n", report, report, target)
                self.assertFalse(info["static"])
                self.assertEqual(info["interpreter"], target["interpreter"])
                self.assertEqual(info["needed"], [])
                self.assertEqual(info["systemPackages"], ["libc"])

    def test_fully_static_programs_remain_supported(self):
        target = CONFIG["targets"][0]
        result = elf.validate_elf(f"Machine: {target['machine']}\n", "LOAD\n", "There is no dynamic section", target)
        self.assertTrue(result["static"])
        self.assertEqual(result["systemPackages"], [])

    def test_only_packaged_openwrt_shared_libraries_are_allowed(self):
        target = CONFIG["targets"][0]
        header = f"Machine: {target['machine']}\n"
        program = f"INTERP\n [Requesting program interpreter: {target['interpreter']}]"
        result = elf.validate_elf(header, program, "(NEEDED) Shared library: [libc.so]", target)
        self.assertEqual(result["needed"], ["libc.so"])
        for library in ("libfrr.so.0", "libc.musl-x86_64.so.1", "libreadline.so.8"):
            with self.subTest(library=library), self.assertRaisesRegex(ValueError, "Unpackaged"):
                elf.validate_elf(header, program, f"(NEEDED) Shared library: [{library}]", target)

    def test_wrong_machine_or_loader_is_rejected(self):
        target = CONFIG["targets"][0]
        with self.assertRaisesRegex(ValueError, "Wrong ELF machine"):
            elf.validate_elf("Machine: AArch64", "", "", target)
        with self.assertRaisesRegex(ValueError, "interpreter"):
            elf.validate_elf(f"Machine: {target['machine']}\n", "INTERP\n[Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]", "", target)

    def test_host_checks_never_execute_openwrt_dynamic_binaries(self):
        target = CONFIG["targets"][0]
        report = (ROOT / "tests/fixtures/frr-amd64.readelf.txt").read_text()
        with patch.object(elf.subprocess, "check_output", side_effect=[f"Machine: {target['machine']}\n", report, report] * len(openwrt.FRR_BINS)), patch.object(build, "run") as execute:
            result = build.check_binaries(Path("/test"), target, "frr")
        self.assertEqual(len(result), len(openwrt.FRR_BINS))
        self.assertTrue(all(info["systemPackages"] == ["libc"] for info in result.values()))
        execute.assert_not_called()


class AcceptanceTests(unittest.TestCase):
    def test_rootfs_import_uses_pinned_hash_and_standard_oci_architecture(self):
        for original_target in CONFIG["targets"]:
            for fmt in CONFIG["formats"]:
                with self.subTest(platform=original_target["platform"], fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                    config = copy.deepcopy(CONFIG)
                    target = copy.deepcopy(original_target)
                    payload = b"verified rootfs fixture"
                    digest = hashlib.sha256(payload).hexdigest()
                    target["rootfs_sha256"][fmt] = digest
                    def downloaded(url, path, expected):
                        self.assertEqual(expected, digest)
                        path.write_bytes(payload)
                    with patch.object(acceptance, "download", side_effect=downloaded) as download, patch.object(acceptance.subprocess, "run") as run:
                        image = acceptance.prepare_rootfs(config, target, fmt, Path(temporary))
                        acceptance.prepare_rootfs(config, target, fmt, Path(temporary))
                    self.assertEqual(download.call_count, 1)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--arch") + 1], target["platform"].split("/")[1])
                    self.assertEqual(command[-1], image)
                    if target["platform"] == "linux/arm/v7":
                        self.assertEqual(command[command.index("--variant") + 1], "v7")

    def test_single_alias_uses_native_arch_acceptance_packages(self):
        target = CONFIG["targets"][1]
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "ci").mkdir()
            (repo / "ci/targets.json").write_text(json.dumps(CONFIG))
            lock = {"packages": {"frr": {"version": "10.4.1-r1"}}}
            (repo / "ci/sources.lock.json").write_text(json.dumps(lock))
            packages = repo / "out/frr/linux-arm64/acceptance"
            packages.mkdir(parents=True)
            (packages / "frr_10.4.1-r1_aarch64_generic.ipk").write_bytes(b"fixture")
            argv = ["acceptance.py", "--package", "frr", "--target", target["target"], "--arches", "aarch64_cortex-a53", "--formats", "ipk"]
            with patch.object(sys, "argv", argv), patch.object(acceptance.Path, "cwd", return_value=repo), patch.object(acceptance, "prepare_rootfs", return_value="verified-image"), patch.object(acceptance.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                acceptance.main()
            command = run.call_args.args[0]
            self.assertIn(f"{packages.resolve()}:/pkgs:ro,Z", command)
            self.assertIn("linux/arm64", command)
            self.assertIn("ARCH=aarch64_generic", command)
            self.assertTrue(any(value.startswith("BINARIES=") and "/usr/bin/vtysh" in value and "/usr/sbin/babeld" in value for value in command))


if __name__ == "__main__":
    unittest.main()
