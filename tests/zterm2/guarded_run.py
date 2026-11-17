import argparse
import ctypes
import json
import os
import signal
import struct
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--mib", type=int, default=512)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60 or not 0 < args.mib <= 512:
        parser.error("limits must be at most 60 seconds and 512 MiB")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    library.proc_pid_rusage.restype = ctypes.c_int
    timebase = (ctypes.c_uint32 * 2)()
    system_library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    if system_library.mach_timebase_info(ctypes.byref(timebase)) != 0 or not timebase[1]:
        raise RuntimeError("Mach timebase unavailable")
    seconds_per_tick = timebase[0] / timebase[1] / 1e9
    started = time.monotonic()
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    peak = 0
    cpu = 0
    reason = None
    samples = 0
    try:
        while child.poll() is None:
            usage = ctypes.create_string_buffer(4096)
            if library.proc_pid_rusage(child.pid, 2, usage) != 0:
                if child.poll() is None:
                    reason = "memory-observer-unavailable"
                    break
                continue
            physical = struct.unpack_from("Q", usage, 72)[0]
            user, system = struct.unpack_from("QQ", usage, 16)
            peak = max(peak, physical)
            cpu = max(cpu, user + system)
            samples += 1
            if physical > args.mib * 1024 * 1024:
                reason = "memory-limit"
                break
            if time.monotonic() - started > args.seconds:
                reason = "time-limit"
                break
            time.sleep(0.05)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
    stdout, stderr = child.communicate(timeout=5)
    elapsed = time.monotonic() - started
    result = dict(command=command, exit_code=child.returncode, stop_reason=reason,
                  elapsed_seconds=elapsed, cpu_seconds=cpu * seconds_per_tick,
                  cpu_percent_one_core=cpu * seconds_per_tick / elapsed * 100,
                  peak_physical_mib=peak / 1024 / 1024, samples=samples,
                  observer="proc_pid_rusage-v2-50ms", mach_timebase=list(timebase), guard_mib=args.mib,
                  stdout=stdout.decode(), stderr=stderr.decode())
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if reason or child.returncode else 0)


if __name__ == "__main__":
    main()
