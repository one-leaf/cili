"""整理教材版本核对数据，生成 core/templates/textbooks/china_textbook_editions.json。

版本 → 出版单位的对应关系来自清单源路径内嵌的出版社名与公开资料检索核实。
脚本会把映射与清单做交叉校验：正文教材（book_type=textbook）里出现的每个版本
都必须有核对条目，缺漏时直接失败，避免清单更新后版本说明悄悄过期。
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_JSON = ROOT / "core/templates/textbooks/china_textbook_catalog.json"
OUT_JSON = ROOT / "core/templates/textbooks/china_textbook_editions.json"
WIZARD_STAGES = ("小学", "初中", "高中")

EDITION_PUBLISHERS = {
    "人教版": "人民教育出版社",
    "人教版（A版）": "人民教育出版社",
    "人教版（B版）": "人民教育出版社",
    "人教版（PEP）（三年级起点）": "人民教育出版社",
    "人教版（一年级起点）": "人民教育出版社",
    "人教版（精通）（三年级起点）": "人民教育出版社",
    "人教版（五线谱）": "人民教育出版社",
    "人教版（简谱）": "人民教育出版社",
    "人教鄂教版": "人民教育出版社、湖北教育出版社",
    "人教中图版": "人民教育出版社、中国地图出版社",
    "人美版": "人民美术出版社",
    "人音版": "人民音乐出版社",
    "人音版（五线谱）": "人民音乐出版社",
    "人音版（简谱）": "人民音乐出版社",
    "统编版": "教育部统编，人民教育出版社",
    "北师大版": "北京师范大学出版社",
    "北京版": "北京出版社",
    "冀教版": "河北教育出版社",
    "冀教版（一年级起点）": "河北教育出版社",
    "冀教版（三年级起点）": "河北教育出版社",
    "冀人版": "河北人民出版社",
    "冀少版": "河北少年儿童出版社",
    "冀美版": "河北美术出版社",
    "晋教版": "山西教育出版社",
    "晋人版": "山西人民出版社",
    "鲁教版": "山东教育出版社",
    "鲁科版": "山东科学技术出版社",
    "鲁美版": "山东美术出版社",
    "鲁教湘教版": "山东教育出版社、湖南教育出版社（常见叫法「湘鲁版」）",
    "苏教版": "江苏凤凰教育出版社",
    "苏科版": "江苏凤凰科学技术出版社",
    "苏少版": "江苏凤凰少年儿童出版社",
    "苏少版（五线谱）": "江苏凤凰少年儿童出版社",
    "苏少版（简谱）": "江苏凤凰少年儿童出版社",
    "浙教版": "浙江教育出版社",
    "浙科版": "浙江科学技术出版社",
    "浙人美版": "浙江人民美术出版社",
    "沪教版": "上海教育出版社",
    "沪科技版": "上海科学技术出版社",
    "沪科技粤教版": "上海科学技术出版社、广东教育出版社",
    "沪科教版": "上海科技教育出版社",
    "沪外教版": "上海外语教育出版社",
    "沪音版": "上海音乐出版社",
    "沪书画版": "上海书画出版社",
    "湘教版": "湖南教育出版社",
    "湘少版": "湖南少年儿童出版社",
    "湘科版": "湖南科学技术出版社",
    "湘美版": "湖南美术出版社",
    "湘文艺版": "湖南文艺出版社",
    "粤教版": "广东教育出版社",
    "粤教粤科版": "广东教育出版社、广东科技出版社",
    "粤教粤人版": "广东教育出版社、广东人民出版社",
    "粤教花城版": "广东教育出版社、花城出版社",
    "桂教版": "广西教育出版社",
    "桂美版": "广西美术出版社",
    "闽教版": "福建教育出版社",
    "川教版": "四川教育出版社",
    "鄂教版": "湖北教育出版社",
    "豫科版": "河南科学技术出版社",
    "赣美版": "江西美术出版社",
    "岭南美版": "岭南美术出版社",
    "辽海版": "辽海出版社",
    "辽宁师大版": "辽宁师范大学出版社",
    "华东师大版": "华东师范大学出版社",
    "华中师大版": "华中师范大学出版社",
    "广西师大版": "广西师范大学出版社",
    "西南师大版": "西南师范大学出版社（现西南大学出版社）",
    "西南大学版": "西南师范大学出版社（现西南大学出版社）",
    "科学粤教版": "科学出版社、广东教育出版社",
    "科学社版": "科学出版社",
    "科普版": "科学普及出版社",
    "教科版": "教育科学出版社",
    "教科版（EEC学院）": "教育科学出版社（即常见「教科版（EEC）」小学英语）",
    "商务星图版": "商务印书馆、星球地图出版社",
    "中图版": "中国地图出版社",
    "中图中华地图版": "中国地图出版社、中华地图学社",
    "地质社版": "地质出版社",
    "济南版": "济南出版社",
    "武汉版": "武汉出版社",
    "青岛版": "青岛出版社",
    "外研社版": "外语教学与研究出版社",
    "外研社版（一年级起点）": "外语教学与研究出版社",
    "外研社版（三年级起点）": "外语教学与研究出版社",
    "译林版": "译林出版社",
    "译林版三年级起点": "译林出版社",
    "接力社版": "接力出版社",
    "接力版三年级起点": "接力出版社",
    "清华大学版": "清华大学出版社",
    "重庆大学版": "重庆大学出版社",
    "陕旅版": "陕西旅游出版社",
    "西泠印社版": "西泠印社出版社",
    "华文社版": "华文出版社",
    "未来社版": "未来出版社",
    "大象社版": "大象出版社",
}

NOTES = [
    "所有版本简称均真实存在，名称与通行叫法一致。",
    "四个同书异名：鲁教湘教版=湘鲁版；教科版（EEC学院）=教科版（EEC）；西南师大版=西南大学版（同社）；接力社版=接力版（同社）。",
    "path_verified=true 表示清单源路径内嵌出版社全名可直接自证；false 表示经公开资料（国家中小学智慧教育平台、教习网、21世纪教育网等）检索核实。",
]


def main() -> None:
    data = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    source_commit = (data.get("source") or {}).get("commit", "")

    stages_by_edition: dict[str, set[str]] = defaultdict(set)
    subjects_by_edition: dict[str, set[str]] = defaultdict(set)
    path_verified: dict[str, bool] = defaultdict(bool)
    missing: set[str] = set()
    for book in data.get("books", []):
        edition = book.get("edition") or ""
        if book.get("book_type") != "textbook" or book.get("stage") not in WIZARD_STAGES:
            continue
        stages_by_edition[edition].add(book["stage"])
        subjects_by_edition[edition].add(book.get("subject") or "")
        parts = (book.get("source_path") or "").split("/")
        segment = parts[2] if len(parts) > 2 else ""
        if "-" in segment and "出版" in segment:
            path_verified[edition] = True
        if edition and edition not in EDITION_PUBLISHERS:
            missing.add(edition)

    if missing:
        raise SystemExit(f"清单中有版本缺少核对条目: {sorted(missing)}")

    editions = []
    for name in sorted(EDITION_PUBLISHERS):
        editions.append({
            "name": name,
            "publisher": EDITION_PUBLISHERS[name],
            "path_verified": bool(path_verified.get(name)),
            "stages": sorted(stages_by_edition.get(name, ())),
            "subjects": sorted(subjects_by_edition.get(name, ())),
        })

    out = {
        "schema_version": 1,
        "source_commit": source_commit,
        "generated_at": datetime.now().strftime("%Y-%m-%d"),
        "notes": NOTES,
        "editions": editions,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"ok: {len(editions)} editions -> {OUT_JSON.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
