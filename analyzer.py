from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Iterable, TextIO

import pandas as pd


TARGET_CODES = {"GFNCL", "PFNCL", "XRD", "ZRD", "RXR", "RZR"}


@dataclass(frozen=True)
class RuleSettings:
    pairing_window_minutes: int = 10
    fixed_slow_seconds: int = 90
    spike_iqr_multiplier: float = 1.5


def parse_code_map(text: str) -> dict[str, str]:
    """Return CODE -> readable label from the supplied JavaScript-style map."""
    mapping: dict[str, str] = {}
    for name, code in re.findall(r"^\s*([A-Z0-9_]+)\s*:\s*[\"']([^\"']+)[\"']", text, re.M):
        mapping[code.strip()] = name.replace("_", " ").title()
    return mapping


def _decode(source: str | bytes | TextIO | BinaryIO) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, bytes):
        return source.decode("utf-8-sig", errors="replace")
    data = source.read()
    return data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data


def parse_audit(
    source: str | bytes | TextIO | BinaryIO,
    code_map: dict[str, str],
    source_name: str = "",
) -> pd.DataFrame:
    records: list[dict] = []
    for source_line, raw in enumerate(_decode(source).splitlines(), start=1):
        if not raw.strip():
            continue
        fields = raw.rstrip("\r\n").split("\t")
        if len(fields) < 5:
            records.append({"source_file": source_name, "source_line": source_line, "parse_status": "Rejected: fewer than 5 tab fields", "raw_line": raw})
            continue
        timestamp = pd.to_datetime(fields[0].strip(), format="%Y/%m/%d %H:%M:%S", errors="coerce")
        if pd.isna(timestamp):
            records.append({"source_file": source_name, "source_line": source_line, "parse_status": "Rejected: invalid timestamp", "raw_line": raw})
            continue
        code = fields[4].strip()
        detail_text = "\t".join(fields[5:]).strip()
        json_text = next((part for part in fields[5:] if part.strip().startswith(("{", "["))), "")
        try:
            details = json.loads(json_text) if json_text else {}
            json_status = "Parsed" if json_text else "None"
        except json.JSONDecodeError:
            details, json_status = {}, "Invalid JSON"
        records.append(
            {
                "source_file": source_name,
                "source_line": source_line,
                "timestamp": timestamp,
                "business_date": fields[1].strip() if len(fields) > 1 else "",
                "terminal": fields[2].strip() if len(fields) > 2 else "",
                "user": fields[3].strip() if len(fields) > 3 else "",
                "code": code,
                "activity": code_map.get(code, code or "Unknown"),
                "is_mapped_activity": code in code_map,
                "details": details,
                "detail_text": detail_text,
                "parse_status": "Accepted",
                "json_status": json_status,
                "raw_line": raw,
            }
        )
    frame = pd.DataFrame(records)
    if frame.empty:
        return frame
    accepted = frame[frame["parse_status"] == "Accepted"].copy()
    return accepted.sort_values(["timestamp", "source_file", "source_line"], kind="stable").reset_index(drop=True)


def _module(row: pd.Series) -> str:
    details = row.get("details")
    return str(details.get("module", "")) if isinstance(details, dict) else ""


def _run(
    run_type: str,
    activity_code: str,
    start: pd.Series,
    end: pd.Series,
    rule: str,
    confidence: str,
    status: str = "Paired",
) -> dict:
    duration = (end["timestamp"] - start["timestamp"]).total_seconds()
    return {
        "run_type": run_type,
        "activity_code": activity_code,
        "activity": end["activity"],
        "terminal": end["terminal"],
        "user": end["user"],
        "start_time": start["timestamp"],
        "end_time": end["timestamp"],
        "duration_seconds": duration,
        "duration": str(timedelta(seconds=int(duration))),
        "start_code": start["code"],
        "end_code": end["code"],
        "start_line": int(start["source_line"]),
        "end_line": int(end["source_line"]),
        "start_file": start.get("source_file", ""),
        "end_file": end.get("source_file", ""),
        "rule": rule,
        "confidence": confidence,
        "pairing_status": status,
    }


def detect_runs(events: pd.DataFrame, settings: RuleSettings = RuleSettings()) -> pd.DataFrame:
    """Build conservative runs. Unconfirmed markers are retained instead of invented as completions."""
    columns = [
        "run_type", "activity_code", "activity", "terminal", "user", "start_time", "end_time",
        "duration_seconds", "duration", "start_code", "end_code", "start_file", "start_line", "end_file", "end_line", "rule",
        "confidence", "pairing_status",
    ]
    if events.empty:
        return pd.DataFrame(columns=columns)
    window = pd.Timedelta(minutes=settings.pairing_window_minutes)
    runs: list[dict] = []

    # Direct activity response: closest preceding matching supervisor authorization on same terminal/user.
    modules_by_code = {
        "GFNCL": ("sm_financialReport",),
        "XRD": ("x_reading", "z_reading"),
        "ZRD": ("z_reading",),
    }
    for idx, event in events[events["code"].isin(TARGET_CODES)].iterrows():
        expected_modules = modules_by_code.get(event["code"], ())
        prior = events.loc[: idx - 1]
        candidates = prior[
            (prior["code"] == "SAUTH")
            & (prior["terminal"] == event["terminal"])
            & (prior["user"] == event["user"])
            & (prior["timestamp"] >= event["timestamp"] - window)
        ]
        if expected_modules:
            exact = candidates[candidates.apply(lambda r: any(m.lower() in _module(r).lower() for m in expected_modules), axis=1)]
            if not exact.empty:
                candidates = exact
        if not candidates.empty:
            start = candidates.iloc[-1]
            confidence = "Medium" if event["code"] == "PFNCL" else "High"
            rule = "nearest SAUTH -> PFNCL marker" if event["code"] == "PFNCL" else "matching SAUTH -> activity event"
            runs.append(_run("Authorized activity", event["code"], start, event, rule, confidence))
        else:
            orphan = event.copy()
            row = _run("Activity marker", event["code"], orphan, event, "no confirmed start marker found", "Low", "Unpaired")
            row["duration_seconds"] = pd.NA
            row["duration"] = ""
            runs.append(row)

    # Financial report ready proxy: GFNCL followed by PPRVW, before another GFNCL and within the time window.
    financial = events[events["code"].isin(["GFNCL", "PPRVW"])].copy()
    for pos, (_, start) in enumerate(financial.iterrows()):
        if start["code"] != "GFNCL":
            continue
        later = financial.iloc[pos + 1 :]
        if later.empty or later.iloc[0]["code"] != "PPRVW":
            continue
        end = later.iloc[0]
        if end["terminal"] == start["terminal"] and end["timestamp"] - start["timestamp"] <= window:
            runs.append(_run("Financial report readiness", "GFNCL", start, end, "GFNCL -> next PPRVW, with no intervening GFNCL", "Medium"))

    # Z-reading sequence observed in the sample: z_reading authorization through ZRD.
    for _, end in events[events["code"] == "ZRD"].iterrows():
        candidates = events[
            (events["timestamp"] <= end["timestamp"])
            & (events["timestamp"] >= end["timestamp"] - window)
            & (events["terminal"] == end["terminal"])
            & (events["user"] == end["user"])
            & (events["code"] == "SAUTH")
        ]
        candidates = candidates[candidates.apply(lambda r: "z_reading" in _module(r).lower(), axis=1)]
        if not candidates.empty:
            start = candidates.iloc[0]
            runs.append(_run("Z-reading end-to-end", "ZRD", start, end, "first z_reading SAUTH -> ZRD within window", "High"))

    # General activity interval proxy requested for complete mapped-code coverage.
    # Keep the pairing within one file/session so concurrent terminals and users are not mixed.
    mapped_events = (
        events[events["is_mapped_activity"].eq(True)].copy()
        if "is_mapped_activity" in events.columns
        else events.iloc[0:0].copy()
    )
    group_columns = ["source_file", "terminal", "user"]
    for _, session_events in mapped_events.groupby(group_columns, dropna=False, sort=False):
        session_events = session_events.sort_values(["timestamp", "source_line"], kind="stable")
        for position in range(len(session_events) - 1):
            start = session_events.iloc[position]
            end = session_events.iloc[position + 1]
            elapsed = end["timestamp"] - start["timestamp"]
            if elapsed < pd.Timedelta(0) or elapsed > window:
                continue
            row = _run(
                "Activity to next activity",
                start["code"],
                start,
                end,
                "mapped activity -> next mapped activity on same terminal/user/file",
                "Low",
            )
            row["activity"] = start["activity"]
            runs.append(row)

    result = pd.DataFrame(runs, columns=columns)
    if result.empty:
        return result
    result = result.sort_values(["start_time", "end_time", "run_type"], kind="stable").reset_index(drop=True)
    return classify_runs(result, settings)


def classify_runs(runs: pd.DataFrame, settings: RuleSettings = RuleSettings()) -> pd.DataFrame:
    result = runs.copy()
    result["status"] = "Unpaired"
    paired = result["duration_seconds"].notna()
    result.loc[paired, "status"] = "Normal"
    for _, group in result[paired].groupby(["run_type", "activity_code"]):
        durations = group["duration_seconds"].astype(float)
        q1, q3 = durations.quantile([0.25, 0.75]) if len(durations) >= 4 else (durations.min(), durations.max())
        iqr = q3 - q1
        data_threshold = q3 + settings.spike_iqr_multiplier * iqr if len(durations) >= 4 else float("inf")
        threshold = max(settings.fixed_slow_seconds, data_threshold)
        result.loc[group.index, "threshold_seconds"] = threshold
        result.loc[group.index[durations >= threshold], "status"] = "Slow/spike"
    return result


def summary_by_activity(runs: pd.DataFrame, code_map: dict[str, str] | None = None) -> pd.DataFrame:
    columns = [
        "run_type", "activity_code", "activity", "runs", "average_seconds",
        "median_seconds", "minimum_seconds", "maximum_seconds", "slow_or_spike",
    ]
    paired = runs[runs["duration_seconds"].notna()].copy()
    if paired.empty:
        summary = pd.DataFrame(columns=columns)
    else:
        paired["is_slow"] = paired["status"].eq("Slow/spike")
        summary = (
            paired.groupby(["run_type", "activity_code"], as_index=False)
            .agg(
                runs=("duration_seconds", "size"),
                average_seconds=("duration_seconds", "mean"),
                median_seconds=("duration_seconds", "median"),
                minimum_seconds=("duration_seconds", "min"),
                maximum_seconds=("duration_seconds", "max"),
                slow_or_spike=("is_slow", "sum"),
            )
        )
        summary.insert(
            2,
            "activity",
            summary["activity_code"].map(code_map or {}).fillna("Unmapped activity"),
        )

    if code_map:
        measured_codes = set(summary["activity_code"].dropna())
        missing = pd.DataFrame(
            [
                {
                    "run_type": "No detected duration rule",
                    "activity_code": code,
                    "activity": activity,
                    "runs": 0,
                    "average_seconds": pd.NA,
                    "median_seconds": pd.NA,
                    "minimum_seconds": pd.NA,
                    "maximum_seconds": pd.NA,
                    "slow_or_spike": 0,
                }
                for code, activity in sorted(code_map.items())
                if code not in measured_codes
            ],
            columns=columns,
        )
        summary = pd.concat([summary, missing], ignore_index=True)

    return summary.sort_values(
        ["runs", "maximum_seconds", "activity_code"],
        ascending=[False, False, True],
        na_position="last",
    ).reset_index(drop=True)


def build_excel(events: pd.DataFrame, runs: pd.DataFrame, code_map: dict[str, str], assumptions: Iterable[str]) -> bytes:
    output = io.BytesIO()
    summary = summary_by_activity(runs, code_map)
    mapping = pd.DataFrame(sorted(code_map.items()), columns=["code", "activity"])
    assumptions_df = pd.DataFrame({"duration_rule_assumptions": list(assumptions)})
    export_events = events.drop(columns=["details", "raw_line"], errors="ignore")
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        runs.to_excel(writer, sheet_name="Runs", index=False)
        export_events.to_excel(writer, sheet_name="Events", index=False)
        mapping.to_excel(writer, sheet_name="Code Mapping", index=False)
        assumptions_df.to_excel(writer, sheet_name="Assumptions", index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            ws.sheet_view.showGridLines = False
            for cell in ws[1]:
                cell.font = cell.font.copy(bold=True, color="FFFFFF")
                cell.fill = cell.fill.copy(fill_type="solid", fgColor="182957")
            for column_cells in ws.columns:
                length = min(max(len(str(c.value or "")) for c in column_cells) + 2, 48)
                ws.column_dimensions[column_cells[0].column_letter].width = max(length, 12)
    return output.getvalue()
