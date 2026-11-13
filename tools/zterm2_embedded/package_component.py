import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = ["embedded", "tools/zterm2_embedded", "tests/zterm2_embedded",
                "sources/API/iTermAPIHelper.h", "sources/API/iTermAPIHelper.m",
                "sources/AppKit/iTermApplication.h", "sources/AppKit/iTermApplication.m",
                "sources/PTYSession/PTYSession.h", "sources/PTYSession/PTYSession.m",
                "sources/TerminalView/PTYTextView.h", "sources/TerminalView/PTYTextView.m",
                "sources/Tasks/PTYTask.h", "sources/Tasks/PTYTask.m",
                "sources/Tasks/TaskNotifier.h", "sources/Tasks/TaskNotifier.m",
                "sources/Tasks/ChildReaper.swift", "sources/Tasks/iTermLegacyJobManager.m",
                "ModernTests/PTYTaskReadTests.swift", "ModernTests/TaskIOCharacterizationTests.swift"]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def git(*arguments, directory=ROOT):
    return subprocess.check_output(["git", "-C", str(directory), *arguments])


def archive_sources(directory, destination):
    with gzip.open(destination, "wb", compresslevel=6) as stream:
        process = subprocess.Popen(["git", "-C", str(directory), "archive", "HEAD"], stdout=subprocess.PIPE)
        try:
            shutil.copyfileobj(process.stdout, stream)
        finally:
            process.stdout.close()
        if process.wait():
            raise RuntimeError(f"Source archive failed: {directory}")


def main():
    parser = argparse.ArgumentParser(description="Freeze a tested embedded component; UI-unqualified candidates must live under dist/archives.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--host-build", type=Path, required=True)
    parser.add_argument("--api-report", type=Path, required=True)
    parser.add_argument("--api-log", type=Path, required=True)
    parser.add_argument("--unit-log", type=Path, required=True)
    parser.add_argument("--fixture-log", type=Path, required=True)
    parser.add_argument("--engine-log", type=Path, required=True)
    parser.add_argument("--source-packages", type=Path, required=True)
    parser.add_argument("--cargo-vendor", type=Path, required=True)
    parser.add_argument("--ui-report", type=Path)
    arguments = parser.parse_args()
    api = json.loads(arguments.api_report.read_text())
    ui = json.loads(arguments.ui_report.read_text()) if arguments.ui_report else {"passed": False, "reason": "VM privacy permissions pending"}
    if api.get("passed") is not True or "** TEST SUCCEEDED **" not in arguments.unit_log.read_text():
        raise SystemExit("Positive API and unit-test evidence required")
    fixture_log = arguments.fixture_log.read_text()
    if "Ran 11 tests" not in fixture_log or "\nOK" not in fixture_log:
        raise SystemExit("Positive standalone fixture evidence required")
    required_cases = {"rendering", "keyboard", "focus", "paste", "mouse", "resize", "close"}
    passed_cases = {case["id"] for case in ui.get("cases", []) if case.get("passed") is True and case.get("evidence")}
    m1 = ui.get("passed") is True and required_cases <= passed_cases
    output = arguments.output.resolve()
    expected = ROOT / "dist/v0.2.0"
    if m1 and output != expected:
        raise SystemExit("Qualified release destination must be dist/v0.2.0")
    if not m1 and output.parent != ROOT / "dist/archives":
        raise SystemExit("Unqualified candidate destination must be directly under dist/archives")
    build_directory = arguments.host_build.resolve()
    build = json.loads((build_directory / "build.json").read_text())
    app = Path(build["app"])
    library = app / "Contents/Frameworks/libZTermEmbedded.dylib"
    executable = app / "Contents/MacOS/ZTermEmbeddedDemo"
    identity = api.get("executionIdentity", {})
    if identity.get("librarySha256") != digest(library) or identity.get("appExecutableSha256") != digest(executable) or identity.get("hypervisorPresent") != "1":
        raise SystemExit("VM execution identity must match the exact delivered library and host")
    if m1 and (ui.get("librarySha256") != digest(library) or ui.get("appExecutableSha256") != digest(executable)):
        raise SystemExit("UI evidence must identify the exact qualified binaries")
    if "__asan_" in subprocess.check_output(["nm", "-u", str(library)], text=True):
        raise SystemExit("ASAN runtime must not leak into the delivered component")
    output.mkdir(parents=True, exist_ok=False)
    sources = output / "corresponding-sources"
    sources.mkdir()
    archive_sources(ROOT, sources / "base.tar.gz")
    (sources / "embedded.patch").write_bytes(git("diff", "--binary", "HEAD", "--", *SOURCE_PATHS))
    modules = []
    for line in git("ls-files", "--stage").decode().splitlines():
        metadata, path = line.split("\t", 1)
        mode, pinned, stage = metadata.split()
        if mode != "160000":
            continue
        actual = git("rev-parse", "HEAD", directory=ROOT / path).decode().strip()
        if actual != pinned:
            raise RuntimeError(f"Submodule differs from source pin: {path}")
        destination = sources / (path.replace("/", "-") + ".tar.gz")
        archive_sources(ROOT / path, destination)
        modules.append({"path": path, "commit": pinned, "archive": destination.name})
    lock = ROOT / "submodules/railroad_dsl/Cargo.lock"
    if lock.is_file():
        shutil.copy2(lock, sources / "railroad-Cargo.lock")
    (sources / "pins.json").write_text(json.dumps({"base": git("rev-parse", "HEAD").decode().strip(),
                                                   "origin": git("remote", "get-url", "origin").decode().strip(),
                                                   "submodules": modules}, indent=2) + "\n")
    resolved = json.loads((ROOT / "iTerm2.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved").read_text())
    revisions = {pin["identity"].lower(): pin["state"]["revision"] for pin in resolved["pins"]}
    swift_packages = []
    for checkout in sorted((arguments.source_packages / "checkouts").iterdir()):
        if not checkout.is_dir():
            continue
        revision = git("rev-parse", "HEAD", directory=checkout).decode().strip()
        if revisions.get(checkout.name.lower()) != revision or git("diff", "HEAD", directory=checkout):
            raise RuntimeError(f"Swift package differs from its source pin: {checkout.name}")
        destination = sources / ("swift-package-" + checkout.name + ".tar.gz")
        archive_sources(checkout, destination)
        swift_packages.append({"identity": checkout.name, "commit": revision, "archive": destination.name})
    if len(swift_packages) != len(revisions):
        raise RuntimeError("Missing resolved Swift package sources")
    (sources / "swift-package-pins.json").write_text(json.dumps(swift_packages, indent=2) + "\n")
    vendor = arguments.cargo_vendor.resolve()
    if not vendor.is_dir() or not list(vendor.glob("*/.cargo-checksum.json")):
        raise RuntimeError("Locked Cargo vendor sources required")
    with tarfile.open(sources / "cargo-vendor.tar.gz", "w:gz") as archive:
        archive.add(vendor, arcname="cargo-vendor")
    with tarfile.open(output / "ZTermEmbeddedDemo.app.tar.gz", "w:gz") as archive:
        archive.add(app, arcname=app.name)
    with tarfile.open(output / "ZTermEmbeddedSDK.tar.gz", "w:gz") as archive:
        archive.add(build_directory / "component/include", arcname="ZTermEmbeddedSDK/include")
        for name in ("Frameworks", "Resources", "XPCServices"):
            archive.add(app / "Contents" / name, arcname="ZTermEmbeddedSDK/Runtime/Contents/" + name)
        for name in ("ShellLauncher", "iTermServer"):
            archive.add(app / "Contents/MacOS" / name, arcname="ZTermEmbeddedSDK/Runtime/Contents/MacOS/" + name)
    inventory = []
    for path in sorted(app.rglob("*")):
        relative = str(path.relative_to(app))
        if path.is_symlink():
            inventory.append({"path": relative, "symlink": str(path.readlink())})
        elif path.is_file():
            record = {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}
            with path.open("rb") as stream:
                magic = stream.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"):
                record["linkedLibraries"] = subprocess.check_output(["otool", "-L", str(path)], text=True).splitlines()[1:]
            inventory.append(record)
    (output / "runtime-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    evidence = output / "evidence"
    evidence.mkdir()
    for name, source in (("api-report.json", arguments.api_report), ("api.log", arguments.api_log),
                         ("unit-tests.log", arguments.unit_log), ("fixture-tests.log", arguments.fixture_log),
                         ("engine-build.log", arguments.engine_log), ("build.json", build_directory / "build.json")):
        shutil.copy2(source, evidence / name)
    if arguments.ui_report:
        shutil.copy2(arguments.ui_report, evidence / "ui-report.json")
        for case in ui.get("cases", []):
            for relative in case.get("evidence", []):
                if Path(relative).is_absolute() or ".." in Path(relative).parts:
                    raise RuntimeError("UI evidence must have a relative contained path")
                source = (arguments.ui_report.parent / relative).resolve()
                source.relative_to(arguments.ui_report.parent.resolve())
                destination = evidence / "ui" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    for source, name in (("LICENSE", "LICENSE"), ("COPYING", "COPYING"),
                         ("ThirdParty/Licenses.txt", "ThirdParty-Licenses.txt"),
                         ("embedded/integration.txt", "integration.txt"),
                         ("embedded/build.txt", "build.txt"),
                         ("tests/zterm2_embedded/qualification-plan.txt", "qualification-plan.txt")):
        shutil.copy2(ROOT / source, output / name)
    report = {"schema": "zterm2-embedded-artifact-1", "componentVersion": "0.2.0", "m1Passed": m1,
              "status": "qualified-M1" if m1 else "candidate-api-qualified-ui-pending",
              "architecture": "arm64", "deploymentTarget": "13.0", "testedOS": "VM macOS 26.6.2 (25G83)",
              "experimentalKittyGPU": False, "renderer": "CPU", "signature": "local-ad-hoc-not-notarized",
              "librarySha256": digest(library), "appExecutableSha256": digest(app / "Contents/MacOS/ZTermEmbeddedDemo"),
              "api": api, "ui": ui,
              "sourceReconstruction": "Extract base.tar.gz, apply embedded.patch, extract each pinned submodule archive at its path; restore railroad-Cargo.lock. Swift package revision archives and locked Cargo vendor sources are included; see build.txt."}
    (output / "qualification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    manifest = {str(path.relative_to(output)): digest(path) for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "manifest.sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output": str(output), "m1Passed": m1, "files": len(manifest)}))


if __name__ == "__main__":
    main()
