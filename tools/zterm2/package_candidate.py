import hashlib
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="0.1.2")
    args = parser.parse_args()
    if not all(part.isdigit() and str(int(part)) == part for part in args.version.split(".")) or len(args.version.split(".")) != 3:
        parser.error("expected a semantic version")
    root = Path(__file__).resolve().parents[2]
    output = root / ("dist/v" + args.version)
    output.mkdir(parents=True, exist_ok=True)
    sources = ["sources/MetalRenderer/Renderers/ZTermKittyTexturePipeline.swift",
               "sources/MetalRenderer/Shaders/KittyImage.metal", "sources/MetalRenderer/Shaders/iTermShaderTypes.h",
               "tests/zterm2/TextureHarness.swift", "tests/zterm2/guarded_run.py",
               "tools/zterm2/run_isolated.py", "zterm2.upstream.json", "COPYING", "LICENSE"]
    with tarfile.open(output / "isolated-sources.tar.gz", "x:gz") as archive:
        for relative in sources:
            archive.add(root / relative, arcname=relative, recursive=False)
    patch = subprocess.run(["git", "diff", "--binary", "HEAD", "--", ".gitignore", "AGENTS.md", "Makefile",
                            "deps.sb", "iTerm2.xcodeproj", "sources", "ModernTests", "tests/zterm2",
                            "tools/zterm2", "tools/run_tests.expect", "ThirdParty/SSKeychain", "ThirdParty/UKCrashReporter",
                            "interop", "zterm2.upstream.json"], cwd=root,
                           capture_output=True, check=True).stdout
    (output / "candidate.patch").write_bytes(patch)
    shutil.copyfile(root / "COPYING", output / "COPYING")
    shutil.copyfile(root / "LICENSE", output / "LICENSE")
    shutil.copyfile(root / "ThirdParty/Licenses.txt", output / "ThirdParty-Licenses.txt")
    manifest = dict(schema=1, version=args.version, state="isolated-application-candidate",
                    application_built=True, qualification_scope="targeted; not production",
                    application_qualified=False, gpu_default="disabled; ZTERM2_KITTY_GPU=1 opts in",
                    base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                    files={str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(output.rglob("*")) if path.is_file()},
                    reproduce_isolated="python3 -B tools/zterm2/run_isolated.py /tmp/zterm2-new-results",
                    build_application="python3 -B tools/zterm2/build_candidate.py",
                    license="Upstream GPL v2 or later, effectively GPLv3 with dependencies; notices retained.",
                    remaining=["Broader UI pixel qualification of scrolling/cropping, hiding and lifecycle remains.",
                               "GPU remains disabled by default; see report.json for measured scope and results.",
                               "Physical display latency, endurance and total GPU cost need measurement.",
                               "Fast LUT path is qualified only for tested sRGB input and output profiles; other inputs retain Core Image."])
    (output / "candidate-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
