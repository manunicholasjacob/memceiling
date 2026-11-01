"""Build and load the optional compiled kernel library.

The compiled path is optional on purpose: membw must install and run on a
machine with no C toolchain, and must say which path produced a number. This
module is the whole of the "is there a compiler" question, so the rest of
the code only ever asks load_c_kernels() and gets either a library or None.

The built library is cached under the user's cache directory, keyed on the
platform, machine and compiler, so a library built for one architecture is
never loaded on another.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field

ABI_VERSION = 1
SOURCE_NAME = "_ckernels.c"


def source_path() -> str:
    """Absolute path of the shipped C source."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SOURCE_NAME)


def cache_dir() -> str:
    """Directory where membw caches the built library."""
    override = os.environ.get("MEMBW_CACHE_DIR")
    if override:
        return override
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "membw", "cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "membw")


def library_filename() -> str:
    suffix = {"Windows": ".dll", "Darwin": ".dylib"}.get(platform.system(), ".so")
    return "membw_ckernels-%s-%s-abi%d%s" % (
        platform.system().lower(),
        platform.machine().lower().replace(" ", "_") or "unknown",
        ABI_VERSION,
        suffix,
    )


def default_library_path() -> str:
    return os.path.join(cache_dir(), library_filename())


@dataclass
class BuildReport:
    """What happened when membw tried to build the compiled kernels."""

    ok: bool
    library_path: str | None = None
    compiler: str | None = None
    command: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "library_path": self.library_path,
            "compiler": self.compiler,
            "flags": list(self.flags),
            "error": self.error,
        }


def detect_compiler() -> tuple:
    """Return (kind, executable) for the first usable compiler, or (None, None).

    kind is 'unix' for a cc/gcc/clang-style driver and 'msvc' for cl.exe.
    The CC environment variable wins if it is set and found.
    """
    env_cc = os.environ.get("CC")
    if env_cc:
        found = shutil.which(env_cc)
        if found:
            kind = "msvc" if os.path.basename(found).lower().startswith("cl") else "unix"
            return kind, found
    for name in ("cc", "gcc", "clang"):
        found = shutil.which(name)
        if found:
            return "unix", found
    # sysconfig knows the compiler CPython itself was built with, which is
    # present on most Linux images even when 'cc' is not on PATH.
    config_cc = sysconfig.get_config_var("CC")
    if config_cc:
        candidate = config_cc.split()[0]
        found = shutil.which(candidate)
        if found:
            return "unix", found
    found = shutil.which("cl")
    if found:
        return "msvc", found
    if platform.system() == "Windows":
        msvc = _find_msvc_through_setuptools()
        if msvc:
            return "msvc-setuptools", msvc
    return None, None


def _find_msvc_through_setuptools():
    """Ask setuptools where MSVC is, since it is usually not on PATH.

    Returns the cl.exe path, with the environment it needs already merged
    into os.environ for the current process, or None.
    """
    try:
        from setuptools._distutils.ccompiler import new_compiler
    except Exception:
        try:
            from distutils.ccompiler import new_compiler  # type: ignore
        except Exception:
            return None
    try:
        compiler = new_compiler()
        compiler.initialize()
    except Exception:
        return None
    cc = getattr(compiler, "cc", None)
    if not (cc and os.path.isfile(cc)):
        return None

    # initialize() populates the include/lib/path environment MSVC needs.
    # setuptools sets include_dirs and library_dirs at CLASS level, where the
    # instance attributes created by the base __init__ shadow them, so both
    # have to be consulted.
    paths = getattr(compiler, "_paths", None)
    if paths:
        os.environ["PATH"] = paths + os.pathsep + os.environ.get("PATH", "")
    for attr, var in (("include_dirs", "INCLUDE"), ("library_dirs", "LIB")):
        values = getattr(compiler, attr, None) or getattr(type(compiler), attr, None)
        if values:
            os.environ[var] = os.pathsep.join(values)
    return cc


def build_c_kernels(output_path: str | None = None, force: bool = False) -> BuildReport:
    """Compile _ckernels.c into a shared library.

    Returns a BuildReport. Never raises on a compiler failure: a missing or
    broken toolchain is a condition to be reported, since membw still has a
    working numpy path.
    """
    src = source_path()
    if not os.path.isfile(src):
        return BuildReport(ok=False, error="C source not found at %s" % src)

    out = output_path or default_library_path()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    if not force and os.path.isfile(out):
        if os.path.getmtime(out) >= os.path.getmtime(src):
            return BuildReport(
                ok=True,
                library_path=out,
                compiler="cached (built earlier)",
                flags=[],
            )

    kind, exe = detect_compiler()
    if exe is None:
        return BuildReport(
            ok=False,
            error=(
                "no C compiler was found. Looked for $CC, cc, gcc, clang, the "
                "compiler CPython was built with, and cl.exe. The numpy path "
                "will be used instead."
            ),
        )

    if kind == "unix":
        flags = ["-O3", "-fPIC", "-shared", "-fno-builtin-memcpy"]
        command = [exe] + flags + ["-o", out, src]
    else:
        obj = out + ".obj"
        flags = ["/O2", "/LD", "/GS-"]
        command = [exe, "/nologo"] + flags + [src, "/Fe:" + out, "/Fo:" + obj]

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            cwd=os.path.dirname(out) or None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BuildReport(
            ok=False,
            compiler=exe,
            command=command,
            flags=flags,
            error="could not run the compiler: %s" % (exc,),
        )

    if proc.returncode != 0 or not os.path.isfile(out):
        return BuildReport(
            ok=False,
            compiler=exe,
            command=command,
            flags=flags,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            error="compiler exited with status %d" % proc.returncode,
        )

    return BuildReport(
        ok=True,
        library_path=out,
        compiler=exe,
        command=command,
        flags=flags,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


@dataclass
class CKernels:
    """A loaded kernel library with its ctypes signatures applied."""

    lib: object
    library_path: str
    abi_version: int
    compiler: str | None = None
    flags: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "library_path": self.library_path,
            "abi_version": self.abi_version,
            "compiler": self.compiler,
            "flags": list(self.flags),
        }


def _apply_signatures(lib) -> None:
    c_double = ctypes.c_double
    c_size = ctypes.c_size_t
    p_double = ctypes.POINTER(ctypes.c_double)

    lib.membw_abi_version.restype = ctypes.c_int
    lib.membw_abi_version.argtypes = []

    lib.membw_read.restype = c_double
    lib.membw_read.argtypes = [p_double, c_size]

    lib.membw_read_strided.restype = c_double
    lib.membw_read_strided.argtypes = [p_double, c_size, c_size]

    lib.membw_copy.restype = None
    lib.membw_copy.argtypes = [p_double, p_double, c_size]

    lib.membw_scale.restype = None
    lib.membw_scale.argtypes = [p_double, p_double, c_double, c_size]

    lib.membw_add.restype = None
    lib.membw_add.argtypes = [p_double, p_double, p_double, c_size]

    lib.membw_triad.restype = None
    lib.membw_triad.argtypes = [p_double, p_double, p_double, c_double, c_size]

    lib.membw_fill.restype = None
    lib.membw_fill.argtypes = [p_double, c_double, c_size]


def load_c_kernels(
    library_path: str | None = None, build_if_missing: bool = True
):
    """Load the compiled kernels, building them first if needed.

    Returns (CKernels, None) on success or (None, reason_string) on failure.
    The reason is written for a human reading a provenance record, so it
    says what was tried.
    """
    explicit = library_path or os.environ.get("MEMBW_C_LIBRARY")
    report = None
    if explicit:
        path = explicit
        if not os.path.isfile(path):
            return None, "no library at the requested path %s" % path
        compiler, flags = "supplied prebuilt library", []
    else:
        path = default_library_path()
        if not os.path.isfile(path):
            if not build_if_missing:
                return None, "no cached library at %s and building was disabled" % path
            report = build_c_kernels()
            if not report.ok:
                return None, report.error or "build failed"
            path = report.library_path
        compiler = report.compiler if report else "cached (built earlier)"
        flags = report.flags if report else []

    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        return None, "could not load %s: %s" % (path, exc)

    try:
        _apply_signatures(lib)
        abi = int(lib.membw_abi_version())
    except (AttributeError, OSError) as exc:
        return None, "library at %s is missing membw symbols: %s" % (path, exc)

    if abi != ABI_VERSION:
        return None, (
            "library at %s reports ABI version %d but this membw expects %d; "
            "rebuild it with 'membw build-c --force'" % (path, abi, ABI_VERSION)
        )

    return (
        CKernels(
            lib=lib, library_path=path, abi_version=abi, compiler=compiler, flags=flags
        ),
        None,
    )
