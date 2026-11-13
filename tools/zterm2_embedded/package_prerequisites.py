import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "embedded/contract-proposal.json",
    "embedded/feasibility.txt",
    "tests/zterm2_embedded/fixture.c",
    "tests/zterm2_embedded/test_fixture.py",
    "tools/zterm2_embedded/audit_boundary.py",
    "tools/zterm2_embedded/package_prerequisites.py",
)


def run(command, log):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    log.write_text(result.stdout + result.stderr)
    return {"command": command, "exitCode": result.returncode, "log": log.name}


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Package embedding prerequisites; this does not deliver or qualify an embedded terminal component.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--build-observations", type=Path)
    parser.add_argument("--engine-products", type=Path)
    parser.add_argument("--engine-build-log", type=Path, action="append", default=[])
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    steps = {}
    steps["fixtureBuild"] = run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                  "-mmacosx-version-min=13.0", "tests/zterm2_embedded/fixture.c",
                                  "-o", str(output / "fixture")], output / "fixture-build.log")
    steps["fixtureTests"] = run([sys.executable, "-B", "-m", "unittest", "discover", "-s",
                                  "tests/zterm2_embedded", "-v"], output / "fixture-tests.log")
    steps["boundaryAudit"] = run([sys.executable, "-B", "tools/zterm2_embedded/audit_boundary.py",
                                   str(output / "boundary.json"), "--probe-import"], output / "boundary-audit.log")
    for relative in SOURCE_FILES:
        destination = output / "sources" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    for source, destination in (("COPYING", "COPYING"), ("LICENSE", "LICENSE"),
                                ("ThirdParty/Licenses.txt", "ThirdParty-Licenses.txt")):
        shutil.copyfile(ROOT / source, output / destination)
    with tarfile.open(output / "prerequisite-sources.tar.gz", "w:gz") as archive:
        archive.add(output / "sources", arcname="sources")
    patch = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--", *SOURCE_FILES], cwd=ROOT)
    (output / "prerequisites.patch").write_bytes(patch)
    report = {
        "schema": "zterm2-embedded-prerequisites-1",
        "artifactVersion": "0.1.0-embedded-study.1",
        "kind": "prerequisites-and-boundary-study-not-a-terminal-component",
        "sourceHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "steps": steps,
        "m1": {"passed": False, "componentImplemented": False, "distinctHostBuilt": False,
               "realEngineEmbedded": False, "uiQualified": False, "vmAssigned": False},
        "scopeOfFixtureTests": "Standalone fixture under OS PTYs. Unicode and keyboard/mouse/paste escape sequences are byte-level tests; no zterm2 renderer, focus, real keyboard mapping, Retina rendering or GPU teardown is exercised.",
        "remainingManualReception": ["IME", "Option and Command key mapping", "native/terminal focus",
                                     "rich copy", "accessibility", "Retina and multiple displays"],
        "candidatePatchApplied": False,
        "componentVersion": None,
        "engineWorkingTreeChanges": subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).splitlines(),
        "apiProposal": "sources/embedded/contract-proposal.json",
        "binary": {"fixtureOnly": True, "architecture": subprocess.check_output(["uname", "-m"], text=True).strip(),
                   "compiler": subprocess.check_output(["clang", "--version"], text=True).splitlines()[0]},
    }
    boundary_path = output / "boundary.json"
    if boundary_path.is_file():
        boundary = json.loads(boundary_path.read_text())
        report["headerImportProbeExitCode"] = boundary["view_import_probe"]["exit_code"]
    if arguments.build_observations:
        observations = json.loads(arguments.build_observations.read_text())
        (output / "engine-build-observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n")
        report["engineBuildObservations"] = "engine-build-observations.json"
    if arguments.engine_products:
        products = arguments.engine_products.resolve()
        report["engineStaticLibraries"] = {}
        for name in ("libiTerm2Shared.a", "libiTerm2SharedARC.a"):
            library = products / name
            report["engineStaticLibraries"][name] = {
                "path": str(library), "sha256": file_digest(library), "bytes": library.stat().st_size,
                "architectures": subprocess.check_output(["lipo", "-archs", str(library)], text=True).strip(),
                "distributedInThisArtifact": False,
            }
    if arguments.engine_build_log:
        observations = []
        for log in arguments.engine_build_log:
            lines = log.read_text(errors="replace").splitlines()
            command_position = next((position for position, line in enumerate(lines)
                                     if line == "Command line invocation:"), None)
            observations.append({"path": str(log), "sha256": file_digest(log),
                                 "successMarkerObserved": "** BUILD SUCCEEDED **" in lines,
                                 "invocation": lines[command_position + 1] if command_position is not None else None})
        report["engineBuildLogs"] = observations
    dependency_lock = ROOT / "submodules/railroad_dsl/Cargo.lock"
    if dependency_lock.is_file():
        shutil.copyfile(dependency_lock, output / "observed-railroad-Cargo.lock")
    (output / "qualification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    manifest = {str(path.relative_to(output)): file_digest(path)
                for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "manifest.sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output": str(output), "m1Passed": False,
                      "prerequisiteCommandsPassed": all(step["exitCode"] == 0 for step in steps.values())}))
    return 0 if all(step["exitCode"] == 0 for step in steps.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
