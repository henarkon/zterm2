"""Symmetric, bounded application comparison; never profiles during timing."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys


def identity(app):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "org.henarkon.zterm2":
        raise ValueError("only isolated zterm2 applications are accepted")
    files = [app / "Contents/MacOS" / info["CFBundleExecutable"],
             app / "Contents/MacOS/zterm2.debug.dylib", app / "Contents/Resources/default.metallib"]
    return dict(version=info["CFBundleShortVersionString"],
                files={str(path.relative_to(app)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in files if path.is_file()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-app", type=Path)
    parser.add_argument("--candidate-app", type=Path, required=True)
    parser.add_argument("--candidate-only", action="store_true")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--background", dest="background", action="store_true", default=True)
    visibility.add_argument("--visible", dest="background", action="store_false")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not args.candidate_only and args.baseline_app is None:
        parser.error("--baseline-app is required unless --candidate-only is selected")
    apps = {"candidate": args.candidate_app.resolve()}
    if not args.candidate_only:
        apps["baseline"] = args.baseline_app.resolve()
    identities = {name: identity(app) for name, app in apps.items()}
    args.output.mkdir(parents=True, exist_ok=False)
    order = [("baseline", "0", "a"), ("baseline", "1", "a"),
             ("candidate", "1", "a"), ("candidate", "0", "a"),
             ("candidate", "0", "b"), ("candidate", "1", "b"),
             ("baseline", "1", "b"), ("baseline", "0", "b")]
    if args.candidate_only:
        order = [("candidate", "0", "a"), ("candidate", "1", "a"),
                 ("candidate", "1", "b"), ("candidate", "0", "b")]
    conditions = set()
    names = []
    for role, gpu, repeat in order:
        name = role + ("-gpu-" if gpu == "1" else "-cpu-") + repeat
        destination = args.output / name
        result = subprocess.run([sys.executable, "-B", str(root / "tests/zterm2/application_run.py"),
                                 "--app", str(apps[role]), "--gpu", gpu,
                                 "--background" if args.background else "--visible", str(destination)],
                                capture_output=True, text=True, timeout=28)
        if result.returncode:
            raise RuntimeError(name + ": " + result.stdout + result.stderr)
        value = json.loads((destination / "observation.json").read_text())
        fixture = value["fixture"]
        metrics = value["renderer_metrics"]
        if value.get("diagnostic_run") or fixture["measured_frames"] != 90 or fixture["live_shm"]:
            raise RuntimeError("ineligible observation: " + name)
        conditions.add((fixture["fixture_sha256"], tuple(fixture["geometry"]), tuple(fixture["cells"]),
                        fixture["requested_fps"], metrics["output_profile_sha256"], value.get("background_requested", False),
                        metrics.get("viewport_width"), metrics.get("viewport_height"), metrics.get("scale")))
        if len(conditions) != 1:
            raise RuntimeError("comparison conditions differ")
        if role == "candidate" and gpu == "1":
            if metrics.get("lookup_builds") != 1 or metrics.get("core_image_conversions") != 0 or metrics["cpu_uploads"]:
                raise RuntimeError("compact GPU path was not observed throughout: " + name)
        terminal = next(item for item in value["processes"] if item["role"] == "terminal")
        print(json.dumps(dict(case=name, cpu=terminal["cpu_percent_one_core"],
                              physical_mib=terminal["peak_physical_bytes"] / 1048576)), flush=True)
        names.append(name)
    if any(identity(apps[name]) != expected for name, expected in identities.items()):
        raise RuntimeError("application changed during measurement")
    manifest = dict(schema=1, scope="real-terminal", diagnostic_runs=False, cases=names,
                    background_requested=args.background,
                    applications=identities, guard_mib=512, maximum_seconds_per_case=20,
                    source_sha256={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in [root / "tests/zterm2/application_run.py", root / "tests/zterm2/kitty_fixture.py"]},
                    limits=["Short synthetic scene; ACK is not physical presentation.",
                            "Sampling and process footprint do not exclusively attribute GPU/WindowServer."])
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
