import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description="Compile the real-engine view prototype; no host launch or M1 qualification.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--products", type=Path, required=True)
    parser.add_argument("--header-map", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = ["xcrun", "swiftc", "-emit-object", "-emit-module", "-warnings-as-errors", "-swift-version", "5",
               "-module-name", "ZTermEmbeddedPrototype", "-target", "arm64-apple-macos13.0",
               "-module-cache-path", str(output / "modules"), "-I", str(arguments.products.resolve()),
               "-F", str(arguments.products.resolve()), "-import-objc-header", str(ROOT / "embedded/InternalBridge.h"),
               "-Xcc", "-I" + str(arguments.header_map.resolve()),
               "-Xcc", "-iquote", "-Xcc", str(ROOT / "sources/proto"),
               "-Xcc", "-iquote", "-Xcc", str(ROOT / "ThirdParty"),
               "-Xcc", "-I" + str(ROOT / "ThirdParty/ProtobufRuntime")]
    for directory in ("ThirdParty", "BetterFontPicker", "ColorPicker", "SearchableComboListView"):
        command.extend(["-F", str(ROOT / directory)])
    directories = sorted({path.parent for folder in ("sources", "ThirdParty")
                          for path in (ROOT / folder).rglob("*.h")})
    for directory in directories:
        command.extend(["-Xcc", "-iquote", "-Xcc", str(directory)])
    source = ROOT / "embedded/TerminalViewPrototype.swift"
    command.extend(["-emit-objc-header", "-emit-objc-header-path", str(output / "ZTermEmbedded.h"),
                    "-emit-module-path", str(output / "ZTermEmbeddedPrototype.swiftmodule"),
                    "-o", str(output / "TerminalViewPrototype.o"), str(source)])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    (output / "compile.log").write_text(result.stdout + result.stderr)
    report = {"command": command, "exitCode": result.returncode,
              "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "linked": False, "executed": False, "m1Qualified": False}
    (output / "compile.json").write_text(json.dumps(report, indent=2) + "\n")
    print(result.stdout + result.stderr)
    print(json.dumps({"output": str(output), "exitCode": result.returncode, "m1Qualified": False}))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
