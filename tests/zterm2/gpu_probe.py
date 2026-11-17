"""Bounded GPU observation: app command-buffer time plus separate device counters."""
import argparse
import json
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--gpu", choices=["0", "1"], required=True)
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--background", dest="background", action="store_true", default=True)
    visibility.add_argument("--visible", dest="background", action="store_false")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    observer = Path(__file__).with_name("application_run.py")
    process = subprocess.Popen([sys.executable, "-B", str(observer), "--app", str(args.app.resolve()),
                                "--gpu", args.gpu, "--gpu-timing", "--background" if args.background else "--visible", str(args.output)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    samples = []
    errors = []
    try:
        while process.poll() is None:
            if time.monotonic() - started > 25:
                raise TimeoutError("GPU observation deadline")
            if (args.output / "measurement-start.json").exists() and not (args.output / "result.json").exists():
                before = time.monotonic()
                result = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "IOAccelerator", "-d", "1", "-a"],
                                        capture_output=True, timeout=1)
                if result.returncode:
                    errors.append("IORegistry counters unavailable")
                else:
                    for device in plistlib.loads(result.stdout):
                        stats = device.get("PerformanceStatistics", {})
                        values = {key: stats[key] for key in ["Device Utilization %", "Renderer Utilization %", "Tiler Utilization %"]
                                  if isinstance(stats.get(key), (float, int))}
                        if values:
                            samples.append(dict(monotonic=(before + time.monotonic()) / 2,
                                                device=device.get("IORegistryEntryName"), values=values))
            time.sleep(.2)
        stdout, stderr = process.communicate(timeout=2)
        if process.returncode:
            raise RuntimeError(stdout + stderr)
        observation = json.loads((args.output / "observation.json").read_text())
        start = json.loads((args.output / "measurement-start.json").read_text())["monotonic"]
        end = observation["fixture"]["monotonic"]
        samples = [sample for sample in samples if start <= sample["monotonic"] <= end]
        values = [sample["values"]["Device Utilization %"] for sample in samples if "Device Utilization %" in sample["values"]]
        report = dict(scope="dedicated-gpu-observation", gpu_requested=args.gpu,
                      background_requested=args.background,
                      app_execution=observation["gpu_execution"], device_samples=samples,
                      device_percent_summary=dict(mean=sum(values) / len(values), minimum=min(values), maximum=max(values)) if values else None,
                      counter_errors=errors,
                      limits=["Device counters include every app and WindowServer; never exclusively attributed to zterm2.",
                              "App timestamps are command-buffer execution envelopes, not hardware shader occupancy.",
                              "Dedicated instrumentation run; its CPU values are excluded from the performance comparison."])
        (args.output / "gpu-observation.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({key: report[key] for key in ["gpu_requested", "app_execution", "device_percent_summary"]}, indent=2))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    main()
