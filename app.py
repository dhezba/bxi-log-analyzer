from pathlib import Path
import math

import altair as alt
import pandas as pd
import streamlit as st

from analyzer import RuleSettings, build_excel, detect_runs, parse_audit, parse_code_map, summary_by_activity


APP_DIR = Path(__file__).parent
DEFAULT_MAP = APP_DIR / "sample_data" / "bxi_food_audit_trail_codes_011525.txt"

ASSUMPTIONS = [
    "Audit rows are ordered by their timestamp; original line number resolves same-second ties.",
    "SAUTH is a request/authorization proxy. The target activity event is treated as the response marker, not proven screen or print completion.",
    "GFNCL to PPRVW is a medium-confidence report-readiness proxy only when PPRVW is the next financial marker and no GFNCL intervenes.",
    "The Z-reading end-to-end run begins at the first matching z_reading SAUTH within the pairing window and ends at ZRD.",
    "For complete mapped-code coverage, the low-confidence activity interval proxy starts at a mapped activity and ends at the next mapped activity on the same terminal, user, and source file within the pairing window.",
    "Unpaired specialized markers remain visible with a blank duration; higher-confidence financial and reading rules remain separate from the general activity interval proxy.",
    "Slow/spike status uses the larger of the fixed threshold and Q3 + 1.5 x IQR within each run type/activity; small groups use only the fixed threshold.",
]

# Streamlit summary and graph selectors use the complete uploaded activity-code mapping.


st.set_page_config(page_title="BXI Local Log Analyzer", page_icon="⏱️", layout="wide")
st.title("BXI Local Log Analyzer")
st.caption("Local TXT upload, activity-code mapping, conservative duration pairing, spike review, and Excel export")

with st.sidebar:
    st.header("Analysis settings")
    pairing_window = st.number_input("Pairing window (minutes)", min_value=1, max_value=60, value=10)
    fixed_slow = st.number_input("Minimum slow threshold (seconds)", min_value=1, max_value=3600, value=90)
    st.caption("Files are processed inside this local app. Nothing is sent to an external service by this prototype.")

if "audit_uploader_revision" not in st.session_state:
    st.session_state.audit_uploader_revision = 0


def clear_audit_files() -> None:
    st.session_state.audit_uploader_revision += 1


audit_files = st.file_uploader(
    "Upload one or more BXI audit trail TXT files",
    type=["txt"],
    accept_multiple_files=True,
    key=f"audit_files_{st.session_state.audit_uploader_revision}",
)
st.button(
    "Clear uploaded audit files",
    on_click=clear_audit_files,
    disabled=not audit_files,
)
mapping_file = st.file_uploader("Optional: upload a replacement activity-code mapping TXT file", type=["txt"])

if not audit_files:
    st.info("Upload one or more audit trails to begin. A real sample is included in the sample_data folder for local testing.")
    st.stop()

mapping_text = (
    mapping_file.getvalue().decode("utf-8-sig", errors="replace")
    if mapping_file is not None
    else DEFAULT_MAP.read_text(encoding="utf-8-sig")
)
code_map = parse_code_map(mapping_text)
event_frames = []
for audit_file in audit_files:
    frame = parse_audit(audit_file.getvalue(), code_map)
    frame["source_file"] = audit_file.name
    event_frames.append(frame)
events = pd.concat(event_frames, ignore_index=True)
events = events.sort_values(["timestamp", "source_file", "source_line"], kind="stable").reset_index(drop=True)
settings = RuleSettings(pairing_window_minutes=int(pairing_window), fixed_slow_seconds=int(fixed_slow))
runs = detect_runs(events, settings)
summary = summary_by_activity(runs, code_map)

paired = runs[runs["duration_seconds"].notna()]
slow = runs[runs["status"] == "Slow/spike"]
cols = st.columns(6)
cols[0].metric("Uploaded files", f"{len(audit_files):,}")
cols[1].metric("Accepted events", f"{len(events):,}")
cols[2].metric("Detected run records", f"{len(runs):,}")
cols[3].metric("Paired durations", f"{len(paired):,}")
cols[4].metric("Slow/spikes", f"{len(slow):,}")
cols[5].metric("Longest duration", f"{paired['duration_seconds'].max():,.0f} sec" if not paired.empty else "—")

tab_summary, tab_runs, tab_timeline, tab_events, tab_rules = st.tabs(["Summary", "Runs", "Timeline", "Raw events", "Rules & assumptions"])

with tab_summary:
    st.subheader("Performance by activity and rule")
    st.caption(
        "Includes every uploaded mapped activity. General activity intervals use the next mapped activity "
        "in the same terminal/user/file as a low-confidence endpoint."
    )
    st.dataframe(summary, use_container_width=True, hide_index=True)
    if not paired.empty:
        measured_chart_codes = set(paired["activity_code"].dropna().unique())
        chart_codes = sorted(set(code_map) | measured_chart_codes)
        selected_chart_codes = st.multiselect(
            "Graph mapped activity codes",
            chart_codes,
            default=chart_codes,
            format_func=lambda code: f"{code} — {code_map.get(code, 'Unmapped activity')}",
            key="graph_mapped_activity_codes_all_v2",
        )
        st.caption(
            f"{len(selected_chart_codes):,} of {len(chart_codes):,} mapped activity codes selected. "
            "Only activities with a supported paired duration produce plotted points; no zero durations are invented."
        )
        chart_source = paired[paired["activity_code"].isin(selected_chart_codes)]
        if chart_source.empty:
            occurrence_source = events[events["code"].isin(selected_chart_codes)].copy()
            if occurrence_source.empty:
                st.info("The selected mapped activities do not occur in the uploaded audit files.")
            else:
                if len(audit_files) == 1:
                    occurrence_source["interval_start"] = occurrence_source["timestamp"].dt.floor("30min")
                    occurrence_data = (
                        occurrence_source.groupby(["interval_start", "code"], as_index=False)
                        .size()
                        .rename(columns={"code": "activity_code", "size": "occurrences"})
                    )
                    occurrence_x = alt.X(
                        "interval_start:T",
                        title="30-minute interval",
                        axis=alt.Axis(format="%H:%M", labelAngle=-45),
                    )
                    occurrence_time_tooltip = [
                        alt.Tooltip("interval_start:T", title="Interval start", format="%Y-%m-%d %H:%M")
                    ]
                    st.caption("No supported paired duration exists for the selection; showing 30-minute logged occurrence counts.")
                else:
                    occurrence_source["analysis_date"] = occurrence_source["timestamp"].dt.strftime("%Y-%m-%d")
                    occurrence_data = (
                        occurrence_source.groupby(["analysis_date", "code"], as_index=False)
                        .size()
                        .rename(columns={"code": "activity_code", "size": "occurrences"})
                    )
                    occurrence_x = alt.X(
                        "analysis_date:O",
                        title="Date",
                        sort=sorted(occurrence_data["analysis_date"].unique()),
                        axis=alt.Axis(labelAngle=-45),
                    )
                    occurrence_time_tooltip = [alt.Tooltip("analysis_date:O", title="Date")]
                    st.caption("No supported paired duration exists for the selection; showing daily logged occurrence counts.")
                occurrence_chart = (
                    alt.Chart(occurrence_data)
                    .mark_line(point=True)
                    .encode(
                        x=occurrence_x,
                        y=alt.Y("occurrences:Q", title="Logged occurrences", scale=alt.Scale(zero=True)),
                        color=alt.Color(
                            "activity_code:N",
                            title="Mapped activity code",
                            scale=alt.Scale(domain=selected_chart_codes),
                        ),
                        tooltip=occurrence_time_tooltip + [
                            alt.Tooltip("activity_code:N", title="Activity code"),
                            alt.Tooltip("occurrences:Q", title="Logged occurrences", format=".0f"),
                        ],
                    )
                    .properties(height=650)
                )
                st.altair_chart(occurrence_chart, use_container_width=True)
        else:
            if len(audit_files) == 1:
                chart_data = chart_source[
                    ["start_time", "end_time", "activity_code", "duration_seconds"]
                ].sort_values("start_time")
                tick_start = chart_data["start_time"].min().floor("30min")
                tick_end = chart_data["start_time"].max().ceil("30min")
                half_hour_ticks = pd.date_range(tick_start, tick_end, freq="30min").to_pydatetime().tolist()
                st.caption("Duration by row timestamp and activity code")
                x_encoding = alt.X(
                    "start_time:T",
                    title="Row timestamp",
                    scale=alt.Scale(domain=[tick_start.to_pydatetime(), tick_end.to_pydatetime()]),
                    axis=alt.Axis(
                        values=half_hour_ticks,
                        format="%H:%M:%S",
                        labelAngle=-45,
                    ),
                )
                time_tooltip = [
                    alt.Tooltip("start_time:T", title="Start timestamp", format="%Y-%m-%d %H:%M:%S"),
                    alt.Tooltip("end_time:T", title="End timestamp", format="%Y-%m-%d %H:%M:%S"),
                ]
            else:
                daily = chart_source.assign(analysis_date=chart_source["start_time"].dt.strftime("%Y-%m-%d"))
                chart = daily.pivot_table(
                    index="analysis_date",
                    columns="activity_code",
                    values="duration_seconds",
                    aggfunc="max",
                ).sort_index()
                chart_data = chart.reset_index().melt(
                    id_vars="analysis_date",
                    var_name="activity_code",
                    value_name="duration_seconds",
                ).dropna()
                st.caption("Daily maximum duration by activity code")
                x_encoding = alt.X(
                    "analysis_date:O",
                    title="Date",
                    sort=sorted(chart_data["analysis_date"].unique()),
                    axis=alt.Axis(labelAngle=-45),
                )
                time_tooltip = [alt.Tooltip("analysis_date:O", title="Date")]
            y_max = max(10, int(math.ceil(chart_data["duration_seconds"].max() / 10) * 10))
            performance_chart = (
                alt.Chart(chart_data)
                .mark_line(point=True)
                .encode(
                    x=x_encoding,
                    y=alt.Y(
                        "duration_seconds:Q",
                        title="Duration (seconds)",
                        scale=alt.Scale(domain=[0, y_max]),
                        axis=alt.Axis(
                            values=list(range(0, y_max + 10, 10)),
                            labelOverlap=False,
                        ),
                    ),
                    color=alt.Color(
                        "activity_code:N",
                        title="Mapped activity code",
                        scale=alt.Scale(domain=selected_chart_codes),
                        legend=alt.Legend(columns=2, symbolLimit=0),
                    ),
                    tooltip=time_tooltip + [
                        alt.Tooltip("activity_code:N", title="Activity code"),
                        alt.Tooltip("duration_seconds:Q", title="Duration (seconds)", format=".0f"),
                    ],
                )
                .properties(height=650)
            )
            st.altair_chart(performance_chart, use_container_width=True)
    if not paired.empty:
        st.subheader("Slowest response by activity")
        st.caption("All paired activities are ranked by their maximum observed response time.")
        activity_ranking = (
            paired.groupby(["activity_code", "activity"], dropna=False)
            .agg(
                response_count=("duration_seconds", "size"),
                slowest_seconds=("duration_seconds", "max"),
                average_seconds=("duration_seconds", "mean"),
                median_seconds=("duration_seconds", "median"),
                slow_spike_count=("status", lambda values: (values == "Slow/spike").sum()),
            )
            .reset_index()
            .sort_values(["slowest_seconds", "average_seconds"], ascending=False)
            .reset_index(drop=True)
        )
        activity_ranking.insert(0, "rank", activity_ranking.index + 1)
        activity_ranking[["slowest_seconds", "average_seconds", "median_seconds"]] = activity_ranking[
            ["slowest_seconds", "average_seconds", "median_seconds"]
        ].round(1)
        st.dataframe(activity_ranking, use_container_width=True, hide_index=True)

        st.subheader("All response records — slowest first")
        review_codes = sorted(paired["activity_code"].dropna().unique())
        review_dates = paired["start_time"].dt.date
        review_filter_cols = st.columns(2)
        selected_review_codes = review_filter_cols[0].multiselect(
            "Activity codes to review",
            review_codes,
            default=review_codes,
        )
        selected_review_dates = review_filter_cols[1].date_input(
            "Response date range",
            value=(review_dates.min(), review_dates.max()),
            min_value=review_dates.min(),
            max_value=review_dates.max(),
        )
        filtered_responses = paired[paired["activity_code"].isin(selected_review_codes)].copy()
        if len(selected_review_dates) == 2:
            start_date, end_date = selected_review_dates
            record_dates = filtered_responses["start_time"].dt.date
            filtered_responses = filtered_responses[record_dates.between(start_date, end_date)]
        filtered_responses.insert(0, "date", filtered_responses["start_time"].dt.date)
        response_columns = [
            "date", "start_time", "end_time", "duration_seconds", "activity_code", "activity",
            "run_type", "status", "threshold_seconds", "terminal", "user", "start_file",
            "start_line", "end_file", "end_line",
        ]
        st.dataframe(
            filtered_responses[response_columns].sort_values("duration_seconds", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

with tab_runs:
    codes = sorted(runs["activity_code"].dropna().unique())
    selected_codes = st.multiselect("Activity codes", codes, default=codes)
    statuses = sorted(runs["status"].dropna().unique())
    selected_statuses = st.multiselect("Status", statuses, default=statuses)
    filtered = runs[runs["activity_code"].isin(selected_codes) & runs["status"].isin(selected_statuses)]
    st.dataframe(filtered, use_container_width=True, hide_index=True)

with tab_timeline:
    st.caption("Each row is a paired or unpaired duration rule, with source-line traceability.")
    timeline_cols = ["start_time", "end_time", "duration_seconds", "activity_code", "run_type", "status", "confidence", "start_file", "start_line", "end_file", "end_line"]
    st.dataframe(runs[timeline_cols], use_container_width=True, hide_index=True)

with tab_events:
    st.dataframe(events.drop(columns=["details", "raw_line"], errors="ignore"), use_container_width=True, hide_index=True)

with tab_rules:
    for assumption in ASSUMPTIONS:
        st.write(f"- {assumption}")
    st.subheader("Mapped activity codes")
    st.dataframe(pd.DataFrame(sorted(code_map.items()), columns=["Code", "Activity"]), hide_index=True, use_container_width=True)

excel_data = build_excel(events, runs, code_map, ASSUMPTIONS)
st.download_button(
    "Export analysis to Excel",
    data=excel_data,
    file_name=(
        f"{Path(audit_files[0].name).stem}_analysis.xlsx"
        if len(audit_files) == 1
        else "combined_audit_trails_analysis.xlsx"
    ),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
