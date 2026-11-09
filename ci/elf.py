"""Check ELF architecture and dependencies against the OpenWrt runtime contract."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def validate_elf(header, program, dynamic, target):
    machines = re.findall(r"^\s*Machine:\s*(.+)$", header, re.M)
    if machines != [target["machine"]]:
        raise ValueError(f"Wrong ELF machine: {machines}; expected {target['machine']}")
    interpreters = re.findall(r"Requesting program interpreter:\s*([^\]]+)\]", program)
    if "INTERP" in program and len(interpreters) != 1:
        raise ValueError("Cannot identify the ELF interpreter")
    interpreter = interpreters[0] if interpreters else None
    if interpreter is not None and interpreter != target["interpreter"]:
        raise ValueError(f"Unsupported OpenWrt interpreter: {interpreter}")
    needed = sorted(set(re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", dynamic)))
    # Other libraries are embedded in these builds. Never silently publish an
    # Alpine SONAME (e.g. libc.musl-*.so.1) or an unpackaged third-party .so.
    unsupported = set(needed) - {"libc.so"}
    if unsupported:
        raise ValueError(f"Unpackaged OpenWrt shared libraries: {sorted(unsupported)}")
    if interpreter is not None and not needed:
        raise ValueError("Mixed static/PIE ELF: musl interpreter present but no shared libc dependency")
    if needed and interpreter is None:
        raise ValueError("Shared libc dependency requires the OpenWrt musl interpreter")
    static = interpreter is None and not needed
    return {"static": static, "interpreter": interpreter, "needed": needed,
            "systemPackages": [] if static else ["libc"]}


def inspect_binary(binary, target):
    reports = [subprocess.check_output(["readelf", option, str(binary)], text=True)
               for option in ("-h", "-l", "-d")]
    try:
        return validate_elf(*reports, target)
    except ValueError as error:
        raise ValueError(f"{binary}: {error}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("targets.json").read_text())
    target = next(row for row in config["targets"] if row["platform"] == args.platform)
    binaries = sorted(args.bin_dir.iterdir())
    if not binaries:
        parser.error("No binaries to validate")
    runtime = {binary.name: inspect_binary(binary, target) for binary in binaries}
    args.output.write_text(json.dumps(runtime, indent=2) + "\n")
    print(f"OpenWrt ELF checks passed for {len(runtime)} binaries on {args.platform}", flush=True)


if __name__ == "__main__":
    main()
