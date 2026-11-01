"""Pin the calling thread and report the mask that is actually in effect.

The distinction this module exists to preserve: a tool that prints the mask
it ASKED for is reporting its own intention, not the machine's behaviour. A
request can be silently narrowed by a cpuset, a cgroup, a container runtime
or a processor-group limit. membw therefore always reads the mask back after
setting it, and additionally records which CPU each worker was observed
running on, which is the only direct evidence that a thread did not migrate.

Linux uses sched_setaffinity and sched_getaffinity. Windows uses
SetThreadAffinityMask, whose return value is the mask previously in effect;
calling it twice with the same mask therefore reads the effective mask back.
Everything else reports pinning as unsupported rather than pretending.
"""

from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import dataclass, field

from .topology import format_cpu_list

WINDOWS_GROUP_CPU_LIMIT = 64


@dataclass
class AffinityResult:
    """What was asked for, what took effect, and how that was established."""

    requested: tuple[int, ...]
    effective: tuple[int, ...] | None
    supported: bool
    mechanism: str
    observed_cpus: tuple[int, ...] = ()
    error: str | None = None

    @property
    def as_requested(self) -> bool:
        """True only if pinning is supported AND the mask read back matches."""
        if not self.supported or self.effective is None:
            return False
        return set(self.effective) == set(self.requested)

    def as_dict(self) -> dict:
        return {
            "requested_cpu_list": format_cpu_list(self.requested),
            "effective_cpu_list": (
                None if self.effective is None else format_cpu_list(self.effective)
            ),
            "mask_as_requested": self.as_requested,
            "pinning_supported": self.supported,
            "mechanism": self.mechanism,
            "observed_cpu_list": format_cpu_list(self.observed_cpus),
            "error": self.error,
        }


def pinning_supported() -> bool:
    """True if this platform has a thread-pinning mechanism membw can use."""
    if hasattr(os, "sched_setaffinity") and hasattr(os, "sched_getaffinity"):
        return True
    return platform.system() == "Windows"


def _mask_from_cpus(cpus) -> int:
    mask = 0
    for cpu in cpus:
        mask |= 1 << int(cpu)
    return mask


def _cpus_from_mask(mask: int, limit: int = 256) -> tuple[int, ...]:
    return tuple(bit for bit in range(limit) if mask & (1 << bit))


def _windows_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    kernel32.SetThreadAffinityMask.restype = ctypes.c_size_t
    kernel32.GetCurrentProcessorNumber.restype = ctypes.c_ulong
    return kernel32


def pin_current_thread(cpus) -> AffinityResult:
    """Pin the CALLING thread to cpus and read the resulting mask back.

    Returns an AffinityResult whose `effective` field is the mask actually in
    effect, never the requested one. Never raises: a failure to pin is a
    reportable condition, not an exception, because the run should still
    produce a record saying the number is untrustworthy.
    """
    requested = tuple(sorted(set(int(c) for c in cpus)))
    if not requested:
        return AffinityResult(
            requested=requested,
            effective=None,
            supported=False,
            mechanism="none",
            error="empty CPU set requested",
        )

    if hasattr(os, "sched_setaffinity"):
        mechanism = "sched_setaffinity(0, mask) then sched_getaffinity(0)"
        try:
            os.sched_setaffinity(0, set(requested))
        except OSError as exc:
            try:
                effective = tuple(sorted(os.sched_getaffinity(0)))
            except OSError:
                effective = None
            return AffinityResult(
                requested=requested,
                effective=effective,
                supported=True,
                mechanism=mechanism,
                error="sched_setaffinity failed: %s" % (exc,),
            )
        try:
            effective = tuple(sorted(os.sched_getaffinity(0)))
        except OSError as exc:
            return AffinityResult(
                requested=requested,
                effective=None,
                supported=True,
                mechanism=mechanism,
                error="sched_getaffinity failed: %s" % (exc,),
            )
        return AffinityResult(
            requested=requested,
            effective=effective,
            supported=True,
            mechanism=mechanism,
        )

    if platform.system() == "Windows":
        mechanism = (
            "SetThreadAffinityMask(GetCurrentThread(), mask), effective mask "
            "read back from the return value of a second identical call"
        )
        if max(requested) >= WINDOWS_GROUP_CPU_LIMIT:
            return AffinityResult(
                requested=requested,
                effective=None,
                supported=False,
                mechanism=mechanism,
                error=(
                    "CPU id %d is outside the single 64-CPU processor group "
                    "that SetThreadAffinityMask can address; membw does not "
                    "use SetThreadGroupAffinity yet"
                    % max(requested)
                ),
            )
        try:
            kernel32 = _windows_kernel32()
        except (OSError, AttributeError) as exc:
            return AffinityResult(
                requested=requested,
                effective=None,
                supported=False,
                mechanism=mechanism,
                error="could not load kernel32: %s" % (exc,),
            )
        mask = _mask_from_cpus(requested)
        handle = kernel32.GetCurrentThread()
        first = kernel32.SetThreadAffinityMask(handle, mask)
        if first == 0:
            return AffinityResult(
                requested=requested,
                effective=None,
                supported=True,
                mechanism=mechanism,
                error="SetThreadAffinityMask failed with error %d"
                % ctypes.get_last_error(),
            )
        # The second call returns the mask that the first call installed,
        # which is the mask now in effect.
        second = kernel32.SetThreadAffinityMask(handle, mask)
        if second == 0:
            return AffinityResult(
                requested=requested,
                effective=None,
                supported=True,
                mechanism=mechanism,
                error="read-back SetThreadAffinityMask failed with error %d"
                % ctypes.get_last_error(),
            )
        return AffinityResult(
            requested=requested,
            effective=_cpus_from_mask(second, WINDOWS_GROUP_CPU_LIMIT),
            supported=True,
            mechanism=mechanism,
        )

    return AffinityResult(
        requested=requested,
        effective=None,
        supported=False,
        mechanism="none available on %s" % platform.system(),
        error="no thread pinning mechanism on this platform",
    )


_CURRENT_CPU_FN = None
_CURRENT_CPU_PROBED = False


def _probe_current_cpu_fn():
    global _CURRENT_CPU_FN, _CURRENT_CPU_PROBED
    if _CURRENT_CPU_PROBED:
        return _CURRENT_CPU_FN
    _CURRENT_CPU_PROBED = True
    if platform.system() == "Windows":
        try:
            kernel32 = _windows_kernel32()
            _CURRENT_CPU_FN = lambda: int(kernel32.GetCurrentProcessorNumber())
        except (OSError, AttributeError):
            _CURRENT_CPU_FN = None
        return _CURRENT_CPU_FN
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.sched_getcpu.restype = ctypes.c_int
        libc.sched_getcpu.argtypes = []
        _CURRENT_CPU_FN = lambda: int(libc.sched_getcpu())
    except (OSError, AttributeError):
        _CURRENT_CPU_FN = None
    return _CURRENT_CPU_FN


def current_cpu() -> int | None:
    """Return the CPU the calling thread is running on right now, or None.

    This is corroboration, not control: it is sampled inside each worker so
    that the record can say which CPUs were actually touched, which catches
    a migration that a mask read-back alone would not.
    """
    fn = _probe_current_cpu_fn()
    if fn is None:
        return None
    try:
        value = fn()
    except OSError:
        return None
    return None if value < 0 else value
