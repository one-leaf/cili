#!/usr/bin/env python3
"""Update version in footer.json to current date."""

import json
from datetime import datetime
from pathlib import Path

FOOTER_PATH = Path(__file__).parent.parent / "web" / "static" / "footer.json"


def update_version():
    """Update version field to v + current date (YYYYMMDD)."""
    if not FOOTER_PATH.exists():
        print(f"Error: {FOOTER_PATH} not found")
        return False

    with open(FOOTER_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    new_version = datetime.now().strftime("v%Y%m%d")
    if data.get("version") == new_version:
        print(f"Version already up to date: {new_version}")
        return True

    data["version"] = new_version

    with open(FOOTER_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Version updated to: {new_version}")
    return True


if __name__ == "__main__":
    update_version()
