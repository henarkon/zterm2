import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2] / "interop/v1"
FIXTURE = ROOT.parent / "inbox/cardo/911c11a72802102c190f3ce2372fa556ed14284436af8e1b2a6311a30493ce0c"
spec = importlib.util.spec_from_file_location("exchange_bridge", ROOT / "bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "export"
        bridge.export(FIXTURE, self.bundle)

    def rewrite(self, name, transform):
        path = self.bundle / name
        value = json.loads(path.read_text())
        transform(value)
        path.write_bytes(bridge.encode(value))
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][name] = bridge.digest(path.read_bytes())
        manifest_path.write_bytes(bridge.encode(manifest))

    def test_roundtrip_and_idempotent_receipt_does_not_qualify(self):
        first = bridge.receive(self.bundle, self.root / "inbox", "zterm2")
        second = bridge.receive(self.bundle, self.root / "inbox", "zterm2")
        self.assertEqual(first["decision"], "received-not-qualified")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["bundleSha256"], second["bundleSha256"])
        self.assertEqual((self.bundle / "performance.json").read_bytes(), (Path(first["path"]) / "performance.json").read_bytes())

    def test_changed_or_forged_contract_is_not_accepted_by_rehashing(self):
        self.rewrite("contract.json", lambda v: v.update(contractVersion="2.0.0"))
        with self.assertRaisesRegex(ValueError, "not locally accepted"):
            bridge.validate(self.bundle)

    def test_modified_payload_rejected_before_receiving(self):
        (self.bundle / "tests.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Checksum"):
            bridge.receive(self.bundle, self.root / "inbox", "zterm2")
        self.assertFalse((self.root / "inbox").exists())

    def test_unsupported_version_and_unknown_status_rejected(self):
        self.rewrite("capabilities.json", lambda v: v.update(schemaVersion=2))
        with self.assertRaisesRegex(ValueError, "schema version"):
            bridge.validate(self.bundle)

    def test_peer_cannot_smuggle_qualified_capability(self):
        self.rewrite("capabilities.json", lambda v: v["data"]["features"][0].update(status="qualified"))
        with self.assertRaisesRegex(ValueError, "Capability status"):
            bridge.validate(self.bundle)

    def test_path_escape_and_unknown_entries_rejected(self):
        p = self.bundle / "manifest.json"
        value = json.loads(p.read_text())
        value["files"]["../outside"] = "0" * 64
        p.write_bytes(bridge.encode(value))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            bridge.validate(self.bundle)

    def test_payload_symlink_rejected(self):
        path = self.bundle / "tests.json"
        outside = self.root / "elsewhere.json"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "non-symlink"):
            bridge.validate(self.bundle)

    def test_corrupt_existing_receipt_not_overwritten(self):
        result = bridge.receive(self.bundle, self.root / "inbox", "zterm2")
        receipt = Path(result["path"]) / "receipt.json"
        raw = b'{"decision":"qualified"}'
        receipt.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "receipt changed"):
            bridge.receive(self.bundle, self.root / "inbox", "zterm2")
        self.assertEqual(raw, receipt.read_bytes())

    def test_same_project_and_false_producer_rejected(self):
        with self.assertRaisesRegex(ValueError, "peer producer"):
            bridge.receive(self.bundle, self.root / "inbox", "cardo")
        self.rewrite("performance.json", lambda v: v.update(producer="zterm2"))
        with self.assertRaisesRegex(ValueError, "Producer mismatch"):
            bridge.validate(self.bundle)

    def test_unbounded_measurements_and_wrong_units_rejected(self):
        self.rewrite("performance.json", lambda v: v["data"]["cases"][0].update(seconds=3600))
        with self.assertRaisesRegex(ValueError, "Bounded"):
            bridge.validate(self.bundle)

    def test_duplicate_json_keys_and_non_finite_values_rejected(self):
        path = self.root / "input.json"
        path.write_text('{"schemaVersion":1,"schemaVersion":2}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            bridge.read(path)
        path.write_text('{"metric":NaN}')
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            bridge.read(path)

    def test_export_refuses_overwrite_and_unexpected_file_rejected(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            bridge.export(FIXTURE, self.bundle)
        (self.bundle / "run.sh").write_text("do not execute")
        with self.assertRaisesRegex(ValueError, "Unexpected bundle entries"):
            bridge.validate(self.bundle)

    def test_oversize_or_excessive_depth_rejected(self):
        path = self.root / "input.json"
        path.write_bytes(b" " * (bridge.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            bridge.read(path)
        path.write_text("[" * 20 + "0" + "]" * 20)
        with self.assertRaisesRegex(ValueError, "nesting"):
            bridge.read(path)

    def test_historical_results_cannot_be_called_matched(self):
        result = bridge.compare(self.bundle, self.bundle)
        self.assertEqual(result["status"], "not-comparable")
        self.assertTrue(any("fixtureSha256" in reason for reason in result["reasons"]))
        self.assertTrue(any("outputColourProfile" in reason for reason in result["reasons"]))

    def test_shared_colour_vectors_have_verified_pixels(self):
        self.rewrite("tests.json", lambda v: v["data"]["colourVectors"][0]["rgba"].__setitem__(0, 42))
        with self.assertRaisesRegex(ValueError, "Colour vector checksum"):
            bridge.validate(self.bundle)

    def test_boolean_cannot_be_misread_as_matching_numeric_condition(self):
        self.rewrite("performance.json", lambda v: v["data"]["comparisonKey"].update(audio=0))
        with self.assertRaisesRegex(ValueError, "boolean"):
            bridge.validate(self.bundle)

    def test_declared_match_does_not_qualify_and_scope_change_rejected(self):
        def known(value):
            value["data"]["comparisonKey"].update(fixtureSha256="a" * 64, hardware="test fixture hardware", outputColourProfile="sRGB")
        self.rewrite("performance.json", known)
        result = bridge.compare(self.bundle, self.bundle)
        self.assertEqual(result["status"], "matching-declared-conditions")
        self.assertTrue(result["qualification"].startswith("none"))
        other = self.root / "other"
        import shutil
        shutil.copytree(self.bundle, other)
        self.rewrite("performance.json", lambda v: v["data"].update(scope="isolated-renderer"))
        self.assertEqual(bridge.compare(self.bundle, other)["status"], "not-comparable")


if __name__ == "__main__":
    unittest.main()
