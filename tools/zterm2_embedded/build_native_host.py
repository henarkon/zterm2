import argparse
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

from compile_view_prototype import ROOT


def run(command, output, name):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    (output / (name + ".log")).write_text(result.stdout + result.stderr)
    if result.returncode:
        print((result.stdout + result.stderr)[-10000:])
        raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Build a separate native host using the real zterm2 engine. Does not launch it.")
    parser.add_argument("output", type=Path)
    parser.add_argument("--products", type=Path, required=True)
    parser.add_argument("--header-map", type=Path, required=True)
    parser.add_argument("--engine-link-log", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    products = arguments.products.resolve()
    engine_app = products / "iTerm2.app"
    component = output / "component"
    run([sys.executable, "-B", str(ROOT / "tools/zterm2_embedded/compile_view_prototype.py"), str(component),
         "--products", str(products), "--header-map", str(arguments.header_map.resolve())], output, "component-compile")
    invocations = [shlex.split(line.strip()) for line in arguments.engine_link_log.read_text().splitlines()
                   if " -filelist " in line and " -o " in line and "/Contents/MacOS/iTerm2" in line and " -c " not in line]
    if not invocations:
        raise SystemExit("No application linker invocation found in the successful engine build log")
    original = invocations[-1]
    linker = []
    position = 0
    while position < len(original):
        token = original[position]
        if token in ("-filelist", "-o"):
            position += 2
        elif token == "-Xlinker" and original[position + 1] in ("-add_ast_path", "-object_path_lto", "-dependency_info"):
            position += 4
        elif token == "-rdynamic":
            position += 1
        else:
            linker.append(token)
            position += 1
    library = component / "libZTermEmbedded.dylib"
    component_link = linker + ["-dynamiclib", "-Wl,-install_name,@rpath/libZTermEmbedded.dylib",
                                "-Wl,-current_version,0.2.0", "-Wl,-compatibility_version,0.2.0",
                                str(component / "TerminalViewPrototype.o"), "-o", str(library)]
    run(component_link, output, "component-link")
    include = component / "include"
    include.mkdir()
    shutil.copy2(ROOT / "embedded/ZTermEmbedded.h", include / "ZTermEmbedded.h")
    (include / "module.modulemap").write_text('module ZTermEmbedded {\n  header "ZTermEmbedded.h"\n  export *\n}\n')
    app = output / "ZTermEmbeddedDemo.app"
    contents = app / "Contents"
    executable_dir = contents / "MacOS"
    executable_dir.mkdir(parents=True)
    shutil.copytree(engine_app / "Contents/Frameworks", contents / "Frameworks", symlinks=True)
    shutil.copytree(engine_app / "Contents/Resources", contents / "Resources", symlinks=True,
                    ignore=shutil.ignore_patterns("InfoPlist.strings"))
    if (engine_app / "Contents/XPCServices").is_dir():
        shutil.copytree(engine_app / "Contents/XPCServices", contents / "XPCServices", symlinks=True)
    for helper in ("ShellLauncher", "iTermServer"):
        shutil.copy2(engine_app / "Contents/MacOS" / helper, executable_dir / helper)
    shutil.copy2(library, contents / "Frameworks" / library.name)
    run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", "-mmacosx-version-min=13.0",
         "tests/zterm2_embedded/fixture.c", "-o", str(contents / "Resources/fixture")], output, "fixture-build")
    host_compile = ["xcrun", "swiftc", "-emit-object", "-warnings-as-errors", "-swift-version", "5",
                    "-module-name", "ZTermEmbeddedDemo", "-target", "arm64-apple-macos13.0",
                    "-module-cache-path", str(output / "host-modules"), "-parse-as-library", "-I", str(include),
                    "-o", str(output / "NativeHost.o"), str(ROOT / "tests/zterm2_embedded/NativeHost.swift")]
    run(host_compile, output, "host-compile")
    executable = executable_dir / "ZTermEmbeddedDemo"
    host_link = ["xcrun", "swiftc", "-target", "arm64-apple-macos13.0", str(output / "NativeHost.o"),
                 "-L", str(component), "-lZTermEmbedded", "-F", str(contents / "Frameworks"),
                 "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks", "-o", str(executable)]
    run(host_link, output, "host-link")
    info = {"CFBundleIdentifier": "org.henarkon.zterm2.embedded.demo", "CFBundleExecutable": "ZTermEmbeddedDemo",
            "CFBundleName": "ZTermEmbeddedDemo", "CFBundlePackageType": "APPL", "CFBundleVersion": "0.2.0",
            "CFBundleShortVersionString": "0.2.0", "LSMinimumSystemVersion": "13.0",
            "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
            "NSDisablePersistence": True, "NSQuitAlwaysKeepsWindows": False}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    run(["codesign", "--force", "--deep", "--sign", "-", str(app)], output, "adhoc-sign")
    run(["codesign", "--verify", "--deep", "--strict", str(app)], output, "signature-verify")
    run(["otool", "-L", str(executable), str(contents / "Frameworks/libZTermEmbedded.dylib")], output, "linked-dependencies")
    (output / "build.json").write_text(json.dumps({"app": str(app), "component": str(library),
                                                  "componentLinkCommand": component_link, "hostCompileCommand": host_compile,
                                                  "hostLinkCommand": host_link,
                                                  "built": True, "executed": False, "m1Qualified": False}, indent=2) + "\n")
    print(json.dumps({"app": str(app), "built": True, "executed": False, "m1Qualified": False}))


if __name__ == "__main__":
    main()
