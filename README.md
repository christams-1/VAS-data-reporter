# VAS Radar Report Generator

Generates a Traffic Analysis Report PDF from hourly CSV data exported by a Vehicle Activated Sign (VAS) radar unit.

## Requirements

```
pip install -r requirements.txt
```

## Usage

```
python3 generate_report.py [csv_file ...] [options]
```

The output PDF defaults to `<csv_file>_report.pdf` if `--output` is not given. Omitting all CSV files processes every `*.csv` in the current directory.

### Options

| Option | Default | Description |
|---|---|---|
| `--output FILE`, `-o FILE` | *(auto)* | Output PDF path (single-file mode only) |
| `--speed-limit MPH` | `30` | Speed limit for the site in mph |
| `--location NAME` | *(empty)* | Location or direction label (e.g. `Incoming`) |
| `--notes TEXT` | *(empty)* | Project notes or address |

### Examples

```bash
# Minimal — use all defaults
python3 generate_report.py data.csv

# Specify output path
python3 generate_report.py data.csv --output report.pdf

# Specify all parameters
python3 generate_report.py data.csv -o report.pdf --speed-limit 30 --location "Incoming" --notes "Station Road (by School)"

# Process all CSVs in the current directory
python3 generate_report.py
```

## Input CSV format

The script expects hourly CSV exports from a VAS radar device with the following columns:

| Column | Description |
|---|---|
| `Log time` | End of the hourly interval (`DD/MM/YYYY HH:MM`) |
| `Vehicle no` | Total vehicles counted |
| `Overspeed vehicles` | Vehicles flagged as exceeding the speed limit |
| `The fastest vehicle time` | Timestamp of the fastest vehicle (`01/01/2000` = none) |
| `0-24mph`, `25-34mph`, … `> 124mph` | Vehicle counts per speed band |
| `Average speed` | Mean speed for the hour (mph) |
| `Max speed` | Highest speed recorded (mph) |
| `Occupancy in %` | Road occupancy percentage |
| `Sign activity in %` | Percentage of time the VAS sign was active |

Speed bands and the speed interval are detected automatically from the column headers, so CSVs exported with different interval widths (e.g. 5 mph or 10 mph) are handled without any changes to the script.

## Report contents

The generated PDF contains:

- **Header** — project name, location, date range, key summary statistics (85th percentile speed, max speed, total vehicles, AADT)
- **Volumes** — average daily, AM peak, and PM peak counts for 5-day (Mon–Fri) and 7-day periods
- **Speed summary** — speed limit, 85th/50th percentile speeds, 10 mph pace interval, average speed
- **Day-of-week breakdown** — count over limit, % over limit, average speeder speed, and average speed for each day
- **Hourly table** — per-hour 85th percentile speed, counts, max speed, average speeder speed, % speeders, and average speed for the full dataset
