from pathlib import Path

import pandas as pd

from analyzer import RuleSettings, build_excel, detect_runs, parse_audit, parse_code_map, summary_by_activity


ROOT = Path(__file__).parents[1]


def test_real_sample_parses_and_detects_observed_sequences():
    mapping = parse_code_map((ROOT / "sample_data" / "bxi_food_audit_trail_codes_011525.txt").read_text())
    events = parse_audit((ROOT / "sample_data" / "AUDIT_TRAIL_2026_09_13.txt").read_bytes(), mapping)
    runs = detect_runs(events, RuleSettings())
    assert len(events) == 2125
    assert mapping["GFNCL"] == "Generate Financial"
    assert (events["code"] == "GFNCL").sum() == 16
    assert (events["code"] == "XRD").sum() == 2
    assert (events["code"] == "ZRD").sum() == 1
    z_e2e = runs[runs["run_type"] == "Z-reading end-to-end"]
    assert len(z_e2e) == 1
    assert z_e2e.iloc[0]["duration_seconds"] == 67
    print_proxy = runs[(runs["activity_code"] == "PFNCL") & (runs["run_type"] == "Authorized activity")]
    assert len(print_proxy) == 1
    assert print_proxy.iloc[0]["duration_seconds"] == 24


def test_unpaired_marker_does_not_invent_duration():
    text = "2026/09/13 12:00:00\t2026-09-13\t1\tUser\tXRD\t\t0\t0\t{}\n"
    events = parse_audit(text, {"XRD": "X Reading"})
    runs = detect_runs(events)
    assert runs.iloc[0]["pairing_status"] == "Unpaired"
    assert pd.isna(runs.iloc[0]["duration_seconds"])


def test_excel_has_expected_sheets():
    mapping = {"GFNCL": "Generate Financial"}
    text = (
        '2026/09/13 12:00:00\t2026-09-13\t1\tUser\tSAUTH\t0\t0\t0\t{"module":"sm_financialReport"}\n'
        "2026/09/13 12:00:05\t2026-09-13\t1\tUser\tGFNCL\n"
    )
    events = parse_audit(text, mapping)
    runs = detect_runs(events)
    payload = build_excel(events, runs, mapping, ["test assumption"])
    workbook = pd.ExcelFile(payload)
    assert workbook.sheet_names == ["Summary", "Runs", "Events", "Code Mapping", "Assumptions"]


def test_summary_includes_mapped_activities_without_detected_runs():
    mapping = {"GFNCL": "Generate Financial", "UNUSED": "Unused mapped activity"}
    text = (
        '2026/09/13 12:00:00\t2026-09-13\t1\tUser\tSAUTH\t0\t0\t0\t{"module":"sm_financialReport"}\n'
        "2026/09/13 12:00:05\t2026-09-13\t1\tUser\tGFNCL\n"
    )
    runs = detect_runs(parse_audit(text, mapping))
    summary = summary_by_activity(runs, mapping)
    unused = summary[summary["activity_code"] == "UNUSED"].iloc[0]
    assert unused["activity"] == "Unused mapped activity"
    assert unused["runs"] == 0
    assert pd.isna(unused["maximum_seconds"])


def test_mapped_activity_duration_ends_at_next_activity_in_same_session():
    mapping = {"ADD": "Add Item", "PAY": "Payment"}
    text = (
        "2026/09/13 12:00:00\t2026-09-13\t1\tUser\tADD\n"
        "2026/09/13 12:00:07\t2026-09-13\t2\tOther\tPAY\n"
        "2026/09/13 12:00:12\t2026-09-13\t1\tUser\tPAY\n"
    )
    events = parse_audit(text, mapping, source_name="audit.txt")
    runs = detect_runs(events)
    interval = runs[
        (runs["run_type"] == "Activity to next activity")
        & (runs["activity_code"] == "ADD")
    ].iloc[0]
    assert interval["duration_seconds"] == 12
    assert interval["end_code"] == "PAY"
    assert interval["terminal"] == "1"
