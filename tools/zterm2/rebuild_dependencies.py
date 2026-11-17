import argparse
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description="Rebuild pinned upstream binary dependencies locally; never install an app.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    prefixes = [Path("/opt/homebrew"), Path("/usr/local")]
    rustup = next((prefix / "opt/rustup/bin/rustup" for prefix in prefixes
                   if (prefix / "opt/rustup/bin/rustup").is_file()), None)
    if rustup is None:
        raise SystemExit("rustup missing; install required CMake, Automake, pkg-config, rustup and cbindgen tools first")
    path = os.pathsep.join([str(rustup.parent), *[str(prefix / "bin") for prefix in prefixes], os.environ["PATH"]])
    missing = [name for name in ["cmake", "automake", "pkg-config", "cbindgen"] if not shutil.which(name, path=path)]
    if missing:
        raise SystemExit("Missing build tools: " + ", ".join(missing))
    tools = root / ".zterm2-tools"
    environment = dict(os.environ, PATH=path, RUSTUP_HOME=str(tools / "rustup"),
                       CARGO_HOME=str(tools / "cargo"), CARGO_BUILD_JOBS="2")

    def run(command, **options):
        return subprocess.run(list(map(str, command)), cwd=root, env=environment, check=True, **options)

    if args.fetch:
        run(["git", "submodule", "update", "--init", "--recursive", "--depth", "1", "--jobs", "2"])
        run([rustup, "toolchain", "install", "stable", "--profile", "minimal"])
    version = run([rustup, "run", "stable", "rustc", "--version"], capture_output=True, text=True).stdout
    if not version.startswith("rustc 1.99.1 "):
        raise SystemExit("Dependency baseline requires rustc 1.99.1; qualify a toolchain change explicitly")
    if args.check:
        print("Build tools available; Rust baseline matches. No application installed or built.")
        return
    run(["tools/check-submodule-cleanliness"])
    lock = root / "submodules/railroad_dsl/Cargo.lock"
    pinned_lock = (root / "tools/zterm2/locks/railroad-Cargo.lock").read_bytes()
    if lock.exists() and lock.read_bytes() != pinned_lock:
        raise SystemExit("Existing Cargo.lock differs; preserving it")
    lock.write_bytes(pinned_lock)
    if args.fetch:
        fetch_environment = dict(environment, CARGO_HTTP_PROXY="", CARGO_NET_RETRY="0")
        subprocess.run([str(rustup.parent / "cargo"), "fetch", "--locked", "--manifest-path", str(lock.with_name("Cargo.toml"))],
                       cwd=root, env=fetch_environment, check=True)
    binary_tools = tools / "bin"
    binary_tools.mkdir(parents=True, exist_ok=True)
    scripts = {
        "xcodebuild": '#!/bin/sh\nif test "$1" = "-version"; then exec /usr/bin/xcodebuild "$@"; fi\nexec /usr/bin/xcodebuild -jobs 2 "$@"\n',
        "sysctl": '#!/bin/sh\nif test "$1" = "-n" && test "$2" = "hw.ncpu"; then printf "2\\n"; exit 0; fi\nexec /usr/sbin/sysctl "$@"\n'
    }
    for name, text in scripts.items():
        target = binary_tools / name
        target.write_text(text)
        target.chmod(0o755)
    environment.update(PATH=str(binary_tools) + os.pathsep + path, CARGO_NET_OFFLINE="true")
    targets = ["fatlibsixel", "CoreParse", "NMSSH", "bindeps", "libgit2", "sparkle", "librailroad_dsl", "pwmadapters"]
    run(["/usr/bin/sandbox-exec", "-f", "deps.sb", "/usr/bin/make", "-j1", *targets,
         "BUILD_DIR=" + str(root / ".zterm2-build/dependencies"), "PATH=" + environment["PATH"], "RUSTUP=" + str(rustup)])
    actual = run(["/usr/bin/xcodebuild", "-version"], capture_output=True).stdout
    (root / "last-xcode-version").write_bytes(actual)
    print("Binary dependencies rebuilt. Checked-in SFSymbol generated source retained without regeneration.")


if __name__ == "__main__":
    main()
