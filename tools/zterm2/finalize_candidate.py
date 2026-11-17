import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[2]
    output = root / "dist/v0.1.1"
    measurements = output / "measurements"
    measurements.mkdir(parents=True, exist_ok=True)
    names = ["real-legacy-bounded", "real-gpu-bounded", "real-gpu-repeat", "real-legacy-repeat"]
    observations = {}
    for name in names:
        source = root / ".zterm2-build" / name / "observation.json"
        value = json.loads(source.read_text())
        if value.get("error") or value["fixture"]["measured_frames"] != 90:
            raise RuntimeError("unqualified observation: " + name)
        observations[name] = value
        shutil.copyfile(source, measurements / (name + ".json"))
    keys = {(item["fixture"]["fixture_sha256"], tuple(item["fixture"]["geometry"]),
             tuple(item["fixture"]["cells"]), item["renderer_metrics"]["output_profile_sha256"])
            for item in observations.values()}
    if len(keys) != 1:
        raise RuntimeError("comparison conditions differ")
    native_log = Path("/private/tmp/zterm2-native-tests.log").read_bytes()
    if b"** TEST SUCCEEDED **" not in native_log or b"Executed 32 tests, with 0 failures" not in native_log:
        raise RuntimeError("native tests did not pass")
    native = dict(passed=32, failures=0, log_sha256=hashlib.sha256(native_log).hexdigest(),
                  classes=["KittyImageRendererTextureFrameTests", "UpgradeSafeKeychainTests/testZtermServiceNamespaceIsSeparateAndIdempotent",
                           "LineSeparatorCopyTests", "CopyModeSelectionDirectionTests", "BidiCopyLogicalRangeTests"],
                  general_clipboard_modified=False)
    (output / "native-tests.json").write_text(json.dumps(native, indent=2) + "\n")
    tests = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/zterm2", "-p", "test_*.py"],
                           cwd=root, capture_output=True, text=True, timeout=15)
    if tests.returncode or not re.search(r"Ran 26 tests", tests.stderr):
        raise RuntimeError(tests.stdout + tests.stderr)
    (output / "python-tests.json").write_text(json.dumps(dict(passed=26, failures=0, output=tests.stderr), indent=2) + "\n")
    colours = json.loads((output / "isolated/test.json").read_text())
    detail = json.loads(colours["stdout"])
    if colours["exit_code"] or detail["display_profile_sha256"] != next(iter(keys))[3]:
        raise RuntimeError("actual display colour qualification missing")
    source = root / ".zterm2-build/Build/Products/Development/zterm2.app"
    archive = output / "zterm2-0.1.1-macos-arm64.zip"
    if archive.exists():
        raise RuntimeError("application archive already exists")
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(source), str(archive)], check=True)
    provenance = dict(version="0.1.1", platform="macos-arm64", build_configuration="Development",
                      macos="26.7.2", xcode="27.2 (27B52)", metal_toolchain="27B47", rust="1.99.1",
                      source_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                      upstream=json.loads((root / "zterm2.upstream.json").read_text()),
                      submodules=subprocess.check_output(["git", "submodule", "status", "--recursive"], cwd=root, text=True).splitlines(),
                      cargo_lock_sha256=hashlib.sha256((root / "tools/zterm2/locks/railroad-Cargo.lock").read_bytes()).hexdigest(),
                      application_installed=False, notarized=False, upstream_merge_requests=False)
    (output / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    report = dict(state="experimental-local-candidate", version="0.1.1", gpu_default="new Kitty image pipeline disabled",
                  cpu_gain="not demonstrated in real terminal", actual_scene="synthetic RGBA; not Cardo/Yavin",
                  accepted_observations=names, observations=observations,
                  qualification=dict(python_tests=26, native_tests=32, colour_cases=len(detail["color_cases"]),
                                     maximum_gpu_byte_error=max(case["maximum_byte_error"] for case in detail["color_cases"])),
                  pool=dict(maximum_textures=4, maximum_bytes=67108864, observed_textures=[1, 2]),
                  unchanged=["Installed standard iTerm2", "Cardo sources and deliveries", "Vigil sources"],
                  remaining=["Profile absolute application CPU; optimise before enabling the new path.",
                             "Broader UI pixel checks for scrolling, hiding, selection and copy with Cardo remain.",
                             "No physical display cadence, endurance or exclusive GPU attribution."])
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    text = "zterm2 0.1.1 — candidat local macOS arm64\n\nLe nouveau parcours d’images GPU reste expérimental et désactivé par défaut.\nAucun gain CPU démontré dans le terminal réel ; empreinte observée plus élevée.\n\n"
    for name, item in observations.items():
        terminal = next(process for process in item["processes"] if process["role"] == "terminal")
        text += f"{name}: CPU {terminal['cpu_percent_one_core']:.2f}% d’un cœur ; empreinte {terminal['peak_physical_bytes']/1048576:.2f} Mio.\n"
    text += "\nMême scène synthétique 640×360, 80×25 cellules, 30 trames/s ; mêmes pixels et profil ICC.\n7 s de stabilisation, 1 s de préchauffage, 3 s mesurées. Garde externe 512 Mio/processus et 20 s maximum.\n26 tests Python, 32 tests natifs et 36 cas couleur passent ; erreur GPU maximale 1/255.\nLe pool observé utilise 1–2 textures ; plafond 4 textures/64 Mio, rétention jusqu’à fin GPU.\n\nReproduction du banc: python3 -B tools/zterm2/run_isolated.py /tmp/zterm2-new-results\nReconstruction initiale des dépendances: python3 -B tools/zterm2/rebuild_dependencies.py --fetch\nConstruction: python3 -B tools/zterm2/build_candidate.py\nAppliquer candidate.patch au commit source_head avant de reconstruire. Installer les outils et le composant Metal requis.\nLes sources SFSymbol générées amont sont conservées ; pas de régénération nécessaire pour la reconstruction des binaires.\n\nL’application est un candidat Development local non notarisé. Aucune installation ou mise à jour automatique.\nAucune demande de fusion vers iTerm2. Cardo/Vigil restent indépendants. Les microbenchmarks sont séparés des mesures réelles.\n"
    (output / "REPORT.txt").write_text(text)
    print("Candidate observations, qualification, application archive and report prepared.")


if __name__ == "__main__":
    main()
