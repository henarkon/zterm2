import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


FEED = "https://iterm2.com/appcasts/final_modern.xml"
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
MAX_BYTES = 2 * 1024 * 1024


def stable_releases(raw):
    if len(raw) > MAX_BYTES or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("invalid or oversized appcast")
    releases = {}
    for item in ET.fromstring(raw).findall("./channel/item"):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        version = (enclosure.get(SPARKLE + "shortVersionString") or
                   item.findtext(SPARKLE + "shortVersionString") or enclosure.get(SPARKLE + "version"))
        if not version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            continue
        url = enclosure.get("url", "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "iterm2.com":
            raise ValueError("unexpected release download origin")
        releases[version] = dict(version=version, tag="v" + version,
                                 published=item.findtext("pubDate"), download_url=url)
    if not releases:
        raise ValueError("no official stable releases found")
    return sorted(releases.values(), key=lambda entry: tuple(map(int, entry["version"].split("."))))


def main():
    parser = argparse.ArgumentParser(description="Detect official stable releases; never integrate or install.")
    parser.add_argument("--feed-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    pinned = json.loads((root / "zterm2.upstream.json").read_text())
    if args.feed_file:
        with args.feed_file.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    else:
        request = urllib.request.Request(FEED, headers={"User-Agent": "zterm2-release-observer/1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            if urllib.parse.urlparse(response.url).hostname != "iterm2.com":
                raise ValueError("unexpected appcast redirect")
            raw = response.read(MAX_BYTES + 1)
    releases = stable_releases(raw)
    latest = releases[-1]
    newer = tuple(map(int, latest["version"].split("."))) > tuple(map(int, pinned["stable"]["version"].split(".")))
    result = dict(schema=1, source=FEED, source_sha256=hashlib.sha256(raw).hexdigest(),
                  checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  pinned=pinned["stable"], latest_detected=latest, new_stable_detected=newer,
                  integration=dict(state="not-requested", base=pinned["integration_base"]),
                  qualification=dict(state="pending", qualified_release=None),
                  automatic_installation=False, upstream_merge_requests=False)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as destination:
            destination.write(text)
    print(text, end="")


if __name__ == "__main__":
    main()
