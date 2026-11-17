#!/usr/bin/env python3
"""Declarative Cardo/zterm2 exchanges. Never execute peer data or update a client."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile

PROTOCOL = "cardo-zterm2"
PROJECTS = {"cardo", "zterm2"}
DOCUMENTS = ("contract.json", "capabilities.json", "tests.json", "performance.json")
MAX_BYTES = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def bounded(value, depth=0):
    require(depth <= 12, "JSON nesting limit")
    if isinstance(value, dict):
        require(len(value) <= 128, "Object limit")
        for key, item in value.items():
            require(len(key) <= 128, "Key limit")
            bounded(item, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= 256, "Array limit")
        for item in value:
            bounded(item, depth + 1)
    elif isinstance(value, str):
        require(len(value) <= 16384, "String limit")
    elif isinstance(value, float):
        require(math.isfinite(value), "Non-finite number")


def read(path):
    require(not path.is_symlink() and path.is_file(), "Expected regular non-symlink file: " + path.name)
    require(path.stat().st_size <= MAX_BYTES, "File size limit")
    raw = path.read_bytes()
    require(len(raw) <= MAX_BYTES, "File size limit")
    value = json.loads(raw, object_pairs_hook=pairs)
    bounded(value)
    return raw, value


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def envelope(value):
    require(type(value) is dict and value.get("protocol") == PROTOCOL, "Wrong protocol")
    require(type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1, "Unsupported schema version")


def validate_document(name, value, project):
    envelope(value)
    require(set(value) == {"protocol", "schemaVersion", "kind", "producer", "release", "data"}, "Envelope fields")
    require(value["producer"] == project, "Producer mismatch")
    require(isinstance(value["release"], str) and 0 < len(value["release"]) < 128, "Release identity required")
    require(value["kind"] == name.removesuffix(".json"), "Document kind mismatch")
    data = value["data"]
    require(type(data) is dict, "Document data must be object")
    if name == "capabilities.json":
        require(data.get("role") == ("client" if project == "cardo" else "terminal"), "Capability role")
        require(isinstance(data.get("features"), list), "Capability features")
        ids = set()
        for feature in data["features"]:
            require(type(feature) is dict and set(feature) == {"id", "status", "condition", "evidence"}, "Feature fields")
            require(feature["id"] not in ids, "Duplicate feature")
            ids.add(feature["id"])
            require(feature["status"] in {"supported", "conditional", "unsupported", "planned"}, "Capability status")
            require(all(isinstance(feature[k], str) and feature[k] for k in ("id", "condition", "evidence")), "Capability evidence required")
    elif name == "tests.json":
        require(isinstance(data.get("cases"), list), "Test cases required")
        ids = set()
        for case in data["cases"]:
            require(type(case) is dict and set(case) == {"id", "scope", "procedure", "assertions", "evidence"}, "Test fields")
            require(case["id"] not in ids, "Duplicate test")
            ids.add(case["id"])
            require(isinstance(case["assertions"], list) and case["assertions"], "Test assertions required")
            require(all(isinstance(case[k], str) and case[k] for k in ("id", "scope", "procedure", "evidence")), "Test procedure/evidence required")
        for vector in data.get("colourVectors", []):
            require(set(vector) == {"id", "colourSpace", "alpha", "width", "height", "rgba", "sha256"}, "Colour vector fields")
            require(vector["colourSpace"] in {"sRGB", "DisplayP3"} and vector["alpha"] in {"straight", "premultiplied"}, "Colour vector policy")
            require(all(type(vector[k]) is int and 0 < vector[k] <= 32 for k in ("width", "height")), "Colour vector dimensions")
            require(type(vector["rgba"]) is list and len(vector["rgba"]) == vector["width"] * vector["height"] * 4, "Colour vector pixels")
            require(all(type(v) is int and 0 <= v <= 255 for v in vector["rgba"]), "Colour vector channels")
            require(digest(bytes(vector["rgba"])) == vector["sha256"], "Colour vector checksum")
    else:
        require(data.get("scope") in {"real-terminal", "isolated-renderer", "protocol-model", "pending"}, "Measurement scope")
        require(data.get("cpuConvention") == "100%=one logical core", "CPU units required")
        require(data.get("memoryConvention") == "physical-footprint-bytes", "Memory units required")
        require(isinstance(data.get("cases"), list) and isinstance(data.get("limits"), list) and data["limits"], "Measurement cases/limits")
        require(type(data.get("comparisonKey")) is dict and data["comparisonKey"], "Comparison conditions required")
        key = data["comparisonKey"]
        if data["scope"] != "pending":
            require(type(key.get("audio")) is bool, "Audio condition must be boolean")
            require(type(key.get("requestedFps")) in (int, float) and 0 < key["requestedFps"] <= 120, "Cadence condition required")
            require(type(key.get("qualityTier")) is int and key["qualityTier"] >= 0, "Quality condition required")
            for name in ("seekSeconds", "warmupSeconds"):
                require(type(key.get(name)) in (int, float) and key[name] >= 0, "Timing condition required")
            if key.get("cells") is not None:
                require(type(key["cells"]) is dict and set(key["cells"]) == {"width", "height"} and all(type(n) is int and n > 0 for n in key["cells"].values()), "Geometry condition")
            if key.get("fixtureSha256") is not None:
                require(type(key["fixtureSha256"]) is str and len(key["fixtureSha256"]) == 64 and all(c in "0123456789abcdef" for c in key["fixtureSha256"]), "Fixture SHA")
        for case in data["cases"]:
            require(type(case) is dict and isinstance(case.get("id"), str), "Measurement case identity")
            require(type(case.get("seconds")) in (int, float) and 0 < case["seconds"] <= 60, "Bounded measurement duration required")
            require(type(case.get("memoryGuardBytes")) is int and case["memoryGuardBytes"] > 0, "Independent memory guard required")
            require(isinstance(case.get("processes"), list) and case["processes"], "Process accounting required")
            for process in case["processes"]:
                require(set(process) == {"role", "release", "cpuPercent", "memoryPeakBytes"}, "Process metric fields")
                require(type(process["cpuPercent"]) in (int, float) and process["cpuPercent"] >= 0, "CPU metric")
                require(type(process["memoryPeakBytes"]) is int and process["memoryPeakBytes"] >= 0, "RAM metric")


def validate(path):
    require(path.is_dir() and not path.is_symlink(), "Expected bundle directory")
    manifest_raw, manifest = read(path / "manifest.json")
    envelope(manifest)
    require(set(manifest) == {"protocol", "schemaVersion", "producer", "files"}, "Manifest fields")
    project = manifest["producer"]
    require(project in PROJECTS, "Unknown producer")
    require(type(manifest["files"]) is dict and set(manifest["files"]) == set(DOCUMENTS), "Manifest file allowlist")
    require(set(p.name for p in path.iterdir()) <= set(DOCUMENTS) | {"manifest.json", "receipt.json"}, "Unexpected bundle entries")
    files = {}
    for name in DOCUMENTS:
        raw, value = read(path / name)
        require(digest(raw) == manifest["files"][name], "Checksum mismatch: " + name)
        if name == "contract.json":
            require(raw == Path(__file__).with_name("contract.json").read_bytes(), "Contract not locally accepted; review a versioned change")
        else:
            validate_document(name, value, project)
        files[name] = raw
    files["manifest.json"] = manifest_raw
    return manifest, files, digest(manifest_raw)


def publish(files, target):
    require(not target.exists() and not target.is_symlink(), "Destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".exchange-", dir=target.parent))
    try:
        for name, raw in files.items():
            (stage / name).write_bytes(raw)
        os.rename(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def export(profile, output):
    files = {"contract.json": Path(__file__).with_name("contract.json").read_bytes()}
    project = None
    for name in DOCUMENTS[1:]:
        raw, value = read(profile / name)
        project = project or value.get("producer")
        require(project in PROJECTS, "Unknown producer")
        validate_document(name, value, project)
        files[name] = raw
    manifest = {"protocol": PROTOCOL, "schemaVersion": 1, "producer": project,
                "files": {name: digest(raw) for name, raw in files.items()}}
    files["manifest.json"] = encode(manifest)
    publish(files, output)
    return {"status": "exported", "bundleSha256": validate(output)[2], "producer": project, "path": str(output)}


def receive(source, inbox, receiver):
    manifest, files, identity = validate(source)
    require(receiver in PROJECTS and receiver != manifest["producer"], "Expected peer producer")
    target = inbox / manifest["producer"] / identity
    receipt = {"protocol": PROTOCOL, "schemaVersion": 1, "receiver": receiver,
               "producer": manifest["producer"], "bundleSha256": identity,
               "decision": "received-not-qualified"}
    if target.exists():
        require(validate(target)[2] == identity, "Existing import changed")
        require(read(target / "receipt.json")[1] == receipt, "Existing receipt changed")
        return {**receipt, "reused": True, "path": str(target)}
    publish({**files, "receipt.json": encode(receipt)}, target)
    return {**receipt, "reused": False, "path": str(target)}


def compare(left, right):
    results = []
    for path in (left, right):
        manifest, files, identity = validate(path)
        results.append((manifest, json.loads(files["performance.json"])["data"], identity))
    a, b = results[0][1], results[1][1]
    required = {"fixtureSha256", "hardware", "outputColourProfile", "cells", "qualityTier", "requestedFps", "audio", "seekSeconds", "warmupSeconds", "platform"}
    reasons = []
    if a["scope"] != b["scope"] or a["scope"] == "pending":
        reasons.append("Measurement scopes differ or are pending")
    for key in sorted(required):
        av, bv = a["comparisonKey"].get(key), b["comparisonKey"].get(key)
        if av is None or bv is None or av == "unknown" or bv == "unknown":
            reasons.append("Unknown comparison condition: " + key)
        elif av != bv:
            reasons.append("Different comparison condition: " + key)
    if a["comparisonKey"] != b["comparisonKey"]:
        reasons.append("Comparison keys differ")
    if not a["cases"] or not b["cases"]:
        reasons.append("Measured cases missing")
    return {"status": "not-comparable" if reasons else "matching-declared-conditions",
            "bundleSha256": [r[2] for r in results], "scope": [a["scope"], b["scope"]],
            "reasons": reasons, "qualification": "none; declared conditions do not independently verify evidence or qualify a release"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="op", required=True)
    p = sub.add_parser("export"); p.add_argument("profile", type=Path); p.add_argument("output", type=Path)
    p = sub.add_parser("check"); p.add_argument("bundle", type=Path)
    p = sub.add_parser("receive"); p.add_argument("bundle", type=Path); p.add_argument("inbox", type=Path); p.add_argument("--receiver", choices=sorted(PROJECTS), required=True)
    p = sub.add_parser("compare"); p.add_argument("left", type=Path); p.add_argument("right", type=Path)
    args = parser.parse_args()
    try:
        if args.op == "export":
            result = export(args.profile, args.output)
        elif args.op == "receive":
            result = receive(args.bundle, args.inbox, args.receiver)
        elif args.op == "compare":
            result = compare(args.left, args.right)
        else:
            manifest, _, identity = validate(args.bundle)
            result = {"status": "valid", "producer": manifest["producer"], "bundleSha256": identity}
        print(json.dumps(result))
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
