"""把验收后的通用知识增量包合并到完整数据；先在独立目录验证。"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from clean import Cleaner, ID_KEYS, ROOT, SCRIPT_DIR, key_text, sha256, write_json, write_markdown
from clean_general_knowledge import ID_FIELDS, STATUSES, inventory, load_json, load_rows, require, validate_bundle
from validate import validate_directory


def merge_record(existing: dict, incoming: dict, table: str) -> dict:
    """仅合并身份明确的同ID记录；旧字段和两侧追溯均保留。"""
    identity = {"entities": ["entity_type", "name"], "aliases": ["entity_id", "entity_type"],
                "relations": ["head_id", "relation_type", "tail_id"]}
    if table not in identity:
        require(existing == incoming, f"{table}: 同ID业务内容冲突")
        return copy.deepcopy(existing)
    require(all(existing[k] == incoming[k] for k in identity[table]), f"{table}: 同ID含义冲突")
    if table == "aliases":
        require(key_text(existing["alias"]) == key_text(incoming["alias"]), "aliases: 同ID别名不同")
    merged = copy.deepcopy(existing)
    for field in ["source_ids", "raw_locators"]:
        merged[field] = sorted(set(existing.get(field, []) + incoming.get(field, [])))
    merged["raw_locators"] = sorted(set(merged["raw_locators"] + [existing["raw_locator"], incoming["raw_locator"]]))
    merged["merge_notes"] = sorted({n for n in [existing.get("notes"), incoming.get("notes"),
                                                 *existing.get("merge_notes", [])] if n})
    if incoming["review_status"] == "accepted":
        merged["review_status"] = "accepted"
    if table == "aliases":
        merged["ambiguous"] = bool(existing.get("ambiguous") or incoming.get("ambiguous"))
    return merged


def merge_partitions(base: Path, bundle: Path) -> tuple[dict, dict]:
    partitions, statistics = {}, {}
    for table, key in ID_KEYS.items():
        records = {}
        for status in STATUSES:
            for row in load_rows(base / ("" if status == "accepted" else status) / (table + ".jsonl")):
                require(row[key] not in records, f"基础数据ID重复: {table}/{row[key]}")
                records[row[key]] = row
        added, combined = [], []
        if table in ID_FIELDS:
            for status in STATUSES:
                for incoming in load_rows(bundle / ("" if status == "accepted" else status) / (table + ".jsonl")):
                    rid = incoming[key]
                    if rid in records:
                        records[rid] = merge_record(records[rid], incoming, table)
                        combined.append(rid)
                    else:
                        records[rid] = incoming
                        added.append(rid)
        partitions[table] = {status: sorted((r for r in records.values() if r["review_status"] == status), key=lambda r: r[key])
                             for status in STATUSES}
        statistics[table] = {"added_ids": sorted(added), "merged_ids": sorted(combined)}
    return partitions, statistics


def stage_merge(base: Path, bundle: Path, output: Path, as_of: date) -> dict:
    base, bundle, output = base.resolve(), bundle.resolve(), output.resolve()
    for protected in [base, bundle, ROOT / "data_raw", ROOT / "data"]:
        require(not (output == protected or output.is_relative_to(protected) or protected.is_relative_to(output)),
                "合并须使用独立的暂存目录，不能直接覆盖输入或主data")
    initial = validate_directory(base, ROOT / "data_raw")
    require(initial["passed"], "基准数据验证失败")
    initial = validate_bundle(bundle)
    require(initial["passed"], "增量包验证失败: " + "; ".join(initial["errors"][:5]))
    before = inventory(base)
    partitions, statistics = merge_partitions(base, bundle)
    shutil.copytree(base, output, dirs_exist_ok=True)
    for table, groups in partitions.items():
        for status, rows in groups.items():
            write_json(output / ("" if status == "accepted" else status) / (table + ".jsonl"), rows, lines=True)
    for category in ["audit", "auxiliary"]:
        for path in sorted((bundle / category).glob("*.jsonl")):
            target = output / category / "general_knowledge" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    overlaps = load_rows(bundle / "audit/existing_knowledge_overlaps.jsonl")
    for row in overlaps:
        row["merge_decision"] = "retain_both_with_original_scope"
        row["review_reason"] = "本批逐对核对后保留：表述、条件或信息粒度不同，无完全相同正文，来源各自追溯。"
    write_json(output / "audit/general_knowledge/overlap_merge_decisions.jsonl", overlaps, lines=True)
    report = load_json(base / "quality_report.json")
    report["base_cleaning_as_of_date"] = report["as_of_date"]
    report["as_of_date"] = as_of.isoformat()
    for table, groups in partitions.items():
        report["counts"][table] = {**{s: len(r) for s, r in groups.items()}, "total": sum(map(len, groups.values()))}
    report["source_evidence"] = dict(Counter(r["evidence_form"] for rows in partitions["sources"].values() for r in rows))
    report["knowledge_categories"] = dict(Counter(r["category"] for r in partitions["knowledge"]["accepted"]))
    report["knowledge_origins"] = dict(Counter(r["content_origin"] for r in partitions["knowledge"]["accepted"]))
    report["incremental_counts"] = load_json(bundle / "quality_report.json")["input_counts"]
    report["limitations"] = [s for s in report["limitations"] if "颜色/甜度基础" not in s and "未生成embedding" not in s]
    report["limitations"].append("已加入90条通用常识，原有地域、品种趋势、侍酒参考与保鲜条件随正文保留；本地整理未重新联网核验。")
    report["limitations"].append("清洗与合并本身不构建向量，数据库及向量构建结果以database中的实际报告为准。")
    merge_report = {"as_of_date": as_of.isoformat(), "base_dir": str(base), "bundle_dir": str(bundle),
        "knowledge_before": len(load_rows(base / "knowledge.jsonl")), "knowledge_added": len(statistics["knowledge"]["added_ids"]),
        "knowledge_after": len(partitions["knowledge"]["accepted"]), "statistics": statistics,
        "overlap_pairs_reviewed": len(overlaps), "overlap_policy": "retain_both_with_original_scope",
        "output_counts": {n: len(g["accepted"]) for n, g in partitions.items()},
        "database_import_performed_by_merge_script": False, "database_import_report": "../database/database_import_report.json"}
    write_json(output / "merge_report.json", merge_report)
    write_json(output / "quality_report.json", report)
    writer = Cleaner.__new__(Cleaner)
    writer.rules, writer.as_of, writer.report = {"rules_version": report["rules_version"]}, as_of, report
    writer.visitor_input_count = report["counts"]["visitor_reviews"]["input_rows"]
    writer.visitor_reviews = load_rows(base / "auxiliary/visitor_reviews.jsonl") + load_rows(base / "excluded/visitor_reviews.jsonl")
    readme = writer.data_readme(partitions).replace("来源S...→Document", "来源S.../GS...→Document").replace("知识K.../KP_.../KR_...→Chunk", "知识K.../KP_.../KR_.../WK...→Chunk")
    readme += "\n\n## 通用常识合并（2026-11-15）\n\n"
    readme += ("原138条正式知识与新增90条合并为228条。8个共享实体和6条共享别名按业务ID合并来源与追溯，"
               "正式实体200个、别名236条、来源84个、图关系746条。原产品、酒庄、价格和专业评价完整保留。\n\n"
               "通用知识的text包含问题、回答和适用范围；question、answer、category_raw另行保留。"
               "FAQ和开发题位于auxiliary/general_knowledge，不重复索引。11对文本重叠候选的逐对合并决定见"
               "audit/general_knowledge/overlap_merge_decisions.jsonl。新资料原件保存在data_raw/葡萄酒通用常识知识库_20261111。\n\n"
               "完整复跑须先将原宁夏资料清洗到独立基准目录，再清洗通用知识并执行data_cleaning/merge_general_knowledge.py。"
               "不要仅用旧clean.py覆盖主data而遗漏本次新增知识。数据库导入和向量状态见../database中的报告。\n")
    write_markdown(output / "README.md", readme)
    manifest = load_json(base / "manifest.json")
    increment_manifest = load_json(bundle / "manifest.json")
    raw_root = (ROOT / "data_raw").resolve()
    input_root = Path(increment_manifest["input_root"]).resolve()
    require(input_root.is_relative_to(raw_root), "合并前请把增量原件复制到项目data_raw并重新清洗")
    inputs = {r["path"]: r for r in manifest["input_files"]}
    for relative, digest in increment_manifest["input_files"].items():
        path = input_root / relative
        require(sha256(path) == digest, "通用资料原件发生变化")
        name = "data_raw/" + path.relative_to(raw_root).as_posix()
        inputs[name] = {"path": name, "size_bytes": path.stat().st_size, "sha256": digest,
                        "handling": "general_knowledge_canonical_or_export_crosscheck"}
    manifest.update(as_of_date=as_of.isoformat(), output_root=str(ROOT / "data"), input_files=[inputs[p] for p in sorted(inputs)],
        merge_base_manifest_sha256=sha256(base / "manifest.json"), increment_manifest_sha256=sha256(bundle / "manifest.json"),
        script_hashes={p.name: sha256(p) for p in sorted(SCRIPT_DIR.glob("*.py"))},
        incremental_rules_sha256=sha256(SCRIPT_DIR / "general_knowledge_rules.json"))
    manifest["output_files"] = [{"path": p.relative_to(output).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256(p)}
        for p in sorted(output.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "validation_report.json"}]
    write_json(output / "manifest.json", manifest)
    validation = validate_directory(output, raw_root)
    write_json(output / "validation_report.json", validation)
    require(validation["passed"], "合并后验证失败: " + "; ".join(validation["errors"][:10]))
    require(inventory(base) == before, "基准副本发生变化")
    return {"staged_dir": str(output), "counts": merge_report["output_counts"], "validation": validation}


def apply_staged(base: Path, stage: Path, target: Path) -> None:
    base, stage, target = base.resolve(), stage.resolve(), target.resolve()
    require(target == (ROOT / "data").resolve(), "本发布函数只写入项目主data")
    require(inventory(target) == inventory(base), "主data已偏离合并基准，停止覆盖")
    verification = validate_directory(stage, ROOT / "data_raw")
    require(verification["passed"], "暂存数据验证失败")
    paths = [p for p in sorted(stage.rglob("*")) if p.is_file() and p.name != "manifest.json"]
    for path in [*paths, stage / "manifest.json"]:
        destination = target / path.relative_to(stage)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".merge_tmp")
        shutil.copy2(path, temporary)
        temporary.replace(destination)
    final = validate_directory(target, ROOT / "data_raw")
    require(final["passed"], "主data验证失败")
    require(inventory(target) == inventory(stage), "主data与验收的暂存数据不同")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, default=SCRIPT_DIR / "wine_general_knowledge_20261111")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    parser.add_argument("--apply", action="store_true", help="暂存验证通过且主数据仍匹配基准后，替换主data的同名文件")
    args = parser.parse_args()
    result = stage_merge(args.base_dir, args.bundle_dir, args.output_dir, args.as_of_date)
    if args.apply:
        apply_staged(args.base_dir, args.output_dir, ROOT / "data")
        result["applied_to_main"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
