"""Regenerate the workspace score history page from quiz-results.jsonl.

Usage:
    python render_scores.py <workspace_root>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.courseware_records import regenerate_scores  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "workspace_root required"}, ensure_ascii=False))
        return 2
    root = Path(sys.argv[1]).resolve()
    path = regenerate_scores(root)
    print(json.dumps({"success": True, "output": str(path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
