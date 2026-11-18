"""按已审核的规则填补业务空值；生成来源单独记录，不联网调用模型。"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from clean import ID_KEYS, ROOT, SCRIPT_DIR, read_jsonl, sha256, write_json, write_markdown

sys.path.insert(0, str(ROOT / "database"))
from dataset import load_dataset
from generate_mysql_sql import build_tables

REVISION = "realistic-null-fill-20261116-v1"
RULES_PATH = SCRIPT_DIR / "realistic_null_fill_rules.json"
TARGET_KEYS = {"winery": ["winery_id"], "product": ["product_id"],
               "product_grape": ["head_id", "tail_id"], "price_observation": ["price_id"],
               "professional_review": ["review_id"]}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def inventory(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def field_is_filled(row, field):
    """识别带前值、后值和版本记录的已批准空值补充，不能用泛化布尔标志绕过检查。"""
    metadata = row.get("null_fill", {})
    value = metadata.get("fields", {}).get(field, {})
    return (metadata.get("revision") == REVISION and value.get("before_missing") is True
            and value.get("before") is None and "after" in value and value["after"] is not None
            and value["after"] == row.get(field) and value.get("origin") in {"generated", "derived", "public_source", "reused"})


def fill_field(journal, table, row, field, value, *, origin="generated", source=None):
    if row.get(field) is not None:
        return False
    require(value is not None, "不能用空值填充")
    row[field] = value
    entry = {"before_missing": True, "before": None, "after": value, "origin": origin,
             "generated": origin in {"generated", "derived"}}
    if source:
        entry["source"] = source
    metadata = row.setdefault("null_fill", {"revision": REVISION, "approved_on": "2026-11-16", "fields": {}})
    require(metadata["revision"] == REVISION, "记录已含其他版本的填充")
    metadata["fields"][field] = entry
    journal.append({"table": table, "record_id": "/".join(str(row[k]) for k in TARGET_KEYS[table]),
                    "field": field, **entry})
    return True


def build_overlay(tables, rules):
    data = copy.deepcopy(tables)
    journal, dependencies = [], []
    wineries = {r["winery_id"]: r for r in data["wineries"]}
    require({w for w, r in wineries.items() if r["company_name"] is None} == set(rules["company_names"]), "公司名称候选与已审核范围不一致")
    for wid, row in wineries.items():
        public = rules["public_contacts"].get(wid, {})
        if row["company_name"] is None:
            fill_field(journal, "winery", row, "company_name", rules["company_names"][wid], origin="public_source", source=rules["public_sources"][wid])
        address = row.get("address_normalized") or row.get("address_raw") or public.get("address") or rules["generated_addresses"].get(wid)
        origin = "reused" if row.get("address_normalized") or row.get("address_raw") else "public_source" if public.get("address") else "generated"
        require(bool(address), f"缺少地址候选: {wid}")
        for field in ["address_raw", "address_normalized"]:
            fill_field(journal, "winery", row, field, address, origin=origin, source=public.get("source_url") if origin == "public_source" else "same_winery_existing_address" if origin == "reused" else None)
        company_year = row.get("company_founded_year")
        founded = row.get("founded_year") or rules["winery_founded_years"][wid]
        if company_year is not None:
            founded = max(founded, company_year)
        fill_field(journal, "winery", row, "founded_year", founded,
                   origin="public_source" if wid == "W012" else "generated", source=rules["public_sources"].get(wid) if wid == "W012" else None)
        fill_field(journal, "winery", row, "company_founded_year", row["founded_year"] - (1 + int(wid[1:]) % 3))
        require(row["company_founded_year"] <= row["founded_year"], f"公司与酒庄年份冲突: {wid}")
        domain = urlsplit(row.get("website") or public.get("website") or "https://www." + rules["generated_domains"].get(wid, "unused.cn")).hostname
        domain = domain.removeprefix("www.")
        fill_field(journal, "winery", row, "website", public.get("website") or "https://www." + domain,
                   origin="public_source" if public.get("website") else "generated", source=public.get("source_url") if public.get("website") else None)
        fill_field(journal, "winery", row, "email", public.get("email") or "info@" + domain,
                   origin="public_source" if public.get("email") else "generated", source=public.get("source_url") if public.get("email") else None)
        area = "0952" if wid == "W019" else "0951"
        fill_field(journal, "winery", row, "telephone", public.get("telephone") or f"{area}-869{int(wid[1:]):04d}",
                   origin="public_source" if public.get("telephone") else "generated", source=public.get("source_url") if public.get("telephone") else None)
    products = {r["product_id"]: r for r in data["products"]}
    require({p for p, r in products.items() if r["series"] is None} == set(rules["series"]), "系列范围与审核清单不一致")
    require({p for p, r in products.items() if r["price_cny"] is None} == set(rules["prices_cny"]), "价格范围与审核清单不一致")
    sweetness = {"dry": "干型", "semi_dry": "半干型", "semi_sweet": "半甜型", "sweet": "甜型", "other": "其他"}
    for pid, row in products.items():
        if row["series"] is None:
            fill_field(journal, "product", row, "series", rules["series"][pid], origin="reused" if rules["series"][pid] == "西鸽N系列" else "generated")
        if row["vintage"] is None and row["vintage_status"] == "unspecified":
            fill_field(journal, "product", row, "vintage", rules["vintages"][pid])
            before = row["vintage_status"]
            row["vintage_status"] = "specified"
            row["null_fill"]["dependencies"] = {"vintage_status": {"before": before, "after": "specified", "because": "vintage"}}
            dependencies.append({"table": "product", "record_id": pid, "field": "vintage_status", "before": before, "after": "specified"})
        fill_field(journal, "product", row, "volume_ml", 750, origin="derived", source="existing_standard_bottle_volume")
        if row["color"] is None:
            fill_field(journal, "product", row, "color", rules["colors"][pid])
        fill_field(journal, "product", row, "sweetness", "other" if row.get("is_sparkling") else "dry")
        fill_field(journal, "product", row, "sweetness_raw", "Brut" if pid == "P043" else sweetness[row["sweetness"]], origin="derived", source="sweetness")
        fill_field(journal, "product", row, "is_sparkling", False)
        if row["alcohol_pct_vol"] is None:
            fill_field(journal, "product", row, "alcohol_pct_vol", rules["alcohol_pct_vol"][pid])
        if fill_field(journal, "product", row, "region_id", wineries[row["winery_id"]]["region_id"], origin="derived", source="winery:" + row["winery_id"]):
            if row.get("region_raw") is None:
                region = next(e for e in data["entities"] if e["entity_id"] == row["region_id"])
                row["region_raw"] = region["name"]
                row["null_fill"].setdefault("derived_fields", {})["region_raw"] = {"before": None, "after": region["name"], "because": "region_id"}
        fill_field(journal, "product", row, "grapes_complete", False)
        fill_field(journal, "product", row, "price_cny", rules["prices_cny"][pid])
        require((row["vintage"] is not None) == (row["vintage_status"] == "specified"), "年份状态不一致")
    grapes = defaultdict(list)
    for row in data["relations"]:
        if row["relation_type"] == "MADE_FROM":
            grapes[row["head_id"]].append(row)
    for pid, relations in grapes.items():
        if not any(r["proportion_pct"] is None for r in relations):
            continue
        require(all(r["proportion_pct"] is None for r in relations), "不能覆盖已有明确比例")
        proportions = {1: [100], 2: [60, 40], 3: [50, 30, 20]}[len(relations)]
        for relation, pct in zip(sorted(relations, key=lambda r: r["tail_id"]), proportions):
            fill_field(journal, "product_grape", relation, "proportion_pct", pct)
            for grape in products[pid]["grape_varieties"]:
                if grape["entity_id"] == relation["tail_id"]:
                    require(grape.get("proportion_pct") is None, "嵌套品种比例已有值")
                    grape["proportion_pct"] = pct
                    products[pid]["null_fill"].setdefault("derived_fields", {})["grape_varieties." + grape["entity_id"] + ".proportion_pct"] = {"before": None, "after": pct, "because": relation["relation_id"]}
        require(sum(r["proportion_pct"] for r in relations) == 100, "配比合计不是100")
    for row in data["price_observations"]:
        fill_field(journal, "price_observation", row, "pack_quantity", 1)
        fill_field(journal, "price_observation", row, "minimum_order_raw", "1瓶起订")
    red_summaries = ["酒液呈深宝石红色，带有黑莓、黑樱桃和淡淡雪松香气，单宁细致，结构饱满，余味持久。",
                     "红色水果与香料气息交织，入口圆润，单宁柔顺，酸度与酒体较为均衡，收尾带有轻微烘烤香。",
                     "以成熟李子和黑加仑香气为主，伴随可可与香草气息，口感集中，单宁紧实，余味清晰。"]
    for row in data["professional_reviews"]:
        product = products[row["product_id"]]
        summary = ("酒液呈浅柠檬黄色，带有柑橘、白桃和淡淡烘烤香气，酸度清爽，口感圆润。" if product["color"] == "white"
                   else "酒液呈淡鲑红色，草莓与红樱桃香气清新，入口轻盈，酸度活泼，余味爽净。" if product["color"] == "rose"
                   else red_summaries[int(row["review_id"][1:]) % len(red_summaries)])
        fill_field(journal, "professional_review", row, "summary", summary)
        fill_field(journal, "professional_review", row, "score", 85 + int(row["review_id"][1:]) % 8)
        fill_field(journal, "professional_review", row, "score_scale", 100)
        fill_field(journal, "professional_review", row, "score_raw", f"{row['score']:g}/{row['score_scale']:g}", origin="derived", source="score_and_score_scale")
    require(len(journal) == rules["expected_filled_cells"], f"填充值数量不符: {len(journal)}")
    require(len(dependencies) == 14, "年份联动记录数不符")
    return data, journal, dependencies


def validate_preservation(before, after):
    """独立逐字段验证，只允许空值填补、重复业务属性同步及必要年份状态变化。"""
    for table, key in ID_KEYS.items():
        old = {r[key]: r for r in before[table]}
        new = {r[key]: r for r in after[table]}
        require(set(old) == set(new), f"记录集合改变: {table}")
        for rid, original in old.items():
            current = new[rid]
            for field, value in original.items():
                if value is None or current.get(field) == value:
                    continue
                if table == "products" and field == "vintage_status" and original["vintage"] is None and value == "unspecified":
                    require(current[field] == "specified" and field_is_filled(current, "vintage"), "年份状态越界修改")
                    continue
                if table == "products" and field == "grape_varieties":
                    a, b = copy.deepcopy(value), copy.deepcopy(current[field])
                    require(len(a) == len(b), "品种集合改变")
                    for first, second in zip(a, b):
                        if first.get("proportion_pct") is None:
                            second["proportion_pct"] = None
                    require(a == b, "除空配比外的品种信息改变")
                    continue
                require(False, f"已有非空值发生变化: {table}/{rid}/{field}")
    require(before["knowledge"] == after["knowledge"], "知识原文发生变化")


def stage(base, output):
    from validate import validate_directory
    base, output = base.resolve(), output.resolve()
    for protected in [base, ROOT / "data", ROOT / "data_raw"]:
        require(not (output == protected or output.is_relative_to(protected) or protected.is_relative_to(output)), "必须使用独立暂存目录")
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    dataset = load_dataset(base)
    require(dataset.snapshot == rules["base_snapshot"], "数据版本与已审核版本不一致")
    before = dataset.tables
    before_nulls = {t.name: sum(v is None for r in t.rows for v in r.values()) for t in build_tables(dataset)}
    require(sum(before_nulls.values()) == rules["expected_nulls_before"], "空值范围发生变化")
    base_hashes = inventory(base)
    data, journal, dependencies = build_overlay(before, rules)
    validate_preservation(before, data)
    shutil.copytree(base, output, dirs_exist_ok=True)
    schema = json.loads((base / "schema.json").read_text(encoding="utf-8"))
    schema["$defs"]["products"]["properties"]["price_cny"] = {
        "type": ["number", "null"], "exclusiveMinimum": 0,
        "description": "非空值仅用于经审核的展示价格，须有null_fill.price_cny来源记录，不表示已核验零售价。"}
    schema["$defs"]["products"].setdefault("allOf", []).append({
        "if": {"required": ["price_cny"], "properties": {"price_cny": {"type": "number"}}},
        "then": {"required": ["null_fill"]}})
    write_json(output / "schema.json", schema)
    for name, key in ID_KEYS.items():
        write_json(output / (name + ".jsonl"), sorted(data[name], key=lambda r: r[key]), lines=True)
    counts = Counter(r["table"] for r in journal)
    fields = defaultdict(Counter)
    for r in journal:
        fields[r["table"]][r["field"]] += 1
    report = {"revision": REVISION, "as_of_date": rules["as_of_date"], "base_snapshot": dataset.snapshot,
              "null_cells_before": sum(before_nulls.values()), "filled_cells": len(journal),
              "expected_null_cells_after": rules["expected_nulls_after"], "fills_by_table": dict(counts),
              "fills_by_field": {k: dict(v) for k, v in fields.items()},
              "origins": dict(Counter(r["origin"] for r in journal)), "year_status_dependencies": len(dependencies),
              "knowledge_text_unchanged": True, "called_external_model": False,
              "protected_nulls": {"source_document.published_at": 66, "price_observation.verified_at": 10,
                                  "product.non_vintage_year": 3, "professional_review.medal": 11,
                                  "professional_review.competition_year": 11},
              "note": "用户授权的真实风格测试填充。业务字段不加占位前缀；生成、派生、复用和公开资料在null_fill元数据分别记录。生成的网站、邮箱及电话号码不保证可访问或可联系。"}
    write_json(output / "null_fill_report.json", report)
    write_json(output / "audit/null_fill_changes.jsonl", journal, lines=True)
    write_json(output / "audit/null_fill_dependencies.jsonl", dependencies, lines=True)
    quality = json.loads((base / "quality_report.json").read_text(encoding="utf-8"))
    quality["as_of_date"] = rules["as_of_date"]
    quality["null_fill"] = {k: report[k] for k in ["revision", "filled_cells", "fills_by_table", "origins", "expected_null_cells_after"]}
    all_products = data["products"] + read_jsonl(base / "review/products.jsonl") + read_jsonl(base / "excluded/products.jsonl")
    for field, stats in quality["product_fields"].items():
        stats["all_non_null"] = sum(r[field] is not None and r[field] != [] for r in all_products)
        stats["accepted_non_null"] = sum(r[field] is not None and r[field] != [] for r in data["products"])
    quality["limitations"].append("2026-11-16用户授权补齐533个业务空值。生成值与原始采集值通过null_fill字段来源区分，保留原来的筛选和价格核验资格。")
    write_json(output / "quality_report.json", quality)
    readme = (base / "README.md").read_text(encoding="utf-8").replace("price_cny本批保持null", "price_cny含审定生成的展示金额，见null_fill元数据")
    readme = readme.replace("- price_cny全部为空；价格只在独立观察表中。", "- price_cny的空值已生成展示金额，字段来源标记为generated；当前可用售价仍由独立核验报价决定。")
    readme += "\n\n## 已审核空值填充（2026-11-16）\n\n业务空值由634处减至101处，填充533处。"
    readme += "公司名称优先对应公开资料，地区复用所属酒庄；34条系列按审核名单编排。生成参数和联系方式仅作测试，"
    readme += "逐字段来源与原值保存在null_fill和audit/null_fill_changes.jsonl。原有非空业务值保留，14条年份状态随新增年份联动。"
    readme += "来源发布日期、报价核验时间、3款NV酒年份以及非赛事奖项等101处空值保留。知识正文和228条向量保持原样，不调用外部模型。\n"
    write_markdown(output / "README.md", readme)
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(as_of_date=rules["as_of_date"], output_root=str(ROOT / "data"),
                    null_fill_revision=REVISION, null_fill_base_manifest_sha256=sha256(base / "manifest.json"),
                    null_fill_rules_sha256=sha256(RULES_PATH),
                    script_hashes={p.name: sha256(p) for p in sorted(SCRIPT_DIR.glob("*.py"))})
    manifest["output_files"] = [{"path": p.relative_to(output).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256(p)}
                                for p in sorted(output.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "validation_report.json"}]
    write_json(output / "manifest.json", manifest)
    staged = load_dataset(output)
    after_nulls = {t.name: sum(v is None for r in t.rows for v in r.values()) for t in build_tables(staged)}
    require(sum(after_nulls.values()) == rules["expected_nulls_after"], "剩余空值数量不符")
    require(sha256(base / "knowledge.jsonl") == sha256(output / "knowledge.jsonl"), "知识文件被改动")
    validation = validate_directory(output, ROOT / "data_raw")
    write_json(output / "validation_report.json", validation)
    require(validation["passed"], "填充结果验证失败: " + "; ".join(validation["errors"][:10]))
    require(inventory(base) == base_hashes, "备份文件发生变化")
    return {"snapshot": staged.snapshot, "filled_cells": len(journal), "remaining_nulls": sum(after_nulls.values()),
            "fills_by_table": dict(counts), "validation": validation, "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = stage(args.base_dir, args.output_dir)
    if args.apply:
        from merge_general_knowledge import apply_staged
        apply_staged(args.base_dir, args.output_dir, ROOT / "data")
        result["applied_to_main"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
