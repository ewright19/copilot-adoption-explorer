"""Build a clean, customer-installable ZIP.

    python package.py

Ships source + docs only.  It deliberately EXCLUDES:
  * config/app.json   - contains a client secret
  * out/              - contains real user names from whatever tenant it was last run in

Refuses to produce an archive if either would have been included.
"""
from __future__ import annotations

import hashlib
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"
NAME = "copilot-adoption-explorer"

INCLUDE = [
    "run.py",
    "package.py",
    "requirements.txt",
    "README.md",
    "INSTALL.md",
    ".gitignore",
    "src/bootstrap.py",
    "src/bootstrap_webapp.py",
    "src/preflight.py",
    "src/diagnose.py",
    "src/graph_client.py",
    "src/collect.py",
    "src/build_report.py",
    "webapp/app.py",
    "webapp/access_control.py",
    "config/access_control.json.example",
]

# Exact real-config filenames that must never be bundled (the .example templates
# of these are fine and are handled separately below).
FORBIDDEN_EXACT = {"config/app.json", "config/webapp.json", "config/access_control.json"}
# Substring patterns that must never appear anywhere in a shipped path.
FORBIDDEN_SUBSTR = ("out/", "dist/", "__pycache__", ".db", ".xlsx", ".csv", ".png")


def is_forbidden(path: str) -> bool:
    norm = path.replace("\\", "/")
    return norm in FORBIDDEN_EXACT or any(bad in norm for bad in FORBIDDEN_SUBSTR)


def main() -> int:
    missing = [f for f in INCLUDE if not (ROOT / f).exists()]
    if missing:
        print("Missing expected files: " + ", ".join(missing))
        return 1

    for f in INCLUDE:
        if is_forbidden(f):
            print(f"REFUSING TO PACKAGE - '{f}' matches an excluded pattern")
            return 1

    DIST.mkdir(exist_ok=True)
    out = DIST / f"{NAME}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in INCLUDE:
            z.write(ROOT / f, f"{NAME}/{f}")
        # Empty dirs the tool expects to exist at runtime.
        z.writestr(f"{NAME}/config/.gitkeep", "")
        z.writestr(f"{NAME}/out/.gitkeep", "")

    # Verify nothing sensitive slipped in.
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    leaked = [n for n in names
              if any(bad in n for bad in ("app.json", "webapp.json", ".db", ".xlsx", ".csv", ".png"))
              and not n.endswith(".example")]
    if leaked:
        out.unlink()
        print("REFUSING TO SHIP - archive contained: " + ", ".join(leaked))
        return 1

    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"built   : {out}")
    print(f"size    : {out.stat().st_size / 1024:.1f} KB")
    print(f"files   : {len(names)}")
    print(f"sha256  : {sha}")
    print("\ncontains no credentials and no tenant data - safe to hand to a customer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
