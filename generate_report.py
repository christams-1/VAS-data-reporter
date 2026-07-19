#!/usr/bin/env python3
"""
Generate a Traffic Analysis Report PDF from VAS radar CSV data.
Speed bands and the speed interval are detected automatically from the CSV headers.

Usage:
    python3 generate_report.py <csv_file> [output_file.pdf]
                               [--speed-limit MPH] [--location NAME] [--notes TEXT]
"""

import argparse
import csv
import re
import sys
import os
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from xml.sax.saxutils import escape

from reportlab import rl_config

# Paragraph text is parsed as markup, so a <img src="..."> smuggled into a
# user-supplied field would otherwise read local files or fetch remote URLs.
# Escaping every interpolated value (see build_pdf) is the actual defence.
# This only narrows URL-scheme fetches -- it does NOT stop bare filesystem
# paths, which reach ImageReader without a scheme check.
rl_config.trustedSchemes = []

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle,
    Paragraph, Spacer,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Defaults only. These are never reassigned at runtime: report settings are
# passed as arguments, so concurrent callers (e.g. two Streamlit sessions in
# one process) cannot overwrite each other's values.
DEFAULT_SPEED_LIMIT = 30
DEFAULT_LOCATION    = ""
DEFAULT_NOTES       = ""


# Band detection

# calc_pace() expands each band one mph at a time, so an absurd upper bound in a
# CSV header ('35-999999999 mph') would exhaust memory. Real bands are far
# narrower than this ceiling.
_MAX_BAND_WIDTH = 200

_RANGE_RE  = re.compile(r'^(\d+)\s*-\s*(\d+)\s*mph$', re.IGNORECASE)
_GT_RE     = re.compile(r'^>\s*(\d+)\s*mph$',          re.IGNORECASE)
_SRANGE_RE = re.compile(r'^Speed range(\d+)$',          re.IGNORECASE)

# Standard VAS 12-band layout used when headers are generic "Speed rangeN"
_VAS_12_BANDS = [
    (0, 24), (25, 34), (35, 44), (45, 54), (55, 64), (65, 74),
    (75, 84), (85, 94), (95, 104), (105, 114), (115, 124), (125, None),
]


def detect_bands(fieldnames):
    """
    Parse speed-band column names from CSV headers.
    Returns (bands, speed_interval_mph).
    Each band: (column_name, lower_bound, upper_bound, midpoint).
    """
    bands       = []
    srange_cols = []

    for name in fieldnames:
        name = name.strip()
        m = _RANGE_RE.match(name)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi < lo:
                raise ValueError(
                    f'Speed-band column {name!r} has an inverted range.')
            if hi - lo + 1 > _MAX_BAND_WIDTH:
                raise ValueError(
                    f'Speed-band column {name!r} spans more than '
                    f'{_MAX_BAND_WIDTH} mph.')
            bands.append((name, lo, hi, (lo + hi) / 2))
            continue
        m = _GT_RE.match(name)
        if m:
            lo = int(m.group(1)) + 1
            bands.append((name, lo, None, None))   # upper/mid filled in below
            continue
        m = _SRANGE_RE.match(name)
        if m:
            srange_cols.append((int(m.group(1)), name))

    # Fall back to generic "Speed rangeN" columns mapped to known VAS bands
    if not bands and srange_cols:
        srange_cols.sort()
        n = len(srange_cols)
        band_map = _VAS_12_BANDS if n == 12 else (
            [(i * 10, i * 10 + 9) for i in range(n - 1)] + [(n * 10 - 10, None)]
        )
        for (_, col_name), (lo, hi) in zip(srange_cols, band_map):
            bands.append((col_name, lo, hi, (lo + hi) / 2 if hi is not None else None))

    if not bands:
        raise ValueError('No speed-band columns found in CSV headers.')

    # Detect interval from widths of interior bands (skip first and last)
    interior = [(hi - lo + 1) for _, lo, hi, _ in bands[1:-1] if hi is not None]
    interval = Counter(interior).most_common(1)[0][0] if interior else 10

    # Fix the open-ended last band
    if bands[-1][2] is None:
        name, lo, _, _ = bands[-1]
        hi  = lo + interval - 1
        mid = (lo + hi) / 2
        bands[-1] = (name, lo, hi, mid)

    return bands, interval


# Parsing

def parse_csv(filepath, speed_limit=DEFAULT_SPEED_LIMIT):
    """
    Returns (rows, bands, interval).
    bands/interval are detected from the CSV column headers.
    """
    rows = []
    with open(filepath, newline='', encoding='utf-8-sig') as f:
        reader     = csv.DictReader(f)
        bands, interval = detect_bands(reader.fieldnames)
        band_names      = [b[0] for b in bands]
        speeder_idx     = [i for i, (_, lo, _, _) in enumerate(bands) if lo >= speed_limit]

        for row in reader:
            try:
                log_time = datetime.strptime(row['Log time'].strip(), '%d/%m/%Y %H:%M')
                end_time = log_time.replace(minute=0, second=0) + timedelta(hours=1)

                band_counts = []
                for name in band_names:
                    raw = row.get(name, '0').strip()
                    band_counts.append(int(raw) if raw else 0)

                fvt_str = row.get('The fastest vehicle time', '').strip()
                try:
                    fvt = datetime.strptime(fvt_str, '%d/%m/%Y %H:%M')
                    if fvt.year == 2000:
                        fvt = None
                except ValueError:
                    fvt = None

                rows.append({
                    'log_time':         log_time,
                    'end_time':         end_time,
                    'total':            int(row['Vehicle no']),
                    'avg_speed':        float(row.get('Average speed') or 0),
                    'max_speed':        float(row.get('Max speed') or 0),
                    'fastest_veh_time': fvt,
                    'bands':            band_counts,
                    'weekday':          log_time.weekday(),
                    'date':             log_time.date(),
                    'hour':             end_time.hour,
                    'speeder_idx':      speeder_idx,
                    'speed_limit':      speed_limit,
                })
            except (ValueError, KeyError):
                continue

    return rows, bands, interval


# Statistics

def agg_bands(rows, n_bands):
    totals = [0] * n_bands
    for r in rows:
        for i, c in enumerate(r['bands']):
            totals[i] += c
    return totals


def calc_percentile(band_counts, bands, percentile):
    total = sum(band_counts)
    if total == 0:
        return 0.0, 0
    target = total * percentile / 100.0
    cum = 0
    for count, (_, lo, hi, _) in zip(band_counts, bands):
        if cum + count >= target:
            frac  = (target - cum) / count if count else 0
            speed = lo + frac * (hi - lo + 1)
            return round(speed, 1), int(round(target))
        cum += count
    return float(bands[-1][2]), int(round(target))


def calc_pace(band_counts, bands):
    fine = {}
    for count, (_, lo, hi, _) in zip(band_counts, bands):
        per_mph = count / (hi - lo + 1)
        for s in range(lo, hi + 1):
            fine[s] = fine.get(s, 0) + per_mph
    best_lo, best_cnt = 0, -1.0
    for lo in range(0, 131):
        cnt = sum(fine.get(s, 0) for s in range(lo, lo + 10))
        if cnt > best_cnt:
            best_cnt, best_lo = cnt, lo
    return float(best_lo), float(best_lo + 10)


def calc_speeder_stats(band_counts, bands, speeder_idx):
    sc, wsum = 0, 0.0
    for i in speeder_idx:
        c   = band_counts[i]
        mid = bands[i][3]
        sc   += c
        wsum += c * mid
    avg = wsum / sc if sc > 0 else 0.0
    return sc, round(avg, 1)


def weighted_avg_speed(rows):
    total = sum(r['total'] for r in rows)
    if total == 0:
        return 0.0
    return sum(r['avg_speed'] * r['total'] for r in rows) / total


def band_avg_speed(band_counts, bands):
    total = sum(band_counts)
    if total == 0:
        return 0.0
    return sum(c * mid for c, (_, _, _, mid) in zip(band_counts, bands)) / total


def safe_mean(lst):
    return sum(lst) / len(lst) if lst else 0.0


# PDF generation

HDR_BG   = colors.HexColor('#1F3864')
HDR_FG   = colors.white
ROW_ALT  = colors.HexColor('#DCE6F1')
GRID_CLR = colors.HexColor('#8EA9C1')


def make_styles():
    base    = getSampleStyleSheet()
    normal  = ParagraphStyle('rpt_normal',  parent=base['Normal'],
                             fontName='Helvetica',      fontSize=9,  leading=13)
    title   = ParagraphStyle('rpt_title',   parent=base['Normal'],
                             fontName='Helvetica-Bold', fontSize=14, leading=18, spaceAfter=4)
    section = ParagraphStyle('rpt_section', parent=base['Normal'],
                             fontName='Helvetica-Bold', fontSize=10, leading=14,
                             spaceBefore=8, spaceAfter=2)
    return normal, title, section


def make_table(data, col_widths, header_rows=1, zebra=True):
    t = Table(data, colWidths=col_widths, repeatRows=header_rows)
    t.setStyle(TableStyle([
        ('FONTNAME',       (0, 0), (-1, header_rows - 1), 'Helvetica-Bold'),
        ('FONTSIZE',       (0, 0), (-1, -1),               8),
        ('LEADING',        (0, 0), (-1, -1),               10),
        ('BACKGROUND',     (0, 0), (-1, header_rows - 1),  HDR_BG),
        ('TEXTCOLOR',      (0, 0), (-1, header_rows - 1),  HDR_FG),
        ('ALIGN',          (0, 0), (-1, -1),               'RIGHT'),
        ('ALIGN',          (0, 0), (0,  -1),               'LEFT'),
        ('VALIGN',         (0, 0), (-1, -1),               'MIDDLE'),
        ('TOPPADDING',     (0, 0), (-1, -1),               2),
        ('BOTTOMPADDING',  (0, 0), (-1, -1),               2),
        ('LEFTPADDING',    (0, 0), (-1, -1),               4),
        ('RIGHTPADDING',   (0, 0), (-1, -1),               4),
        ('GRID',           (0, 0), (-1, -1),               0.25, GRID_CLR),
        ('ROWBACKGROUNDS', (0, header_rows), (-1, -1),
         [colors.white, ROW_ALT] if zebra else [colors.white]),
    ]))
    return t


def build_pdf(rows, bands, interval, out_path, csv_path,
              location=DEFAULT_LOCATION, notes=DEFAULT_NOTES):
    if not rows:
        print('No data rows found.')
        return

    n_bands     = len(bands)
    speeder_idx = rows[0]['speeder_idx']
    # Taken from the parsed rows rather than a separate argument, so the limit
    # reported here cannot drift from the one speeder_idx was computed with.
    speed_limit = rows[0]['speed_limit']

    # Statistics
    project_name = os.path.splitext(os.path.basename(csv_path))[0]
    start_log    = rows[0]['end_time'] - timedelta(hours=1)
    end_log      = rows[-1]['end_time'] - timedelta(seconds=1)
    generated    = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    total_vehicles = sum(r['total'] for r in rows)
    all_bands_agg  = agg_bands(rows, n_bands)

    p85_speed, p85_cnt = calc_percentile(all_bands_agg, bands, 85)
    p50_speed, _       = calc_percentile(all_bands_agg, bands, 50)
    pace_lo, pace_hi   = calc_pace(all_bands_agg, bands)
    avg_spd_overall    = band_avg_speed(all_bands_agg, bands)

    max_row  = max(rows, key=lambda r: r['max_speed'])
    max_spd  = max_row['max_speed']
    fvt      = max_row['fastest_veh_time']
    max_time = (fvt if fvt else max_row['log_time']).strftime('%d/%m/%Y %H:%M:%S')

    dates  = sorted({r['date'] for r in rows})
    n_days = len(dates)
    aadt   = total_vehicles / n_days if n_days > 0 else 0.0

    daily_totals  = defaultdict(int)
    for r in rows:
        daily_totals[r['date']] += r['total']
    weekday_of    = {d: d.weekday() for d in dates}
    workdays      = {0, 1, 2, 3, 4}
    five_day_vals = [v for d, v in daily_totals.items() if weekday_of[d] in workdays]
    avg_daily_5   = safe_mean(five_day_vals)
    avg_daily_7   = safe_mean(list(daily_totals.values()))

    def hour_avg(end_hour, workdays_only=False):
        sub = [r for r in rows if r['hour'] == end_hour]
        if workdays_only:
            sub = [r for r in sub if r['weekday'] in workdays]
        return safe_mean([r['total'] for r in sub])

    am_5, am_7 = hour_avg(8,  workdays_only=True),  hour_avg(8)
    pm_5, pm_7 = hour_avg(17, workdays_only=True),  hour_avg(17)

    DOW_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    dow_stats  = {}
    for wd in range(7):
        wd_rows  = [r for r in rows if r['weekday'] == wd]
        if not wd_rows:
            dow_stats[wd] = (0, 0.0, 0.0, 0.0)
            continue
        wd_total     = sum(r['total'] for r in wd_rows)
        wd_agg       = agg_bands(wd_rows, n_bands)
        sc, avg_sp   = calc_speeder_stats(wd_agg, bands, speeder_idx)
        pct          = (sc / wd_total * 100) if wd_total > 0 else 0.0
        dow_stats[wd] = (sc, pct, avg_sp, weighted_avg_speed(wd_rows))

    hourly_data = []
    for r in rows:
        sc, avg_sp       = calc_speeder_stats(r['bands'], bands, speeder_idx)
        pct_sp           = (sc / r['total'] * 100) if r['total'] > 0 else 0.0
        p85_h, p85_h_cnt = calc_percentile(r['bands'], bands, 85)
        hourly_data.append((
            r['end_time'].strftime('%d/%m/%Y %H:%M:%S'),
            f'{p85_h:.1f}',
            str(p85_h_cnt),
            str(r['total']),
            f'{r["max_speed"]:.1f}',
            f'{avg_sp:.1f}' if avg_sp > 0 else '0.0',
            f'{pct_sp:.1f}%',
            f'{r["avg_speed"]:.1f}',
        ))

    # Build PDF
    normal_s, title_s, section_s = make_styles()
    margin  = 1.5 * cm
    doc     = SimpleDocTemplate(out_path, pagesize=A4,
                                leftMargin=margin, rightMargin=margin,
                                topMargin=margin,  bottomMargin=margin)
    usable_w = A4[0] - 2 * margin
    story    = []

    def sp(h=4):
        story.append(Spacer(1, h))

    # Title + metadata
    story.append(Paragraph('TRAFFIC ANALYSIS REPORT', title_s))
    for label, val in [
        ('For Project: ',            project_name),
        ('Projects Notes/Address: ', notes),
        ('Location/Name: ',          location),
        ('Report Generated: ',       generated),
        ('Speed Intervals = ',       f'{interval} MPH'),
        ('Time Intervals = ',        'Instant'),
        ('Traffic Report From ',
         f'{start_log.strftime("%d/%m/%Y %H:%M:%S")} '
         f'through {end_log.strftime("%d/%m/%Y %H:%M:%S")}'),
        ('85th Percentile Speed = ',    f'{p85_speed:.1f} MPH'),
        ('85th Percentile Vehicles = ', f'{p85_cnt:,} counts'),
        ('Max Speed = ',                f'{max_spd:.1f} MPH on {max_time}'),
        ('Total Vehicles =',            f'{total_vehicles:,} counts'),
        ('AADT: ',                      f'{aadt:.1f}'),
    ]:
        story.append(Paragraph(f'<b>{label}</b>{escape(str(val))}', normal_s))

    sp(8)

    # Volumes
    story.append(Paragraph('Volumes - weekly vehicle counts', section_s))
    vol_w = [usable_w * 0.55, usable_w * 0.225, usable_w * 0.225]
    story.append(make_table([
        ['Time',                     '5 Day',          '7 Day'],
        ['Average Daily',            f'{avg_daily_5:,.0f}', f'{avg_daily_7:,.0f}'],
        ['AM Peak  07:00 to 08:00',  f'{am_5:,.0f}',   f'{am_7:,.0f}'],
        ['PM Peak  16:00 to 17:00',  f'{pm_5:,.0f}',   f'{pm_7:,.0f}'],
    ], vol_w))
    sp(8)

    # Speed summary
    story.append(Paragraph('Speed', section_s))
    for label, val in [
        ('Speed Limit: ',          f'{speed_limit} MPH'),
        ('85th Percentile Speed: ', f'{p85_speed:.1f} MPH'),
        ('50th Percentile Speed: ', f'{p50_speed:.1f} MPH'),
        ('10 MPH Pace Interval: ',  f'{pace_lo:.1f} MPH to {pace_hi:.1f} MPH'),
        ('Average Speed: ',         f'{avg_spd_overall:.1f} MPH'),
    ]:
        story.append(Paragraph(f'<b>{label}</b>{escape(str(val))}', normal_s))
    sp(8)

    # Day-of-week
    lw  = usable_w * 0.22
    dcw = (usable_w - lw) / 7
    story.append(make_table(
        [[''] + DOW_LABELS,
         ['Count over limit'] + [f'{int(dow_stats[d][0]):,}' for d in range(7)],
         ['% over limit']     + [f'{dow_stats[d][1]:.1f}'    for d in range(7)],
         ['Avg Speeder']      + [f'{dow_stats[d][2]:.1f}'    for d in range(7)],
         ['Avg Speed']        + [f'{dow_stats[d][3]:.1f}'    for d in range(7)]],
        [lw] + [dcw] * 7,
    ))
    sp(10)

    # Hourly table
    story.append(Paragraph(
        '85th percentile speeds, counts and total counts by hour:', section_s))
    h_widths = [
        usable_w * 0.175,   # Date/Time Ending
        usable_w * 0.090,   # 85th pctl (MPH)
        usable_w * 0.095,   # 85th pctl counts
        usable_w * 0.075,   # Total Cnts
        usable_w * 0.075,   # Max Speed
        usable_w * 0.095,   # Avg Speeder
        usable_w * 0.095,   # % Speeders
        usable_w * 0.090,   # Avg Speed
    ]
    story.append(make_table(
        [['Date/Time Ending', '85th pctl\n(MPH)', '85th pctl\ncounts',
          'Total\nCnts', 'Max\nSpeed', 'Avg\nSpeeder', '% Speeders', 'Avg\nSpeed']]
        + [list(r) for r in hourly_data],
        h_widths,
    ))

    doc.build(story)
    print(f'Report written to: {out_path}')


# Entry point

def main():
    parser = argparse.ArgumentParser(
        description='Generate Traffic Analysis Report PDFs from VAS radar CSV data.')
    parser.add_argument('csv_files', nargs='*',
                        help='CSV file(s) to process. Defaults to all *.csv in current directory.')
    parser.add_argument('--output', '-o', default=None, metavar='FILE',
                        help='Output PDF path (only valid when processing a single CSV file).')
    parser.add_argument('--speed-limit', type=int, default=DEFAULT_SPEED_LIMIT, metavar='MPH',
                        help=f'Speed limit in mph (default: {DEFAULT_SPEED_LIMIT})')
    parser.add_argument('--location', default=DEFAULT_LOCATION, metavar='NAME',
                        help=f'Location/direction label (default: "{DEFAULT_LOCATION}")')
    parser.add_argument('--notes', default=DEFAULT_NOTES, metavar='TEXT',
                        help='Project notes / address (default: empty)')
    args = parser.parse_args()

    import glob
    csv_files = args.csv_files or sorted(glob.glob('*.csv'))
    if not csv_files:
        print('No CSV files found.')
        sys.exit(1)

    if args.output and len(csv_files) > 1:
        print('Error: --output can only be used with a single CSV file.')
        sys.exit(1)

    for csv_path in csv_files:
        out_path = args.output if (args.output and len(csv_files) == 1) \
                   else os.path.splitext(csv_path)[0] + '_report.pdf'
        print(f'\nReading: {csv_path}')
        try:
            rows, bands, interval = parse_csv(csv_path, speed_limit=args.speed_limit)
            print(f'Parsed {len(rows)} hourly records  |  '
                  f'Detected {len(bands)} speed bands  |  '
                  f'Speed interval: {interval} MPH')
            build_pdf(rows, bands, interval, out_path, csv_path,
                      location=args.location, notes=args.notes)
        except ValueError as e:
            print(f'Skipping: {e}')


if __name__ == '__main__':
    main()
