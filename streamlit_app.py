"""
Streamlit front-end for generate_report.py.

Lets a non-technical user upload VAS radar CSV export(s), set the report
options, and download the generated PDF(s) — no command line needed.

Deploy for free on Streamlit Community Cloud (streamlit.io/cloud):
point it at this repo, entry point = streamlit_app.py.
"""

import os
import tempfile

import streamlit as st

import generate_report as gr

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
        for uploaded_file in uploaded_files:
            # The client controls this name; strip any directory components so a
            # crafted upload cannot write outside tmpdir.
            safe_name = os.path.basename(uploaded_file.name).lstrip(".") or "upload.csv"

            csv_path = os.path.join(tmpdir, safe_name)
            with open(csv_path, "wb") as f:
                f.write(uploaded_file.getbuffer())

            out_name = os.path.splitext(safe_name)[0] + "_report.pdf"
            out_path = os.path.join(tmpdir, out_name)

            try:
                rows, bands, interval = gr.parse_csv(
                    csv_path, speed_limit=int(speed_limit)
                )
            except ValueError as e:
                st.error(f"**{uploaded_file.name}**: {e}")
                continue

            if not rows:
                st.warning(f"**{uploaded_file.name}**: no valid data rows found — skipped.")
                continue

            gr.build_pdf(rows, bands, interval, out_path, csv_path,
                         location=location, notes=notes)

            with open(out_path, "rb") as f:
                pdf_bytes = f.read()

            st.success(f"Report generated for **{uploaded_file.name}**")
            st.download_button(
                label=f"⬇ Download {out_name}",
                data=pdf_bytes,
                file_name=out_name,
                mime="application/pdf",
                key=out_name,
            )
