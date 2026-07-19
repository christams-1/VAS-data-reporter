"""
Streamlit front-end for generate_report.py.

Lets a non-technical user upload VAS radar CSV export(s), set the report
options, and download the generated PDF(s) — no command line needed.

Deploy for free on Streamlit Community Cloud (streamlit.io/cloud):
point it at this repo, entry point = streamlit_app.py.
"""

import logging
import os
import tempfile

import streamlit as st

import generate_report as gr

logger = logging.getLogger(__name__)

st.set_page_config(page_title="VAS Radar Report Generator", page_icon="🚦")

st.title("🚦 VAS Radar Report Generator")
st.write(
    "Upload one or more hourly CSV exports from a Vehicle Activated Sign (VAS) "
    "radar unit and generate a Traffic Analysis Report PDF."
)

uploaded_files = st.file_uploader(
    "CSV file(s)", type="csv", accept_multiple_files=True
)

col1, col2 = st.columns(2)
with col1:
    speed_limit = st.number_input(
        "Speed limit (mph)", min_value=0, max_value=130, value=30, step=5
    )
with col2:
    location = st.text_input("Location / direction (e.g. 'Incoming')", value="")

notes = st.text_area("Notes / address (e.g. 'Station Road, by School')", value="")

generate = st.button(
    "Generate report(s)", type="primary", disabled=not uploaded_files
)

if generate:
    # Passed as arguments, never assigned onto the generate_report module: the
    # module is shared by every session in this process, so module-level state
    # would let a concurrent user's notes/location land in this user's PDF.
    with tempfile.TemporaryDirectory() as tmpdir:
        used_names = set()

        for idx, uploaded_file in enumerate(uploaded_files):
            # The client controls this name; strip any directory components so a
            # crafted upload cannot write outside tmpdir.
            safe_name = os.path.basename(uploaded_file.name).lstrip(".") or "upload.csv"

            # Two uploads may share a name, which would otherwise overwrite each
            # other inside tmpdir and collide on the download widget key.
            base, ext = os.path.splitext(safe_name)
            suffix = 2
            while safe_name in used_names:
                safe_name = f"{base}_{suffix}{ext}"
                suffix += 1
            used_names.add(safe_name)

            csv_path = os.path.join(tmpdir, safe_name)
            out_name = os.path.splitext(safe_name)[0] + "_report.pdf"
            out_path = os.path.join(tmpdir, out_name)

            try:
                with open(csv_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())

                rows, bands, interval = gr.parse_csv(
                    csv_path, speed_limit=int(speed_limit)
                )

                if not rows:
                    st.warning(f"**{safe_name}**: no valid data rows found — skipped.")
                    continue

                gr.build_pdf(rows, bands, interval, out_path, csv_path,
                             location=location, notes=notes)

                with open(out_path, "rb") as f:
                    pdf_bytes = f.read()
            except ValueError as e:
                # Raised by detect_bands for CSVs we can describe precisely.
                st.error(f"**{safe_name}**: {e}")
                continue
            except Exception:
                # Anything else (csv.Error on NUL bytes or oversized fields, I/O
                # failures) must not take down the whole run. Detail goes to the
                # server log rather than the page, which would expose internal
                # paths and tracebacks to an anonymous visitor.
                logger.exception("Failed to generate report for %s", safe_name)
                st.error(
                    f"**{safe_name}**: could not be processed — "
                    "please check it is a valid VAS radar CSV export."
                )
                continue

            st.success(f"Report generated for **{safe_name}**")
            st.download_button(
                label=f"⬇ Download {out_name}",
                data=pdf_bytes,
                file_name=out_name,
                mime="application/pdf",
                key=f"download_{idx}",
            )
