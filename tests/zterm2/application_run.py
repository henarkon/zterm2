import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import struct
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Observe one isolated zterm2 app and its bounded Kitty producer.")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--gpu", choices=["0", "1"], required=True)
    parser.add_argument("--diagnostics", action="store_true", help="Capture stacks and memory; excludes this run from performance qualification.")
    parser.add_argument("--gpu-timing", action="store_true", help="Record application's Metal execution envelopes, not global GPU utilisation.")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--background", dest="background", action="store_true", default=True,
                            help="Default: requires invisible-window qualification mode.")
    visibility.add_argument("--visible", dest="background", action="store_false",
                            help="Visible qualification only when explicitly requested by the user.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    info = plistlib.loads((args.app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "org.henarkon.zterm2":
        parser.error("refusing to launch any app other than isolated zterm2")
    if args.background and not info.get("ZTermBackgroundTestsSupported"):
        parser.error("this application does not guarantee invisible test windows")
    if args.background:
        args.gpu_timing = True
    args.output.mkdir(parents=True, exist_ok=False)
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    system.shm_unlink.argtypes = [ctypes.c_char_p]
    timebase = (ctypes.c_uint32 * 2)()
    if system.mach_timebase_info(ctypes.byref(timebase)) or not timebase[1]:
        raise RuntimeError("Mach clock unavailable")
    seconds_per_tick = timebase[0] / timebase[1] / 1e9

    def usage(pid):
        data = ctypes.create_string_buffer(4096)
        if library.proc_pid_rusage(pid, 2, data):
            return None
        user, kernel = struct.unpack_from("QQ", data, 16)
        return dict(cpu=(user + kernel) * seconds_per_tick, physical=struct.unpack_from("Q", data, 72)[0],
                    birth=struct.unpack_from("Q", data, 80)[0])

    fixture = Path(__file__).with_name("kitty_fixture.py").resolve()
    command = shlex.join([sys.executable, "-B", str(fixture), str(args.output.resolve())])
    environment = dict(os.environ, ZTERM2_KITTY_GPU=args.gpu, ZTERM2_RENDER_METRICS="1",
                       ZTERM2_GPU_TIMING="1" if args.gpu_timing else "0")
    environment["ZTERM2_BACKGROUND_TESTS"] = "1" if args.background else "0"
    executable = args.app / "Contents/MacOS" / info["CFBundleExecutable"]
    suite = "org.henarkon.zterm2.bench." + hashlib.sha256(str(args.output.resolve()).encode()).hexdigest()[:12]
    started = time.monotonic()
    producer = None
    processes = {}
    baseline = None
    result = None
    error = None
    diagnostics = []
    with (args.output / "stdout.log").open("xb") as stdout, (args.output / "stderr.log").open("xb") as stderr:
        # Historical baseline binaries matched helper crashes by filename prefix.
        # A launch-only argument suppresses old modal reports in this fresh suite;
        # the observer still fails on any actual application exit during the run.
        app = subprocess.Popen([str(executable), "-suite", suite,
                                "-UKCrashReporterLastCrashReportDate", str(int(time.time()) + 3600),
                                "-RunJobsInServers", "NO", "--command=" + command],
                               env=environment, stdout=stdout, stderr=stderr, start_new_session=True)
        processes[app.pid] = dict(role="terminal", peak=0, measured_peak=0, last=None)
        try:
            while time.monotonic() - started < 20:
                producer_file = args.output / "producer.json"
                if producer is None and producer_file.exists():
                    producer = json.loads(producer_file.read_text())
                    if not isinstance(producer["pid"], int) or producer["pid"] <= 1:
                        raise RuntimeError("invalid producer identity")
                    processes[producer["pid"]] = dict(role="producer", peak=0, measured_peak=0, last=None)
                for pid, entry in processes.items():
                    observed = usage(pid)
                    if observed is None:
                        if pid == app.pid and app.poll() is None:
                            raise RuntimeError("terminal memory observer unavailable")
                        continue
                    if entry["last"] and entry["last"]["birth"] != observed["birth"]:
                        raise RuntimeError("observed process identity changed")
                    entry["last"] = observed
                    entry["peak"] = max(entry["peak"], observed["physical"])
                    if baseline is not None:
                        entry["measured_peak"] = max(entry["measured_peak"], observed["physical"])
                    if observed["physical"] > 512 * 1024 * 1024:
                        raise RuntimeError("512 MiB physical memory guard")
                if baseline is None and (args.output / "measurement-start.json").exists():
                    baseline = dict(time=time.monotonic(), cpu={pid: entry["last"]["cpu"]
                                                             for pid, entry in processes.items() if entry["last"]})
                    if args.diagnostics:
                        for command, name in [(["/usr/bin/sample", str(app.pid), "2", "10", "-file", str(args.output / "stacks.txt")], "sample"),
                                              (["/usr/bin/vmmap", "-summary", str(app.pid)], "vmmap")]:
                            with (args.output / (name + ".log")).open("xb") as log:
                                diagnostics.append(subprocess.Popen(command, stdout=log, stderr=log))
                if (args.output / "error.json").exists():
                    raise RuntimeError("producer failed: " + (args.output / "error.json").read_text())
                if (args.output / "result.json").exists():
                    result = json.loads((args.output / "result.json").read_text())
                    break
                if app.poll() is not None:
                    raise RuntimeError("terminal exited before producer completed")
                if stdout.tell() + stderr.tell() > 8 * 1024 * 1024:
                    raise RuntimeError("log budget exceeded")
                time.sleep(0.05)
            if result is None or baseline is None:
                raise TimeoutError("20 second application deadline")
            renderer_metrics = [json.loads(line.removeprefix("ZTERM2_METRICS "))
                                for line in (args.output / "stderr.log").read_text(errors="replace").splitlines()
                                if line.startswith("ZTERM2_METRICS ")]
            if "ZTERM2_UNEXPECTED_CRASH_REPORTER" in (args.output / "stderr.log").read_text(errors="replace"):
                raise RuntimeError("interactive crash reporter invalidated this measurement")
            if not renderer_metrics or (args.gpu == "1" and not renderer_metrics[-1]["gpu_uploads"]):
                raise RuntimeError("requested renderer path was not observed")
            if args.background:
                log = (args.output / "stderr.log").read_text(errors="replace")
                if "ZTERM2_BACKGROUND_ENABLED" not in log or sum(renderer_metrics[-1][key] for key in ["gpu_uploads", "cpu_uploads"]) < 90:
                    raise RuntimeError("invisible test mode did not render the moving image workload")
            if renderer_metrics[-1].get("output_profile_sha256", "unknown") == "unknown":
                raise RuntimeError("output colour profile not observed")
            elapsed = time.monotonic() - baseline["time"]
            gpu_execution = None
            if args.gpu_timing:
                start = json.loads((args.output / "measurement-start.json").read_text())["monotonic"]
                end = result["monotonic"]
                times = [json.loads(line.removeprefix("ZTERM2_GPU_TIME "))
                         for line in (args.output / "stderr.log").read_text(errors="replace").splitlines()
                         if line.startswith("ZTERM2_GPU_TIME ")]
                inside = [item for item in times if item["start"] >= start and item["end"] <= end]
                if not inside or any(item["status"] != 4 or item["end"] < item["start"] for item in inside):
                    raise RuntimeError("GPU timestamps unavailable or GPU command failed")
                if args.background and len(inside) < 60:
                    raise RuntimeError("invisible window throttled GPU rendering; this is not a valid comparison")
                durations = sorted((item["end"] - item["start"]) * 1000 for item in inside)
                gpu_execution = dict(command_buffers=len(durations), total_envelope_ms=sum(durations),
                                     mean_envelope_ms=sum(durations) / len(durations),
                                     p95_envelope_ms=durations[min(len(durations) - 1, int(len(durations) * .95))],
                                     window_seconds=end - start,
                                     global_utilisation_percent=None,
                                     definition="App Metal command-buffer execution envelopes; not shader occupancy or whole-device utilisation.")
            metrics = [{"role": entry["role"], "pid": pid,
                        "cpu_percent_one_core": (entry["last"]["cpu"] - baseline["cpu"][pid]) / elapsed * 100,
                        "peak_physical_bytes": entry["measured_peak"], "startup_peak_bytes": entry["peak"]}
                       for pid, entry in processes.items() if entry["last"] and pid in baseline["cpu"]]
            report = dict(scope="real-terminal", gpu_requested=args.gpu, fixture=result,
                          diagnostic_run=args.diagnostics,
                          historical_crash_dialogs_suppressed=True, detached_session_servers=False,
                          gpu_execution=gpu_execution,
                          background_requested=args.background,
                          suite=suite,
                          renderer_metrics=renderer_metrics[-1],
                          measurement_seconds=elapsed, processes=metrics, guard_mib=512,
                          maximum_seconds=20, observer_interval_ms=50,
                          limits=["ACK cadence is not physical screen presentation.",
                                  "Counters confirm the upload path and output ICC hash; physical display timing is not measured.",
                                  "CPU window edges and memory peaks are sampled; GPU/WindowServer not exclusively attributed."])
        except Exception as failure:
            error = str(failure)
            report = dict(scope="real-terminal", gpu_requested=args.gpu, error=error, processes=processes,
                          guard_mib=512, maximum_seconds=20)
        finally:
            for diagnostic in diagnostics:
                if diagnostic.poll() is None:
                    diagnostic.kill()
                diagnostic.wait(timeout=2)
            if app.poll() is None:
                try:
                    os.killpg(app.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    app.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(app.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    app.wait(timeout=2)
            if producer:
                entry = processes.get(producer["pid"])
                observed = usage(producer["pid"])
                if observed and entry and entry["last"] and observed["birth"] == entry["last"]["birth"]:
                    try:
                        os.kill(producer["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                prefix = producer.get("shm_prefix", "")
                if re.fullmatch(r"/zt2-[0-9a-f]{6}-" + str(producer["pid"]) + "-", prefix):
                    for frame in range(120):
                        system.shm_unlink((prefix + str(frame)).encode())
        (args.output / "observation.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        if error:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
