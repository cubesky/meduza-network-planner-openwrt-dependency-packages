"""Link a small archive graph with the mgmtd rules before and after our patch."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = shutil.which("patch")
if not PATCH and os.name == "nt":
    git_patch = Path("C:/Program Files/Git/usr/bin/patch.exe")
    PATCH = str(git_patch) if git_patch.is_file() else None
ZIG = shutil.which("zig") if os.name == "nt" else None
CC = [ZIG, "cc", "-target", "x86_64-linux-musl"] if ZIG else [shutil.which("cc") or shutil.which("gcc")]
AR = [ZIG, "ar"] if ZIG else [shutil.which("ar")]
LLD = shutil.which("ld.lld")
if ZIG and not LLD:
    # rust-lld can link ELF objects on Windows without Zig's linker-flag parser.
    LLD = next((str(path) for path in (Path.home() / ".rustup/toolchains").glob("*/lib/rustlib/*/bin/rust-lld.exe")), None)


def archives(source):
    values = [line.split("=", 1)[1] for line in source.splitlines() if line.startswith("mgmtd_mgmtd_LDADD ")]
    return [token.removesuffix(".la") + ".a" if token.endswith(".la") else token
            for value in values for token in value.split() if token.endswith((".a", ".la"))]


@unittest.skipUnless(PATCH and CC[0] and AR[0] and (not ZIG or LLD), "Requires patch and a C compiler/archiver (or Zig and LLD on Windows)")
class FrrStaticLinkTests(unittest.TestCase):
    def test_libfrr_patch_removes_the_forced_interpreter_from_compiled_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "lib").mkdir()
            source = work / "lib/libfrr.c"
            source.write_text((ROOT / "tests/fixtures/frr-libfrr-interp.c").read_text(), newline="\n")
            def compile_object(name):
                result = subprocess.run([*CC, "-g0", "-fno-sanitize=all", "-c", str(source), "-o", name], cwd=work, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return (work / name).read_bytes()
            original = compile_object("original.o")
            self.assertIn(b".interp\0", original)
            self.assertIn(b"/lib/ld-musl-x86_64.so.1\0", original)
            applied = subprocess.run([PATCH, "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / "build/patches/frr/0002-omit-libfrr-interpreter-section.patch")], cwd=work, capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            fixed = compile_object("fixed.o")
            self.assertNotIn(b".interp\0", fixed)
            self.assertNotIn(b"/lib/ld-musl-x86_64.so.1\0", fixed)

    def test_mgmtd_patch_resolves_rip_cli_static_archive_backreferences(self):
        # Model the reported references in separate objects, as in libfrr.a.
        sources = {
            "main": "int mgmt_init(void); int main(void) { return mgmt_init(); }",
            "mgmt": "int rip_cli_init(void), ripng_cli_init(void), frr_init(void); int mgmt_init(void) { return rip_cli_init() + ripng_cli_init() + frr_init(); }",
            "frr": "int frr_init(void) { return 0; }",
            "if_rmap": "int if_rmap_init(void) { return 0; } int cli_show_if_route_map(void) { return 0; }",
            "distribute": "int group_distribute_list_ipv4_cli_show(void) { return 0; } int group_distribute_list_ipv6_cli_show(void) { return 0; }",
        }
        for name, family in (("rip", "ipv4"), ("ripng", "ipv6")):
            sources[name] = (
                f"int if_rmap_init(void), cli_show_if_route_map(void), group_distribute_list_{family}_cli_show(void); "
                f"int (*{name}_callbacks[])(void) = {{cli_show_if_route_map, group_distribute_list_{family}_cli_show}}; "
                f"int {name}_cli_init(void) {{ return if_rmap_init() + {name}_callbacks[0]() + {name}_callbacks[1](); }}"
            )
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            (work / "mgmtd").mkdir()
            (work / "lib").mkdir()
            makefile = work / "mgmtd/subdir.am"
            makefile.write_text((ROOT / "tests/fixtures/frr-mgmtd-subdir.am").read_text(), newline="\n")
            original = makefile.read_text()

            def run(command):
                return subprocess.run(command, cwd=work, capture_output=True, text=True)

            for name, source in sources.items():
                (work / f"{name}.c").write_text(source)
                compiled = run([*CC, "-fno-sanitize=all", "-fno-stack-protector", "-c", f"{name}.c", "-o", f"{name}.o"])
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for archive, objects in {
                "mgmtd/libmgmtd.a": ["mgmt.o"],
                "mgmtd/libmgmt_be_nb.a": ["rip.o", "ripng.o"],
                "lib/libfrr.a": ["frr.o", "if_rmap.o", "distribute.o"],
            }.items():
                result = run([*AR, "rcs", archive, *objects])
                self.assertEqual(result.returncode, 0, result.stderr)

            def link(source):
                # LLD normally accepts backward archive references; make it
                # enforce GNU ld's archive ordering for the Windows/Zig check.
                if ZIG:
                    linker = [LLD, "-flavor", "gnu"] if Path(LLD).stem == "rust-lld" else [LLD]
                    return run([*linker, "--warn-backrefs", "--fatal-warnings", "-static", "-e", "main", "main.o", *archives(source), "-o", "mgmtd-probe"])
                return run([*CC, "-static", "-nostdlib", "-Wl,-e,main", "main.o", *archives(source), "-o", "mgmtd-probe"])

            broken = link(original)
            self.assertNotEqual(broken.returncode, 0)
            # LLD reports one backward reference per archive member; GNU ld
            # reports every unresolved symbol. Both must expose these two.
            for symbol in ("if_rmap_init", "group_distribute_list_ipv4_cli_show"):
                self.assertIn(symbol, broken.stderr)

            applied = run([PATCH, "--batch", "--fuzz=0", "-p1", "-i", str(ROOT / "build/patches/frr/0001-mgmtd-static-link-order.patch")])
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            fixed = link(makefile.read_text())
            self.assertEqual(fixed.returncode, 0, fixed.stderr)


if __name__ == "__main__":
    unittest.main()
