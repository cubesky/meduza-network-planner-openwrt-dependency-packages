from contextlib import redirect_stdout
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))
import common
import matrix
import openwrt
import sync_upstream

CONFIG = json.loads((ROOT / "ci/targets.json").read_text())


class FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200, length=None, content_type="application/octet-stream"):
        super().__init__(payload)
        self.status = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload) if length is None else length)}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        capture = redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_corrupt_download_retries_with_cache_bust(self):
        good = b"verified source"
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.tar.gz"
            with patch.object(common, "urlopen", side_effect=[FakeResponse(b"corrupt"), FakeResponse(good)]) as request, patch.object(common.time, "sleep"):
                common.download("https://example.com/source.tar.gz", destination, hashlib.sha256(good).hexdigest())
            self.assertEqual(destination.read_bytes(), good)
            first, second = [call.args[0] for call in request.call_args_list]
            self.assertTrue(second.full_url.startswith(first.full_url + "?download_retry="))
            self.assertEqual(second.get_header("Accept-encoding"), "identity")
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_permanent_corruption_preserves_previous_file_and_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.tar.gz"
            destination.write_bytes(b"previous verified file")
            with patch.object(common, "urlopen", side_effect=[FakeResponse(b"bad"), FakeResponse(b"bad")]), patch.object(common.time, "sleep"), self.assertRaisesRegex(RuntimeError, "expected .*actual"):
                common.download("https://example.com/source.tar.gz", destination, "a" * 64, attempts=2)
            self.assertEqual(destination.read_bytes(), b"previous verified file")
            self.assertFalse(destination.with_name(destination.name + ".part").exists())

    def test_truncated_successful_response_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.tar.gz"
            with patch.object(common, "urlopen", side_effect=[FakeResponse(b"short", length=100), FakeResponse(b"complete")]), patch.object(common.time, "sleep"):
                common.download("https://example.com/source.tar.gz", destination)
            self.assertEqual(destination.read_bytes(), b"complete")

    def test_partial_http_response_and_html_are_rejected(self):
        for response in (FakeResponse(b"range", status=206), FakeResponse(b"<html>error</html>", content_type="text/html")):
            with self.subTest(status=response.status), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "source.tar.gz"
                with patch.object(common, "urlopen", return_value=response), self.assertRaises(RuntimeError):
                    common.download("https://example.com/source.tar.gz", destination, attempts=1)
                self.assertFalse(destination.exists())

    def test_incomplete_read_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "source.tar.gz"
            with patch.object(common, "urlopen", side_effect=[IncompleteRead(b"part", 100), FakeResponse(b"complete")]), patch.object(common.time, "sleep"):
                common.download("https://example.com/source.tar.gz", destination)
            self.assertEqual(destination.read_bytes(), b"complete")


class MatrixTests(unittest.TestCase):
    def test_six_builds_cover_all_32_application_architecture_format_combinations(self):
        rows = matrix.build_matrix(CONFIG)["include"]
        combinations = {(row["package"], arch, fmt) for row in rows for arch in row["arches"].split(",") for fmt in row["formats"].split(",")}
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row["artifact"] for row in rows}), 6)
        self.assertEqual(len(combinations), 32)
        self.assertEqual(len({arch for package, arch, fmt in combinations}), 8)

    def test_generic_arm64_is_not_missing(self):
        rows = matrix.build_matrix(CONFIG, "all", "all", "aarch64_generic")["include"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["arches"] == "aarch64_generic" and row["formats"] == "ipk,apk" for row in rows))

    def test_platform_aliases_select_whole_upstream_platform(self):
        rows = matrix.build_matrix(CONFIG, "tinc", "apk", "arm64,aarch64_generic,arm-v7,x64,linux-amd64")["include"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(len(row["arches"].split(",")) for row in rows), 8)
        self.assertTrue(all(row["formats"] == "apk" for row in rows))

    def test_unsupported_and_removed_architectures_fail(self):
        for kwargs in ({"architectures": "x86"}, {"architectures": "all,x64"}, {"architectures": ""}, {"package": "bad"}, {"package_format": "deb"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                matrix.build_matrix(CONFIG, **kwargs)

    def test_architectures_and_image_match_recorded_upstream_constants(self):
        layout = (ROOT / "tests/fixtures/openwrt.rs").read_text()
        script = f"IMAGE={CONFIG['builder_image']}\n"
        sync_upstream.check_contract(CONFIG, layout, script)
        with self.assertRaises(ValueError):
            sync_upstream.check_contract(CONFIG, layout.replace('"aarch64_generic", ', ''), script)


def untar(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return {entry.name: (archive.extractfile(entry).read(), entry.mode, entry.mtime, entry.uid, entry.gid)
                for entry in archive if entry.isfile()}


class PackagingTests(unittest.TestCase):
    def test_ipk_matches_upstream_outer_layout_and_preserves_config_and_modes(self):
        files = {"usr/sbin/tincd": (b"static ELF fixture", 0o755), "etc/config/tinc": (b"config tinc", 0o644)}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "tinc_1.1_pre18-r1_aarch64_generic.ipk"
            openwrt.write_ipk(files, "tinc", "1.1_pre18-r1", "aarch64_generic", [], output)
            first = output.read_bytes()
            openwrt.write_ipk(files, "tinc", "1.1_pre18-r1", "aarch64_generic", [], output)
            self.assertEqual(first, output.read_bytes())
            outer = untar(first)
            self.assertEqual(list(outer), ["./debian-binary", "./data.tar.gz", "./control.tar.gz"])
            self.assertEqual(outer["./debian-binary"][0], b"2.0\n")
            control = untar(outer["./control.tar.gz"][0])
            self.assertIn(b"Architecture: aarch64_generic\n", control["./control"][0])
            self.assertEqual(control["./conffiles"][0], b"/etc/config/tinc\n")
            binary = untar(outer["./data.tar.gz"][0])["./usr/sbin/tincd"]
            self.assertEqual(binary[1:], (0o755, 0, 0, 0))

    def test_frr_exact_version_dependency_is_native_to_each_format(self):
        text = openwrt.control_text("frr-bgpd", "10.4.1-r1", "x86_64", {}, ["frr"])
        self.assertIn("Depends: frr (= 10.4.1-r1)", text)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frr-bgpd_10.4.1-r1_x86_64.apk"
            stage = openwrt.write_apk_stage({"usr/sbin/bgpd": (b"ELF", 0o755)}, "frr-bgpd", "10.4.1-r1", "x86_64", ["frr"], output)
            script = (stage / "mkpkg.sh").read_text()
            self.assertIn("exec apk mkpkg", script)
            self.assertIn("arch:x86_64", script)
            self.assertIn("depends:frr=10.4.1-r1", script)
            self.assertIn("--files root", script)
            self.assertEqual(json.loads((stage / "stage.json").read_text())["platform"], "openwrt/x86_64")

    def test_openwrt_libc_dependency_does_not_use_the_frr_version(self):
        text = openwrt.control_text("frr", "10.4.1-r1", "x86_64", {}, ["libc"])
        self.assertIn("Depends: libc\n", text)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "frr_10.4.1-r1_x86_64.apk"
            stage = openwrt.write_apk_stage({}, "frr", "10.4.1-r1", "x86_64", ["libc"], output)
            script = (stage / "mkpkg.sh").read_text()
            self.assertIn("depends:libc", script)
            self.assertNotIn("libc=10.4.1", script)

    def test_descriptors_match_main_project_schema_and_actual_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "tinc_1_x86_64.ipk"
            output.write_bytes(b"package")
            meta = openwrt.descriptor(output, "tinc", "1", "x86_64", "ipk")
            self.assertEqual(set(meta), {"package", "version", "file", "artifact"})
            self.assertEqual(meta["artifact"]["platform"], "openwrt/x86_64")
            self.assertEqual(meta["artifact"]["sha256"], hashlib.sha256(b"package").hexdigest())
            self.assertEqual(meta["artifact"]["size"], 7)

    def test_frr_has_each_required_binary_in_an_independent_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / "bin").mkdir()
            for name in openwrt.FRR_BINS:
                (tree / "bin" / name).write_bytes(b"ELF fixture")
            packages = openwrt.package_files(tree, "frr", {"static": False, "runtime": {"systemPackages": ["libc"]}}, ROOT)
            self.assertEqual(len(packages), 23)
            self.assertEqual(packages["frr"][1], ["libc"])
            for name in ("zebra", "bgpd", "bfdd", "ospfd", "mgmtd", "vtysh"):
                self.assertIn("frr-" + name, packages)
            self.assertIn("usr/bin/vtysh", packages["frr-vtysh"][0])
            self.assertTrue(all(depends == ["frr"] for name, (files, depends) in packages.items() if name != "frr"))
            self.assertFalse(any("tinc" in name for name in packages))

    def test_source_lock_pins_all_required_sources(self):
        lock = json.loads((ROOT / "ci/sources.lock.json").read_text())
        sources = {source["name"]: source for source in lock["sources"]}
        for package in lock["packages"].values():
            for name in package["sources"]:
                self.assertRegex(sources[name]["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(sources[name]["url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
