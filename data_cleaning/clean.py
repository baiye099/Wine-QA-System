"""宁夏葡萄酒问答数据清洗。仅依赖 Python 标准库，不修改 data_raw。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
TZ_CN = timezone(timedelta(hours=8))
MISSING = {"", "null", "none", "nan", "n/a", "未知", "暂无", "未获取", "未披露", "-", "--"}
COLOR = {"红": "red", "红色": "red", "白": "white", "白色": "white", "桃红": "rose"}
SWEETNESS = {"干型": "dry", "半干型": "semi_dry", "半甜型": "semi_sweet", "甜型": "sweet"}
REL_TYPES = {
    "PRODUCES": ({"Winery"}, {"Product"}),
    "LOCATED_IN": ({"Winery"}, {"Region"}),
    "ORIGINATES_FROM": ({"Product"}, {"Region"}),
    "MADE_FROM": ({"Product"}, {"GrapeVariety"}),
    "HAS_FLAVOR": ({"Product"}, {"Flavor"}),
    "PART_OF": ({"Region"}, {"Region"}),
    "HAS_CHUNK": ({"Document"}, {"Chunk"}),
    "ABOUT": ({"Chunk"}, {"Winery", "Product", "Region", "GrapeVariety", "Flavor", "Concept"}),
}
ID_KEYS = {
    "sources": "source_id", "wineries": "winery_id", "products": "product_id",
    "price_observations": "price_id", "entities": "entity_id", "aliases": "alias_id",
    "knowledge": "knowledge_id", "relations": "relation_id", "professional_reviews": "review_id",
}


def text(value: Any) -> str | None:
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value))
    s = html.unescape(s).replace("\\r\\n", "\n").replace("\\n", "\n")
    # U+2028/U+2029在JSON字符串中合法，但str.splitlines会误拆JSONL；统一成可转义换行。
    s = s.replace("\u2028", "\n").replace("\u2029", "\n")
    s = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", "", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = re.sub(r"</?[A-Za-z][^>]*>", "", s)
    s = "".join(c for c in s if not unicodedata.category(c).startswith("C") or c in "\n\t")
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = re.sub(r"\n\s*\n+", "\n", s)
    return None if s.casefold() in MISSING else s


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def key_text(value: Any) -> str:
    return re.sub(r"\s+", "", text(value) or "").casefold()


def boolean(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"true", "1", "是"}:
        return True
    if s in {"false", "0", "否"}:
        return False
    return None


def number(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> int | float | None:
    if isinstance(value, bool) or text(value) is None:
        return None
    s = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", s):
        return None
    n = float(s)
    if not math.isfinite(n) or (minimum is not None and n < minimum) or (maximum is not None and n > maximum):
        return None
    return int(n) if n.is_integer() else n


def volume_ml(value: Any) -> int | float | None:
    s = text(value)
    if s is None:
        return None
    m = re.fullmatch(r"([\d.]+)\s*(ml|mL|毫升|cl|cL|L|l|升)?", s)
    if not m:
        return None
    n = number(m[1], minimum=0.000001)
    if n is None:
        return None
    factor = {"cl": 10, "l": 1000, "升": 1000}.get((m[2] or "ml").lower(), 1)
    result = round(n * factor, 6)
    return int(result) if float(result).is_integer() else result


def alcohol_pct(value: Any) -> int | float | None:
    s = text(value)
    return number(re.sub(r"\s*(?:%\s*vol|%|vol)\s*$", "", s, flags=re.I), minimum=0, maximum=100) if s else None


def date_value(value: Any) -> tuple[str | None, str]:
    s = text(value)
    if not s:
        return None, "unknown"
    dotnet = re.fullmatch(r"/Date\((\d+)([+-]\d{4})?\)/", s)
    if dotnet:
        dt = datetime.fromtimestamp(int(dotnet[1]) / 1000, TZ_CN)
        return dt.date().isoformat(), "day"
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[ T].*)?", s)
    if not m:
        return None, "unknown"
    year, month, day = m.groups()
    try:
        if day:
            return date(int(year), int(month), int(day)).isoformat(), "day"
        if month and 1 <= int(month) <= 12:
            return f"{int(year):04d}-{int(month):02d}", "month"
        if month:
            return None, "unknown"
        return f"{int(year):04d}", "year"
    except ValueError:
        return None, "unknown"


def vintage_value(row: dict, as_of_year: int) -> tuple[int | None, str, str | None]:
    raw_scope = str(row.get("record_scope") or "")
    if ("随机" in raw_scope or "混合规格" in raw_scope or raw_scope == "mixed_variant" or
            row.get("vintage_status") == "mixed" or "随机" in str(row.get("vintage") or "")):
        return None, "mixed", None
    nv = boolean(row.get("is_non_vintage"))
    n = number(row.get("vintage"), minimum=1000, maximum=9999)
    if nv is True:
        return None, "non_vintage", "nv_year_conflict" if n is not None else None
    if n is not None:
        if not float(n).is_integer() or n > as_of_year:
            return None, "unspecified", "invalid_or_future_vintage"
        return int(n), "specified", None
    return None, "unspecified", None


def is_catalog_candidate(raw: dict, strong_source_ids: list[str], catalog_source_ids: list[str]) -> bool:
    status = str(raw.get("review_status") or "")
    notes = str(raw.get("notes") or "")
    return bool(("目录" in status and any(t in status for t in ["候选", "待补", "详情"])) or
                "仅目录" in notes or "供货目录" in notes or
                (strong_source_ids and set(strong_source_ids).issubset(set(catalog_source_ids))))


def array(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def merge_list(*lists: list) -> list:
    result, seen = [], set()
    for seq in lists:
        for v in seq:
            k = json.dumps(v, ensure_ascii=False, sort_keys=True)
            if k not in seen:
                result.append(v)
                seen.add(k)
    return result


def read_jsonl(path: Path) -> list[dict]:
    result = []
    with path.open(encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{path}:{line_no}: expected JSON object")
                result.append(row)
    return result


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, data: Any, *, lines: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        if lines:
            for row in data:
                f.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        else:
            json.dump(data, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.write("\n")
    tmp.replace(path)


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8", newline="\n")


def sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def redact_review(value: Any) -> tuple[str | None, bool]:
    s = text(value)
    if not s:
        return None, False
    patterns = [
        (r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[手机号已隐去]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱已隐去]"),
        (r"(?<!\d)\d{17}[\dXx](?!\d)", "[证件号已隐去]"),
        (r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", "[IP已隐去]"),
    ]
    old = s
    for pattern, replacement in patterns:
        s = re.sub(pattern, replacement, s)
    return s, s != old


def extract_flavors(value: str, terms: list[str]) -> list[str]:
    found, occupied = [], []
    for term in sorted(set(terms), key=lambda t: (-len(t), t)):
        for match in re.finditer(re.escape(term), value):
            if any(a <= match.start() and match.end() <= b for a, b in occupied):
                continue
            if re.search(r"(?:没有|缺乏|不具备|并无|无明显)[^，。；]{0,6}$", value[max(0, match.start() - 12):match.start()]):
                continue
            found.append(term)
            occupied.append(match.span())
            break
    return sorted(set(found))


def date_compatible(left: str | None, right: str | None) -> bool:
    return not left or not right or left.startswith(right) or right.startswith(left)


def external_review_id(raw: dict, platform: str) -> str | None:
    direct = raw.get("commentId") or raw.get("comment_id")
    if direct and re.fullmatch(r"\d+", str(direct)):
        return str(direct)
    rid = str(raw.get("review_id") or "")
    prefixed = re.fullmatch(r"ctrip_(\d{6,})", rid)
    if platform == "ctrip" and prefixed:
        return prefixed[1]
    if platform != "unknown" and re.fullmatch(r"\d{6,}", rid):
        return rid
    # compat中的id通常是导出行号，不是平台评论ID。
    return None


class Cleaner:
    def __init__(self, raw_dir: Path, output_dir: Path, as_of: date):
        self.raw = raw_dir.resolve()
        self.out = output_dir.resolve()
        if self.out == self.raw or self.raw in self.out.parents or self.out in self.raw.parents:
            raise ValueError("输出目录不得等于、位于或包含原始数据目录")
        self.as_of = as_of
        self.rules = json.loads((SCRIPT_DIR / "rules.json").read_text(encoding="utf-8"))
        self.tables: dict[str, list[dict]] = defaultdict(list)
        self.audit: dict[str, list[dict]] = defaultdict(list)
        self.issues: dict[str, dict] = {}
        self.entities: dict[str, dict] = {}
        self.relations: dict[str, dict] = {}
        self.aliases: dict[str, dict] = {}
        self.input_rows: dict[str, list[dict]] = {}
        self.mirror_checks: list[dict] = []
        self.files_read: set[Path] = set()
        self.pkg: Path | None = None
        self.source_by_id: dict[str, dict] = {}
        self.winery_by_id: dict[str, dict] = {}
        self.product_by_id: dict[str, dict] = {}
        self.node_status: dict[str, str] = {}
        self.node_types: dict[str, str] = {}
        self.raw_before = self.inventory()

    def relpath(self, p: Path) -> str:
        return "data_raw/" + p.relative_to(self.raw).as_posix()

    def inventory(self) -> list[dict]:
        return [{"path": self.relpath(p), "size_bytes": p.stat().st_size, "sha256": sha256(p)}
                for p in sorted(self.raw.rglob("*")) if p.is_file()]

    def locate(self, name: str) -> Path:
        assert self.pkg is not None
        p = self.pkg / name
        if not p.is_file():
            raise FileNotFoundError(p)
        self.files_read.add(p.resolve())
        return p

    def issue(self, record_type: str, record_id: str, field: str, kind: str, finding: str,
              handling: str, source_ids: list | None = None, severity: str = "warning", **extra) -> str:
        uid = stable_id("Q_", record_type, record_id, field, kind, finding)
        self.issues[uid] = {"issue_id": uid, "record_type": record_type, "record_id": record_id,
                            "field": field, "issue_type": kind, "finding": finding, "handling": handling,
                            "source_ids": sorted(set(source_ids or [])), "severity": severity,
                            "status": "recorded", **extra}
        return uid

    def provenance(self, filename: str, row: dict, id_field: str, record_type: str) -> dict:
        path = self.locate(filename)
        rid = str(row[id_field])
        locator = f"{self.relpath(path)}#{id_field}={rid}"
        self.audit["id_mapping"].append({"input_file": self.relpath(path), "original_type": record_type,
                                         "original_id": rid, "clean_type": record_type, "clean_id": rid,
                                         "action": "preserved", "reason": "原ID唯一，保留业务键"})
        return {"source_ids": sorted(set(array(row.get("source_ids")) or ([row["source_id"]] if row.get("source_id") else []))),
                "raw_locator": locator, "raw_locators": [locator], "review_status": "accepted",
                "notes": text(row.get("notes")), "external_verification": "not_performed"}

    def strong_sources(self, source_ids: list[str]) -> list[str]:
        return [s for s in source_ids if s in self.source_by_id and self.source_by_id[s]["evidence_form"] == "collector_summary"
                and self.source_by_id[s]["access_evidence_status"] == "reported_body_read"]

    def field_evidence(self, record_type: str, record_id: str, field: str, raw: Any, clean: Any,
                       sources: list, locator: str, decision: str, *, exact_sources: bool = False) -> None:
        self.audit["field_evidence"].append({"record_type": record_type, "record_id": record_id, "field": field,
            "raw_value": raw, "clean_value": clean, "source_ids": sources, "raw_locator": locator,
            "evidence_kind": "collector_record", "attribution_level": "single_source_record" if len(sources) == 1 else
            ("explicit_field_link" if exact_sources else "record_level_candidates"), "decision": decision})

    def load_inputs(self) -> None:
        packages = [p.parent for p in self.raw.rglob("products.json") if (p.parent / "wineries.json").is_file()]
        if len(packages) != 1:
            raise ValueError(f"需要唯一结构化资料包，发现{len(packages)}个，请明确版本")
        self.pkg = packages[0]
        keys = {"wineries": "winery_id", "products": "product_id", "sources": "source_id",
                "price_observations": "price_id", "reviews": "review_id", "quality_issues": "issue_id"}
        for name in [*keys, "aliases", "product_grapes"]:
            rows = json.loads(self.locate(name + ".json").read_text(encoding="utf-8-sig"))
            if not isinstance(rows, list):
                raise ValueError(f"{name}.json must be array")
            self.input_rows[name] = rows
            csv_rows = read_csv(self.locate(name + ".csv"))
            if len(rows) != len(csv_rows):
                raise ValueError(f"JSON/CSV记录数不同: {name}")
            mismatches = []
            for index, (a, b) in enumerate(zip(rows, csv_rows)):
                for field, val in a.items():
                    raw_csv = b.get(field, "")
                    normalized = array(raw_csv) if isinstance(val, list) else boolean(raw_csv) if isinstance(val, bool) else number(raw_csv) if isinstance(val, (int, float)) else text(raw_csv)
                    if normalized != (text(val) if isinstance(val, str) else val):
                        mismatches.append([index + 1, field])
            if mismatches:
                raise ValueError(f"JSON/CSV内容不一致: {name}: {mismatches[:8]}")
            self.mirror_checks.append({"dataset": name, "json_records": len(rows), "csv_records": len(csv_rows), "content_equal": True})
            if name in keys:
                ids = [str(r[keys[name]]) for r in rows]
                if len(set(ids)) != len(ids):
                    raise ValueError(f"原始ID冲突: {name}")
        self.input_rows["knowledge"] = read_jsonl(self.locate("knowledge.jsonl"))
        named = {r.get("winery_id"): r for r in self.input_rows["wineries"]}
        named.update({r.get("product_id"): r for r in self.input_rows["products"]})
        for rid, expected in self.rules.get("expected_names", {}).items():
            if rid not in named or text(named[rid].get("name")) != text(expected):
                raise ValueError(f"人工规则对象不匹配: {rid}，请复核rules.json，不能把旧ID规则套到其他对象")
        # 派生RAG文件只做重复/来源审计，正文由清洗后的规范记录重新构建。
        self.input_rows["rag_documents"] = read_jsonl(self.locate("rag_documents.jsonl"))
        db = self.locate("ningxia_wine.sqlite")
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            counts = {}
            for name in [*keys, "aliases", "product_grapes", "knowledge"]:
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                counts[name] = count
                if count != len(self.input_rows[name]):
                    raise ValueError(f"SQLite与主载体数量不一致: {name}")
            self.audit["input_checks"].append({"check": "sqlite_readonly_row_counts", "counts": counts, "passed": True})
        for r in self.input_rows["quality_issues"]:
            self.issue("raw_issue", str(r["record_id"]), str(r["field"]), "inherited_issue", str(r["finding"]),
                       str(r["handling"]), array(r.get("source_ids")), original_issue_id=r["issue_id"], original_status=r.get("status"))

    def clean_sources(self) -> None:
        for raw in self.input_rows["sources"]:
            status = str(raw.get("access_status") or "")
            snippet = "索引" in status
            body = "已读取" in status
            row = {"source_id": raw["source_id"], "title": text(raw.get("title")), "publisher": text(raw.get("publisher")),
                   "url": text(raw.get("url")), "raw_file": self.relpath(self.locate("sources.json")),
                   "source_type": "unknown", "source_type_raw": raw.get("source_type"),
                   "evidence_form": "search_snippet" if snippet else "collector_summary" if body else "unknown",
                   "access_evidence_status": "reported_body_read" if body else "snippet_only" if snippet else "unavailable",
                   "published_at": text(raw.get("published_at")), "accessed_date": text(raw.get("accessed_date")),
                   "access_status_raw": text(status), "snapshot_path": None, "dataset_role": "real",
                   "review_status": "accepted", "external_verification": "not_performed",
                   "raw_locator": f"{self.relpath(self.locate('sources.json'))}#source_id={raw['source_id']}",
                   "notes": "来源登记有效不等于其每项事实已核验；当前没有网页原件快照。"}
            st, pub = str(raw.get("source_type") or ""), str(raw.get("publisher") or "")
            if "赛事" in st:
                row["source_type"] = "competition"
            elif "专业酒评" in st:
                row["source_type"] = "professional_review"
            elif "教育" in st:
                row["source_type"] = "education"
            elif any(x in st for x in ["供应", "供货", "经销商", "展商"]):
                row["source_type"] = "merchant"
            elif raw["source_id"] <= "S021" or "酒庄官网" in st:
                row["source_type"] = "winery_official"
            else:
                row["source_type"] = "other"
            proof = self.pkg / "来源事实核对" / (raw["source_id"] + ".json")
            row["collector_check_path"] = self.relpath(proof) if proof.is_file() else None
            self.source_by_id[row["source_id"]] = row
            self.node_types[row["source_id"]] = "Document"
            self.node_status[row["source_id"]] = "accepted"
        self.tables["sources"] = list(self.source_by_id.values())

    def add_entity(self, kind: str, name: str, sources: list, locator: str, *, status: str = "accepted", region_kind: str | None = None) -> str:
        name = text(name)
        if not name:
            raise ValueError("实体名不能为空")
        # 只统一明确的同一文字产区的可选“产区”后缀，不猜测行政/葡萄酒地理归属。
        canonical = "宁夏贺兰山东麓" if kind == "Region" and name in {"宁夏贺兰山东麓", "宁夏贺兰山东麓产区"} else name
        eid = stable_id({"Region": "REG_", "GrapeVariety": "G_", "Flavor": "F_", "Concept": "C_"}[kind], kind, canonical)
        if eid not in self.entities:
            self.entities[eid] = {"entity_id": eid, "entity_type": kind, "name": canonical, "description": None,
                                  "region_kind": region_kind if kind == "Region" else None,
                                  "source_ids": sorted(set(sources)), "raw_locator": locator, "raw_locators": [locator],
                                  "review_status": status, "notes": "依据已披露文字抽取，不代表独立外部核验。",
                                  "external_verification": "not_performed"}
        else:
            e = self.entities[eid]
            e["source_ids"] = sorted(set(e["source_ids"] + sources))
            e["raw_locators"] = merge_list(e["raw_locators"], [locator])
            if status == "accepted":
                e["review_status"] = "accepted"
        self.node_types[eid] = kind
        self.node_status[eid] = self.entities[eid]["review_status"]
        return eid

    def add_relation(self, head: str, relation: str, tail: str, sources: list, locator: str,
                     *, status: str = "accepted", proportion: Any = None, notes: str | None = None) -> None:
        rid = stable_id("REL_", head, relation, tail)
        status = "accepted" if status == "accepted" and all(self.node_status.get(x) == "accepted" for x in [head, tail]) else "review"
        if rid not in self.relations:
            self.relations[rid] = {"relation_id": rid, "head_id": head, "relation_type": relation, "tail_id": tail,
                                   "proportion_pct": proportion, "source_ids": sorted(set(sources)),
                                   "raw_locator": locator, "raw_locators": [locator], "review_status": status,
                                   "notes": notes, "external_verification": "not_performed"}
        else:
            r = self.relations[rid]
            r["source_ids"] = sorted(set(r["source_ids"] + sources))
            r["raw_locators"] = merge_list(r["raw_locators"], [locator])
            if status == "accepted":
                r["review_status"] = "accepted"

    def clean_wineries(self) -> None:
        for raw in self.input_rows["wineries"]:
            row = self.provenance("wineries.json", raw, "winery_id", "Winery")
            rid = raw["winery_id"]
            row.update({"winery_id": rid, "name": text(raw.get("name")), "company_name": text(raw.get("company_name")),
                        "region_id": None, "location_raw": text(raw.get("location")), "address_raw": text(raw.get("address_raw")),
                        "address_normalized": text(raw.get("address_normalized")), "address_note": text(raw.get("address_note")),
                        "founded_year": number(raw.get("founded_year"), minimum=1000, maximum=self.as_of.year),
                        "company_founded_year": None, "website": text(raw.get("website")),
                        "telephone": text(raw.get("telephone")), "email": text(raw.get("email")), "introduction": None})
            if not self.strong_sources(row["source_ids"]):
                row["review_status"] = "review"
                self.issue("Winery", rid, "source_ids", "low_evidence", "仅有索引或未取得正文的来源", "保留待核验，不进入正式酒庄查询", row["source_ids"])
            override = self.rules["field_overrides"].get(rid, {}).get("founded_year")
            if override:
                row["company_founded_year"] = row["founded_year"]
                row["founded_year"] = override["value"]
                self.issue("Winery", rid, "founded_year", "wrong_scope", override["reason"], "公司年另存，酒庄成立年留空", row["source_ids"])
            self.winery_by_id[rid] = row
            self.node_types[rid] = "Winery"
            self.node_status[rid] = row["review_status"]
            if row["location_raw"]:
                loc = row["location_raw"]
                kind = "wine_region" if "贺兰山东麓" in loc or "产区" in loc else "administrative"
                row["region_id"] = self.add_entity("Region", loc, row["source_ids"], row["raw_locator"], status=row["review_status"], region_kind=kind)
                self.add_relation(rid, "LOCATED_IN", row["region_id"], row["source_ids"], row["raw_locator"])
            pieces = [row["name"] + "。"]
            if row["location_raw"]:
                pieces.append("采集资料将其所在地记为" + row["location_raw"] + "。")
            if row["founded_year"]:
                pieces.append(f"资料记载酒庄成立于{row['founded_year']}年。")
            if row["company_founded_year"]:
                pieces.append(f"{row['company_founded_year']}年为公司成立年，酒庄成立年未确认。")
            if row["notes"]:
                pieces.append(row["notes"])
            row["introduction"] = "".join(pieces)
            for field in ["name", "location_raw", "address_normalized", "founded_year", "company_founded_year"]:
                self.field_evidence("Winery", rid, field, raw.get({"location_raw": "location", "company_founded_year": "founded_year"}.get(field, field)), row[field],
                                    row["source_ids"], row["raw_locator"], "按原记录定位；多来源未有精确归因的保留候选来源，不任意选一个来源")
        self.tables["wineries"] = list(self.winery_by_id.values())

    def clean_products(self) -> None:
        for raw in self.input_rows["products"]:
            row = self.provenance("products.json", raw, "product_id", "Product")
            rid = raw["product_id"]
            name = text(raw.get("name"))
            vintage, vstatus, vproblem = vintage_value(raw, self.as_of.year)
            notes = str(raw.get("notes") or "")
            strong = self.strong_sources(row["source_ids"])
            catalog = is_catalog_candidate(raw, strong, self.rules["catalog_source_ids"])
            parent_ok = self.node_status.get(raw["winery_id"]) == "accepted"
            row["review_status"] = "accepted" if strong and parent_ok and not catalog else "review"
            color = COLOR.get(text(raw.get("color")))
            sweetness = SWEETNESS.get(text(raw.get("sweetness")))
            unknown_sweetness = text(raw.get("sweetness"))
            if unknown_sweetness and sweetness is None:
                sweetness = "other"  # Brut Nature单独保留原始标签，不当作静止酒残糖类别。
            # 只从明确的“干红/干白/半甜”等类型词补类别，不据品种猜测。
            label = " ".join(filter(None, [name, text(raw.get("wine_type_raw"))]))
            if color is None:
                color = "rose" if "桃红" in label else "red" if "干红" in label else "white" if "干白" in label else None
            if sweetness is None and ("干红" in label or "干白" in label):
                sweetness = "dry"
            override = self.rules["field_overrides"].get(rid, {})
            if "sweetness" in override:
                sweetness = override["sweetness"]["value"]
            row.update({"product_id": rid, "winery_id": raw["winery_id"], "name": name,
                        "series": text(raw.get("series")), "record_scope": "catalog_candidate" if catalog else
                        "specific_variant" if vstatus in {"specified", "non_vintage"} else "unspecified_product_page",
                        "vintage": vintage, "vintage_status": vstatus,
                        "volume_ml": volume_ml(raw.get("volume_ml")), "wine_type_raw": text(raw.get("wine_type_raw")),
                        "color": color, "sweetness": sweetness, "sweetness_raw": unknown_sweetness,
                        "is_sparkling": True if re.search(r"起泡|瓶中二次发酵", label + " " + notes) else None,
                        "alcohol_pct_vol": alcohol_pct(raw.get("alcohol_pct_vol")), "region_id": None,
                        "region_raw": text(raw.get("region")), "grapes_complete": boolean(raw.get("grapes_complete")),
                        "grape_varieties": [], "oak_aging_months": number(raw.get("oak_aging_months"), minimum=0),
                        "description": None, "price_cny": None, "strict_product_filter_eligible": False,
                        "field_filter_eligibility": {}, "field_filter_reasons": {},
                        "original_review_status": raw.get("review_status"),
                        "original_filter_eligible": boolean(raw.get("strict_product_filter_eligible"))})
            if not strong:
                self.issue("Product", rid, "source_ids", "low_evidence", "仅索引或正文未取得", "全部筛选字段停用，记录进入review", row["source_ids"])
            if catalog:
                self.issue("Product", rid, "record_scope", "catalog_candidate", "仅目录/供应商产品清单，缺少已核对详情", "保留候选，不进入正式筛选", row["source_ids"])
            if vproblem:
                self.issue("Product", rid, "vintage", vproblem, "年份状态或范围异常", "年份留空并待核验", row["source_ids"])
            if row["region_raw"]:
                row["region_id"] = self.add_entity("Region", row["region_raw"], row["source_ids"], row["raw_locator"], status=row["review_status"], region_kind="wine_region")
            self.product_by_id[rid] = row
            self.node_types[rid] = "Product"
            self.node_status[rid] = row["review_status"]
            self.add_relation(raw["winery_id"], "PRODUCES", rid, row["source_ids"], row["raw_locator"])
            if row["region_id"]:
                self.add_relation(rid, "ORIGINATES_FROM", row["region_id"], row["source_ids"], row["raw_locator"])
            mixed_sources = len(row["source_ids"]) > 1
            special = "strict_product_filter_eligible" in override
            for field in ["color", "sweetness", "vintage", "volume_ml", "alcohol_pct_vol", "region_id", "is_sparkling"]:
                ok = row["review_status"] == "accepted" and row[field] is not None and not special and not mixed_sources
                if field == "sweetness" and row["sweetness"] == "other":
                    ok = False
                row["field_filter_eligibility"][field] = ok
                row["field_filter_reasons"][field] = "usable" if ok else "missing" if row[field] is None else "special_recipe" if special else "field_source_attribution_unresolved" if mixed_sources else "record_or_label_not_eligible"
                raw_key = "region" if field == "region_id" else field
                self.field_evidence("Product", rid, field, raw.get(raw_key), row[field], row["source_ids"], row["raw_locator"], row["field_filter_reasons"][field])
                if row[field] is None:
                    self.issue("Product", rid, field, "missing", "原资料未提供可确定值", "保留null，不参与该字段筛选", row["source_ids"], severity="info")
            if "sweetness" in override:
                self.issue("Product", rid, "sweetness", "conflict", override["sweetness"]["reason"], "留空", row["source_ids"])
        self.tables["products"] = list(self.product_by_id.values())

    def clean_grapes(self) -> None:
        path = self.locate("product_grapes.json")
        for index, raw in enumerate(self.input_rows["product_grapes"], 1):
            pid, sid = raw["product_id"], raw["source_id"]
            prod = self.product_by_id[pid]
            loc = f"{self.relpath(path)}#record={index}"
            disclosed = boolean(raw.get("usable_for_filter")) is True
            conflict = "grape_varieties" in self.rules["field_overrides"].get(pid, {})
            status = "accepted" if prod["review_status"] == "accepted" and disclosed and not conflict and self.strong_sources([sid]) else "review"
            eid = self.add_entity("GrapeVariety", raw["grape"], [sid], loc, status=status)
            proportion = number(raw.get("proportion_pct"), minimum=0, maximum=100)
            self.add_relation(pid, "MADE_FROM", eid, [sid], loc, status=status, proportion=proportion,
                              notes="配比未知时为null；关系只对本产品记录粒度有效。")
            self.field_evidence("Product", pid, "grape_varieties", {"name": raw["grape"], "proportion_pct": raw.get("proportion_pct")},
                                {"entity_id": eid, "proportion_pct": proportion, "usable": status == "accepted"}, [sid], loc,
                                "冲突/低证据候选隔离" if status != "accepted" else "明确品种披露", exact_sources=True)
            if status == "accepted":
                prod["grape_varieties"].append({"entity_id": eid, "name": text(raw["grape"]), "proportion_pct": proportion,
                                                "source_ids": [sid]})
            else:
                self.issue("Product", pid, "grape_varieties", "conflict" if conflict else "low_evidence", "该品种关系不具备严格筛选资格", "关系进入review，不写入正式产品配方", [sid])
        for prod in self.product_by_id.values():
            grapes = prod["grape_varieties"]
            prod["field_filter_eligibility"]["grape_varieties"] = bool(grapes)
            prod["field_filter_reasons"]["grape_varieties"] = "usable_disclosed_varieties_only" if grapes else "missing_or_conflicted"
            if grapes and all(g["proportion_pct"] is not None for g in grapes) and sum(g["proportion_pct"] for g in grapes) == 100:
                prod["grapes_complete"] = True
            prod["strict_product_filter_eligible"] = any(prod["field_filter_eligibility"].values())
            if prod["product_id"] == "P043":
                prod["strict_product_filter_eligible"] = False
                for field in prod["field_filter_eligibility"]:
                    prod["field_filter_eligibility"][field] = False
                    prod["field_filter_reasons"][field] = "special_recipe"

    def clean_prices(self) -> None:
        for raw in self.input_rows["price_observations"]:
            row = self.provenance("price_observations.json", raw, "price_id", "PriceObservation")
            sid, pid = raw["source_id"], raw["product_id"]
            quote, precision = date_value(raw.get("quote_date"))
            price_type_raw = str(raw.get("price_type") or "")
            kind = "factory" if "出厂" in price_type_raw else "suggested_retail" if "建议" in price_type_raw else "wholesale" if "供货" in price_type_raw else "reference"
            row.update({"price_id": raw["price_id"], "product_id": pid,
                        "amount_min": number(raw.get("amount_min"), minimum=0.000001),
                        "amount_max": number(raw.get("amount_max"), minimum=0.000001),
                        "currency": str(raw.get("currency") or "").upper(),
                        "unit": {"瓶": "bottle", "盒": "box", "箱": "case"}.get(raw.get("unit"), "unknown"),
                        "unit_raw": text(raw.get("unit")), "pack_quantity": None,
                        "minimum_order_raw": text(raw.get("minimum_order_raw")),
                        "price_type": kind, "price_type_raw": text(price_type_raw),
                        "quote_date": quote, "quote_date_precision": precision,
                        "observed_date": date_value(raw.get("observed_date"))[0], "verified_at": None,
                        "tax_shipping_note": text(raw.get("tax_shipping_note")), "source_id": sid,
                        "current_retail_eligible": False, "strict_price_filter_eligible": False,
                        "conflict": boolean(raw.get("conflict")) is True,
                        "usage": "reference_only", "display_notice": "来源参考报价；不代表当前可下单零售价，须同时展示币种、时间、单位和起订条件。"})
            if not self.strong_sources([sid]) or self.product_by_id[pid]["review_status"] != "accepted" or row["conflict"]:
                row["review_status"] = "review"
            if row["amount_min"] is None or row["amount_max"] is None or row["amount_min"] > row["amount_max"]:
                row["review_status"] = "excluded"
                self.issue("PriceObservation", row["price_id"], "amount", "invalid_number", "无效金额或区间", "隔离，不用于查询", [sid])
            self.issue("PriceObservation", row["price_id"], "eligibility", "reference_price_only", "没有本次当前零售核验依据", "保留参考报价；current/strict资格均为false", [sid], severity="info")
            self.field_evidence("PriceObservation", row["price_id"], "price", raw, {k: row[k] for k in ["amount_min", "amount_max", "currency", "unit", "price_type", "quote_date"]}, [sid], row["raw_locator"], "保留报价性质与时间，不补当前价格")
            self.tables["price_observations"].append(row)

    def clean_professional_reviews(self) -> None:
        mapping = {"供应商产品描述": "supplier_description", "展商产品描述": "exhibitor_description",
                   "独立专业酒评": "professional_review", "赛事专业评审": "competition_review", "赛事官方报道": "competition_report"}
        for raw in self.input_rows["reviews"]:
            row = self.provenance("reviews.json", raw, "review_id", "ProfessionalReview")
            pid, sid = raw["product_id"], raw["source_id"]
            row.update({"review_id": raw["review_id"], "product_id": pid, "review_type": mapping.get(raw.get("review_type"), "other"),
                        "review_type_raw": text(raw.get("review_type")), "summary": text(raw.get("summary")),
                        "score": number(raw.get("score"), minimum=0), "score_scale": number(raw.get("score_scale"), minimum=0.000001),
                        "score_raw": text(raw.get("score_raw")), "medal": text(raw.get("medal")),
                        "competition_year": number(raw.get("competition_year"), minimum=1000, maximum=self.as_of.year),
                        "review_date": date_value(raw.get("review_date"))[0], "observed_date": date_value(raw.get("observed_date"))[0],
                        "source_id": sid, "summary_type": "faithful_summary", "consumer_verified_purchase": None,
                        "fact_scope": self.product_scope(self.product_by_id[pid])})
            if not self.strong_sources([sid]) or self.product_by_id[pid]["review_status"] != "accepted":
                row["review_status"] = "review"
            if row["score"] is not None and (row["score_scale"] is None or row["score"] > row["score_scale"]):
                self.issue("ProfessionalReview", row["review_id"], "score", "invalid_score", "分值与满分不一致", "清空规范分值，保留原始分值", [sid])
                row["score"] = None
            self.tables["professional_reviews"].append(row)
            if row["summary"]:
                for flavor in extract_flavors(row["summary"], self.rules["flavor_terms"]):
                    eid = self.add_entity("Flavor", flavor, [sid], row["raw_locator"], status=row["review_status"])
                    self.add_relation(pid, "HAS_FLAVOR", eid, [sid], row["raw_locator"], status=row["review_status"],
                                      notes="来源评价中的字面风味标签，非客观强度测量；仅对应所关联产品与年份。")

    @staticmethod
    def product_scope(prod: dict) -> str:
        v = f"{prod['vintage']}年份" if prod["vintage_status"] == "specified" else "明确NV" if prod["vintage_status"] == "non_vintage" else "未披露年份的产品页"
        vol = f"，{prod['volume_ml']}ml" if prod["volume_ml"] else "，容量未披露"
        return prod["name"] + "（" + v + vol + "）"

    def product_summary(self, prod: dict) -> str:
        parts = [self.product_scope(prod) + "，所属酒庄为" + self.winery_by_id[prod["winery_id"]]["name"] + "。"]
        labels = {"color": {"red": "红", "white": "白", "rose": "桃红", "other": "其他颜色"},
                  "sweetness": {"dry": "干型", "semi_dry": "半干型", "semi_sweet": "半甜型", "sweet": "甜型", "other": prod.get("sweetness_raw")}}
        for field, label in [("color", "颜色"), ("sweetness", "甜度")]:
            if prod[field] is not None:
                parts.append(label + "：" + labels[field][prod[field]] + "。")
        if prod["alcohol_pct_vol"] is not None:
            parts.append(f"酒精度{prod['alcohol_pct_vol']}%vol。")
        if prod["region_raw"]:
            parts.append("产品资料标注产区为" + prod["region_raw"] + "。")
        if prod["grape_varieties"]:
            parts.append("明确披露的品种包括" + "、".join(g["name"] for g in prod["grape_varieties"]) + "；未披露的配比不补齐。")
        if prod["product_id"] == "P003":
            parts.append("品种信息存在正文与图片冲突，不作确定品种回答。")
        if prod["product_id"] == "P043":
            parts.append("该来源记载含10%米酒，不能作为普通纯葡萄酒参与条件筛选。")
        if prod["oak_aging_months"] is not None:
            parts.append(f"资料记载橡木桶陈酿{prod['oak_aging_months']}个月。")
        parts.append("参数仅对应此记录适用范围，不能外推其他年份或规格；当前售价未核验。")
        return "".join(parts)

    def knowledge_category(self, raw: dict) -> str:
        category, title = str(raw.get("category") or ""), str(raw.get("title") or "")
        if "酒庄" in category or category in {"联系信息", "人员介绍", "酿造知识"}:
            return "winery_intro"
        if "产品" in category:
            return "product_intro"
        if any(x in title for x in ["存酒", "存放", "储藏", "保存", "开瓶", "冷藏"]):
            return "storage"
        if any(x in title for x in ["配酒", "配餐", "咸味"]):
            return "pairing"
        if any(x in title for x in ["温度", "室温", "冰", "醒酒", "沉淀", "杯", "倒酒"]):
            return "serving"
        if "单宁" in title or "香气" in title:
            return "tasting"
        return "wine_basics"

    def clean_knowledge(self) -> None:
        by_product_source = defaultdict(list)
        for p in self.product_by_id.values():
            for sid in p["source_ids"]:
                by_product_source[sid].append(p)
        covered = set()
        seen = {}
        for raw in self.input_rows["knowledge"]:
            row = self.provenance("knowledge.jsonl", raw, "knowledge_id", "Chunk")
            kid = raw["knowledge_id"]
            wid, pid = raw.get("winery_id"), raw.get("product_id")
            title, content = text(raw.get("title")), text(raw.get("text"))
            category = self.knowledge_category(raw)
            if raw.get("category") == "产品资料" and not pid:
                candidates = [p for s in row["source_ids"] for p in by_product_source[s] if p["name"] in (title or "")]
                unique = {p["product_id"]: p for p in candidates}
                if len(unique) == 1:
                    pid = next(iter(unique))
            if pid and pid in self.product_by_id:
                prod = self.product_by_id[pid]
                content = self.product_summary(prod)
                covered.add(pid)
                self.audit["derived_records"].append({"record_id": kid, "action": "regenerated_product_text", "product_id": pid,
                                                       "reason": "按清洗主数据重建，避免冲突字段及旧报价进入RAG"})
            if raw.get("category") == "酒庄资料" and wid in self.winery_by_id:
                content = self.winery_by_id[wid]["introduction"]
                self.audit["derived_records"].append({"record_id": kid, "action": "regenerated_winery_text", "winery_id": wid,
                                                       "reason": "与规范主数据一致，区分公司成立年和酒庄成立年"})
            entities = [x for x in [wid, pid] if x]
            if not content:
                row["review_status"] = "excluded"
            elif not self.strong_sources(row["source_ids"]) or any(self.node_status.get(e) != "accepted" for e in entities):
                row["review_status"] = "review"
            if not entities and content:
                topic = self.add_entity("Concept", title, row["source_ids"], row["raw_locator"], status=row["review_status"])
                entities.append(topic)
            row.update({"knowledge_id": kid, "title": title, "text": content, "category": category, "entity_ids": entities,
                        "text_type": "faithful_summary", "fact_scope": self.product_scope(self.product_by_id[pid]) if pid else
                        self.winery_by_id[wid]["name"] if wid in self.winery_by_id else "通用知识，保留原文条件",
                        "content_origin": "regenerated_from_clean_records" if pid or raw.get("category") == "酒庄资料" else "collector_summary",
                        "retrieval_allowed": row["review_status"] == "accepted", "contains_price": False})
            signature = (key_text(content), tuple(sorted(entities)))
            if signature in seen:
                target = seen[signature]
                target["source_ids"] = sorted(set(target["source_ids"] + row["source_ids"]))
                target["raw_locators"] = merge_list(target["raw_locators"], row["raw_locators"])
                self.audit["duplicates"].append({"record_type": "Chunk", "original_id": kid, "canonical_id": target["knowledge_id"], "reason": "同范围规范文本完全相同"})
                continue
            seen[signature] = row
            self.tables["knowledge"].append(row)
        for p in self.product_by_id.values():
            p["description"] = self.product_summary(p)
            if p["product_id"] not in covered:
                self.tables["knowledge"].append({"knowledge_id": "KP_" + p["product_id"], "title": p["name"] + "基础信息",
                    "text": p["description"], "category": "product_intro", "entity_ids": [p["winery_id"], p["product_id"]],
                    "text_type": "faithful_summary", "fact_scope": self.product_scope(p), "source_ids": p["source_ids"],
                    "raw_locator": p["raw_locator"], "raw_locators": p["raw_locators"], "review_status": p["review_status"],
                    "notes": "清洗后的产品记录派生，不增加新事实。", "external_verification": "not_performed",
                    "content_origin": "regenerated_from_clean_records", "retrieval_allowed": p["review_status"] == "accepted", "contains_price": False})
        for r in self.tables["professional_reviews"]:
            if not r["summary"]:
                continue
            p = self.product_by_id[r["product_id"]]
            publisher = self.source_by_id[r["source_id"]]["publisher"]
            content = f"{self.product_scope(p)}。据{publisher}的{r['review_type_raw']}摘要：{r['summary']}描述仅适用于上述记录范围，属于来源评价，不是客观强度测量。"
            self.tables["knowledge"].append({"knowledge_id": "KR_" + r["review_id"], "title": p["name"] + "来源品鉴描述",
                "text": content, "category": "tasting", "entity_ids": [p["product_id"]], "text_type": "subjective_opinion",
                "fact_scope": self.product_scope(p), "source_ids": r["source_ids"], "raw_locator": r["raw_locator"],
                "raw_locators": r["raw_locators"], "review_status": r["review_status"], "notes": "不是消费者购买评论。",
                "external_verification": "not_performed", "content_origin": "professional_review_summary",
                "retrieval_allowed": r["review_status"] == "accepted", "contains_price": False})
        for k in self.tables["knowledge"]:
            self.node_types[k["knowledge_id"]] = "Chunk"
            self.node_status[k["knowledge_id"]] = k["review_status"]
            for s in k["source_ids"]:
                self.add_relation(s, "HAS_CHUNK", k["knowledge_id"], [s], k["raw_locator"], status=k["review_status"])
            for e in k["entity_ids"]:
                self.add_relation(k["knowledge_id"], "ABOUT", e, k["source_ids"], k["raw_locator"], status=k["review_status"])
        for d in self.input_rows["rag_documents"]:
            self.audit["derived_records"].append({"record_id": d.get("doc_id"), "original_doc_type": d.get("doc_type"),
                                                   "action": "not_reimported", "reason": "原始派生文本不重复入库；使用清洗主记录重建检索资料"})

    def clean_aliases(self) -> None:
        def add(alias: str, eid: str, kind: str, sources: list, loc: str):
            if not text(alias) or eid not in self.node_types:
                return
            aid = stable_id("A_", key_text(alias), eid)
            status = "accepted" if self.node_status.get(eid) == "accepted" and self.strong_sources(sources) else "review"
            if aid in self.aliases:
                self.aliases[aid]["source_ids"] = sorted(set(self.aliases[aid]["source_ids"] + sources))
                self.aliases[aid]["raw_locators"] = merge_list(self.aliases[aid]["raw_locators"], [loc])
                return
            self.aliases[aid] = {"alias_id": aid, "alias": text(alias), "entity_id": eid, "entity_type": self.node_types[eid],
                                 "alias_kind": kind, "source_ids": sorted(set(sources)), "raw_locator": loc, "raw_locators": [loc],
                                 "review_status": status, "notes": None, "ambiguous": False}
        p = self.locate("aliases.json")
        for i, r in enumerate(self.input_rows["aliases"], 1):
            alias, eid = r["alias"], r["entity_id"]
            kind = "corporate_name" if "公司" in alias else "canonical" if eid in self.winery_by_id and alias == self.winery_by_id[eid]["name"] else "abbreviation"
            add(alias, eid, kind, [r["source_id"]], f"{self.relpath(p)}#record={i}")
        for row in [*self.winery_by_id.values(), *self.product_by_id.values(), *self.entities.values()]:
            eid = row.get("winery_id") if "product_id" not in row and "entity_id" not in row else row.get("product_id") or row.get("entity_id")
            add(row["name"], eid, "canonical", row["source_ids"], row["raw_locator"])
        groups = defaultdict(set)
        for a in self.aliases.values():
            groups[key_text(a["alias"])].add(a["entity_id"])
        for a in self.aliases.values():
            a["ambiguous"] = len(groups[key_text(a["alias"])]) > 1
        self.tables["aliases"] = list(self.aliases.values())

    def run(self) -> None:
        self.load_inputs()
        self.clean_sources()
        self.clean_wineries()
        self.clean_products()
        self.clean_grapes()
        self.clean_prices()
        self.clean_professional_reviews()
        self.clean_knowledge()
        self.tables["entities"] = list(self.entities.values())
        self.clean_aliases()
        self.tables["relations"] = list(self.relations.values())
        self.clean_visitor_reviews()
        self.finalize()

    def clean_visitor_reviews(self) -> None:
        """统一评论导出、合并可确认的重复；内容始终只进入辅助待审区。"""
        candidates: list[dict] = []
        sources: dict[str, dict] = {}
        alias_to_winery: dict[str, set[str]] = defaultdict(set)
        for a in self.tables["aliases"]:
            if a["entity_type"] == "Winery" and a["review_status"] == "accepted":
                alias_to_winery[key_text(a["alias"])].add(a["entity_id"])
        for p in sorted(self.raw.rglob("*")):
            if not p.is_file() or self.pkg in p.parents or p.suffix.lower() not in {".csv", ".jsonl"}:
                continue
            self.files_read.add(p.resolve())
            rows = read_csv(p) if p.suffix == ".csv" else read_jsonl(p)
            input_path = self.relpath(p)
            for i, raw in enumerate(rows, 1):
                body, redacted = redact_review(raw.get("content") or raw.get("review_text") or raw.get("内容"))
                label = text(raw.get("winery") or raw.get("winery_std") or raw.get("shop_name"))
                label_origin = "record"
                if not label and (p.parent.name == "ctrip" or p.stem.endswith("_评论")):
                    label = p.stem.removesuffix("_评论")
                    label_origin = "file_name_candidate"
                origin = text(raw.get("source") or raw.get("来源"))
                platform = "ctrip" if p.parent.name == "ctrip" or origin in {"ctrip", "携程", "Trip.com(繁體)"} else \
                    "dianping" if "dianping" in p.stem or p.stem.startswith("captured_reviews") or origin in {"大众点评", "dianping_mrn", "大众点评/点评"} else "unknown"
                orig_id = raw.get("commentId") or raw.get("comment_id") or raw.get("review_id") or raw.get("id") or raw.get("序号")
                orig_id = str(orig_id) if orig_id is not None and str(orig_id).strip() else None
                external = external_review_id(raw, platform)
                export_key = (origin, orig_id) if p.stem in {"reviews_all", "reviews_all_before_ctrip"} and orig_id else None
                raw_date = raw.get("publishTime") or raw.get("publish_time") or raw.get("date") or raw.get("时间(标准化)") or raw.get("时间") or raw.get("time_text")
                parsed_date, precision = date_value(raw_date)
                source_id = stable_id("VS_", input_path, origin, platform)
                if source_id not in sources:
                    sources[source_id] = {"source_id": source_id, "title": p.stem + "评论输入",
                                          "publisher": origin, "url": None, "raw_file": input_path,
                                          "source_type": "user_review" if platform != "unknown" else "unknown",
                                          "evidence_form": "user_comment", "accessed_date": None,
                                          "published_at": None, "dataset_role": "real", "review_status": "review",
                                          "notes": "本地评论输入；没有重新访问平台或核实作者/购买身份。"}
                matches = alias_to_winery.get(key_text(label), set())
                wid = next(iter(matches)) if len(matches) == 1 else None
                locator = f"{input_path}#record={i}"
                content_hash = hashlib.sha256(key_text(body).encode("utf-8")).hexdigest()
                urls = [text(raw.get(k)) for k in ["url", "source_url", "jumpH5Url"]]
                urls = sorted(set(u for u in urls if u and u.startswith(("https://", "http://"))))
                rid = stable_id("VR_", platform, external) if external else stable_id("VR_", input_path, orig_id, label, parsed_date, content_hash)
                row = {"review_id": rid, "text": body, "content_hash": content_hash,
                       "winery_name_raw": label, "winery_names_raw": [label] if label else [], "winery_id": wid,
                       "winery_match_status": "exact_alias" if wid else "ambiguous" if matches else "unmapped",
                       "winery_label_origin": label_origin, "platform": platform,
                       "source_label_raw": origin, "external_review_ids": [external] if external else [],
                       "published_date": parsed_date, "date_precision": precision,
                       "date_candidates": [parsed_date] if parsed_date else [],
                       "date_raw": text(raw_date), "source_ids": [source_id], "source_urls": urls,
                       "raw_locator": locator, "raw_locators": [locator],
                       "original_records": [{"input_file": input_path, "original_id": orig_id, "record_number": i}],
                       "review_status": "review", "text_type": "subjective_opinion" if platform != "unknown" else "unclassified_excerpt", "product_id": None,
                       "approved_usage": [], "consumer_verified_purchase": None,
                       "personal_fields_removed": True, "text_redacted": redacted,
                       "content_class": "visitor_experience_candidate" if body and re.search(r"参观|酒庄|讲解|餐|住|旅游|打卡|建筑", body) else "unclassified",
                       "notes": "仅供人工复核的辅助资料，不能直接作为产品参数、口感事实或当前营业政策。",
                       "_external": (platform, external) if external else None,
                       "_export_key": export_key, "_compat": "compat" in p.stem}
                if not body or len(re.sub(r"\W", "", body)) < 4:
                    row["review_status"] = "excluded"
                    row["exclusion_reason"] = "empty_or_low_information"
                candidates.append(row)
        self.audit["input_checks"].append({"check": "visitor_input_rows", "rows": len(candidates),
                                            "files": len({r["original_records"][0]["input_file"] for r in candidates})})
        records: list[dict] = []
        external_index = defaultdict(list)
        export_index = defaultdict(list)
        text_index = defaultdict(list)
        uid_counts = Counter()

        def merge(target: dict, row: dict, reason: str):
            for k in ["source_ids", "source_urls", "raw_locators", "original_records", "external_review_ids", "date_candidates", "winery_names_raw"]:
                target[k] = merge_list(target[k], row[k])
            if target["published_date"] and row["published_date"] and not date_compatible(target["published_date"], row["published_date"]):
                target["published_date"] = None
                target["date_precision"] = "unknown"
                target["date_conflict"] = True
            elif not target.get("date_conflict") and len(row["published_date"] or "") > len(target["published_date"] or ""):
                target["published_date"], target["date_precision"] = row["published_date"], row["date_precision"]
            if not target["winery_name_raw"] and row["winery_name_raw"]:
                for k in ["winery_name_raw", "winery_id", "winery_match_status", "winery_label_origin"]:
                    target[k] = row[k]
            if target["platform"] == "unknown" and row["platform"] != "unknown":
                target["platform"] = row["platform"]
            target["text_redacted"] |= row["text_redacted"]
            if row["_external"] and target not in external_index[row["_external"]]:
                external_index[row["_external"]].append(target)
            if row["_export_key"] and target not in export_index[row["_export_key"]]:
                export_index[row["_export_key"]].append(target)
            self.audit["duplicates"].append({"record_type": "VisitorReview", "original_locator": row["raw_locator"],
                                             "canonical_id": target["review_id"], "content_hash": row["content_hash"], "reason": reason})

        # 确实带平台外部ID的行优先，兼容导出排在后面，便于只合并唯一对应的副本。
        candidates.sort(key=lambda r: (r["_external"] is None, r["_compat"], r["raw_locator"]))
        for row in candidates:
            target, reason = None, None
            if row["_external"]:
                possible = [r for r in external_index[row["_external"]] if r["content_hash"] == row["content_hash"]]
                if len(possible) == 1:
                    target, reason = possible[0], "same_platform_external_id_and_text"
            if target is None and row["_export_key"]:
                possible = [r for r in export_index[row["_export_key"]] if r["content_hash"] == row["content_hash"]]
                if len(possible) == 1:
                    target, reason = possible[0], "same_collection_id_and_text_across_exports"
            if target is None and row["_compat"] and row["text"]:
                possible = [r for r in text_index[row["content_hash"]]
                            if (r["platform"] == row["platform"] or "unknown" in {r["platform"], row["platform"]})
                            and (not r["winery_name_raw"] or not row["winery_name_raw"] or key_text(r["winery_name_raw"]) == key_text(row["winery_name_raw"]))
                            and date_compatible(r["published_date"], row["published_date"])]
                if len(possible) == 1:
                    target, reason = possible[0], "unique_compatible_export_copy"
            if target is not None:
                merge(target, row, reason)
                continue
            if row["_external"] and external_index[row["_external"]]:
                row["identity_conflict"] = True
                self.issue("VisitorReview", row["review_id"], "identity", "same_id_different_text", "相同平台ID出现不同正文", "保留变体待审，不按ID覆盖")
            if text_index[row["content_hash"]] and row["text"]:
                row["duplicate_candidate"] = True
                self.audit["duplicate_candidates"].append({"record_type": "VisitorReview", "raw_locator": row["raw_locator"],
                    "content_hash": row["content_hash"], "reason": "正文相同但缺少唯一同一评论身份依据，保留待审"})
            uid_counts[row["review_id"]] += 1
            if uid_counts[row["review_id"]] > 1:
                row["review_id"] = stable_id("VR_", row["review_id"], row["content_hash"], row["raw_locator"])
            records.append(row)
            if row["_external"]:
                external_index[row["_external"]].append(row)
            if row["_export_key"]:
                export_index[row["_export_key"]].append(row)
            text_index[row["content_hash"]].append(row)
        for row in records:
            for k in list(row):
                if k.startswith("_"):
                    del row[k]
            # 同一平台ID的来源对象标签若不一致，保留候选集合而不是任意定归属。
            names = {key_text(n) for n in row["winery_names_raw"]}
            if len(names) > 1:
                ids = {w for n in names for w in alias_to_winery.get(n, set())}
                if len(ids) != 1:
                    row["winery_id"] = None
                    row["winery_match_status"] = "conflicting_labels"
            self.audit["review_decisions"].append({"review_id": row["review_id"], "decision": row["review_status"],
                "original_record_count": len(row["original_records"]), "reason": row.get("exclusion_reason", "auxiliary_only_no_product_facts"),
                "winery_match_status": row["winery_match_status"]})
        self.visitor_reviews = sorted(records, key=lambda r: r["review_id"])
        self.visitor_sources = sorted(sources.values(), key=lambda r: r["source_id"])
        self.visitor_input_count = len(candidates)

    def build_report(self, partitions: dict[str, dict[str, list]]) -> dict:
        counts = {name: {status: len(rows) for status, rows in split.items()} | {"total": sum(map(len, split.values()))}
                  for name, split in partitions.items()}
        counts["visitor_reviews"] = {"input_rows": self.visitor_input_count,
            "after_confirmed_dedup": len(self.visitor_reviews),
            "confirmed_duplicate_rows": self.visitor_input_count - len(self.visitor_reviews),
            "auxiliary_review": sum(r["review_status"] == "review" for r in self.visitor_reviews),
            "excluded": sum(r["review_status"] == "excluded" for r in self.visitor_reviews)}
        all_products = list(self.product_by_id.values())
        accepted = partitions["products"]["accepted"]
        fields = ["vintage", "volume_ml", "color", "sweetness", "alcohol_pct_vol", "region_id", "grape_varieties", "is_sparkling"]
        stats = {}
        for field in fields:
            present = lambda p: p[field] is not None and p[field] != []
            stats[field] = {"all_non_null": sum(present(p) for p in all_products),
                            "accepted_non_null": sum(present(p) for p in accepted),
                            "accepted_filter_usable": sum(p["field_filter_eligibility"].get(field, False) for p in accepted)}
        prices = self.tables["price_observations"]
        raw_flag_changed = [p["product_id"] for p in all_products if p["original_filter_eligible"] is True and not p["strict_product_filter_eligible"]]
        return {"rules_version": self.rules["rules_version"], "as_of_date": self.as_of.isoformat(),
                "counts": counts, "product_fields": stats,
                "raw_structured_counts": {name: len(rows) for name, rows in self.input_rows.items()},
                "source_evidence": dict(Counter(s["evidence_form"] for s in self.tables["sources"])),
                "price_distribution": {field: dict(Counter(str(p[field]) for p in prices)) for field in ["currency", "price_type", "quote_date_precision", "unit"]},
                "current_retail_eligible_count": sum(p["current_retail_eligible"] for p in prices),
                "strict_price_filter_eligible_count": sum(p["strict_price_filter_eligible"] for p in prices),
                "filter_eligibility_downgraded_ids": raw_flag_changed,
                "knowledge_categories": dict(Counter(k["category"] for k in partitions["knowledge"]["accepted"])),
                "knowledge_origins": dict(Counter(k["content_origin"] for k in partitions["knowledge"]["accepted"])),
                "supported_filter_examples": {
                    "dry_red_750ml_product_ids": [p["product_id"] for p in accepted if all(p["field_filter_eligibility"].get(f, False) for f in ["color", "sweetness", "volume_ml"])
                                                  and p["color"] == "red" and p["sweetness"] == "dry" and p["volume_ml"] == 750],
                    "current_price_budget_filter": "unavailable_no_qualified_current_prices"},
                "quality_issue_counts": dict(Counter(q["issue_type"] for q in self.issues.values())),
                "input_mirror_checks": self.mirror_checks, "raw_files_unchanged": True,
                "limitations": ["只执行本地清洗，没有重新访问网站、核验当前报价或真实购买身份。",
                    "accepted表示本地结构和证据一致性通过；来源多为采集摘要，不是独立真实性认证。",
                    "没有合格当前零售价，不支持当前零售价预算筛选；参考报价单独展示。",
                    "未指定年份页面不外推具体年份；混合来源缺少字段精确归因时保守停用对应筛选。",
                    "旅游/平台评论全部为辅助待审资料，不进入正式RAG和产品风味图。",
                    "未生成embedding；正式文本与结构验收后再统一向量化。",
                    "现有通用知识以保存、侍酒和品鉴为主；颜色/甜度基础与品种百科仍需补充。"]}

    def schema(self) -> dict:
        def obj(required: list[str], properties: dict) -> dict:
            return {"type": "object", "required": required, "properties": properties, "additionalProperties": True}
        string = {"type": "string", "minLength": 1}
        nullable_string = {"type": ["string", "null"]}
        nullable_number = {"type": ["number", "null"]}
        refs = {"type": "array", "items": string, "minItems": 1, "uniqueItems": True}
        common = {"source_ids": refs, "raw_locator": string,
                  "review_status": {"enum": ["accepted", "review", "excluded"]}}
        common_required = list(common)
        definitions = {
            "sources": obj(["source_id", "title", "raw_file", "evidence_form", "dataset_role"], {
                "source_id": string, "title": string, "url": nullable_string, "raw_file": string,
                "evidence_form": {"enum": ["original_text", "collector_summary", "search_snippet", "user_comment", "unknown"]},
                "dataset_role": {"enum": ["real", "demo"]}}),
            "wineries": obj(["winery_id", "name", *common_required], {**common, "winery_id": string, "name": string,
                "region_id": nullable_string, "founded_year": {"type": ["integer", "null"]}}),
            "products": obj(["product_id", "winery_id", "name", "vintage_status", "record_scope", "field_filter_eligibility", *common_required], {
                **common, "product_id": string, "winery_id": string, "name": string,
                "vintage": {"type": ["integer", "null"]}, "vintage_status": {"enum": ["specified", "non_vintage", "unspecified", "mixed"]},
                "record_scope": {"enum": ["specific_variant", "unspecified_product_page", "catalog_candidate", "mixed_variant"]},
                "volume_ml": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "color": {"enum": ["red", "white", "rose", "other", None]},
                "sweetness": {"enum": ["dry", "semi_dry", "semi_sweet", "sweet", "other", None]},
                "alcohol_pct_vol": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                "price_cny": {"type": "null"}, "grape_varieties": {"type": "array"},
                "strict_product_filter_eligible": {"type": "boolean"}, "field_filter_eligibility": {"type": "object", "additionalProperties": {"type": "boolean"}}}),
            "price_observations": obj(["price_id", "product_id", "amount_min", "amount_max", "currency", "unit", "price_type", "current_retail_eligible", "strict_price_filter_eligible", *common_required], {
                **common, "price_id": string, "product_id": string, "amount_min": {"type": ["number", "null"]},
                "amount_max": {"type": ["number", "null"]}, "currency": string,
                "unit": {"enum": ["bottle", "box", "case", "other", "unknown"]},
                "price_type": {"enum": ["retail_observed", "suggested_retail", "wholesale", "factory", "promotion_conditional", "reference", "unknown"]},
                "current_retail_eligible": {"const": False}, "strict_price_filter_eligible": {"const": False}}),
            "entities": obj(["entity_id", "entity_type", "name", *common_required], {**common, "entity_id": string, "name": string,
                "entity_type": {"enum": ["Region", "GrapeVariety", "Flavor", "Concept"]}}),
            "aliases": obj(["alias_id", "alias", "entity_id", "entity_type", "alias_kind", *common_required], {
                **common, "alias_id": string, "alias": string, "entity_id": string,
                "entity_type": {"enum": ["Winery", "Product", "Region", "GrapeVariety", "Flavor", "Concept"]},
                "alias_kind": {"enum": ["canonical", "abbreviation", "english_name", "corporate_name", "other"]}, "ambiguous": {"type": "boolean"}}),
            "knowledge": obj(["knowledge_id", "title", "text", "category", "entity_ids", "text_type", "fact_scope", *common_required], {
                **common, "knowledge_id": string, "title": string, "text": string,
                "category": {"enum": ["winery_intro", "product_intro", "region", "grape", "wine_basics", "tasting", "storage", "serving", "pairing"]},
                "entity_ids": {"type": "array", "items": string},
                "text_type": {"enum": ["excerpt", "faithful_summary", "subjective_opinion"]},
                "fact_scope": string, "contains_price": {"const": False}, "retrieval_allowed": {"type": "boolean"}}),
            "relations": obj(["relation_id", "head_id", "relation_type", "tail_id", *common_required], {
                **common, "relation_id": string, "head_id": string, "tail_id": string,
                "relation_type": {"enum": list(REL_TYPES)}, "proportion_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 100}}),
            "professional_reviews": obj(["review_id", "product_id", "review_type", "fact_scope", *common_required], {
                **common, "review_id": string, "product_id": string, "review_type": string,
                "score": nullable_number, "score_scale": nullable_number, "summary": nullable_string}),
        }
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:ningxia-wine:clean-data:1.0",
                "title": "宁夏葡萄酒清洗记录Schema（各JSONL按同名$defs逐行验证）", "$defs": definitions,
                "x-datasets": {name + ".jsonl": {"$ref": "#/$defs/" + name} for name in definitions}}

    def data_readme(self, partitions: dict) -> str:
        rows = ["# 清洗数据结构与使用说明", "", f"数据版本：{self.rules['rules_version']}；评估日期：{self.as_of.isoformat()}。", "",
                "目录与 `data_raw`、`data_cleaning` 同级。所有业务和审计数据均为 UTF-8 JSON/JSONL，文档使用 Markdown。JSONL 每个非空行是一个 JSON 对象；null 表示未知，数组是真实数组，数值不夹带单位。", "",
                "## 文件与实际数量", "", "| 文件 | 正式 accepted | 待核验 review | 排除 excluded |", "|---|---:|---:|---:|"]
        for name, split in sorted(partitions.items()):
            rows.append(f"| `{name}.jsonl` | {len(split['accepted'])} | {len(split['review'])} | {len(split['excluded'])} |")
        rows += ["", "根目录同名 JSONL 只含 accepted；review/ 与 excluded/ 中的同名文件保存对应候选或排除记录，不能一起加载进正式问答。空文件表示该状态为0条。", "",
                 "`sources.jsonl` 是来源登记，包含弱证据/不可访问来源以支持追溯；登记记录 accepted 不代表其事实已核验。", "",
                 "## 主文件结构", "",
                 "| 文件 | 主键与内容 | 使用方式 |", "|---|---|---|",
                 "| wineries.jsonl | winery_id；name、company_name、region_id、location_raw、地址、founded_year、company_founded_year、introduction、联系方式 | MySQL酒庄表；按ID到Neo4j关联 |",
                 "| products.jsonl | product_id；winery_id、name、series、年份/年份状态、容量、颜色、甜度、酒精度、品种列表、描述、字段筛选资格 | MySQL产品主数据；price_cny本批保持null |",
                 "| price_observations.jsonl | price_id；product_id、金额上下限、币种、单位、报价类型、报价/观察日期、起订条件、资格 | 参考报价，不能用作当前零售价 |",
                 "| entities.jsonl | entity_id；entity_type、name、region_kind | Region可生成MySQL产区表；其余为Neo4j实体 |",
                 "| aliases.jsonl | alias_id；alias、entity_id、entity_type、alias_kind、ambiguous | 实体名称匹配；一个名称允许多个目标 |",
                 "| knowledge.jsonl | knowledge_id；title、text、category、entity_ids、text_type、fact_scope、retrieval_allowed | 正式RAG文本；后续再生成embedding |",
                 "| relations.jsonl | relation_id；head_id、relation_type、tail_id、proportion_pct | Neo4j边，使用业务ID而非内部节点ID |",
                 "| sources.jsonl | source_id；来源名、URL、输入文件、原访问状态、证据形式、时间、dataset_role | 答案来源与数据追溯 |",
                 "| professional_reviews.jsonl | review_id；product_id、评价类型、摘要、原评分与满分、奖项年、fact_scope | 附加专业/商家描述；不是消费者购买评论 |", "",
                 "完整可机读字段约束见 `schema.json` 的 `$defs`；每个主JSONL按同名定义逐行验证。附加字段允许存在，跨文件引用与业务条件由 `data_cleaning/validate.py` 检查。", "",
                 "## 通用追溯与状态", "",
                 "主记录保留 source_ids、raw_locator、raw_locators、review_status、notes。raw_locator采用 `data_raw/文件#原ID或记录位置`。external_verification=not_performed表示本次没有重新访问网页。",
                 "accepted仅表示本轮本地清洗检查通过；不能解释成网站真实性认证、库存确认或当前价格保证。多来源不能精确归因的字段在audit/field_evidence.jsonl中标为record_level_candidates，不任意指定一个来源。", "",
                 "## 字段值与筛选规则", "",
                 "- color：red/white/rose/other/null；sweetness：dry/semi_dry/semi_sweet/sweet/other/null。Brut Nature保留在sweetness_raw，不据此换算静止酒残糖分类。",
                 "- vintage_status：specified/non_vintage/unspecified/mixed。specified才有确定vintage；NV和未知年份不同。specific_variant表示资料指定年份或NV的记录，不保证已取得完整可购买SKU。",
                 "- volume_ml为单容器毫升；alcohol_pct_vol=14.2表示14.2%vol；未知不填750或常见酒精度。",
                 "- grape_varieties为明确且无冲突的披露列表，各项含entity_id、name、proportion_pct和source_ids；未知配比为null。完整百分比确实合计100时grapes_complete才可置true，不补比例。",
                 "- 每次条件筛选同时检查 strict_product_filter_eligible、field_filter_eligibility[目标字段]和值本身。主记录accepted不代表所有字段可筛选。field_filter_reasons解释缺失、来源归因、特殊配方等原因。",
                 "- 未指定年份的官网页面只在该记录范围内使用，不为所有年份继承参数、评分或风味。",
                 "- price_cny全部为空；价格只在独立观察表中。current_retail_eligible与strict_price_filter_eligible本批均false。参考报价必须带性质、币种、日期/日期不明、单位和起订条件。",
                 "- is_sparkling未知为null；没有“起泡”字样不能反推为false。P043含米酒的特殊配方保留介绍、停用普通葡萄酒筛选。", "",
                 "## 图关系与节点映射", "",
                 "来源S...→Document；酒庄W...→Winery；产品P...→Product；entities按entity_type建节点；知识K.../KP_.../KR_...→Chunk。关系包含PRODUCES、LOCATED_IN、ORIGINATES_FROM、MADE_FROM、HAS_FLAVOR、HAS_CHUNK、ABOUT；PART_OF仅在有明确依据时建立，本批不猜测区域层级。",
                 "HAS_FLAVOR来自该具体酒款/年份的来源描述，未自动传播到同品牌或其他年份。风味字面标签不代表量化强度。HAS_CHUNK为来源追溯边，faithful_summary仍表示采集摘要。", "",
                 "## 辅助评论", "",
                 f"原始评论输入共{self.visitor_input_count}行，确认重复合并后{len(self.visitor_reviews)}条；计数包含多个导出载体，不能当作独立消费者总数。",
                 "`auxiliary/visitor_reviews.jsonl` 保存规范化、规则脱敏的待审评论，`auxiliary/review_sources.jsonl` 保存其本地来源；低信息量记录在 `excluded/visitor_reviews.jsonl`。评论未进入正式knowledge或产品风味图。",
                 "字段包括review_id、text、平台、酒庄原始标签/可确定的winery_id、日期及精度、source_ids、source_urls、raw_locators、original_records和content_hash。product_id固定null，approved_usage为空。姓名、用户等级、IP位置等显式用户字段不导出；正文做手机号/邮箱/证件号/IP规则脱敏，未声称完成所有个人信息识别。",
                 "仅同平台外部ID且正文一致、同采集版本ID且正文一致、兼容导出唯一对应副本被合并。无法确认身份的相同正文标为duplicate_candidate并保留。", "",
                 "## 审计与报告", "",
                 "| 文件 | 内容 |", "|---|---|",
                 "| audit/quality_issues.jsonl | 继承及新增问题、字段、来源、处理与严重程度 |",
                 "| audit/field_evidence.jsonl | 原值/规范值、字段来源粒度与转换决策 |",
                 "| audit/id_mapping.jsonl | 原始ID与规范ID映射；保留原ID也登记 |",
                 "| audit/duplicates.jsonl | 确认重复的原位置、规范ID和合并理由 |",
                 "| audit/duplicate_candidates.jsonl | 相同正文但身份不能确认的候选 |",
                 "| audit/derived_records.jsonl | 旧RAG派生记录不重导入、按规范主数据重建说明 |",
                 "| audit/input_checks.jsonl | JSON/CSV/SQLite版本核对与评论输入数量 |",
                 "| audit/review_decisions.jsonl | 评论保留/排除及合并来源行数 |",
                 "| quality_report.json | 实际记录数、字段缺失/可用数、价格与证据分布、限制 |",
                 "| manifest.json | 原始文件及输出文件SHA256、版本与读取方式 |",
                 "| validation_report.json | 结构、Schema、外键、筛选条件及原始文件完整性检查结果 |", "",
                 "manifest的输出哈希不包括manifest本身和validation_report，避免自引用；后两者通过重新运行验证脚本检查。", "",
                 "## 读取示例", "", "```python", "import json", "from pathlib import Path", "",
                 "def read_jsonl(path):", "    with Path(path).open(encoding='utf-8') as f:", "        return [json.loads(line) for line in f if line.strip()]", "",
                 "products = read_jsonl('data/products.jsonl')", "matched = [p for p in products", "           if p['strict_product_filter_eligible']",
                 "           and all(p['field_filter_eligibility'].get(k, False)", "                   for k in ['color', 'sweetness', 'volume_ml'])",
                 "           and p['color'] == 'red' and p['sweetness'] == 'dry'", "           and p['volume_ml'] == 750]", "```", "",
                 "脚本示例只筛选数据已支持的字段。当前价格预算查询应返回“当前零售价数据不足”，不能自行把参考报价填入产品price_cny。"]
        return "\n".join(rows)

    def finalize(self) -> None:
        if self.inventory() != self.raw_before:
            raise RuntimeError("运行期间data_raw发生变化，已停止；请在原始输入稳定后重跑")
        partitions = {}
        for name, rows in self.tables.items():
            split = {s: [] for s in ["accepted", "review", "excluded"]}
            id_key = ID_KEYS[name]
            ids = [r[id_key] for r in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"输出ID重复: {name}")
            for row in sorted(rows, key=lambda r: r[id_key]):
                split[row["review_status"]].append(row)
            partitions[name] = split
            for status, records in split.items():
                path = self.out / ("" if status == "accepted" else status) / (name + ".jsonl")
                write_json(path, records, lines=True)
        write_json(self.out / "auxiliary" / "visitor_reviews.jsonl", [r for r in self.visitor_reviews if r["review_status"] == "review"], lines=True)
        write_json(self.out / "excluded" / "visitor_reviews.jsonl", [r for r in self.visitor_reviews if r["review_status"] == "excluded"], lines=True)
        write_json(self.out / "auxiliary" / "review_sources.jsonl", self.visitor_sources, lines=True)
        self.audit["quality_issues"] = sorted(self.issues.values(), key=lambda r: r["issue_id"])
        self.audit["input_checks"] = [{"check": "json_csv_equivalent", **r} for r in self.mirror_checks] + self.audit["input_checks"]
        for name in ["quality_issues", "field_evidence", "id_mapping", "duplicates", "duplicate_candidates", "derived_records", "input_checks", "review_decisions"]:
            write_json(self.out / "audit" / (name + ".jsonl"), self.audit[name], lines=True)
        self.report = self.build_report(partitions)
        write_json(self.out / "quality_report.json", self.report)
        write_json(self.out / "schema.json", self.schema())
        write_markdown(self.out / "README.md", self.data_readme(partitions))
        # 无删除操作：仅原子覆盖此流程管理的同名输出，不触碰额外用户文件。
        outputs = []
        for p in sorted(self.out.rglob("*")):
            if p.is_file() and p.name not in {"manifest.json", "validation_report.json"}:
                outputs.append({"path": p.relative_to(self.out).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256(p)})
        inputs = []
        for r in self.raw_before:
            p = self.raw / r["path"].removeprefix("data_raw/")
            if p.resolve() in self.files_read:
                role = "read_canonical_or_crosscheck"
            elif "来源事实核对" in p.parts:
                role = "collector_fact_register_not_original_snapshot"
            else:
                role = "alternate_export_or_context_not_reimported"
            inputs.append({**r, "handling": role})
        write_json(self.out / "manifest.json", {"schema_version": "1.0.0", "rules_version": self.rules["rules_version"],
            "as_of_date": self.as_of.isoformat(), "raw_root": str(self.raw), "output_root": str(self.out),
            "raw_files_unchanged": True, "input_files": inputs, "output_files": outputs,
            "script_hashes": {p.name: sha256(p) for p in sorted(SCRIPT_DIR.glob("*.py"))} | {"rules.json": sha256(SCRIPT_DIR / "rules.json")}})
        from validate import validate_directory
        validation = validate_directory(self.out, self.raw)
        write_json(self.out / "validation_report.json", validation)
        if not validation["passed"]:
            raise RuntimeError("输出未通过验证：" + json.dumps(validation["errors"][:10], ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "data_raw")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--as-of-date", type=date.fromisoformat, default=datetime.now(TZ_CN).date())
    args = parser.parse_args()
    cleaner = Cleaner(args.raw_dir, args.output_dir, args.as_of_date)
    cleaner.run()
    print(json.dumps(cleaner.report["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
