"""验证清洗结果的JSON结构、业务关系、筛选资格与文件完整性。"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from clean import ID_KEYS, REL_TYPES, ROOT, read_jsonl, sha256


def schema_errors(value, schema: dict, path: str = "record") -> list[str]:
    """实现本项目Schema实际使用的关键字，避免引入第三方依赖。"""
    errors = []
    types = schema.get("type")
    if types:
        types = [types] if isinstance(types, str) else types
        predicates = {
            "null": value is None, "boolean": isinstance(value, bool),
            "string": isinstance(value, str), "number": isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "object": isinstance(value, dict), "array": isinstance(value, list),
        }
        if not any(predicates.get(t, False) for t in types):
            return [f"{path}: expected {types}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum {value!r}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"{path}: const mismatch")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: string too short")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors.extend(schema_errors(item, props[key], path + "." + key))
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(schema_errors(item, schema["additionalProperties"], path + "." + key))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True, ensure_ascii=False) for x in value}) != len(value):
            errors.append(f"{path}: duplicate array items")
        for i, item in enumerate(value):
            if "items" in schema:
                errors.extend(schema_errors(item, schema["items"], f"{path}[{i}]"))
    return errors


def validate_directory(output: Path, raw: Path) -> dict:
    output, raw = output.resolve(), raw.resolve()
    errors, checks = [], Counter()

    def check(ok: bool, message: str, category: str):
        checks[category] += 1
        if not ok:
            errors.append(message)

    schema = json.loads((output / "schema.json").read_text(encoding="utf-8"))
    report = json.loads((output / "quality_report.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    all_tables, accepted = {}, {}
    for name, id_key in ID_KEYS.items():
        all_tables[name] = []
        counts = {}
        for status in ["accepted", "review", "excluded"]:
            path = output / ("" if status == "accepted" else status) / (name + ".jsonl")
            check(path.is_file(), f"missing {path}", "files")
            if not path.is_file():
                continue
            records = read_jsonl(path)
            counts[status] = len(records)
            if status == "accepted":
                accepted[name] = records
            for record in records:
                check(record.get("review_status") == status, f"{path.name}: status/path mismatch", "status")
                se = schema_errors(record, schema["$defs"][name], f"{name}:{record.get(id_key)}")
                checks["schema"] += 1
                errors.extend(se)
                all_tables[name].append(record)
        ids = [r[id_key] for r in all_tables[name]]
        check(len(ids) == len(set(ids)), f"duplicate IDs: {name}", "uniqueness")
        for status, count in counts.items():
            check(report["counts"][name][status] == count, f"report count mismatch {name}/{status}", "report")
    all_sources = {s["source_id"]: s for s in all_tables["sources"]}
    nodes, node_types = {}, {}
    for name, kind in [("sources", "Document"), ("wineries", "Winery"), ("products", "Product"), ("knowledge", "Chunk"), ("entities", None)]:
        for r in all_tables[name]:
            rid = r[ID_KEYS[name]]
            check(rid not in nodes, f"cross-type ID collision {rid}", "uniqueness")
            nodes[rid] = r
            node_types[rid] = kind or r["entity_type"]
    accepted_nodes = {nid for nid, r in nodes.items() if r["review_status"] == "accepted"}
    all_wineries = {r["winery_id"]: r for r in all_tables["wineries"]}
    all_products = {r["product_id"]: r for r in all_tables["products"]}
    entities = {r["entity_id"]: r for r in all_tables["entities"]}
    for name, records in all_tables.items():
        for r in records:
            if name == "sources":
                check(r["dataset_role"] == "real", "demo source entered cleaned dataset", "provenance")
                check(r.get("snapshot_path") is None, "unexpected fabricated snapshot", "provenance")
                continue
            sources = r.get("source_ids", [])
            check(bool(sources) and all(s in all_sources for s in sources), f"{name}:{r[ID_KEYS[name]]}: source missing", "foreign_keys")
            locator = r.get("raw_locator", "")
            check(locator.startswith("data_raw/"), f"invalid raw locator {locator}", "provenance")
            raw_path = raw / locator.removeprefix("data_raw/").split("#", 1)[0]
            check(raw_path.is_file(), f"locator file absent {raw_path}", "provenance")
            if r["review_status"] == "accepted":
                check(all(all_sources[s]["dataset_role"] == "real" for s in sources), "accepted row references demo", "provenance")
    for p in all_products.values():
        check(p["winery_id"] in all_wineries, f"{p['product_id']}: winery missing", "foreign_keys")
        check((p["vintage"] is not None) == (p["vintage_status"] == "specified"), f"{p['product_id']}: vintage inconsistency", "business")
        check(p["price_cny"] is None, f"{p['product_id']}: reference price leaked into product", "business")
        if p["review_status"] == "accepted":
            check(p["winery_id"] in accepted_nodes, f"{p['product_id']}: accepted product has nonaccepted parent", "foreign_keys")
            check(p["record_scope"] != "catalog_candidate", f"{p['product_id']}: catalog in formal data", "business")
        for field, flag in p["field_filter_eligibility"].items():
            check(isinstance(flag, bool), f"{p['product_id']}/{field}: non-bool eligibility", "business")
            if flag:
                check(p["review_status"] == "accepted" and p.get(field) is not None and p.get(field) != [],
                      f"{p['product_id']}/{field}: eligibility without usable field", "business")
        check(p["strict_product_filter_eligible"] == any(p["field_filter_eligibility"].values()),
              f"{p['product_id']}: overall filter flag mismatch", "business")
        if p["region_id"]:
            check(p["region_id"] in entities and entities[p["region_id"]]["entity_type"] == "Region", f"{p['product_id']}: invalid region", "foreign_keys")
        for g in p["grape_varieties"]:
            check(g["entity_id"] in entities and entities[g["entity_id"]]["entity_type"] == "GrapeVariety", "invalid grape entity", "foreign_keys")
    for w in all_wineries.values():
        if w["region_id"]:
            check(w["region_id"] in entities, f"{w['winery_id']}: region missing", "foreign_keys")
    for name in ["price_observations", "professional_reviews"]:
        for r in all_tables[name]:
            check(r["product_id"] in all_products, f"{name}: missing product {r['product_id']}", "foreign_keys")
            if r["review_status"] == "accepted":
                check(r["product_id"] in accepted_nodes, f"{name}: formal record references review product", "foreign_keys")
    for p in all_tables["price_observations"]:
        check(p["current_retail_eligible"] is False and p["strict_price_filter_eligible"] is False and p["verified_at"] is None,
              "unverified current retail price", "business")
        if p["review_status"] != "excluded":
            check(p["amount_min"] is not None and p["amount_max"] is not None and 0 < p["amount_min"] <= p["amount_max"], "invalid quote amounts", "business")
    for a in all_tables["aliases"]:
        check(a["entity_id"] in nodes and node_types.get(a["entity_id"]) == a["entity_type"], "alias type or target invalid", "foreign_keys")
        if a["review_status"] == "accepted":
            check(a["entity_id"] in accepted_nodes, "formal alias targets review node", "foreign_keys")
    for rel in all_tables["relations"]:
        head, tail, kind = rel["head_id"], rel["tail_id"], rel["relation_type"]
        check(head in nodes and tail in nodes, f"relation missing endpoint: {rel['relation_id']}", "foreign_keys")
        if kind in REL_TYPES:
            check(node_types.get(head) in REL_TYPES[kind][0] and node_types.get(tail) in REL_TYPES[kind][1],
                  f"relation type mismatch {rel['relation_id']}", "business")
        if rel["review_status"] == "accepted":
            check(head in accepted_nodes and tail in accepted_nodes, "formal relation references review endpoint", "foreign_keys")
    text_keys = set()
    for k in all_tables["knowledge"]:
        check(all(e in nodes for e in k["entity_ids"]), f"{k['knowledge_id']}: knowledge entity missing", "foreign_keys")
        if k["review_status"] == "accepted":
            check(k["retrieval_allowed"] is True and k["contains_price"] is False, "formal knowledge retrieval/price flag invalid", "business")
            check(not re.search(r"(?:[¥￥]|CNY|HKD)\s*\d|\d+(?:\.\d+)?\s*元", k["text"]),
                  f"{k['knowledge_id']}: quote amount leaked into general RAG", "business")
            check(all(e in accepted_nodes for e in k["entity_ids"]), "formal knowledge references review entity", "foreign_keys")
            signature = (k["text"], tuple(sorted(k["entity_ids"])))
            check(signature not in text_keys, "duplicate formal knowledge text/scope", "uniqueness")
            text_keys.add(signature)
            check(not any(all_sources[s]["evidence_form"] == "search_snippet" for s in k["source_ids"]) or
                  any(all_sources[s]["evidence_form"] == "collector_summary" for s in k["source_ids"]),
                  "snippet-only knowledge in formal data", "business")
    auxiliary_sources = {r["source_id"] for r in read_jsonl(output / "auxiliary/review_sources.jsonl")}
    visitor = read_jsonl(output / "auxiliary/visitor_reviews.jsonl") + read_jsonl(output / "excluded/visitor_reviews.jsonl")
    forbidden = {"user_name", "user_level", "userInfo", "ip", "ip_location", "user", "用户"}
    check(len({r["review_id"] for r in visitor}) == len(visitor), "visitor IDs not unique", "uniqueness")
    for r in visitor:
        check(not (forbidden & r.keys()), "personal profile field exported", "privacy")
        check(r["product_id"] is None and r["approved_usage"] == [] and r["review_status"] != "accepted", "visitor promoted to product fact", "business")
        check(bool(r["source_ids"]) and all(s in auxiliary_sources for s in r["source_ids"]), "visitor source missing", "foreign_keys")
        if r["winery_id"]:
            check(r["winery_id"] in all_wineries, "visitor winery missing", "foreign_keys")
    check(len(visitor) == report["counts"]["visitor_reviews"]["after_confirmed_dedup"], "visitor report mismatch", "report")
    check(sum(len(r["original_records"]) for r in visitor) == report["counts"]["visitor_reviews"]["input_rows"], "visitor input accounting mismatch", "report")
    for entry in manifest["input_files"]:
        p = raw / entry["path"].removeprefix("data_raw/")
        check(p.is_file() and sha256(p) == entry["sha256"], f"raw input changed: {p}", "raw_integrity")
    for entry in manifest["output_files"]:
        p = output / entry["path"]
        check(p.is_file() and sha256(p) == entry["sha256"], f"output checksum mismatch: {p}", "output_integrity")
    # 对已知高风险样本做可读的回归检查。
    if "P003" in all_products:
        check(all_products["P003"]["grape_varieties"] == [] and not all_products["P003"]["field_filter_eligibility"]["grape_varieties"], "P003 conflicted grapes enabled", "regression")
    if "P002" in all_products:
        check(all_products["P002"]["color"] is None, "P002 inferred color from grape variety", "regression")
    if "P014" in all_products:
        check(all_products["P014"]["review_status"] == "review" and not all_products["P014"]["strict_product_filter_eligible"],
              "P014 directory + unavailable details promoted to formal", "regression")
    if "W009" in all_wineries:
        check(all_wineries["W009"]["founded_year"] is None and all_wineries["W009"]["company_founded_year"] == 2018, "W009 company/winery year mixed", "regression")
    if "P055" in all_products:
        check(all_products["P055"]["vintage"] is None and all_products["P055"]["vintage_status"] == "non_vintage", "P055 base year used as vintage", "regression")
    if "P043" in all_products:
        check(not all_products["P043"]["strict_product_filter_eligible"], "P043 special recipe filter enabled", "regression")
    return {"passed": not errors, "checks_total": sum(checks.values()), "checks_by_category": dict(checks),
            "errors": errors, "error_count": len(errors), "raw_file_count": len(manifest["input_files"]),
            "note": "本地结构、业务与文件完整性验证；没有重新核验外部网页事实。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data_raw")
    args = parser.parse_args()
    report = validate_directory(args.data_dir, args.raw_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
