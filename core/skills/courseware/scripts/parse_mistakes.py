"""错题本 CLI：解析 cw-q 条目并合并 quiz-results.jsonl。

实现位于 ``core.courseware_records``（与 web 路由共用同一格式定义）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 独立脚本运行（cwd 可能是工作区）：把项目根加入 sys.path 再导入 core
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from core.courseware_records import merge_quiz_events, parse_mistake_book


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("book")
    parser.add_argument("events", nargs="?")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    book = Path(args.book).resolve()
    entries = parse_mistake_book(book)
    merged = merge_quiz_events(entries, Path(args.events).resolve()) if args.events else 0
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "success": True,
        "entries": len(entries),
        "merged_events": merged,
        "output": str(out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
