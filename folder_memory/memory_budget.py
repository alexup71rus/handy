"""Explicit-process memory accounting, with Darwin physical-footprint support.

RUSAGE_INFO_V0 is the stable 96-byte public layout in Apple's sys/resource.h;
the local Xcode SDK was checked against the published XNU definition:
https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/resource.h

This measures OS accounting at a point in time. It is not a hard memory limit,
does not discover descendants, and does not include arbitrary system caches.
"""

import ctypes
import errno
import functools
import itertools
import os
import platform
import re
import subprocess
from decimal import Decimal


def descendant_pids(root):
    """Discover owned descendants, excluding an already-running user browser."""
    root=_pid(root)
    try:
        result=_run(['/bin/ps','-axo','pid=,ppid='])
        pairs=[tuple(map(int,line.split())) for line in result.stdout.splitlines() if len(line.split())==2]
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return [root]
    selected={root}
    for _ in range(16):
        found={pid for pid,parent in pairs if parent in selected}
        if found<=selected:
            break
        selected.update(found)
    return sorted(selected)[:64]


class _RUsageInfoV0(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


def _pid(value):
    if type(value) is not int or not 1 <= value <= 2 ** 31 - 1:
        raise ValueError("An explicit positive PID is required")
    return value


@functools.lru_cache(maxsize=1)
def _libproc():
    return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)


def _darwin_rusage(pid):
    if ctypes.sizeof(_RUsageInfoV0) != 96 or _RUsageInfoV0.ri_resident_size.offset != 64 or _RUsageInfoV0.ri_phys_footprint.offset != 72:
        raise OSError(errno.ENOTSUP, "Unexpected rusage_info_v0 ABI")
    function = _libproc().proc_pid_rusage
    # The public declaration is (pid_t, int, rusage_info_t*), where
    # rusage_info_t is void*. Apple callers cast the STRUCT ADDRESS to it.
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    function.restype = ctypes.c_int
    info = _RUsageInfoV0()
    address = ctypes.cast(ctypes.byref(info), ctypes.POINTER(ctypes.c_void_p))
    ctypes.set_errno(0)
    if function(pid, 0, address) != 0:  # RUSAGE_INFO_V0 = 0, not CURRENT.
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code))
    return int(info.ri_resident_size), int(info.ri_phys_footprint), int(info.ri_proc_start_abstime)


def _parse_size(value):
    match = re.fullmatch(r"\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(bytes?|[KMGTPE]?(?:i?B)?)?\s*", value, re.I)
    if not match:
        return None
    number = Decimal(match[1].replace(",", ""))
    unit = (match[2] or "B").upper()
    exponent = 0 if unit in {"B", "BYTE", "BYTES", ""} else "KMGTPE".index(unit[0]) + 1
    return int(number * (1024 ** exponent))


def _parse_vmmap(text):
    # The peak is a different line and must not be confused with current charge.
    match = re.search(r"^\s*Physical footprint\s*:\s*([^\r\n]+)", text, re.M | re.I)
    return _parse_size(match[1]) if match else None


def _run(args):
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2, check=False, env={**os.environ, "LC_ALL": "C"})


def _vmmap_footprint(pid):
    try:
        result = _run(["/usr/bin/vmmap", "-summary", str(pid)])
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_vmmap(result.stdout[:1024 * 1024]) if result.returncode == 0 else None


def _ps_rss(pid):
    try:
        result = _run(["/bin/ps", "-p", str(pid), "-o", "rss="])
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = result.stdout.strip()
    return int(raw) * 1024 if result.returncode == 0 and re.fullmatch(r"\d+", raw) else None


def _exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _exited(pid, source):
    return {"pid": pid, "rss_bytes": 0, "physical_footprint_bytes": None, "source": source, "status": "exited", "known": True, "exists": False, "approximate": False}


def process_memory(pid):
    """Measure only this PID. Unknown values are None; exited is known zero."""
    pid = _pid(pid)
    darwin = platform.system() == "Darwin"
    if darwin:
        try:
            rss, footprint, identity = _darwin_rusage(pid)
            return {"pid": pid, "rss_bytes": rss, "physical_footprint_bytes": footprint, "source": "proc_pid_rusage:RUSAGE_INFO_V0", "status": "ok", "known": True, "exists": True, "approximate": False, "process_start_abstime": identity}
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return _exited(pid, "proc_pid_rusage:ESRCH")
        except (AttributeError, TypeError):
            pass
    rss = _ps_rss(pid)
    footprint = _vmmap_footprint(pid) if darwin else None
    if rss is None and footprint is None:
        exists = _exists(pid)
        if exists is False:
            return _exited(pid, "process_absent")
        return {"pid": pid, "rss_bytes": None, "physical_footprint_bytes": None, "source": "unavailable", "status": "unavailable", "known": False, "exists": exists, "approximate": False}
    return {"pid": pid, "rss_bytes": rss, "physical_footprint_bytes": footprint, "source": "vmmap:summary+ps:rss" if footprint is not None else "ps:rss", "status": "ok", "known": True, "exists": True, "approximate": footprint is not None}


def tree_memory(pids, budget=None):
    """Sum caller-supplied PID accounting; duplicate PIDs are counted once.

Physical footprint is preferred to RSS. RSS-only or missing measurements cannot
prove the complete tree is within budget; a conservative overage still fails.
"""
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("Memory budget must be a positive byte count")
    values = list(itertools.islice(iter(pids), 65))
    if len(values) > 64:
        raise ValueError("At most 64 explicit PIDs may be measured")
    unique = list(dict.fromkeys(_pid(pid) for pid in values))
    processes = [process_memory(pid) for pid in unique]
    unknown = [p["pid"] for p in processes if not p["known"]]
    exited = [p["pid"] for p in processes if p["status"] == "exited"]
    fallback = [p["pid"] for p in processes if p["status"] == "ok" and p["physical_footprint_bytes"] is None]
    charged = sum(p["physical_footprint_bytes"] if p["physical_footprint_bytes"] is not None else (p["rss_bytes"] or 0) for p in processes)
    rss = sum(p["rss_bytes"] or 0 for p in processes)
    footprint_complete = not unknown and not fallback
    status = "not_configured" if budget is None else "exceeded" if charged > budget else "within" if footprint_complete else "unknown"
    return {"processes": processes, "rss_bytes": rss, "charged_bytes": charged, "budget_bytes": budget, "within_budget": {"within": True, "exceeded": False}.get(status), "budget_status": status, "measurement_complete": not unknown, "footprint_complete": footprint_complete, "unknown_pids": unknown, "rss_fallback_pids": fallback, "exited_pids": exited, "scope": "explicit_pids", "hard_limit": False}
