"""正式数据快照与业务实体目录。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Dataset:
    data_dir: Path
    tables: dict[str, list[dict]]
    node_types: dict[str, str]
    snapshot: str
    hashes: dict[str, str]

    @property
    def catalog(self) -> list[dict]:
        rows = []
        for table, id_key, kind in [("wineries", "winery_id", "Winery"), ("products", "product_id", "Product"), ("entities", "entity_id", None)]:
            for record in self.tables[table]:
                rows.append({"entity_id": record[id_key], "entity_type": kind or record["entity_type"],
                             "name": record["name"], "source_ids": record["source_ids"], "raw_record": record})
        return sorted(rows, key=lambda r: r["entity_id"])
