# BXI Local Log Analyzer

This prototype runs locally on a Mac. It accepts a BXI audit-trail TXT file, maps activity codes to readable names, builds conservative duration records, highlights slow runs or spikes, shows timelines and exports the analysis to Excel.

## Quick start on Mac

1. Install Python 3.11 or newer from https://www.python.org/downloads/macos/ if `python3 --version` does not work in Terminal.
2. In Finder, open this folder.
3. Double-click `run.command`. The first run creates a private `.venv` folder and installs the three required packages.
4. If macOS blocks the script, right-click `run.command`, choose **Open**, then confirm.
5. The browser opens at `http://localhost:8501`. Upload a BXI audit-trail TXT file.
6. When finished, return to Terminal and press **Control-C**.

Terminal alternative:

```bash
cd /path/to/bxi-local-log-analyzer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py
```

The prototype processes the uploaded data in the local Streamlit process. It has no network calls in the application code.

## What the real sample showed

The supplied audit file contains 2,125 accepted nonblank events. Its target markers include 16 `GFNCL`, 1 `PFNCL`, 2 `XRD`, and 1 `ZRD` event. The log does not contain generic `Started` and `Completed` fields.

Observed sequences support these rules:

- `SAUTH` with `module=sm_financialReport` to `GFNCL`: high-confidence authorization/request-to-generation-marker duration.
- `GFNCL` to the next `PPRVW`, only when no `GFNCL` intervenes: medium-confidence report-readiness proxy.
- `SAUTH` with `module=x_reading` or `module=z_reading` to `XRD`, and `module=z_reading` to `ZRD`: high-confidence authorization/request-to-reading-marker duration when a matching module is present.
- First `SAUTH` with `module=z_reading` through `ZRD`: high-confidence end-to-end Z-reading sequence. In the sample this is 67 seconds, from 21:29:22 to 21:30:29.
- `PFNCL` is paired to the nearest prior authorization as a medium-confidence request-to-marker proxy. In the sample that interval is 24 seconds. `PFNCL` occurs 17 seconds before `ZRD`; this proximity is visible in the timeline but is not silently treated as proof that printing completed at `ZRD`.
- A target event without a defensible start remains **Unpaired** with a blank duration.

## Items to confirm with the BXI product or operations team

1. Does `GFNCL` mean generation was requested, generation completed, or an audit record written after completion?
2. Does `PPRVW` mean the preview became ready, the preview was opened, or printing began?
3. Does `PFNCL` mean the print request started or the physical print finished?
4. For an X-reading without a preceding `SAUTH`, should `DEC`, a screen-open event, or another hidden event be used as the start?
5. Should a complete Z-reading include the preceding X-reading, the second authorization, printing, backup, or only the interval ending at `ZRD`?
6. What operating thresholds should replace the prototype default of 90 seconds for each activity?

Until those semantics are confirmed, the app labels proxy confidence and preserves unpaired events instead of implying exact active-processing or print duration.

## Excel export

The export contains these sheets:

- **Summary**: count, average, median, minimum, maximum and slow/spike count by rule and activity.
- **Runs**: every paired or unpaired duration record with rule, confidence, source lines and thresholds.
- **Events**: parsed source events.
- **Code Mapping**: the supplied code-to-name mapping.
- **Assumptions**: the duration rules and caveats used by the app.

## Project files

- `app.py`: Streamlit interface.
- `analyzer.py`: parser, pairing logic, classification and Excel export.
- `sample_data/`: copies of the two supplied TXT files.
- `tests/`: checks against the real sample and Excel export.
