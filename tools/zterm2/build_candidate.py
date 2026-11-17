import argparse
from pathlib import Path
import plistlib
import subprocess


def main():
    parser = argparse.ArgumentParser(description="Build isolated zterm2; never install or launch.")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with (root / "plists/dev-iTerm2.plist").open("rb") as source:
        info = plistlib.load(source)
    info.update(CFBundleIdentifier="org.henarkon.zterm2", CFBundleName="zterm2",
                CFBundleDisplayName="zterm2", CFBundleExecutable="zterm2",
                CFBundleShortVersionString="0.1.2", CFBundleVersion="0.1.2",
                ZTermBackgroundTestsSupported=True,
                SUEnableAutomaticChecks=False, SUAutomaticallyUpdate=False)
    for key in ["SUFeedURL", "SUFeedURLForTesting", "SUFeedURLForFinal", "SUPublicEDKey",
                "CFBundleURLTypes", "CFBundleDocumentTypes", "NSServices"]:
        info.pop(key, None)
    with (root / "plists/zterm2.plist").open("wb") as destination:
        plistlib.dump(info, destination)
    if args.prepare_only:
        return
    output = root / ".zterm2-build"
    subprocess.run(["xcodebuild", "-project", "iTerm2.xcodeproj", "-scheme", "iTerm2",
                    "-configuration", "Development", "-destination", "platform=macOS",
                    "-derivedDataPath", str(output), "-jobs", "2",
                    "-disableAutomaticPackageResolution", "-skipPackageUpdates",
                    "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "build"],
                   cwd=root, check=True)


if __name__ == "__main__":
    main()
