"""Repacking must not invoke a compiler or silently accept stale compiled outputs."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import binaries
import build
import openwrt

CONFIG = json.loads((ROOT / "ci/targets.json").read_text())
LOCK = json.loads((ROOT / "ci/sources.lock.json").read_text())


def fixture_repo(repo):
    for path in ("build/Containerfile", "build/build.sh", "ci/binaries.py", "ci/elf.py",
                 "build/musl-startup-smoke.c", "build/patches/frr/order.patch"):
        file = repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(path + "\n")
    (repo / "ci/targets.json").write_text(json.dumps(CONFIG))
    (repo / "ci/sources.lock.json").write_text(json.dumps(LOCK))
    shutil.copytree(ROOT / "packaging", repo / "packaging")


def fixture_tree(tree):
    for directory in binaries.PAYLOAD_DIRS:
        (tree / directory).mkdir(parents=True, exist_ok=True)
    for name in openwrt.FRR_BINS:
        (tree / "bin" / name).write_bytes(b"compiled " + name.encode())
    (tree / "licenses/COPYING").write_bytes(b"license fixture")
    (tree / "runtime.json").write_text("{}\n")
    (tree / "sources.lock.json").write_text(json.dumps(LOCK))


class FingerprintTests(unittest.TestCase):
    def test_packaging_release_docs_and_arch_alias_changes_do_not_recompile(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            before = binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0])
            config, lock = copy.deepcopy(CONFIG), copy.deepcopy(LOCK)
            lock["packages"]["frr"]["version"] = "10.4.1-r999"
            config["targets"][0]["arches"].append("another-package-label")
            config["upstream"]["revision"] = "new packaging revision"
            for name in ("packaging/frr.postinst", "ci/openwrt.py", "ci/build.py", "README.md"):
                (repo / name).write_text("packaging changed\n")
            after = binaries.compile_fingerprint(repo, config, lock, "frr", config["targets"][0])
            self.assertEqual(before, after)

    def test_sources_platform_toolchain_and_shared_recipe_changes_recompile(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            before = binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0])
            lock = copy.deepcopy(LOCK)
            next(row for row in lock["sources"] if row["name"] == "frr")["sha256"] = "a" * 64
            self.assertNotEqual(before, binaries.compile_fingerprint(repo, CONFIG, lock, "frr", CONFIG["targets"][0]))
            self.assertNotEqual(before, binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][1]))
            config = copy.deepcopy(CONFIG)
            config["builder_image"] = "different immutable toolchain"
            self.assertNotEqual(before, binaries.compile_fingerprint(repo, config, LOCK, "frr", CONFIG["targets"][0]))
            (repo / "build/build.sh").write_text("new compiler flags\n")
            self.assertNotEqual(before, binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0]))

    def test_frr_patch_changes_do_not_invalidate_tinc(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            before = {p: binaries.compile_fingerprint(repo, CONFIG, LOCK, p, CONFIG["targets"][0]) for p in ("frr", "tinc")}
            (repo / "build/patches/frr/new.patch").write_text("fix FRR\n")
            self.assertNotEqual(before["frr"], binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0]))
            self.assertEqual(before["tinc"], binaries.compile_fingerprint(repo, CONFIG, LOCK, "tinc", CONFIG["targets"][0]))


class BinaryReuseTests(unittest.TestCase):
    def test_verified_reuse_never_downloads_sources_or_invokes_compilation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            tree = repo / "out/frr/linux-amd64"
            fixture_tree(tree)
            fingerprint = binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0])
            binaries.save_manifest(tree, fingerprint, "original-compiled-commit")
            (tree / "share").rmdir()  # Actions artifacts omit empty directories.
            with patch.object(binaries, "compile_binaries") as compile_step, patch.object(binaries, "download") as download:
                result = binaries.prepare_binaries(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0], tree, reuse=True)
            compile_step.assert_not_called()
            download.assert_not_called()
            self.assertEqual(result["commit"], "original-compiled-commit")
            self.assertTrue((tree / "share").is_dir())

    def test_stale_or_tampered_reuse_fails_instead_of_compiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            tree = repo / "out/frr/linux-amd64"
            fixture_tree(tree)
            fingerprint = binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0])
            binaries.save_manifest(tree, fingerprint, "old")
            (tree / "bin/bgpd").write_bytes(b"corrupted")
            with patch.object(binaries, "compile_binaries") as compile_step:
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    binaries.prepare_binaries(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0], tree, reuse=True)
                with self.assertRaisesRegex(ValueError, "compilation inputs"):
                    binaries.verify_manifest(tree, "stale fingerprint")
            compile_step.assert_not_called()

    def test_cache_miss_compiles_and_writes_a_complete_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            tree = repo / "out/frr/linux-amd64"
            with patch.object(binaries, "compile_binaries", side_effect=lambda *args: fixture_tree(args[-1])) as compile_step:
                result = binaries.prepare_binaries(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0], tree)
            compile_step.assert_called_once()
            self.assertEqual(binaries.verify_manifest(tree, result["fingerprint"]), result)

    def test_repackage_produces_new_install_hooks_and_metadata_without_podman_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            tree = repo / "out/frr/linux-amd64"
            fixture_tree(tree)
            fingerprint = binaries.compile_fingerprint(repo, CONFIG, LOCK, "frr", CONFIG["targets"][0])
            binaries.save_manifest(tree, fingerprint, "binary-commit")
            hook = b"#!/bin/sh\n# new packaging hook\n"
            (repo / "packaging/frr.postinst").write_bytes(hook)
            # openwrt.py locates the package script relative to its module.
            runtime = {name: {"static": True, "systemPackages": []} for name in openwrt.FRR_BINS}
            argv = ["build.py", "--package", "frr", "--target", CONFIG["targets"][0]["target"],
                    "--arches", "x86_64", "--formats", "ipk", "--reuse-binaries"]
            with (patch.object(sys, "argv", argv), patch.object(build.Path, "cwd", return_value=repo),
                  patch.object(build, "check_binaries", return_value=runtime),
                  patch.object(openwrt, "frr_postinst", return_value=hook),
                  patch.object(binaries, "compile_binaries") as compile_step,
                  patch.object(binaries, "download") as download, patch.object(build, "run") as execute,
                  patch.dict(os.environ, {"GITHUB_SHA": "packaging-commit"}), redirect_stdout(io.StringIO())):
                build.main()
            compile_step.assert_not_called()
            download.assert_not_called()
            execute.assert_not_called()
            path = repo / f"dist/frr_{LOCK['packages']['frr']['version']}_x86_64.ipk"
            with tarfile.open(path) as outer:
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile("./control.tar.gz").read())) as control:
                    self.assertEqual(control.extractfile("./postinst").read(), hook)
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile("./data.tar.gz").read())) as data:
                    info = json.loads(data.extractfile("./usr/share/meduza-np-dependencies/frr/build-info.json").read())
            self.assertEqual(info["commit"], "packaging-commit")
            self.assertEqual(info["binaryBuild"], {"fingerprint": fingerprint, "commit": "binary-commit", "reused": True})
            self.assertEqual(len(list((repo / "dist").glob("*.ipk"))), 23)


class ArtifactLookupTests(unittest.TestCase):
    def test_lookup_uses_exact_fingerprint_across_runs_and_rejects_other_workflows(self):
        def artifact(ident, **kwargs):
            return {"id": ident, "name": "compiled-exact", "expired": False,
                    "workflow_run": {"id": ident + 100, "repository_id": 5, "head_repository_id": 5}, **kwargs}
        response = {"artifacts": [artifact(1), artifact(2),
                                  artifact(3, expired=True), artifact(4, name="other-inputs")]}
        details = [{"path": ".github/workflows/other.yml", "event": "push"},
                   {"path": ".github/workflows/build-packages.yml", "event": "workflow_dispatch"}]
        with patch.object(binaries.subprocess, "check_output", side_effect=[json.dumps(response), *map(json.dumps, details)]) as api:
            self.assertEqual(binaries.find_artifact("owner/repo", "compiled-exact"), {"artifact_id": "1", "run_id": "101"})
        self.assertIn("name=compiled-exact", api.call_args_list[0].args[0][-1])

    def test_repackage_mode_fails_on_miss_and_rebuild_bypasses_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            fixture_repo(repo)
            output = repo / "step-outputs"
            for mode in ("repackage", "rebuild"):
                with (self.subTest(mode=mode), patch.object(binaries.Path, "cwd", return_value=repo),
                      patch.object(sys, "argv", ["binaries.py", "--package", "frr", "--target", CONFIG["targets"][0]["target"], "--mode", mode]),
                      patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_OUTPUT": str(output)}),
                      patch.object(binaries, "find_artifact", return_value={}) as lookup,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    if mode == "repackage":
                        with self.assertRaises(SystemExit) as failed:
                            binaries.main()
                        self.assertEqual(failed.exception.code, 2)
                        self.assertFalse(output.exists())
                    else:
                        binaries.main()
                        lookup.assert_not_called()
                        self.assertIn("hit=false\n", output.read_text())


if __name__ == "__main__":
    unittest.main()
