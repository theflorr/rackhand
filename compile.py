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
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    if cargo_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{cargo_bin}:{os.environ.get('PATH', '')}"
    cargo = shutil.which("cargo")
    rustc = shutil.which("rustc")
    if not cargo or not rustc:
        print("[ERROR] Rust and Cargo are required to compile with PyApp.")
        print("Please install Rust from https://rustup.rs/ and ensure 'cargo' is on your PATH.")
        sys.exit(1)
    return cargo, rustc


def clean_artifacts(base_dir: Path) -> None:
    # 1. Clean local project build artifacts
    for name in ["dist", "build", "rackhand.egg-info", ".pytest_cache"]:
        p = base_dir / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)

    exe_file = base_dir / "rackhand.exe"
    if exe_file.exists():
        exe_file.unlink(missing_ok=True)

    for pycache in base_dir.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)

    # 2. Clean PyApp target installation caches
    cache_dirs: list[Path] = []
    if sys.platform == "win32" and "LOCALAPPDATA" in os.environ:
        local_app_data = Path(os.environ["LOCALAPPDATA"])
        cache_dirs.extend([
            local_app_data / "rackhand",
            local_app_data / "pyapp" / "rackhand",
        ])
    else:
        cache_dirs.extend([
            Path.home() / ".local" / "share" / "rackhand",
            Path.home() / ".local" / "share" / "pyapp" / "rackhand",
        ])

    for c in cache_dirs:
        if c.exists():
            shutil.rmtree(c, ignore_errors=True)

    print("      ✓ Cleaned dist, build, egg-info, __pycache__, and PyApp target caches.")


def build_wheel(base_dir: Path) -> Path:
    print("      Building project wheel via pyproject.toml...")
    dist_dir = base_dir / "dist"
    lib_dir = base_dir / "build" / "lib"
    egg_info = base_dir / "rackhand.egg-info"

    # Always clear stale build artifacts before compiling wheel
    if dist_dir.exists():
        shutil.rmtree(dist_dir, ignore_errors=True)
    if lib_dir.exists():
        shutil.rmtree(lib_dir, ignore_errors=True)
    if egg_info.exists():
        shutil.rmtree(egg_info, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    # Inject dynamic build timestamp version into pyproject.toml
    pyproject_toml = base_dir / "pyproject.toml"
    original_toml = pyproject_toml.read_text(encoding="utf-8")
    import re
    base_version = "1.2.0"
    m = re.search(r'version\s*=\s*"([^"]+)"', original_toml)
    if m:
        base_version = m.group(1).split(".post")[0].split("+")[0]

    build_version = f"{base_version}.post{int(time.time())}"
    modified_toml = re.sub(r'version\s*=\s*"[^"]+"', f'version = "{build_version}"', original_toml, count=1)
    pyproject_toml.write_text(modified_toml, encoding="utf-8")

    try:
        # Prefer pip wheel as pip is universally available in all Python installations
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            str(dist_dir),
            ".",
        ]
        res = subprocess.run(cmd, cwd=str(base_dir), capture_output=True, text=True)
        if res.returncode != 0:
            # Fallback to python -m build if pip wheel failed
            cmd_build = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist_dir)]
            res = subprocess.run(cmd_build, cwd=str(base_dir), capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[ERROR] Wheel build failed:\n{res.stderr or res.stdout}")
                sys.exit(res.returncode)
    finally:
        # Always restore clean pyproject.toml so working tree stays pristine
        pyproject_toml.write_text(original_toml, encoding="utf-8")

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

    # Remove stale or version-incompatible Cargo.lock so cargo regenerates it cleanly
    lock_file = pyapp_dir / "Cargo.lock"
    if lock_file.exists():
        lock_file.unlink(missing_ok=True)

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
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winres::WindowsResource::new();
        res.set_icon("rackhand.ico");
        res.set_windres_path("x86_64-w64-mingw32-windres");
        res.set_ar_path("x86_64-w64-mingw32-ar");
        if let Err(e) = res.compile() {
            eprintln!("Failed to compile Windows resource: {:?}", e);
        }
        if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("gnu") {
            if let Ok(out_dir) = std::env::var("OUT_DIR") {
                let res_o = std::path::Path::new(&out_dir).join("resource.o");
                println!("cargo:rustc-link-arg={}", res_o.display());
            }
        }
    }
"""
        # Insert before the closing brace of main()
        idx = build_content.rfind("}")
        if idx != -1:
            build_content = build_content[:idx] + icon_hook + build_content[idx:]
            build_rs.write_text(build_content, encoding="utf-8")
            print("      ✓ Injected Windows icon embedding hook into build.rs.")

    # Patch distribution.rs to auto-update cache and clean old version directories
    dist_rs = pyapp_dir / "src" / "distribution.rs"
    dist_content = dist_rs.read_text(encoding="utf-8")
    is_crlf = "\r\n" in dist_content
    dist_norm = dist_content.replace("\r\n", "\n")
    if ".project_hash" not in dist_norm:
        target = """    if !app::install_dir().is_dir() {
        materialize()?;

        if !app::skip_install() {
            install_project()?;
        }
    }"""
        replacement = """    // Clean up older version directories from previous builds
    if let Some(parent) = app::install_dir().parent() {
        if let Ok(entries) = fs::read_dir(parent) {
            for entry in entries.flatten() {
                if let Ok(file_type) = entry.file_type() {
                    if file_type.is_dir() && entry.file_name() != OsStr::new(&app::project_version()) {
                        let _ = fs::remove_dir_all(entry.path());
                    }
                }
            }
        }
    }

    let embedded = app::embedded_project();
    let hash_file = app::install_dir().join(".project_hash");
    let mut needs_install = false;

    if !app::install_dir().is_dir() {
        materialize()?;
        needs_install = true;
    } else if !embedded.is_empty() {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut hasher = DefaultHasher::new();
        embedded.hash(&mut hasher);
        let current_hash = format!("{:016x}", hasher.finish());
        let installed_hash = fs::read_to_string(&hash_file).unwrap_or_default();
        if current_hash != installed_hash {
            if fs::remove_dir_all(app::install_dir()).is_ok() {
                materialize()?;
            }
            needs_install = true;
        }
    }

    if needs_install && !app::skip_install() {
        install_project()?;
        if !embedded.is_empty() {
            use std::collections::hash_map::DefaultHasher;
            use std::hash::{Hash, Hasher};
            let mut hasher = DefaultHasher::new();
            embedded.hash(&mut hasher);
            let current_hash = format!("{:016x}", hasher.finish());
            let _ = fs::write(&hash_file, current_hash);
        }
    }"""
        if target in dist_norm:
            dist_norm = dist_norm.replace(target, replacement, 1)
            final_content = dist_norm.replace("\n", "\r\n") if is_crlf else dist_norm
            dist_rs.write_text(final_content, encoding="utf-8")
            print("      ✓ Injected embedded project cache auto-invalidation into distribution.rs.")
    elif "Clean up older version directories" not in dist_norm:
        cleanup_hook = """    // Clean up older version directories from previous builds
    if let Some(parent) = app::install_dir().parent() {
        if let Ok(entries) = fs::read_dir(parent) {
            for entry in entries.flatten() {
                if let Ok(file_type) = entry.file_type() {
                    if file_type.is_dir() && entry.file_name() != OsStr::new(&app::project_version()) {
                        let _ = fs::remove_dir_all(entry.path());
                    }
                }
            }
        }
    }

    let embedded = app::embedded_project();"""
        dist_norm = dist_norm.replace("    let embedded = app::embedded_project();", cleanup_hook, 1)
        final_content = dist_norm.replace("\n", "\r\n") if is_crlf else dist_norm
        dist_rs.write_text(final_content, encoding="utf-8")
        print("      ✓ Injected version directory auto-cleanup hook into distribution.rs.")


def compile_pyapp(pyapp_dir: Path, wheel_path: Path, embed_runtime: bool, output_exe: Path) -> None:
    # Ensure embedded project file in PyApp source tree is updated with the new wheel
    embed_target = pyapp_dir / "src" / "embed" / "project"
    embed_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel_path, embed_target)

    # Touch main.rs to ensure Cargo recompiles the binary with the updated embedded asset
    main_rs = pyapp_dir / "src" / "main.rs"
    if main_rs.exists():
        main_rs.touch()

    # Read project version from built wheel filename: rackhand-<version>-py3-none-any.whl
    project_version = wheel_path.stem.split("-")[1]

    env = os.environ.copy()
    env["PYAPP_PROJECT_NAME"] = "rackhand"
    env["PYAPP_PROJECT_VERSION"] = project_version
    env["PYAPP_PROJECT_PATH"] = str(wheel_path)
    env["PYAPP_EXEC_MODULE"] = "app"
    env["PYAPP_PYTHON_VERSION"] = "3.12"
    env["PYAPP_UV_ENABLED"] = "1"
    env["PYAPP_PIP_EXTRA_ARGS"] = "--force-reinstall"
    
    if embed_runtime:
        env["PYAPP_DISTRIBUTION_EMBED"] = "1"
        print("      Mode: Fully embedded (offline, single-file with Python 3.12 runtime)")
    else:
        env["PYAPP_DISTRIBUTION_EMBED"] = "0"
        print("      Mode: Lightweight bootstrap (~8MB, downloads Python 3.12 on first run)")

    print(f"      Target Version: {project_version}")
    print("      Invoking Cargo (Rust) release compilation...")
    start_time = time.time()

    target_args = ["--target", "x86_64-pc-windows-gnu"] if sys.platform != "win32" else []

    if target_args:
        built_exe = pyapp_dir / "target" / target_args[1] / "release" / "pyapp.exe"
    else:
        built_exe = pyapp_dir / "target" / "release" / "pyapp.exe"

    # Remove previous binary if it exists so we are guaranteed to get the newly built one
    if built_exe.exists():
        built_exe.unlink(missing_ok=True)

    cargo = shutil.which("cargo") or "cargo"
    cmd = [cargo, "build", "--release"] + target_args
    res = subprocess.run(cmd, cwd=str(pyapp_dir), env=env)
    if res.returncode != 0:
        print(f"\n[ERROR] Cargo compilation failed with exit code {res.returncode}")
        sys.exit(res.returncode)

    elapsed = time.time() - start_time
    if not built_exe.exists():
        candidates = sorted(pyapp_dir.glob("target/**/pyapp.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            built_exe = candidates[0]
        else:
            print(f"[ERROR] Expected binary not found in target directories.")
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
