import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build")
matrix = load("matrix")
CONFIG = json.loads((ROOT / "ci" / "targets.json").read_text())


class MatrixTests(unittest.TestCase):
    def test_full_matrix_has_unique_bundles(self):
        rows = matrix.build_matrix(CONFIG)["include"]
        self.assertEqual(len(rows), 36)
        self.assertEqual(len({row["bundle"] for row in rows}), 36)
        self.assertEqual({row["package"] for row in rows}, {"tinc", "frr"})

    def test_aliases_filter_and_deduplicate(self):
        rows = matrix.build_matrix(CONFIG, "frr", "apk", "x64,linux-amd64,arm-v7,arm64")["include"]
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["arch"] for row in rows},
                         {"x86_64", "arm_cortex-a7_neon-vfpv4", "aarch64_generic"})
        self.assertTrue(all(row["format"] == "apk" and row["package"] == "frr" for row in rows))

    def test_invalid_input_is_rejected(self):
        for kwargs in ({"architectures": "all,x64"}, {"architectures": ""},
                       {"architectures": "x64,$(echo bad)"}, {"package": "other"},
                       {"package_format": "deb"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                matrix.build_matrix(CONFIG, **kwargs)


class SdkTests(unittest.TestCase):
    def test_both_base_and_packages_feeds_are_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            base_revision, packages_revision = "a" * 40, "b" * 40
            (sdk / "feeds.conf.default").write_text(
                f"src-git --root=package base https://git.openwrt.org/openwrt/openwrt.git^{base_revision}\n"
            )
            revisions = build.write_feeds(sdk,
                f"src-git packages https://git.openwrt.org/feed/packages.git^{packages_revision}\n")
            self.assertEqual(revisions, {"base": base_revision, "packages": packages_revision})
            content = (sdk / "feeds.conf").read_text()
            self.assertIn(f"--root=package base https://github.com/openwrt/openwrt.git^{base_revision}", content)
            self.assertIn(f"packages https://github.com/openwrt/packages.git^{packages_revision}", content)

    def test_unpinned_feed_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            (sdk / "feeds.conf.default").write_text("src-git --root=package base https://example.com/repo;main\n")
            with self.assertRaisesRegex(ValueError, "pinned base"):
                build.write_feeds(sdk, f"src-git packages https://example.com/packages.git^{'b' * 40}\n")

    def test_sdk_selection_excludes_imagebuilder_toolchain_and_wrong_host(self):
        for suffix in ("xz", "zst"):
            expected = f"openwrt-sdk-25.12.6-x86-64_gcc-14.3.0_musl.Linux-x86_64.tar.{suffix}"
            checksums = {expected: "a", expected.replace("-sdk-", "-imagebuilder-"): "b",
                         expected.replace("x86_64.tar", "aarch64.tar"): "c"}
            self.assertEqual(build.sdk_name(checksums, "25.12.6", "x86/64"), expected)
        with self.assertRaises(ValueError):
            build.sdk_name({}, "25.12.6", "x86/64")

    def test_sha256_formats_and_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = directory / "payload"
            payload.write_bytes(b"valid SDK")
            digest = hashlib.sha256(b"valid SDK").hexdigest()
            manifest = directory / "sha256sums"
            manifest.write_text(f"{digest} *./payload\n{digest}  second\n")
            self.assertEqual(build.read_checksums(manifest), {"payload": digest, "second": digest})
            build.verify(payload, digest)
            payload.write_bytes(b"corrupt SDK")
            with self.assertRaises(ValueError):
                build.verify(payload, digest)

    def test_frr_recipe_selects_daemons_and_explicit_packages(self):
        recipe = "\n".join(f"$(eval $(call BuildPackage,{name}))" for name in
                           ("frr", "frr-zebra", "frr-watchfrr", "frr-pythontools"))
        recipe += "\n$(eval $(call BuildDaemon,bgpd,))\n$(eval $(call BuildDaemon,ospfd,))"
        self.assertEqual(build.package_names(recipe, "frr"),
                         ["frr", "frr-bgpd", "frr-ospfd", "frr-pythontools", "frr-watchfrr", "frr-zebra"])
        with self.assertRaises(ValueError):
            build.package_names("$(call BuildPackage,frr)", "frr")

    def test_configuration_rejects_wrong_package_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            recipe = sdk / "feeds/packages/net/tinc/Makefile"
            recipe.parent.mkdir(parents=True)
            recipe.write_text("$(eval $(call BuildPackage,tinc))")
            (sdk / ".config").write_text("CONFIG_USE_APK=y\nCONFIG_ALL=y\n")
            with patch.object(build, "run"), self.assertRaises(ValueError):
                build.configure(sdk, "tinc", "ipk")


class OutputTests(unittest.TestCase):
    def test_collect_separates_dependencies_preserves_native_names_and_excludes_kmods(self):
        for fmt, names in (
            ("ipk", ["tinc_1.1pre18-r4_x86_64.ipk", "libopenssl3_3.0-r1_x86_64.ipk", "kmod-tun_6.6-r1_x86_64.ipk"]),
            ("apk", ["tinc-1.1_pre18-r4.apk", "libopenssl3-3.5.0-r1.apk", "kmod-tun-6.12-r1.apk"]),
        ):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                sdk = Path(temporary) / "sdk"
                binary = sdk / "bin/packages/arch/packages"
                binary.mkdir(parents=True)
                for name in names:
                    (binary / name).write_bytes(name.encode())
                output = Path(temporary) / "output"
                payloads = build.collect(sdk, output, fmt, ["tinc"])
                self.assertEqual(set(payloads), {f"packages/{names[0]}", f"dependencies/{names[1]}"})
                self.assertEqual((output / "packages" / names[0]).read_bytes(), names[0].encode())

    def test_missing_daemon_causes_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary) / "sdk"
            (sdk / "bin").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "frr-bgpd"):
                build.collect(sdk, Path(temporary) / "output", "apk", ["frr-bgpd"])

    def test_other_application_is_never_bundled(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary) / "sdk"
            binary = sdk / "bin/packages"
            binary.mkdir(parents=True)
            (binary / "frr-bgpd-10.4.1-r1.apk").write_bytes(b"wrong package")
            with self.assertRaisesRegex(ValueError, "Unselected"):
                build.collect(sdk, Path(temporary) / "output", "apk", ["tinc"])


if __name__ == "__main__":
    unittest.main()
