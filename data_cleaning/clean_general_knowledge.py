"""检查并清洗葡萄酒通用常识增量包；只读输入，不执行资料包内的程序。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from clean import ROOT, SCRIPT_DIR, TZ_CN, REL_TYPES, key_text, sha256, stable_id, text, write_json, write_markdown
from validate import schema_errors


LOCAL_INPUT = ROOT / "data_raw" / "葡萄酒通用常识知识库_20261111"
DEFAULT_INPUT = LOCAL_INPUT if LOCAL_INPUT.is_dir() else Path("D:/Desktop/葡萄酒通用常识知识库_20261111")
DEFAULT_OUTPUT = SCRIPT_DIR / "wine_general_knowledge_20261111"
RULES_PATH = SCRIPT_DIR / "general_knowledge_rules.json"
ID_FIELDS = {"sources": "source_id", "knowledge": "knowledge_id", "entities": "entity_id",
             "aliases": "alias_id", "relations": "relation_id"}
STATUSES = ("accepted", "review", "excluded")
BASELINE_FILES = [n + ".jsonl" for n in ID_FIELDS] + ["schema.json"]
CODE_FILES = ["clean_general_knowledge.py", "general_knowledge_rules.json", "clean.py", "validate.py", "fill_nulls.py"]


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def load_json(path: Path):
    def invalid(value):
        raise ValueError(f"{path}: 非法JSON数值 {value}")
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=invalid)


def load_rows(path: Path) -> list[dict]:
    result = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, 1):
            if line.strip():
                row = json.loads(line, parse_constant=lambda v: require(False, f"非法JSON数值 {v}"))
                require(isinstance(row, dict), f"{path}:{line_no}: 每行须为JSON对象")
                result.append(row)
    return result


def indexed(rows: list[dict], key: str) -> dict[str, dict]:
    result = {}
    for row in rows:
        value = row.get(key)
        require(isinstance(value, str) and bool(value), f"缺少有效ID字段 {key}")
        require(value not in result, f"重复ID: {key}={value}")
        result[value] = row
    return result


def inventory(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def safe_child(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), f"清单中的路径越界: {relative}")
    return path


def check_output_location(output: Path, *inputs: Path) -> None:
    output = output.resolve()
    for path in inputs:
        path = path.resolve()
        require(not (output == path or output.is_relative_to(path) or path.is_relative_to(output)),
                f"输出目录不能等于、包含或位于输入目录: {path}")
    require(output != SCRIPT_DIR and not SCRIPT_DIR.is_relative_to(output), "请选择data_cleaning下的独立输出目录")


def find_package(path: Path) -> Path:
    candidates = [p.parent.parent for p in path.resolve().rglob("knowledge.jsonl")
                  if p.parent.name == "data" and (p.parent / "manifest.json").is_file()
                  and (p.parent.parent / "schema/knowledge.schema.json").is_file()]
    require(len(candidates) == 1, f"需要唯一通用常识资料包，实际找到 {len(candidates)} 个")
    return candidates[0]


def valid_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def inspect_package(package: Path, as_of: date) -> tuple[dict, dict]:
    """独立核对导出内容；不使用输入文件中的校验结果作为验收结论。"""
    checks = Counter()

    def check(ok, message, category):
        require(ok, message)
        checks[category] += 1

    data = package / "data"
    manifest = load_json(data / "manifest.json")
    for relative, digest in manifest["files_sha256"].items():
        path = safe_child(package, relative)
        check(path.is_file() and sha256(path) == digest, f"原包校验值不一致: {relative}", "hashes")
    tables = {n: load_rows(data / (n + ".jsonl")) for n in
              ["sources", "knowledge", "aliases", "faq", "rag_documents", "retrieval_smoke_questions"]}
    sources = indexed(tables["sources"], "source_id")
    cards = indexed(tables["knowledge"], "knowledge_id")
    faqs = indexed(tables["faq"], "faq_id")
    rags = indexed(tables["rag_documents"], "id")
    counts = {"sources": len(sources), "knowledge_cards": len(cards), "faq": len(faqs),
              "alias_groups": len(tables["aliases"]), "smoke_questions": len(tables["retrieval_smoke_questions"]),
              "categories": len({r["category"] for r in cards.values()})}
    check(counts == manifest["counts"], "原清单数量与实际数据不一致", "counts")
    check(dict(Counter(r["category"] for r in cards.values())) == manifest["category_counts"],
          "原清单主题分布不一致", "counts")
    card_schema = load_json(package / "schema/knowledge.schema.json")
    signatures = set()
    for sid, source in sources.items():
        check(re.fullmatch(r"GS\d{3}", sid) is not None, f"来源编号异常: {sid}", "schema")
        check(valid_url(source.get("url")), f"来源链接格式错误: {sid}", "source_links")
        check(bool(text(source.get("title"))) and bool(text(source.get("publisher"))), f"来源标题/机构缺失: {sid}", "schema")
        check(date.fromisoformat(source["accessed_date"]) <= as_of, f"来源采集日期在未来: {sid}", "dates")
        check(source.get("published_at") is None or date.fromisoformat(source["published_at"]) <= as_of,
              f"来源发布日期在未来: {sid}", "dates")
    for kid, card in cards.items():
        errors = schema_errors(card, card_schema, kid)
        check(not errors, "原知识结构错误: " + "; ".join(errors), "schema")
        check(not (card.keys() - card_schema["properties"].keys()), f"原知识存在未声明字段: {kid}", "schema")
        check(re.fullmatch(r"WK\d{3}", kid) is not None, f"知识编号异常: {kid}", "schema")
        check(card["language"] == "zh-CN" and card["text_type"] == "editorial_paraphrase", f"原知识类型异常: {kid}", "schema")
        check(all(bool(text(card[f])) for f in ["title", "question", "text", "applicability", "category"]),
              f"知识关键字段为空: {kid}", "completeness")
        check(len(card["source_ids"]) == len(set(card["source_ids"])) and bool(card["source_ids"])
              and all(s in sources for s in card["source_ids"]), f"知识来源缺失/重复: {kid}", "references")
        check(card["source_urls"] == [sources[s]["url"] for s in card["source_ids"]], f"来源URL与编号不对应: {kid}", "references")
        check(date.fromisoformat(card["accessed_date"]) <= as_of, f"知识采集日期在未来: {kid}", "dates")
        signature = (key_text(card["text"]), key_text(card["applicability"]))
        check(signature not in signatures, f"同范围知识正文重复，需先核对主记录: {kid}", "duplicates")
        signatures.add(signature)
    check(set(rags) == set(cards), "RAG导出未与主知识一一对应", "derived_exports")
    check(Counter(r["knowledge_id"] for r in faqs.values()) == Counter(cards.keys()),
          "FAQ未与主知识一一对应", "derived_exports")
    for faq in faqs.values():
        card = cards[faq["knowledge_id"]]
        check(all(faq.get(k) == card[k] for k in ["question", "source_ids", "applicability"])
              and faq.get("answer") == card["text"], f"FAQ内容偏离主知识: {faq['faq_id']}", "derived_exports")
    for kid, rag in rags.items():
        card = cards[kid]
        expected = f"{card['title']}。{card['text']} 适用范围：{card['applicability']}"
        check(rag["page_content"] == expected and rag["metadata"].get("knowledge_id") == kid
              and all(v == card.get(k) for k, v in rag["metadata"].items()), f"RAG导出偏离主知识: {kid}", "derived_exports")
    for group in tables["aliases"]:
        check(bool(text(group["canonical"])) and isinstance(group["aliases"], list)
              and all(isinstance(a, str) and bool(text(a)) for a in group["aliases"])
              and bool(group["knowledge_ids"]) and all(k in cards for k in group["knowledge_ids"]),
              "术语别名缺少名称或有效知识引用", "references")
    check(len({key_text(g["canonical"]) for g in tables["aliases"]}) == len(tables["aliases"]), "术语标准名重复", "duplicates")
    for question in tables["retrieval_smoke_questions"]:
        check(bool(text(question["question"])) and bool(question["relevant_knowledge_ids"])
              and all(k in cards for k in question["relevant_knowledge_ids"]), "开发检查题引用缺失", "references")
    with sqlite3.connect((data / "wine_knowledge.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        check(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite完整性失败", "sqlite")
        check(not db.execute("PRAGMA foreign_key_check").fetchall(), "SQLite外键失败", "sqlite")
        for table, key, fields, expected in [
            ("sources", "source_id", ["title", "publisher", "url", "accessed_date"], sources),
            ("knowledge", "knowledge_id", ["title", "text", "category", "applicability"], cards),
        ]:
            values = db.execute(f"SELECT {key},{','.join(fields)},payload_json FROM {table}").fetchall()
            check({r[0] for r in values} == set(expected) and len(values) == len(expected), f"SQLite {table} ID不一致", "sqlite")
            for value in values:
                record = expected[value[0]]
                check(json.loads(value[-1]) == record and list(value[1:-1]) == [record[f] for f in fields],
                      f"SQLite {table} 内容不一致: {value[0]}", "sqlite")
        links = {(k, s) for k, card in cards.items() for s in card["source_ids"]}
        actual_links = db.execute("SELECT knowledge_id,source_id FROM knowledge_sources").fetchall()
        check(set(actual_links) == links and len(actual_links) == len(links), "SQLite知识来源表不一致", "sqlite")
        db_faq = db.execute("SELECT faq_id,question,knowledge_id FROM faq").fetchall()
        check(set(db_faq) == {(r["faq_id"], r["question"], r["knowledge_id"]) for r in faqs.values()}
              and len(db_faq) == len(faqs), "SQLite FAQ表不一致", "sqlite")
        fts_ids = [r[0] for r in db.execute("SELECT knowledge_id FROM knowledge_fts")]
        check(set(fts_ids) == set(cards) and len(fts_ids) == len(cards), "SQLite索引ID不一致", "sqlite")
    expected_txt = "\n".join(
        f"【{c['knowledge_id']}】{c['title']}\n问题：{c['question']}\n知识：{c['text']}\n"
        f"适用范围：{c['applicability']}\n来源：{'；'.join(c['source_urls'])}\n核对日期：{manifest['collected_date']}\n"
        for c in tables["knowledge"])
    check((data / "葡萄酒通用常识.txt").read_text(encoding="utf-8-sig") == expected_txt,
          "TXT导出偏离主知识", "derived_exports")
    return tables, {"passed": True, "checks_total": sum(checks.values()), "checks_by_category": dict(checks),
                    "counts": counts, "errors": [], "external_verification": "not_performed"}


def clean_card(raw: dict, sources: dict, rules: dict, locator: str) -> dict:
    title, question, answer, scope = [text(raw[k]) for k in ["title", "question", "text", "applicability"]]
    require(all([title, question, answer, scope]), f"知识缺少完整语义: {raw['knowledge_id']}")
    category = rules["category_map"][raw["category"]]
    override = rules["category_overrides"].get(raw["knowledge_id"])
    if override:
        require(title == override["expected_title"], f"旧分类规则不适用: {raw['knowledge_id']}")
        category = override["category"]
    strong = any(sources[s]["access_evidence_status"] == "reported_body_read" for s in raw["source_ids"])
    status = "accepted" if raw["eligible_for_retrieval"] is True and strong else "review"
    return {"knowledge_id": raw["knowledge_id"], "title": title, "question": question, "answer": answer,
            "text": f"问题：{question}\n回答：{answer}\n适用范围：{scope}",
            "category": category, "category_raw": raw["category"], "entity_ids": [],
            "keywords": list(dict.fromkeys(text(v) for v in raw["keywords"] if text(v))),
            "source_ids": sorted(set(raw["source_ids"])),
            "source_urls": [sources[s]["url"] for s in sorted(set(raw["source_ids"]))],
            "fact_scope": scope, "applicability": scope, "language": raw["language"],
            "accessed_date": raw["accessed_date"], "text_type": "faithful_summary",
            "text_type_raw": raw["text_type"], "content_origin": "collector_summary",
            "review_status": status, "review_status_raw": raw["review_status"],
            "retrieval_allowed": status == "accepted", "contains_price": False,
            "raw_locator": locator, "raw_locators": [locator], "external_verification": "not_performed",
            "notes": "中文编辑摘要；保留原问题、否定及条件，尚未经独立行业专家复审。"}


def build_tables(raw: dict, package: Path, rules: dict) -> tuple[dict, dict]:
    tables = {n: [] for n in ID_FIELDS}
    entities, aliases, relations = {}, {}, {}
    audit = {"knowledge_lineage": [], "retrieval_alias_hints": []}

    def locate(name, key, value):
        path = package / "data" / (name + ".jsonl")
        label = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT / "data_raw") else path.as_posix()
        return label + f"#{key}={value}"

    for source in raw["sources"]:
        loc = locate("sources", "source_id", source["source_id"])
        tables["sources"].append({**source, "title": text(source["title"]), "publisher": text(source["publisher"]),
            "raw_file": loc.split("#", 1)[0], "evidence_form": "collector_summary",
            "access_evidence_status": "reported_body_read" if source.get("evidence_status") == "page_read" else "unknown",
            "source_type": "industry_reference", "snapshot_path": None, "dataset_role": "real",
            "review_status": "accepted", "raw_locator": loc, "raw_locators": [loc],
            "external_verification": "not_performed", "notes": "来源登记可追溯；page_read为原包报告，未重新联网或取得网页原件。"})
    source_map = indexed(tables["sources"], "source_id")
    for card in raw["knowledge"]:
        loc = locate("knowledge", "knowledge_id", card["knowledge_id"])
        row = clean_card(card, source_map, rules, loc)
        tables["knowledge"].append(row)
        audit["knowledge_lineage"].append({"knowledge_id": row["knowledge_id"], "raw_locator": loc,
            "original_record": card, "action": "normalize_and_preserve_question_answer_scope",
            "normalized_fields": [k for k in ["title", "question", "text", "applicability", "keywords"]
                                  if card[k] != (row["answer"] if k == "text" else row[k])]})
    cards = indexed(tables["knowledge"], "knowledge_id")

    def add_entity(kind, name, source_ids, locators, status):
        eid = stable_id("G_" if kind == "GrapeVariety" else "C_", kind, name)
        if eid not in entities:
            entities[eid] = {"entity_id": eid, "entity_type": kind, "name": name, "description": None,
                "region_kind": None, "source_ids": [], "raw_locator": locators[0], "raw_locators": [],
                "review_status": status, "external_verification": "not_performed",
                "notes": "仅表达知识主题或已列明术语，不向具体商品传播典型风味或甜度。"}
        row = entities[eid]
        row["source_ids"] = sorted(set(row["source_ids"] + source_ids))
        row["raw_locators"] = sorted(set(row["raw_locators"] + locators))
        if status == "accepted":
            row["review_status"] = status
        return eid

    for card in cards.values():
        card["entity_ids"].append(add_entity("Concept", card["title"], card["source_ids"], card["raw_locators"], card["review_status"]))
    for index, group in enumerate(raw["aliases"], 1):
        canonical = text(group["canonical"])
        refs = [cards[k] for k in group["knowledge_ids"]]
        source_ids = sorted({s for k in refs for s in k["source_ids"]})
        loc = locate("aliases", "record", index)
        locators = sorted({loc, *(p for k in refs for p in k["raw_locators"])})
        kind = "GrapeVariety" if canonical in rules["grape_terms"] else "Concept"
        status = "accepted" if any(k["review_status"] == "accepted" for k in refs) else "review"
        eid = add_entity(kind, canonical, source_ids, locators, status)
        for card in refs:
            card["entity_ids"] = sorted(set(card["entity_ids"] + [eid]))
        for value in [canonical, *group["aliases"]]:
            value = text(value)
            if value in rules["retrieval_only_aliases"]:
                audit["retrieval_alias_hints"].append({"term": value, "canonical": canonical,
                    "knowledge_ids": group["knowledge_ids"], "source_ids": source_ids, "raw_locator": loc,
                    "usage": "retrieval_hint_only", "reason": rules["retrieval_only_aliases"][value]})
                continue
            aid = stable_id("A_", key_text(value), eid)
            alias_kind = "canonical" if value == canonical else "abbreviation" if re.fullmatch(r"[A-Z]{2,}", value) else "english_name" if re.search(r"[A-Za-z]", value) else "other"
            aliases[aid] = {"alias_id": aid, "alias": value, "entity_id": eid, "entity_type": kind,
                "alias_kind": alias_kind, "source_ids": source_ids, "raw_locator": loc, "raw_locators": [loc],
                "review_status": status, "ambiguous": False, "external_verification": "not_performed", "notes": group["origin"]}
    targets = defaultdict(set)
    for alias in aliases.values():
        targets[key_text(alias["alias"])].add(alias["entity_id"])
    for alias in aliases.values():
        alias["ambiguous"] = len(targets[key_text(alias["alias"])]) > 1
        if alias["ambiguous"]:
            alias["review_status"] = "review"
    nodes = {**{s: "accepted" for s in source_map}, **{e: r["review_status"] for e, r in entities.items()},
             **{k: r["review_status"] for k, r in cards.items()}}
    for card in cards.values():
        endpoints = [(s, "HAS_CHUNK", card["knowledge_id"], [s]) for s in card["source_ids"]]
        endpoints += [(card["knowledge_id"], "ABOUT", e, card["source_ids"]) for e in card["entity_ids"]]
        for head, kind, tail, source_ids in endpoints:
            rid = stable_id("REL_", head, kind, tail)
            relations[rid] = {"relation_id": rid, "head_id": head, "relation_type": kind, "tail_id": tail,
                "source_ids": source_ids, "proportion_pct": None, "raw_locator": card["raw_locator"],
                "raw_locators": card["raw_locators"], "review_status": "accepted" if nodes[head] == nodes[tail] == "accepted" else "review",
                "external_verification": "not_performed", "notes": None}
    tables.update(entities=list(entities.values()), aliases=list(aliases.values()), relations=list(relations.values()))
    return tables, audit


def find_overlaps(tables: dict, baseline: Path, rules: dict) -> dict:
    old = {n: load_rows(baseline / (n + ".jsonl")) for n in ID_FIELDS}
    shared_entities, shared_aliases = [], []
    for name, key in ID_FIELDS.items():
        previous = indexed(old[name], key)
        for row in tables[name]:
            rid = row[key]
            if rid not in previous:
                continue
            if name in {"sources", "knowledge"}:
                require(False, f"增量包与现有数据ID冲突: {name}/{rid}")
            elif name == "entities":
                require(all(previous[rid][k] == row[k] for k in ["entity_type", "name"]), f"实体ID含义冲突: {rid}")
                shared_entities.append(rid)
            elif name == "aliases":
                require(previous[rid]["entity_id"] == row["entity_id"] and key_text(previous[rid]["alias"]) == key_text(row["alias"]), f"别名ID含义冲突: {rid}")
                shared_aliases.append(rid)
            else:
                require(all(previous[rid][k] == row[k] for k in ["head_id", "relation_type", "tail_id"]), f"关系ID含义冲突: {rid}")
    old_sources = indexed(old["sources"], "source_id")
    candidates = []
    for new in tables["knowledge"]:
        for previous in old["knowledge"]:
            ratio = SequenceMatcher(None, key_text(new["answer"]), key_text(previous["text"]), autojunk=False).ratio()
            shared_url = sorted(set(new["source_urls"]) & {old_sources[s].get("url") for s in previous["source_ids"]})
            exact = key_text(new["answer"]) == key_text(previous["text"])
            if exact or ratio >= rules["overlap_similarity_threshold"] or key_text(new["title"]) == key_text(previous["title"]):
                candidates.append({"knowledge_id": new["knowledge_id"], "existing_knowledge_id": previous["knowledge_id"],
                    "title": new["title"], "existing_title": previous["title"], "text_similarity": round(ratio, 6),
                    "exact_text_match": exact, "shared_source_urls": shared_url,
                    "decision": "preserve_with_separate_scope_and_provenance",
                    "note": "相似度仅提示人工复核，不证明事实等价；本批没有自动覆盖或删除旧知识。"})
    return {"existing_knowledge_count": len(old["knowledge"]), "candidates": candidates,
            "exact_text_matches": sum(r["exact_text_match"] for r in candidates),
            "shared_entity_ids": sorted(shared_entities), "shared_alias_ids": sorted(shared_aliases)}


def validate_bundle(output: Path) -> dict:
    manifest = load_json(output / "manifest.json")
    schema = load_json(output / "schema.json")
    errors, checks = [], Counter()

    def check(ok, message, category):
        checks[category] += 1
        if not ok:
            errors.append(message)

    for field, root in [("input_files", Path(manifest["input_root"])), ("baseline_files", Path(manifest["baseline_root"]))]:
        for relative, digest in manifest[field].items():
            p = safe_child(root, relative)
            check(p.is_file() and sha256(p) == digest, f"输入发生变化: {p}", "input_integrity")
    check(inventory(Path(manifest["input_root"])) == manifest["input_files"], "原包文件集合或内容发生变化", "input_integrity")
    for relative, digest in manifest["code_files"].items():
        p = safe_child(SCRIPT_DIR, relative)
        check(p.is_file() and sha256(p) == digest, f"清洗程序或规则改变，请重新清洗: {relative}", "code_integrity")
    for entry in manifest["output_files"]:
        p = safe_child(output, entry["path"])
        check(p.is_file() and sha256(p) == entry["sha256"], f"输出校验失败: {entry['path']}", "output_integrity")
    tables, nodes = {}, {}
    counts = {}
    for name, key in ID_FIELDS.items():
        tables[name] = []
        counts[name] = {}
        for status in STATUSES:
            rows = load_rows(output / ("" if status == "accepted" else status) / (name + ".jsonl"))
            counts[name][status] = len(rows)
            for row in rows:
                check(row.get("review_status") == status, f"状态与目录不一致: {name}/{row.get(key)}", "schema")
                se = schema_errors(row, schema["$defs"][name], name)
                check(not se, "; ".join(se), "schema")
                check(row.get("external_verification") == "not_performed", "外部核验标识不实", "provenance")
            tables[name].extend(rows)
        check(len({r[key] for r in tables[name]}) == len(tables[name]), f"输出ID重复: {name}", "uniqueness")
        if name in {"sources", "knowledge", "entities"}:
            for row in tables[name]:
                rid = row[key]
                check(rid not in nodes, f"跨类型ID重复: {rid}", "uniqueness")
                nodes[rid] = {"type": "Document" if name == "sources" else "Chunk" if name == "knowledge" else row["entity_type"],
                              "status": row["review_status"]}
    source_ids = {r["source_id"] for r in tables["sources"]}
    for name, rows in tables.items():
        if name != "sources":
            for row in rows:
                check(bool(row["source_ids"]) and all(s in source_ids for s in row["source_ids"]), f"来源引用缺失: {name}", "references")
    for card in tables["knowledge"]:
        check(card["text"] == f"问题：{card['question']}\n回答：{card['answer']}\n适用范围：{card['fact_scope']}",
              f"问题、答案或适用范围未完整进入正文: {card['knowledge_id']}", "semantics")
        check(card["retrieval_allowed"] == (card["review_status"] == "accepted") and card["contains_price"] is False,
              "检索资格或价格隔离标识错误", "semantics")
        check(all(nodes.get(e, {}).get("type") in {"Concept", "GrapeVariety"} for e in card["entity_ids"]), "知识主题引用缺失", "references")
        if card["review_status"] == "accepted":
            check(all(nodes[e]["status"] == "accepted" for e in card["entity_ids"]), "正式知识引用待审主题", "references")
    for alias in tables["aliases"]:
        check(nodes.get(alias["entity_id"], {}).get("type") == alias["entity_type"], "别名目标错误", "references")
    for relation in tables["relations"]:
        heads, tails = REL_TYPES[relation["relation_type"]]
        check(nodes.get(relation["head_id"], {}).get("type") in heads and nodes.get(relation["tail_id"], {}).get("type") in tails,
              "图关系端点类型错误", "references")
        if relation["review_status"] == "accepted":
            check(all(nodes.get(e, {}).get("status") == "accepted" for e in [relation["head_id"], relation["tail_id"]]),
                  "正式图关系引用待审端点", "references")
    cards = indexed(tables["knowledge"], "knowledge_id")
    lineage = indexed(load_rows(output / "audit/knowledge_lineage.jsonl"), "knowledge_id")
    check(set(lineage) == set(cards), "知识输入输出不守恒", "lineage")
    for kid, card in cards.items():
        original = lineage[kid]["original_record"]
        check(all(card[k] == text(original[k]) for k in ["title", "question", "applicability"])
              and card["answer"] == text(original["text"]) and card["fact_scope"] == text(original["applicability"])
              and set(card["source_ids"]) == set(original["source_ids"]), f"原有含义、条件或来源丢失: {kid}", "lineage")
    faq = load_rows(output / "auxiliary/faq.jsonl")
    check(Counter(r["knowledge_id"] for r in faq) == Counter(cards.keys()), "辅助FAQ数量或ID异常", "auxiliary")
    for row in faq:
        card = cards.get(row["knowledge_id"], {})
        check(row.get("index_as_knowledge") is False and all(row.get(k) == card.get(k) for k in
              ["question", "answer", "applicability", "source_ids"]), "辅助FAQ偏离知识或被重复索引", "auxiliary")
    for row in load_rows(output / "auxiliary/retrieval_smoke_questions.jsonl"):
        check(row.get("index_as_knowledge") is False and all(k in cards for k in row["relevant_knowledge_ids"]), "开发题误入知识或引用缺失", "auxiliary")
    report = load_json(output / "quality_report.json")
    check(counts == report["output_counts"], "质量报告数量不一致", "counts")
    return {"passed": not errors, "checks_total": sum(checks.values()), "checks_by_category": dict(checks),
            "errors": errors, "error_count": len(errors), "input_file_count": len(manifest["input_files"]),
            "note": "本地结构、关联、语义保留及文件完整性验证；未重新核验外部网页，未评测向量检索或大模型。"}


def run(input_dir: Path, output: Path, baseline: Path, as_of: date) -> dict:
    input_dir, output, baseline = input_dir.resolve(), output.resolve(), baseline.resolve()
    check_output_location(output, input_dir, baseline)
    package = find_package(input_dir)
    original_hashes = inventory(input_dir)
    baseline_hashes = {name: sha256(baseline / name) for name in BASELINE_FILES}
    rules = load_json(RULES_PATH)
    raw, inspection = inspect_package(package, as_of)
    tables, audit = build_tables(raw, package, rules)
    overlaps = find_overlaps(tables, baseline, rules)
    check_output_location(output, package)
    paths = []

    def save(relative, value, lines=False):
        path = output / relative
        write_json(path, value, lines=lines)
        paths.append(path)

    counts = {}
    for name, key in ID_FIELDS.items():
        counts[name] = {}
        for status in STATUSES:
            rows = sorted((r for r in tables[name] if r["review_status"] == status), key=lambda r: r[key])
            counts[name][status] = len(rows)
            save(("" if status == "accepted" else status + "/") + name + ".jsonl", rows, True)
    save("schema.json", load_json(baseline / "schema.json"))
    save("input_inspection_report.json", inspection)
    for name, rows in audit.items():
        # 关联物质/单位只作检索提示，不进入同义实体表。
        save(("auxiliary/" if name == "retrieval_alias_hints" else "audit/") + name + ".jsonl", rows, True)
    save("audit/existing_knowledge_overlaps.jsonl", overlaps["candidates"], True)
    save("auxiliary/term_alias_groups.jsonl", [{**g, "index_as_knowledge": False} for g in raw["aliases"]], True)
    cards = indexed(tables["knowledge"], "knowledge_id")
    save("auxiliary/faq.jsonl", [{**r, **{k: cards[r["knowledge_id"]][k] for k in
         ["question", "answer", "applicability", "source_ids"]}, "index_as_knowledge": False} for r in raw["faq"]], True)
    save("auxiliary/retrieval_smoke_questions.jsonl", [{**r, "index_as_knowledge": False,
         "evaluation_role": "same_source_development_only"} for r in raw["retrieval_smoke_questions"]], True)
    quality = {"dataset": "葡萄酒通用常识清洗增量包", "rules_version": rules["rules_version"], "as_of_date": as_of.isoformat(),
        "input_counts": inspection["counts"], "output_counts": counts,
        "normalized_categories": dict(sorted(Counter(c["category"] for c in tables["knowledge"]).items())),
        "raw_categories": dict(sorted(Counter(c["category_raw"] for c in tables["knowledge"]).items())),
        "internal_exact_duplicates": 0, "existing_exact_text_matches": overlaps["exact_text_matches"],
        "existing_overlap_candidates": len(overlaps["candidates"]), "shared_entity_ids": overlaps["shared_entity_ids"],
        "overlap_method": "规范正文SequenceMatcher相似度达到规则阈值、同标题或正文完全相同；不是完整语义去重。",
        "shared_alias_ids": overlaps["shared_alias_ids"], "retrieval_only_alias_hints": len(audit["retrieval_alias_hints"]),
        "external_verification": "not_performed", "database_import_performed": False,
        "limitations": ["来源为中文编辑摘要，page_read沿用原包报告；未取得网页原件，也未重新联网核验。",
            "OIV/香槟规则、温度、保鲜天数和典型品种风味的原有适用范围已保留。",
            "FAQ、TXT、旧RAG导出和SQLite不重复计为独立知识；开发题不进入知识索引。",
            "相似主题只登记候选，不能据此宣称90条均为旧库从未覆盖的新事实。",
            "本程序生成独立增量文件；主数据合并及数据库/向量状态以data和database中的后续报告为准。"]}
    save("quality_report.json", quality)
    readme = f"""# 葡萄酒通用常识清洗增量包

清洗日期：{as_of.isoformat()}。原包采集日期：2026-11-11。规则版本：{rules['rules_version']}。

## 结果与入口

原资料含90条知识卡、35个来源、20组术语、90条同源FAQ和25条开发检查题，覆盖16类主题。
本次正式输出：{counts['knowledge']['accepted']}条知识、{counts['sources']['accepted']}个来源、
{counts['entities']['accepted']}个主题/品种实体、{counts['aliases']['accepted']}条规范别名、{counts['relations']['accepted']}条关系。

| 文件 | 用途 |
|---|---|
| knowledge.jsonl | 正式知识，text已包含问题、回答与适用范围；answer单独保留规范正文 |
| sources.jsonl | 来源登记，保留链接、机构、原访问日期与版权说明 |
| entities.jsonl / aliases.jsonl / relations.jsonl | 与项目既有业务字段及稳定ID规则一致的主题、术语和图关系 |
| review/ / excluded/ | 待审与排除分区，本批为空 |
| auxiliary/faq.jsonl | 同一批知识的问答表示，禁止再次计为独立知识 |
| auxiliary/term_alias_groups.jsonl | 原20组检索归一化词典 |
| auxiliary/retrieval_alias_hints.jsonl | %vol、TCA仅作检索提示，避免把单位/关联物质认作同义概念 |
| auxiliary/retrieval_smoke_questions.jsonl | 原25条同源开发题，禁止建知识索引或作为独立问答准确率 |
| audit/knowledge_lineage.jsonl | 每条知识的原记录、输入位置及规范化字段 |
| audit/existing_knowledge_overlaps.jsonl | 与旧库主题重叠的候选，供合并前复核 |
| schema.json | 沿用项目JSON Schema；category_raw保留原16类，category映射到项目枚举 |
| input_inspection_report.json | 对原包导出、SQLite和引用进行独立本地检查的结果 |
| quality_report.json / validation_report.json / manifest.json | 质量、最终验证与输入/输出SHA256 |

## 清洗与检查

只读原包，未运行包内build_dataset.py、query.py或validate.py。主载体为knowledge.jsonl和sources.jsonl；
FAQ、RAG、TXT及SQLite逐项核对为派生载体，不叠加导入。沿用clean.py的Unicode、空白、HTML规范化和稳定ID，
保留否定、数值、采集日期及适用条件；published_at缺失仍为null。资料内的说明与命令只作为输入内容。

与现有{overlaps['existing_knowledge_count']}条正式知识无编号冲突，同范围规范正文无内部重复；
与旧库正文完全相同的记录为{overlaps['exact_text_matches']}条，按文本规则标出相似候选{len(overlaps['candidates'])}对；
这不是穷尽的语义重复或事实冲突检查。
相似候选保留各自范围与来源，未自动覆盖旧数据。实体按项目既有类型和名称计算ID，
与旧库共享{len(overlaps['shared_entity_ids'])}个实体ID；未来合并时须按ID合并来源及追溯字段。

## 使用边界

本目录是增量包。根目录data的合并结果见data/merge_report.json，实际导入及向量状态见database中的报告。
合并时应复核重叠候选，
按业务ID合并实体/别名及来源字段，重新生成整体manifest并运行完整数据验证。
本目录的五张表沿用项目Schema，不能直接替代原有完整数据库数据快照。

来源均为原包报告已读取页面的中文编辑摘要；本次没有重新访问网页或进行行业专家复审。
来源登记accepted和知识accepted表示通过本地规则，不能视为外部真实性认证。
OIV、香槟及澳大利亚品种资料的适用范围随正文进入检索；典型风味不会转成某瓶产品的确定属性。
没有调用向量模型，也没有进行大模型答案评测。

## 复跑

在项目根目录执行：

```powershell
python data_cleaning/clean_general_knowledge.py --input-dir "{input_dir.as_posix()}" --baseline-dir "{baseline.as_posix()}" --output-dir "{output.as_posix()}" --as-of-date {as_of.isoformat()}
python data_cleaning/clean_general_knowledge.py --validate-only
```

本次输入为`{input_dir.as_posix()}`，输出为本目录，基准副本为`{baseline.as_posix()}`。
可通过--input-dir、--output-dir和--baseline-dir指定独立路径。相同输入、规则、基准与日期生成相同输出。
只覆盖本程序管理的文件，不递归删除额外用户文件。原包或规则变化后需要重新清洗。
"""
    readme_path = output / "README.md"
    write_markdown(readme_path, readme)
    paths.append(readme_path)
    require(inventory(input_dir) == original_hashes, "运行期间原始资料发生变化")
    require(all(sha256(baseline / n) == h for n, h in baseline_hashes.items()), "运行期间现有数据发生变化")
    save("manifest.json", {"schema_version": "1.0.0", "rules_version": rules["rules_version"],
        "as_of_date": as_of.isoformat(), "input_root": str(input_dir), "package_root": str(package),
        "output_root": str(output), "baseline_root": str(baseline), "input_files_unchanged": True,
        "baseline_files_unchanged": True, "input_files": original_hashes, "baseline_files": baseline_hashes,
        "code_files": {n: sha256(SCRIPT_DIR / n) for n in CODE_FILES},
        "output_files": [{"path": p.relative_to(output).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256(p)}
                         for p in sorted(paths)]})
    validation = validate_bundle(output)
    write_json(output / "validation_report.json", validation)
    require(validation["passed"], "输出验证失败: " + "; ".join(validation["errors"][:5]))
    return {"output_dir": str(output), "counts": counts, "validation": validation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=datetime.now(TZ_CN).date())
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        result = validate_bundle(args.output_dir.resolve())
    else:
        result = run(args.input_dir, args.output_dir, args.baseline_dir, args.as_of_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("passed", result.get("validation", {}).get("passed")) else 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
