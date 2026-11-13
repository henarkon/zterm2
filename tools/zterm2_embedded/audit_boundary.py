import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SEEDS = (
    "sources/TerminalView/SessionView.m",
    "sources/PTYSession/PTYSession.m",
    "sources/TerminalView/PTYTextView.m",
    "sources/Tasks/PTYTask.m",
    "sources/VT100/VT100Parser.m",
    "sources/VT100/VT100Terminal.m",
    "sources/VT100Screen/VT100Screen.m",
    "sources/MetalRenderer/iTermMetalDriver.m",
)
IMPORT = re.compile(r'^\s*#\s*(?:import|include)\s*"([^"]+)"', re.MULTILINE)
COUPLING = re.compile(r"sharedInstance|\bNSApp\b|mainBundle|newDefaultLibrary|iTermUserDefaults|standardUserDefaults")


def inspect_sources(repository):
    files = sorted(path for folder in ("sources", "ThirdParty")
                   for path in (repository / folder).rglob("*")
                   if path.is_file() and path.suffix in (".h", ".m", ".mm", ".swift", ".c", ".cc"))
    by_name = collections.defaultdict(list)
    for path in files:
        by_name[path.name].append(path)
    report = {}
    for seed in SEEDS:
        pending = [repository / seed]
        visited = set()
        unresolved = set()
        ambiguous = {}
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            content = path.read_text(errors="replace")
            for name in IMPORT.findall(content):
                relative = path.parent / name
                candidates = [relative] if relative.is_file() else by_name.get(Path(name).name, [])
                if len(candidates) == 1:
                    pending.append(candidates[0])
                elif not candidates:
                    unresolved.add(name)
                else:
                    ambiguous[name] = [str(candidate.relative_to(repository)) for candidate in candidates]
            if path.suffix == ".h":
                for suffix in (".m", ".mm", ".swift", ".c", ".cc"):
                    counterpart = path.with_suffix(suffix)
                    if counterpart.is_file():
                        pending.append(counterpart)
        source = (repository / seed).read_text(errors="replace")
        report[seed] = {
            "direct_quoted_imports": IMPORT.findall(source),
            "conservative_local_file_count": len(visited),
            "conservative_local_files": sorted(str(path.relative_to(repository)) for path in visited),
            "unresolved_imports": sorted(unresolved),
            "ambiguous_imports": ambiguous,
            "coupling_evidence": [
                {"line": number, "text": line.strip()}
                for number, line in enumerate(source.splitlines(), 1) if COUPLING.search(line)
            ],
        }
    return report


def inspect_targets(repository):
    raw = subprocess.check_output(["plutil", "-convert", "json", "-o", "-",
                                   str(repository / "iTerm2.xcodeproj/project.pbxproj")])
    objects = json.loads(raw)["objects"]
    targets = []
    for value in objects.values():
        if value.get("isa") != "PBXNativeTarget":
            continue
        phases = []
        for phase_id in value.get("buildPhases", []):
            phase = objects[phase_id]
            refs = [objects.get(objects.get(file_id, {}).get("fileRef"), {})
                    for file_id in phase.get("files", [])]
            phases.append({"kind": phase["isa"], "name": phase.get("name"),
                           "files": [reference.get("path", reference.get("name")) for reference in refs]})
        targets.append({"name": value["name"], "product_type": value["productType"],
                        "dependencies": [objects.get(objects[identifier].get("target"), {}).get("name")
                                         for identifier in value.get("dependencies", [])], "phases": phases})
    return targets


def probe_view_import(repository):
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    directories = sorted({path.parent for folder in ("sources", "ThirdParty")
                          for path in (repository / folder).rglob("*.h")})
    command = ["xcrun", "clang", "-x", "objective-c", "-fsyntax-only", "-fblocks",
               "-fmodules", "-Werror", "-mmacosx-version-min=13.0", "-isysroot", sdk,
               "-fmodules-cache-path=/private/tmp/zterm2-embedded-probe-modules",
               "-F", str(repository / "ThirdParty"), "-iquote", str(repository / "sources/proto"),
               "-iquote", str(repository / "ThirdParty"), "-iquote", str(repository / "sources")]
    for directory in directories:
        command.extend(["-iquote", str(directory)])
    command.extend(["-I", str(repository / "ThirdParty/ProtobufRuntime")])
    command.append("-")
    source = '#import <Cocoa/Cocoa.h>\n#import "SessionView.h"\n'
    result = subprocess.run(command, input=source, capture_output=True, text=True, timeout=120)
    return {"kind": "external-objective-c-header-import-only", "command": command,
            "stdin": source, "exit_code": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr, "linked": False, "executed": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--probe-import", action="store_true")
    arguments = parser.parse_args()
    repository = arguments.repository.resolve()
    report = {
        "schema": "zterm2-embedding-boundary-audit-1",
        "source_head": subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip(),
        "method": "Quoted local includes plus same-directory implementation counterparts; conservative source reachability, not a compiler, linker or runtime dependency proof. Swift imports and dynamic references are not traversed; ambiguous names are reported, not chosen.",
        "seeds": inspect_sources(repository),
        "targets": inspect_targets(repository),
        "submodules": subprocess.check_output(["git", "-C", str(repository), "ls-tree", "-r", "HEAD"], text=True).splitlines(),
        "source_sha256": {seed: hashlib.sha256((repository / seed).read_bytes()).hexdigest() for seed in SEEDS},
        "qualification": {"component_built": False, "embedded_view_exercised": False, "m1_passed": False},
    }
    report["submodules"] = [line for line in report["submodules"] if line.startswith("160000 ")]
    if arguments.probe_import:
        report["view_import_probe"] = probe_view_import(repository)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({seed: data["conservative_local_file_count"] for seed, data in report["seeds"].items()}))


if __name__ == "__main__":
    main()
