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
        os.environ["PATH"] = f"{cargo_bin}{os.pathsep}{os.environ.get('PATH', '')}"
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
    base_version = "1.3.7"
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
            pyapp_root = pyapp_dir.resolve()
            for member in z.infolist():
                if not member.filename.startswith(root_prefix) or member.is_dir():
                    continue
                rel_path = member.filename[len(root_prefix):]
                target_path = (pyapp_dir / rel_path).resolve()
                if not target_path.is_relative_to(pyapp_root):
                    continue
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

    # Copy icon into pyapp root and generate 64x64 RGBA bytes for tao WindowBuilder
    shutil.copy2(icon_path, pyapp_dir / "rackhand.ico")
    try:
        from PIL import Image
        ico_img = Image.open(icon_path).convert("RGBA").resize((64, 64), Image.LANCZOS)
        (pyapp_dir / "src" / "icon_rgba.bin").write_bytes(ico_img.tobytes())
    except Exception:
        (pyapp_dir / "src" / "icon_rgba.bin").write_bytes(b"\x00" * (64 * 64 * 4))

    # Copy bundled xterm.js assets for offline WebView2 terminal host
    base_dir = Path(__file__).resolve().parent
    xterm_src = base_dir / "assets" / "xterm"
    xterm_dst = pyapp_dir / "src" / "xterm"
    xterm_dst.mkdir(parents=True, exist_ok=True)
    for fname in ("xterm.min.js", "xterm.css", "addon-fit.min.js"):
        src_f = xterm_src / fname
        if src_f.is_file():
            shutil.copy2(src_f, xterm_dst / fname)

    # Copy x64 WebView2Loader.dll from Cargo registry and vendor/patch webview2-com-sys
    # so rackhand.exe has NO static PE import of WebView2Loader.dll (which would fail before main() runs).
    cargo_home = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo")))
    wv2_crate_dirs = sorted(cargo_home.glob("registry/src/**/webview2-com-sys-*"))
    wv2_dll_dst = pyapp_dir / "src" / "WebView2Loader.dll"
    if wv2_crate_dirs:
        wv2_src_dir = wv2_crate_dirs[-1]
        src_dll = wv2_src_dir / "x64" / "WebView2Loader.dll"
        if src_dll.is_file():
            shutil.copy2(src_dll, wv2_dll_dst)
            print(f"      ✓ Bundled WebView2Loader.dll from {wv2_src_dir.name}.")

        vendor_wv2 = pyapp_dir / "vendor" / "webview2-com-sys"
        if not vendor_wv2.exists():
            vendor_wv2.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(wv2_src_dir, vendor_wv2)
        ms_rs = vendor_wv2 / "src" / "Microsoft.rs"
        if ms_rs.is_file():
            ms_text = ms_rs.read_text(encoding="utf-8")
            if 'link(name = "WebView2Loader.dll")' in ms_text:
                # Replace static DLL imports with dynamic GetModuleHandleA + GetProcAddress resolution
                dynamic_loader_block = '''            #[inline]
            unsafe fn _wv2_proc(name: &[u8]) -> *const core::ffi::c_void {
                #[link(name = "kernel32")]
                extern "system" {
                    fn GetModuleHandleA(lpModuleName: *const u8) -> isize;
                    fn GetProcAddress(hModule: isize, lpProcName: *const u8) -> *const core::ffi::c_void;
                }
                let hmod = GetModuleHandleA(b"WebView2Loader.dll\\0".as_ptr());
                if hmod == 0 {
                    return core::ptr::null();
                }
                GetProcAddress(hmod, name.as_ptr())
            }
            #[inline]
            pub unsafe fn CompareBrowserVersions<P0, P1>(
                version1: P0,
                version2: P1,
                result: *mut i32,
            ) -> windows_core::Result<()>
            where
                P0: windows_core::Param<windows_core::PCWSTR>,
                P1: windows_core::Param<windows_core::PCWSTR>,
            {
                let fp = _wv2_proc(b"CompareBrowserVersions\\0");
                if fp.is_null() {
                    return windows_core::HRESULT(-2147467259i32).ok();
                }
                let func: unsafe extern "system" fn(windows_core::PCWSTR, windows_core::PCWSTR, *mut i32) -> windows_core::HRESULT = core::mem::transmute(fp);
                func(version1.param().abi(), version2.param().abi(), result).ok()
            }
            #[inline]
            pub unsafe fn CreateCoreWebView2Environment<P0>(
                environmentcreatedhandler: P0,
            ) -> windows_core::Result<()>
            where
                P0: windows_core::Param<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>,
            {
                let fp = _wv2_proc(b"CreateCoreWebView2Environment\\0");
                if fp.is_null() {
                    return windows_core::HRESULT(-2147467259i32).ok();
                }
                let func: unsafe extern "system" fn(*mut core::ffi::c_void) -> windows_core::HRESULT = core::mem::transmute(fp);
                func(environmentcreatedhandler.param().abi()).ok()
            }
            #[inline]
            pub unsafe fn CreateCoreWebView2EnvironmentWithOptions<P0, P1, P2, P3>(
                browserexecutablefolder: P0,
                userdatafolder: P1,
                environmentoptions: P2,
                environmentcreatedhandler: P3,
            ) -> windows_core::Result<()>
            where
                P0: windows_core::Param<windows_core::PCWSTR>,
                P1: windows_core::Param<windows_core::PCWSTR>,
                P2: windows_core::Param<ICoreWebView2EnvironmentOptions>,
                P3: windows_core::Param<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>,
            {
                let fp = _wv2_proc(b"CreateCoreWebView2EnvironmentWithOptions\\0");
                if fp.is_null() {
                    return windows_core::HRESULT(-2147467259i32).ok();
                }
                let func: unsafe extern "system" fn(windows_core::PCWSTR, windows_core::PCWSTR, *mut core::ffi::c_void, *mut core::ffi::c_void) -> windows_core::HRESULT = core::mem::transmute(fp);
                func(
                    browserexecutablefolder.param().abi(),
                    userdatafolder.param().abi(),
                    environmentoptions.param().abi(),
                    environmentcreatedhandler.param().abi(),
                ).ok()
            }
            #[inline]
            pub unsafe fn GetAvailableCoreWebView2BrowserVersionString<P0>(
                browserexecutablefolder: P0,
                versioninfo: *mut windows_core::PWSTR,
            ) -> windows_core::Result<()>
            where
                P0: windows_core::Param<windows_core::PCWSTR>,
            {
                let fp = _wv2_proc(b"GetAvailableCoreWebView2BrowserVersionString\\0");
                if fp.is_null() {
                    return windows_core::HRESULT(-2147467259i32).ok();
                }
                let func: unsafe extern "system" fn(windows_core::PCWSTR, *mut windows_core::PWSTR) -> windows_core::HRESULT = core::mem::transmute(fp);
                func(browserexecutablefolder.param().abi(), versioninfo).ok()
            }
            #[inline]
            pub unsafe fn GetAvailableCoreWebView2BrowserVersionStringWithOptions<P0, P1>(
                browserexecutablefolder: P0,
                environmentoptions: P1,
                versioninfo: *mut windows_core::PWSTR,
            ) -> windows_core::Result<()>
            where
                P0: windows_core::Param<windows_core::PCWSTR>,
                P1: windows_core::Param<ICoreWebView2EnvironmentOptions>,
            {
                let fp = _wv2_proc(b"GetAvailableCoreWebView2BrowserVersionStringWithOptions\\0");
                if fp.is_null() {
                    return windows_core::HRESULT(-2147467259i32).ok();
                }
                let func: unsafe extern "system" fn(windows_core::PCWSTR, *mut core::ffi::c_void, *mut windows_core::PWSTR) -> windows_core::HRESULT = core::mem::transmute(fp);
                func(
                    browserexecutablefolder.param().abi(),
                    environmentoptions.param().abi(),
                    versioninfo,
                ).ok()
            }'''
                ms_norm = ms_text.replace("\r\n", "\n")
                start_marker = "            #[inline]\n            pub unsafe fn CompareBrowserVersions<P0, P1>("
                end_marker = "            windows_core::imp::define_interface!("
                s_idx = ms_norm.find(start_marker)
                e_idx = ms_norm.find(end_marker)
                if s_idx != -1 and e_idx != -1:
                    ms_norm = ms_norm[:s_idx] + dynamic_loader_block + "\n" + ms_norm[e_idx:]
                    ms_rs.write_text(ms_norm, encoding="utf-8")
                    print("      ✓ Patched webview2-com-sys to resolve WebView2Loader.dll dynamically.")

    # Patch Cargo.toml to add winres and Windows WebView2/ConPTY GUI host deps
    cargo_toml = pyapp_dir / "Cargo.toml"
    content = cargo_toml.read_text(encoding="utf-8")
    if "winres" not in content:
        if "[build-dependencies]" not in content:
            content += '\n[build-dependencies]\nwinres = "0.1"\n'
        else:
            content = content.replace("[build-dependencies]", '[build-dependencies]\nwinres = "0.1"')
    if "[patch.crates-io]" in content:
        content = content.split("[patch.crates-io]")[0].rstrip() + "\n"
    elif "[target.'cfg(windows)'.dependencies]" in content:
        content = content.split("[target.'cfg(windows)'.dependencies]")[0].rstrip() + "\n"
    content += """
[patch.crates-io]
webview2-com-sys = { path = "vendor/webview2-com-sys" }

[target.'cfg(windows)'.dependencies]
tao = "0.30"
wry = "0.47"
serde_json = "1"
windows-sys = { version = "0.59", features = [
    "Win32_Foundation",
    "Win32_Graphics_Dwm",
    "Win32_Security",
    "Win32_Storage_FileSystem",
    "Win32_System_Console",
    "Win32_System_LibraryLoader",
    "Win32_System_Pipes",
    "Win32_System_Threading",
    "Win32_UI_Shell",
] }
"""
    cargo_toml.write_text(content, encoding="utf-8")
    print("      ✓ Updated PyApp Cargo.toml with winres + WebView2/ConPTY dependencies.")

    # Write src/gui_host.rs (standalone WebView2 + xterm.js + ConPTY host + --cli attach)
    gui_host_rs = r'''#![cfg(windows)]

use std::ffi::OsStr;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::ptr::{null, null_mut};
use std::sync::{Arc, Mutex};
use std::thread;

use anyhow::Result;
use base64::Engine;
use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::platform::windows::WindowExtWindows;
use tao::window::{Icon, WindowBuilder};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, HWND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_USE_IMMERSIVE_DARK_MODE};
use windows_sys::Win32::Storage::FileSystem::{ReadFile, WriteFile};
use windows_sys::Win32::System::Console::{
    AttachConsole, ClosePseudoConsole, CreatePseudoConsole, ResizePseudoConsole,
    ATTACH_PARENT_PROCESS, COORD, HPCON,
};
use windows_sys::Win32::System::LibraryLoader::{LoadLibraryW, SetDllDirectoryW};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, InitializeProcThreadAttributeList,
    TerminateProcess, UpdateProcThreadAttribute, WaitForSingleObject,
    EXTENDED_STARTUPINFO_PRESENT, INFINITE, PROCESS_INFORMATION, STARTUPINFOEXW,
};
use windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID;
use wry::WebViewBuilder;

const PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE: usize = 0x00020016;

const ICON_RGBA: &[u8] = include_bytes!("icon_rgba.bin");
const WEBVIEW2_LOADER_DLL: &[u8] = include_bytes!("WebView2Loader.dll");
const XTERM_CSS: &str = include_str!("xterm/xterm.css");
const XTERM_JS: &str = include_str!("xterm/xterm.min.js");
const FIT_JS: &str = include_str!("xterm/addon-fit.min.js");

#[derive(Debug)]
enum GuiEvent {
    PtyOutput(String),
    PtyReady(PtyState),
    FlushPending,
    ChildExited,
}

#[derive(Debug)]
struct PtyState {
    hpc: HPCON,
    in_write: HANDLE,
    h_process: HANDLE,
}

unsafe impl Send for PtyState {}
unsafe impl Sync for PtyState {}

impl PtyState {
    fn write_input(&self, data: &[u8]) {
        if self.in_write != INVALID_HANDLE_VALUE && !data.is_empty() {
            let mut written: u32 = 0;
            unsafe {
                WriteFile(
                    self.in_write,
                    data.as_ptr(),
                    data.len() as u32,
                    &mut written,
                    null_mut(),
                );
            }
        }
    }

    fn resize(&self, cols: i16, rows: i16) {
        if self.hpc != 0 && cols > 0 && rows > 0 {
            unsafe {
                ResizePseudoConsole(self.hpc, COORD { X: cols, Y: rows });
            }
        }
    }

    fn shutdown(&self) {
        unsafe {
            if self.h_process != INVALID_HANDLE_VALUE {
                TerminateProcess(self.h_process, 0);
                CloseHandle(self.h_process);
            }
            if self.in_write != INVALID_HANDLE_VALUE {
                CloseHandle(self.in_write);
            }
            if self.hpc != 0 {
                ClosePseudoConsole(self.hpc);
            }
        }
    }
}

pub fn attach_parent_console() {
    unsafe {
        let _ = AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

pub fn set_app_user_model_id(app_id: &str) {
    let wide: Vec<u16> = OsStr::new(app_id).encode_wide().chain(Some(0)).collect();
    unsafe {
        let _ = SetCurrentProcessExplicitAppUserModelID(wide.as_ptr());
    }
}

fn prefs_path() -> std::path::PathBuf {
    if let Ok(home) = std::env::var("USERPROFILE") {
        let dir = std::path::PathBuf::from(home).join(".rackhand");
        let _ = std::fs::create_dir_all(&dir);
        return dir.join("gui_prefs.json");
    }
    std::env::temp_dir().join("rackhand-webview2").join("gui_prefs.json")
}

fn load_font_size_pref() -> u32 {
    let p = prefs_path();
    if let Ok(raw) = std::fs::read_to_string(&p) {
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) {
            if let Some(sz) = val.get("font_size").and_then(|v| v.as_u64()) {
                return (sz as u32).clamp(9, 32);
            }
        }
    }
    15
}

fn save_font_size_pref(size: u32) {
    let p = prefs_path();
    if let Some(parent) = p.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(&p, format!(r#"{{"font_size": {}}}"#, size.clamp(9, 32)));
}

fn build_html(initial_font_size: u32) -> String {
    format!(
        r#"<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
{xterm_css}
html, body {{
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #09090b;
}}
#terminal {{
  margin: 0;
  padding: 14px 10px 8px 10px;
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #09090b;
}}
.xterm-viewport {{
  overflow: hidden !important;
  scrollbar-width: none !important;
}}
.xterm-viewport::-webkit-scrollbar {{
  display: none !important;
  width: 0 !important;
  height: 0 !important;
}}
</style>
</head>
<body>
<div id="terminal"></div>
<script>
{xterm_js}
{fit_js}
(function() {{
  const term = new Terminal({{
    cursorBlink: false,
    fontFamily: '"Cascadia Mono", "Cascadia Code", "Consolas", "Courier New", monospace',
    fontSize: {initial_font_size},
    lineHeight: 1.0,
    letterSpacing: 0,
    customGlyphs: true,
    scrollback: 2000,
    allowProposedApi: true,
    theme: {{
      background: '#09090b',
      foreground: '#e4e4e7',
      cursor: '#e4e4e7',
      selectionBackground: '#27272a'
    }}
  }});
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById('terminal'));
  fitAddon.fit();
  term.focus();

  function setFontSize(nextSize) {{
    const clamped = Math.max(9, Math.min(32, nextSize));
    if (clamped !== term.options.fontSize) {{
      term.options.fontSize = clamped;
      fitAddon.fit();
      window.ipc.postMessage(JSON.stringify({{ type: 'font_size', size: clamped }}));
      window.ipc.postMessage(JSON.stringify({{ type: 'resize', cols: term.cols, rows: term.rows }}));
    }}
  }}

  term.attachCustomKeyEventHandler(function(e) {{
    if (e.type === 'keydown' && e.ctrlKey && !e.altKey) {{
      if (e.key === '=' || e.key === '+' || e.code === 'NumpadAdd') {{
        e.preventDefault();
        setFontSize((term.options.fontSize || 15) + 1);
        return false;
      }}
      if (e.key === '-' || e.key === '_' || e.code === 'NumpadSubtract') {{
        e.preventDefault();
        setFontSize((term.options.fontSize || 15) - 1);
        return false;
      }}
      if (e.key === '0' || e.code === 'Numpad0') {{
        e.preventDefault();
        setFontSize(15);
        return false;
      }}
    }}
    return true;
  }});

  document.getElementById('terminal').addEventListener('wheel', function(e) {{
    if (e.ctrlKey) {{
      e.preventDefault();
      const delta = e.deltaY < 0 ? 1 : -1;
      setFontSize((term.options.fontSize || 15) + delta);
    }}
  }}, {{ passive: false }});

  term.onData(function(data) {{
    window.ipc.postMessage(JSON.stringify({{ type: 'input', data: data }}));
  }});

  window.addEventListener('resize', function() {{
    fitAddon.fit();
    window.ipc.postMessage(JSON.stringify({{ type: 'resize', cols: term.cols, rows: term.rows }}));
  }});

  window.termWriteB64 = function(b64) {{
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {{
      bytes[i] = bin.charCodeAt(i);
    }}
    term.write(bytes);
  }};

  window.ipc.postMessage(JSON.stringify({{ type: 'ready', cols: term.cols, rows: term.rows }}));
}})();
</script>
</body>
</html>"#,
        xterm_css = XTERM_CSS,
        xterm_js = XTERM_JS,
        fit_js = FIT_JS,
        initial_font_size = initial_font_size
    )
}

fn spawn_conpty_child(
    cols: i16,
    rows: i16,
    proxy: tao::event_loop::EventLoopProxy<GuiEvent>,
) -> Result<PtyState> {
    let mut in_read: HANDLE = INVALID_HANDLE_VALUE;
    let mut in_write: HANDLE = INVALID_HANDLE_VALUE;
    let mut out_read: HANDLE = INVALID_HANDLE_VALUE;
    let mut out_write: HANDLE = INVALID_HANDLE_VALUE;

    unsafe {
        if CreatePipe(&mut in_read, &mut in_write, null(), 0) == 0 {
            anyhow::bail!("CreatePipe(in) failed");
        }
        if CreatePipe(&mut out_read, &mut out_write, null(), 0) == 0 {
            CloseHandle(in_read);
            CloseHandle(in_write);
            anyhow::bail!("CreatePipe(out) failed");
        }

        let mut hpc: HPCON = 0;
        let hr = CreatePseudoConsole(
            COORD {
                X: cols.max(40),
                Y: rows.max(12),
            },
            in_read,
            out_write,
            0,
            &mut hpc,
        );
        CloseHandle(in_read);
        CloseHandle(out_write);
        if hr != 0 {
            CloseHandle(in_write);
            CloseHandle(out_read);
            anyhow::bail!("CreatePseudoConsole failed: 0x{:x}", hr);
        }

        let mut attr_size: usize = 0;
        InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut attr_size);
        let mut attr_buf = vec![0u8; attr_size];
        let attr_list = attr_buf.as_mut_ptr() as *mut _;
        if InitializeProcThreadAttributeList(attr_list, 1, 0, &mut attr_size) == 0 {
            CloseHandle(in_write);
            CloseHandle(out_read);
            ClosePseudoConsole(hpc);
            anyhow::bail!("InitializeProcThreadAttributeList failed");
        }

        if UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE as usize,
            hpc as *const _,
            size_of::<HPCON>(),
            null_mut(),
            null_mut(),
        ) == 0
        {
            DeleteProcThreadAttributeList(attr_list);
            CloseHandle(in_write);
            CloseHandle(out_read);
            ClosePseudoConsole(hpc);
            anyhow::bail!("UpdateProcThreadAttribute failed");
        }

        let py_exe = crate::app::python_path();
        let iso = crate::app::python_isolation_flag();
        let mut parts = vec![format!("\"{}\"", py_exe.display()), iso];
        if !crate::app::exec_code().is_empty() {
            parts.push("-c".to_string());
            parts.push(format!("\"{}\"", crate::app::exec_code().replace('"', "\\\"")));
        } else if !crate::app::exec_module().is_empty() {
            parts.push("-m".to_string());
            parts.push(crate::app::exec_module());
        } else {
            let sp = crate::app::exec_script_path();
            parts.push(format!("\"{}\"", sp.display()));
        }
        let cmd_str = parts.join(" ");

        // Ensure UTF-8 & truecolor terminal environment for Textual inside ConPTY
        std::env::set_var("PYTHONUTF8", "1");
        std::env::set_var("TERM", "xterm-256color");
        std::env::set_var("COLORTERM", "truecolor");
        if crate::app::pass_location() {
            if let Ok(exe) = std::env::current_exe() {
                std::env::set_var("PYAPP", exe);
            }
        } else {
            std::env::set_var("PYAPP", "1");
        }

        let mut cmd_wide: Vec<u16> = OsStr::new(&cmd_str).encode_wide().chain(Some(0)).collect();

        let mut si: STARTUPINFOEXW = zeroed();
        si.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        si.lpAttributeList = attr_list;

        let mut pi: PROCESS_INFORMATION = zeroed();
        let ok = CreateProcessW(
            null(),
            cmd_wide.as_mut_ptr(),
            null(),
            null(),
            0,
            EXTENDED_STARTUPINFO_PRESENT,
            null(),
            null(),
            &si.StartupInfo,
            &mut pi,
        );
        DeleteProcThreadAttributeList(attr_list);
        if ok == 0 {
            CloseHandle(in_write);
            CloseHandle(out_read);
            ClosePseudoConsole(hpc);
            anyhow::bail!("CreateProcessW(--pty-child) failed");
        }
        CloseHandle(pi.hThread);

        // Background thread: read ConPTY output and forward to WebView2
        let proxy_out = proxy.clone();
        let out_handle = out_read as usize;
        thread::spawn(move || {
            let h = out_handle as HANDLE;
            let mut buf = [0u8; 8192];
            loop {
                let mut read_bytes: u32 = 0;
                let res = ReadFile(
                    h,
                    buf.as_mut_ptr(),
                    buf.len() as u32,
                    &mut read_bytes,
                    null_mut(),
                );
                if res == 0 || read_bytes == 0 {
                    break;
                }
                let b64 = base64::engine::general_purpose::STANDARD.encode(&buf[..read_bytes as usize]);
                if proxy_out.send_event(GuiEvent::PtyOutput(b64)).is_err() {
                    break;
                }
            }
            CloseHandle(h);
        });

        // Background thread: wait for child process exit
        let proxy_exit = proxy;
        let proc_handle = pi.hProcess as usize;
        thread::spawn(move || {
            let h = proc_handle as HANDLE;
            WaitForSingleObject(h, INFINITE);
            let _ = proxy_exit.send_event(GuiEvent::ChildExited);
        });

        Ok(PtyState {
            hpc,
            in_write,
            h_process: pi.hProcess,
        })
    }
}

fn ensure_webview2_loader() -> Result<()> {
    let dir = crate::app::install_dir();
    std::fs::create_dir_all(&dir)?;
    let dll_path = dir.join("WebView2Loader.dll");
    if !dll_path.is_file() || std::fs::metadata(&dll_path).map(|m| m.len() as usize).unwrap_or(0) != WEBVIEW2_LOADER_DLL.len() {
        std::fs::write(&dll_path, WEBVIEW2_LOADER_DLL)?;
    }
    let dir_wide: Vec<u16> = OsStr::new(&dir).encode_wide().chain(Some(0)).collect();
    let dll_wide: Vec<u16> = OsStr::new(&dll_path).encode_wide().chain(Some(0)).collect();
    unsafe {
        let _ = SetDllDirectoryW(dir_wide.as_ptr());
        let _ = LoadLibraryW(dll_wide.as_ptr());
    }
    Ok(())
}

pub fn run_gui_host() -> Result<()> {
    set_app_user_model_id("Rackhand.NetworkAssistant");
    ensure_webview2_loader()?;
    let wv2_data_dir = std::env::temp_dir().join("rackhand-webview2");
    let _ = std::fs::create_dir_all(&wv2_data_dir);
    std::env::set_var("WEBVIEW2_USER_DATA_FOLDER", &wv2_data_dir);

    let event_loop = EventLoopBuilder::<GuiEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();

    let icon = Icon::from_rgba(ICON_RGBA.to_vec(), 64, 64).ok();
    let window = WindowBuilder::new()
        .with_title("Rackhand")
        .with_inner_size(LogicalSize::new(1100.0, 565.0))
        .with_window_icon(icon)
        .build(&event_loop)?;

    // Enable dark title bar on Windows 10/11
    let hwnd = window.hwnd() as HWND;
    let dark: i32 = 1;
    unsafe {
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE as u32,
            &dark as *const _ as *const _,
            size_of::<i32>() as u32,
        );
    }

    let pty_slot: Arc<Mutex<Option<PtyState>>> = Arc::new(Mutex::new(None));
    let pty_for_ipc = pty_slot.clone();
    let webview_ready = Arc::new(Mutex::new(false));
    let ready_for_ipc = webview_ready.clone();
    let latest_size: Arc<Mutex<(i16, i16)>> = Arc::new(Mutex::new((120, 30)));
    let size_for_ipc = latest_size.clone();
    let pending_b64: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let pending_for_ipc = pending_b64.clone();
    let proxy_for_ipc = proxy.clone();

    let initial_font_size = load_font_size_pref();
    let html = build_html(initial_font_size);
    let webview = WebViewBuilder::new()
        .with_html(html)
        .with_ipc_handler(move |req| {
            let body = req.body();
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(body) {
                let msg_type = val.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if msg_type == "ready" {
                    let cols = val.get("cols").and_then(|v| v.as_i64()).unwrap_or(120) as i16;
                    let rows = val.get("rows").and_then(|v| v.as_i64()).unwrap_or(30) as i16;
                    *size_for_ipc.lock().unwrap() = (cols, rows);
                    *ready_for_ipc.lock().unwrap() = true;
                    let _ = proxy_for_ipc.send_event(GuiEvent::FlushPending);
                    let guard = pty_for_ipc.lock().unwrap();
                    if let Some(ref state) = *guard {
                        state.resize(cols, rows);
                    }
                } else if msg_type == "input" {
                    if let Some(data) = val.get("data").and_then(|v| v.as_str()) {
                        let guard = pty_for_ipc.lock().unwrap();
                        if let Some(ref state) = *guard {
                            state.write_input(data.as_bytes());
                        }
                    }
                } else if msg_type == "font_size" {
                    if let Some(sz) = val.get("size").and_then(|v| v.as_u64()) {
                        save_font_size_pref(sz as u32);
                    }
                } else if msg_type == "resize" {
                    let cols = val.get("cols").and_then(|v| v.as_i64()).unwrap_or(120) as i16;
                    let rows = val.get("rows").and_then(|v| v.as_i64()).unwrap_or(30) as i16;
                    *size_for_ipc.lock().unwrap() = (cols, rows);
                    let guard = pty_for_ipc.lock().unwrap();
                    if let Some(ref state) = *guard {
                        state.resize(cols, rows);
                    }
                }
            }
        })
        .build(&window)?;

    // Run ensure_ready() + ConPTY Python startup in parallel while WebView2 boots
    let proxy_boot = proxy.clone();
    thread::spawn(move || {
        let hash_file = crate::app::install_dir().join(".project_hash");
        let installed = std::fs::read_to_string(&hash_file).unwrap_or_default();
        if !crate::app::install_dir().is_dir() || installed != crate::app::project_version() {
            let msg = base64::engine::general_purpose::STANDARD
                .encode(b"\x1b[2mInitializing Rackhand environment (first run)...\r\n\x1b[0m");
            let _ = proxy_boot.send_event(GuiEvent::PtyOutput(msg));
        }
        if let Err(err) = crate::distribution::ensure_ready() {
            let msg = base64::engine::general_purpose::STANDARD
                .encode(format!("\r\n\x1b[31mStartup error: {}\x1b[0m\r\n", err).as_bytes());
            let _ = proxy_boot.send_event(GuiEvent::PtyOutput(msg));
            return;
        }
        match spawn_conpty_child(120, 30, proxy_boot.clone()) {
            Ok(state) => {
                let _ = proxy_boot.send_event(GuiEvent::PtyReady(state));
            }
            Err(err) => {
                let msg = base64::engine::general_purpose::STANDARD
                    .encode(format!("\r\n\x1b[31mFailed to start Rackhand: {}\x1b[0m\r\n", err).as_bytes());
                let _ = proxy_boot.send_event(GuiEvent::PtyOutput(msg));
            }
        }
    });

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(GuiEvent::PtyReady(state)) => {
                let (cols, rows) = *latest_size.lock().unwrap();
                if *webview_ready.lock().unwrap() {
                    state.resize(cols, rows);
                }
                *pty_slot.lock().unwrap() = Some(state);
            }
            Event::UserEvent(GuiEvent::FlushPending) => {
                let mut pend = pending_for_ipc.lock().unwrap();
                for chunk in pend.drain(..) {
                    let s = format!("window.termWriteB64 && window.termWriteB64('{}');", chunk);
                    let _ = webview.evaluate_script(&s);
                }
            }
            Event::UserEvent(GuiEvent::PtyOutput(b64)) => {
                if *webview_ready.lock().unwrap() {
                    let mut pend = pending_for_ipc.lock().unwrap();
                    if !pend.is_empty() {
                        for chunk in pend.drain(..) {
                            let s = format!("window.termWriteB64 && window.termWriteB64('{}');", chunk);
                            let _ = webview.evaluate_script(&s);
                        }
                    }
                    let script = format!("window.termWriteB64 && window.termWriteB64('{}');", b64);
                    let _ = webview.evaluate_script(&script);
                } else {
                    pending_for_ipc.lock().unwrap().push(b64);
                }
            }
            Event::UserEvent(GuiEvent::ChildExited) => {
                if let Some(state) = pty_slot.lock().unwrap().take() {
                    state.shutdown();
                }
                *control_flow = ControlFlow::Exit;
            }
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                if let Some(state) = pty_slot.lock().unwrap().take() {
                    state.shutdown();
                }
                *control_flow = ControlFlow::Exit;
            }
            _ => {}
        }
    });
}
'''
    (pyapp_dir / "src" / "gui_host.rs").write_text(gui_host_rs, encoding="utf-8")

    # Write src/main.rs with Windows GUI subsystem + --cli / --pty-child support
    main_rs = r'''#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod app;
mod commands;
mod compression;
mod distribution;
mod fs_utils;
#[cfg(windows)]
mod gui_host;
mod network;
mod process;
mod terminal;

use std::env;

use anyhow::Result;
use clap::Parser;

use crate::commands::cli::Cli;

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    let is_pty_child = args.iter().any(|a| a == "--pty-child");
    let is_cli = args.iter().any(|a| a == "--cli");
    let is_self_cmd = env::args().nth(1).as_deref() == Some(env!("PYAPP_SELF_COMMAND"));

    if is_cli || is_self_cmd {
        #[cfg(windows)]
        gui_host::attach_parent_console();
    }

    app::initialize()?;

    if is_self_cmd {
        match Cli::try_parse() {
            Ok(cli) => return cli.exec(),
            Err(err) => {
                if !err.use_stderr() {
                    err.exit();
                }
            }
        };
    };

    #[cfg(windows)]
    if !is_pty_child && !is_cli {
        return gui_host::run_gui_host();
    }

    distribution::ensure_ready()?;
    distribution::run_project()?;

    Ok(())
}
'''
    (pyapp_dir / "src" / "main.rs").write_text(main_rs, encoding="utf-8")

    # Patch src/process.rs so setup commands (pip/uv/python) run with CREATE_NO_WINDOW on Windows
    process_rs = pyapp_dir / "src" / "process.rs"
    if process_rs.is_file():
        proc_text = process_rs.read_text(encoding="utf-8")
        if "0x08000000" not in proc_text:
            proc_norm = proc_text.replace("\r\n", "\n")
            target_spawn = "    let mut child = command.spawn()?;"
            replacement_spawn = """    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn()?;"""
            if target_spawn in proc_norm:
                proc_norm = proc_norm.replace(target_spawn, replacement_spawn, 1)
                process_rs.write_text(proc_norm, encoding="utf-8")
                print("      ✓ Patched process.rs with CREATE_NO_WINDOW for silent setup.")

    print("      ✓ Configured standalone WebView2 + ConPTY GUI host and --cli mode.")

    # Patch build.rs to compile rackhand.ico and Windows PE version/product metadata
    build_rs = pyapp_dir / "build.rs"
    build_content = build_rs.read_text(encoding="utf-8")
    meta_lines = (
        '        res.set("FileDescription", "Rackhand");\n'
        '        res.set("ProductName", "Rackhand");\n'
        '        res.set("InternalName", "Rackhand");\n'
        '        res.set("OriginalFilename", "rackhand.exe");\n'
    )
    if 'set_icon("rackhand.ico")' not in build_content:
        icon_hook = f"""
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {{
        let mut res = winres::WindowsResource::new();
        res.set_icon("rackhand.ico");
{meta_lines}        if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("gnu") {{
            res.set_windres_path("x86_64-w64-mingw32-windres");
            res.set_ar_path("x86_64-w64-mingw32-ar");
        }}
        if let Err(e) = res.compile() {{
            eprintln!("Failed to compile Windows resource: {{:?}}", e);
        }}
        if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("gnu") {{
            if let Ok(out_dir) = std::env::var("OUT_DIR") {{
                let res_o = std::path::Path::new(&out_dir).join("resource.o");
                println!("cargo:rustc-link-arg={{}}", res_o.display());
            }}
        }}
    }}
"""
        idx = build_content.rfind("}")
        if idx != -1:
            build_content = build_content[:idx] + icon_hook + build_content[idx:]
            build_rs.write_text(build_content, encoding="utf-8")
            print("      ✓ Injected Windows icon + Rackhand metadata hook into build.rs.")
    elif '"FileDescription"' not in build_content:
        build_content = build_content.replace(
            'res.set_icon("rackhand.ico");\n',
            'res.set_icon("rackhand.ico");\n' + meta_lines,
        )
        build_rs.write_text(build_content, encoding="utf-8")
        print("      ✓ Updated build.rs with Rackhand Windows PE metadata.")

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

    let current_ver = app::project_version();
    let hash_file = app::install_dir().join(".project_hash");
    let mut needs_install = false;

    if !app::install_dir().is_dir() {
        materialize()?;
        needs_install = true;
    } else {
        let installed_ver = fs::read_to_string(&hash_file).unwrap_or_default();
        if current_ver != installed_ver {
            if fs::remove_dir_all(app::install_dir()).is_ok() {
                materialize()?;
            }
            needs_install = true;
        }
    }

    if needs_install && !app::skip_install() {
        install_project()?;
        let _ = fs::write(&hash_file, &current_ver);
    }"""
        if target in dist_norm:
            dist_norm = dist_norm.replace(target, replacement, 1)
            final_content = dist_norm.replace("\n", "\r\n") if is_crlf else dist_norm
            dist_rs.write_text(final_content, encoding="utf-8")
            print("      ✓ Injected fast version-based cache check into distribution.rs.")
    elif "DefaultHasher" in dist_norm:
        # Upgrade existing DefaultHasher patch to fast O(1) project_version check
        old_hash_block = """    let embedded = app::embedded_project();
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
        new_fast_block = """    let current_ver = app::project_version();
    let hash_file = app::install_dir().join(".project_hash");
    let mut needs_install = false;

    if !app::install_dir().is_dir() {
        materialize()?;
        needs_install = true;
    } else {
        let installed_ver = fs::read_to_string(&hash_file).unwrap_or_default();
        if current_ver != installed_ver {
            if fs::remove_dir_all(app::install_dir()).is_ok() {
                materialize()?;
            }
            needs_install = true;
        }
    }

    if needs_install && !app::skip_install() {
        install_project()?;
        let _ = fs::write(&hash_file, &current_ver);
    }"""
        if old_hash_block in dist_norm:
            dist_norm = dist_norm.replace(old_hash_block, new_fast_block, 1)
            final_content = dist_norm.replace("\n", "\r\n") if is_crlf else dist_norm
            dist_rs.write_text(final_content, encoding="utf-8")
            print("      ✓ Upgraded distribution.rs to fast O(1) version check.")
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
    env["PYAPP_PASS_LOCATION"] = "1"
    
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
