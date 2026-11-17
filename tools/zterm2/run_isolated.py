import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description="Compile and run bounded isolated texture tests and A/B measurements.")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    sources = [root / "sources/MetalRenderer/Renderers/ZTermKittyTexturePipeline.swift",
               root / "tests/zterm2/TextureHarness.swift"]
    shader = root / "sources/MetalRenderer/Shaders/KittyImage.metal"
    with tempfile.TemporaryDirectory(prefix="zterm2-isolated-") as directory:
        binary = Path(directory) / "texture-harness"
        intermediate = Path(directory) / "kitty.air"
        library = Path(directory) / "kitty.metallib"
        subprocess.run(["xcrun", "-sdk", "macosx", "metal", "-c", str(shader), "-o", str(intermediate)], check=True)
        subprocess.run(["xcrun", "metallib", str(intermediate), "-o", str(library)], check=True)
        subprocess.run(["xcrun", "swiftc", "-O", "-warnings-as-errors", "-module-cache-path",
                        directory + "/modules", *map(str, sources), "-o", str(binary)], check=True)
        for mode in ["test", "legacy", "gpu"]:
            command = [sys.executable, "-B", str(root / "tests/zterm2/guarded_run.py"),
                       "--seconds", "20", "--mib", "512", "--", str(binary), mode, str(library)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=25)
            (args.output / (mode + ".json")).write_text(result.stdout)
            if result.returncode:
                raise RuntimeError("isolated run failed: " + mode + " " + result.stdout + result.stderr)
    manifest = dict(schema=1, kind="isolated-microbenchmark", qualified_application=False,
                    source_sha256={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in [*sources, shader]},
                    limits=["Three-second cold processes, no endurance claim.",
                            "CPU includes synthetic frame generation and texture readback.",
                            "Legacy model uses NSBitmap color conversion and new textures; not the complete upstream application.",
                            "GPU/WindowServer and shared allocations are not attributed exclusively.",
                            "50 ms external sampling can miss shorter memory peaks."])
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
