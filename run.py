"""Copilot Adoption Explorer - one-command runner.

  python run.py                 collect from Graph, then build all deliverables
  python run.py --build-only    rebuild deliverables from the existing snapshot
  python run.py --workers 24    raise concurrency for large tenants
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

# Validate Python version
if sys.version_info < (3, 9):
    print(f"Python 3.9+ required (you have {sys.version_info.major}.{sys.version_info.minor})", file=sys.stderr)
    sys.exit(2)

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import build_report  # noqa: E402
from collect import collect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect M365 Copilot user-level adoption and build reports.")
    ap.add_argument("--build-only", action="store_true", help="skip Graph collection")
    ap.add_argument("--collect-only", action="store_true", help="skip report generation")
    ap.add_argument("--workers", type=int, default=8, help="parallel Graph calls (default 8)")
    ap.add_argument("--include-disabled", action="store_true", help="include disabled accounts")
    args = ap.parse_args()

    cfg_path = ROOT / "config" / "app.json"
    if not args.build_only:
        if not cfg_path.exists():
            print("No config/app.json - run:  python src\\bootstrap.py <tenant-id>", file=sys.stderr)
            return 2
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        print("== Collecting from Microsoft Graph ==")
        t0 = time.time()
        stats = collect(cfg, include_disabled=args.include_disabled, workers=args.workers)
        print(json.dumps(stats, indent=2))
        print(f"   collected in {time.time() - t0:.1f}s\n")

    if args.collect_only:
        return 0

    print("== Building deliverables ==")
    payload = build_report.build_payload()
    html = build_report.write_html(payload)
    csvp = build_report.write_csv(payload)
    xlsx = build_report.write_xlsx(payload)

    # Publish a share-ready copy at the OneDrive-synced workspace root.
    share = ROOT.parent / "copilot-adoption-explorer.html"
    share.write_bytes(html.read_bytes())

    months = payload["meta"]["months"]
    span = f" ({months[0]} - {months[-1]})" if months else ""
    print(f"   people         : {len(payload['users'])}")
    print(f"   leaders        : {len(payload['mgrs'])}")
    print(f"   months         : {len(months)}{span}")
    print(f"\n   dashboard      : {html}")
    print(f"   shareable copy : {share}")
    print(f"   workbook       : {xlsx}")
    print(f"   csv            : {csvp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
