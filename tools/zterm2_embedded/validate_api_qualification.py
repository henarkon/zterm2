import argparse
import json
from pathlib import Path


def validate(log):
    events = []
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and "event" in event:
                events.append(event)
    errors = []
    failures = {"startupFailed", "cycleFailed", "apiQualificationFailed", "closeTimedOut",
                "launchFailed", "terminationStatusUnavailable"}
    for event in events:
        if event["event"] in failures:
            errors.append(event)
    instances = {}
    for event in events:
        if "instance" in event:
            instances.setdefault(event["instance"], []).append(event)
    if len(instances) != 24:
        errors.append(f"Expected 24 instances, observed {len(instances)}")
    natural_codes = []
    requested_codes = []
    for identifier, records in instances.items():
        names = [record["event"] for record in records]
        if names != ["started", "processExited", "closed"]:
            errors.append(f"{identifier}: invalid lifecycle {names}")
            continue
        if [record["sequence"] for record in records] != [1, 2, 3]:
            errors.append(f"{identifier}: noncontiguous event sequence")
        started, exited, closed = records
        if started.get("childPID", -1) <= 0 or started.get("metrics", {}).get("usesMetal"):
            errors.append(f"{identifier}: invalid process/renderer probe")
        code = exited.get("exitCode")
        if code is None or exited.get("rawStatus") != code << 8:
            errors.append(f"{identifier}: inconsistent exit status")
        (requested_codes if exited.get("requested") else natural_codes).append(code)
    for kind in ("cycle", "apiInputVerified", "apiResizeVerified", "resourcesAfterPairClose"):
        records = [event for event in events if event["event"] == kind]
        key = "index" if kind == "cycle" else "cycle"
        if sorted(record[key] for record in records) != list(range(12)):
            errors.append(f"{kind}: missing or duplicate lifecycle cycle")
    if sorted(natural_codes) != [0] * 6 + [75] * 6:
        errors.append(f"Unexpected natural exit codes {natural_codes}")
    if len(requested_codes) != 12 or any(code != 129 for code in requested_codes):
        errors.append(f"Unexpected requested-stop exit codes {requested_codes}")
    if not any(event["event"] == "apiQualificationClosed" and event.get("cycles") == 12 for event in events):
        errors.append("Missing final asynchronous window-close barrier")
    if not any(event["event"] == "apiHostTerminating" for event in events):
        errors.append("Host did not terminate after closing its window")
    probes = [event for event in events if event["event"] == "apiLifecycleProbeClosed"]
    if sorted(event["phase"] for event in probes) != ["created", "starting"]:
        errors.append("Missing created/starting close and API-error probes")
    for probe in probes:
        names = [event["event"] for event in probe["records"]]
        expected = ["closed"] if probe["phase"] == "created" else ["processExited", "closed"]
        if names != expected:
            errors.append(f"Unexpected probe lifecycle: {probe['phase']} {names}")
    samples = [event for event in events if event["event"] == "resourcesAfterPairClose"]
    warm_samples = [event for event in samples if event["cycle"] >= 4]
    descriptor_counts = [event["openDescriptors"] for event in warm_samples if "openDescriptors" in event]
    resident_bytes = [event["residentBytes"] for event in warm_samples if "residentBytes" in event]
    if len(descriptor_counts) != 8 or max(descriptor_counts, default=0) - min(descriptor_counts, default=0) > 2:
        errors.append(f"Descriptors did not remain bounded after warm-up: {descriptor_counts}")
    return {"schema": "zterm2-embedded-api-qualification-1", "passed": not errors,
            "m1Passed": False, "uiKeyboardQualified": False, "errors": errors,
            "instanceCount": len(instances), "naturalExitCodes": natural_codes,
            "requestedStopExitCodes": requested_codes,
            "warmDescriptorCounts": descriptor_counts, "warmResidentBytes": resident_bytes,
            "residentMeasurement": "Immediate post-close resident size; diagnostic samples, not a heap-leak proof",
            "events": events}


def main():
    parser = argparse.ArgumentParser(description="Validate real-engine API qualification; does not qualify keyboard, focus or rendering.")
    parser.add_argument("log", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--execution-identity", type=Path)
    arguments = parser.parse_args()
    report = validate(arguments.log)
    if arguments.execution_identity:
        report["executionIdentity"] = json.loads(arguments.execution_identity.read_text())
    with arguments.report.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "events"}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
