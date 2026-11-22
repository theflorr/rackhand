#!/usr/bin/env python3
"""
Rackhand Compiler (PyApp + Rust Standalone)
===========================================
Packages Rackhand into a native, standalone Windows executable (.exe)
using PyApp (Rust) with embedded Python 3.12, Astral uv, and rackhand.ico.

Advantages over PyInstaller:
- True native Rust executable (no slow temp extraction on every run).
- Powered by python-build-standalone & Astral uv for blazing-fast startup.
- Clean Windows binary with embedded rackhand.ico.
- Avoids generic unpacker antivirus false-positives.

Usage:
    python compile.py               (Full build with embedded Python 3.12 runtime)
    python compile.py --no-embed    (Lightweight ~8MB binary, downloads runtime on first run)
    python compile.py --clean       (Clean build cache before compiling)
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path


PYAPP_RELEASE_ZIP_URL = "https://github.com/ofek/pyapp/releases/latest/download/source.zip"


def print_step(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")
    print("-" * 50)


def check_prerequisites() -> tuple[str, str]:
    cargo = shutil.which("cargo")
    rustc = shutil.which("rustc")
    if not cargo or not rustc:
        print("[ERROR] Rust and Cargo are required to compile with PyApp.")
        print("Please install Rust from https://rustup.rs/ and ensure 'cargo' is on your PATH.")
        sys.exit(1)
    return cargo, rustc


def clean_artifacts(base_dir: Path) -> None:
    for name in ["dist", "rackhand.egg-info"]:
        p = base_dir / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    pyapp_dir = base_dir / "build" / "pyapp"
    if pyapp_dir.exists():
        shutil.rmtree(pyapp_dir, ignore_errors=True)
    print("      Cleaned dist, egg-info, and build/pyapp directories.")


def build_wheel(base_dir: Path) -> Path:
    print("      Building project wheel via pyproject.toml...")
    dist_dir = base_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist_dir)]
    res = subprocess.run(cmd, cwd=str(base_dir), capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[ERROR] Wheel build failed:\n{res.stderr or res.stdout}")
        sys.exit(res.returncode)

    wheels = list(dist_dir.glob("rackhand-*.whl"))
    if not wheels:
        print("[ERROR] No wheel found in dist/ after build!")
        sys.exit(1)

    latest_wheel = max(wheels, key=lambda w: w.stat().st_mtime)
    print(f"      ✓ Built wheel: {latest_wheel.name} ({latest_wheel.stat().st_size:,} bytes)")
    return latest_wheel.resolve()


def setup_pyapp_source(pyapp_dir: Path, icon_path: Path) -> None:
    if not (pyapp_dir / "Cargo.toml").exists():
        print(f"      Downloading PyApp release source...")
        pyapp_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(PYAPP_RELEASE_ZIP_URL, headers={"User-Agent": "Rackhand-Compiler"})
        with urllib.request.urlopen(req) as resp:
            zip_bytes = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            # zip has root folder like 'pyapp-v0.31.0/...'
            root_prefix = z.namelist()[0].split("/")[0] + "/"
            for member in z.infolist():
                if not member.filename.startswith(root_prefix) or member.is_dir():
                    continue
                rel_path = member.filename[len(root_prefix):]
                target_path = pyapp_dir / rel_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(target_path, "wb") as dst:
                    dst.write(src.read())

        print("      ✓ PyApp source prepared.")
    else:
        print("      ✓ Using existing PyApp source cache.")

    # Copy icon into pyapp root
    shutil.copy2(icon_path, pyapp_dir / "rackhand.ico")

    # Patch Cargo.toml to add winres for Windows icon embedding
    cargo_toml = pyapp_dir / "Cargo.toml"
    content = cargo_toml.read_text(encoding="utf-8")
    if "winres" not in content:
        if "[build-dependencies]" not in content:
            content += '\n[build-dependencies]\nwinres = "0.1"\n'
        else:
            content = content.replace("[build-dependencies]", '[build-dependencies]\nwinres = "0.1"')
        cargo_toml.write_text(content, encoding="utf-8")
        print("      ✓ Added winres dependency to PyApp Cargo.toml.")

    # Patch build.rs to compile rackhand.ico resource
    build_rs = pyapp_dir / "build.rs"
    build_content = build_rs.read_text(encoding="utf-8")
    if 'set_icon("rackhand.ico")' not in build_content:
        icon_hook = """
    #[cfg(target_os = "windows")]
    {
        let mut res = winres::WindowsResource::new();
        res.set_icon("rackhand.ico");
        let _ = res.compile();
    }
"""
        # Insert before the closing brace of main()
        idx = build_content.rfind("}")
        if idx != -1:
            build_content = build_content[:idx] + icon_hook + build_content[idx:]
            build_rs.write_text(build_content, encoding="utf-8")
            print("      ✓ Injected Windows icon embedding hook into build.rs.")


def compile_pyapp(pyapp_dir: Path, wheel_path: Path, embed_runtime: bool, output_exe: Path) -> None:
    env = os.environ.copy()
    env["PYAPP_PROJECT_NAME"] = "rackhand"
    env["PYAPP_PROJECT_VERSION"] = "0.1.0"
    env["PYAPP_PROJECT_PATH"] = str(wheel_path)
    env["PYAPP_EXEC_MODULE"] = "app"
    env["PYAPP_PYTHON_VERSION"] = "3.12"
    env["PYAPP_UV_ENABLED"] = "1"
    
    if embed_runtime:
        env["PYAPP_DISTRIBUTION_EMBED"] = "1"
        print("      Mode: Fully embedded (offline, single-file with Python 3.12 runtime)")
    else:
        env["PYAPP_DISTRIBUTION_EMBED"] = "0"
        print("      Mode: Lightweight bootstrap (~8MB, downloads Python 3.12 on first run)")

    print("      Invoking Cargo (Rust) release compilation...")
    start_time = time.time()

    cmd = ["cargo", "build", "--release"]
    res = subprocess.run(cmd, cwd=str(pyapp_dir), env=env)
    if res.returncode != 0:
        print(f"\n[ERROR] Cargo compilation failed with exit code {res.returncode}")
        sys.exit(res.returncode)

    elapsed = time.time() - start_time
    built_exe = pyapp_dir / "target" / "release" / "pyapp.exe"
    if not built_exe.exists():
        print(f"[ERROR] Expected binary not found at {built_exe}")
        sys.exit(1)

    shutil.copy2(built_exe, output_exe)
    print(f"      ✓ Cargo build finished in {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rackhand Compiler (PyApp + Rust Standalone)")
    parser.add_argument("--clean", action="store_true", help="Clean build directories before compiling")
    parser.add_argument("--no-embed", action="store_true", help="Do not embed Python runtime into .exe (reduces file size)")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    icon_path = base_dir / "rackhand.ico"
    output_exe = base_dir / "rackhand.exe"
    pyapp_dir = base_dir / "build" / "pyapp"

    print("=" * 64)
    print(" RACKHAND COMPILER (PyApp / Rust Standalone)")
    print("=" * 64)
    print(f"Base Directory : {base_dir}")
    print(f"Icon File      : {icon_path}")
    print(f"Target Output  : {output_exe}")
    print("=" * 64)

    if not icon_path.exists():
        print(f"[ERROR] Icon file not found: {icon_path}")
        sys.exit(1)

    cargo, rustc = check_prerequisites()
    print(f"Rust Toolchain : cargo ({cargo}), rustc ({rustc})")

    if args.clean:
        print_step("1/4", "Cleaning previous build artifacts")
        clean_artifacts(base_dir)
    else:
        print_step("1/4", "Preparing environment (clean skipped)")

    print_step("2/4", "Building project wheel package")
    wheel_path = build_wheel(base_dir)

    print_step("3/4", "Setting up PyApp Rust wrapper and icon")
    setup_pyapp_source(pyapp_dir, icon_path)

    print_step("4/4", "Compiling native Windows executable")
    compile_pyapp(pyapp_dir, wheel_path, embed_runtime=not args.no_embed, output_exe=output_exe)

    exe_size_mb = output_exe.stat().st_size / (1024 * 1024)
    print("\n" + "=" * 64)
    print(" [SUCCESS] Compilation Complete!")
    print(f" Output Executable : {output_exe} ({exe_size_mb:.1f} MB)")
    print("=" * 64)


if __name__ == "__main__":
    main()
