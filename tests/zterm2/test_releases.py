import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location("check_releases", Path(__file__).parents[2] / "tools/zterm2/check_releases.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def feed(versions):
    items = "".join(f'<item><enclosure sparkle:shortVersionString="{version}" url="https://iterm2.com/downloads/test.zip"/></item>' for version in versions)
    return ('<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel>' + items + '</channel></rss>').encode()


class ReleasesTest(unittest.TestCase):
    def test_numeric_order_and_prerelease_exclusion(self):
        result = MODULE.stable_releases(feed(["3.7.9", "3.7.10", "3.8.0beta1", "3.7.10"]))
        self.assertEqual([entry["version"] for entry in result], ["3.7.9", "3.7.10"])

    def test_untrusted_download_origin(self):
        with self.assertRaises(ValueError):
            MODULE.stable_releases(feed(["3.7.7"]).replace(b"iterm2.com", b"example.com"))

    def test_actual_appcast_version_attribute(self):
        raw = feed(["3.7.7"]).replace(b"shortVersionString", b"version")
        self.assertEqual(MODULE.stable_releases(raw)[0]["version"], "3.7.7")

    def test_xml_entity_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.stable_releases(b'<!DOCTYPE x [<!ENTITY x "test">]>' + feed(["3.7.7"]))

    def test_empty_and_oversized(self):
        for raw in [feed(["3.8.0beta1"]), b" " * (MODULE.MAX_BYTES + 1)]:
            with self.assertRaises(ValueError):
                MODULE.stable_releases(raw)


if __name__ == "__main__":
    unittest.main()
