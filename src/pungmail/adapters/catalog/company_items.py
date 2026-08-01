from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import unicodedata

from pungmail.config import Settings, get_settings


def normalize_catalog_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


@dataclass(frozen=True)
class CatalogCandidate:
    item_code: str
    company_display_name: str
    raw_name: str
    lookup_name: str
    spec: str | None
    product_group: str
    company_from_product_group: str
    status: str
    source_row: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class CompanyCatalog:
    def __init__(self, settings: Settings | None = None, *, path: Path | None = None):
        active = settings or get_settings()
        self.path = path or active.company_catalog_path
        self._records: tuple[CatalogCandidate, ...] | None = None

    def _load(self) -> tuple[CatalogCandidate, ...]:
        if self._records is not None:
            return self._records
        records: list[CatalogCandidate] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                records.append(
                    CatalogCandidate(
                        item_code=str(raw.get("itemCode") or ""),
                        company_display_name=str(raw.get("companyDisplayName") or ""),
                        raw_name=str(raw.get("rawName") or ""),
                        lookup_name=str(raw.get("lookupName") or ""),
                        spec=raw.get("spec"),
                        product_group=str(raw.get("productGroup") or ""),
                        company_from_product_group=str(
                            raw.get("companyFromProductGroup") or ""
                        ),
                        status=str(raw.get("status") or ""),
                        source_row=int(raw.get("sourceRow") or 0),
                    )
                )
        self._records = tuple(records)
        return self._records

    def lookup(
        self,
        query: str,
        *,
        include_deleted: bool = False,
    ) -> list[CatalogCandidate]:
        key = normalize_catalog_key(query)
        if not key:
            return []
        active = [
            record
            for record in self._load()
            if include_deleted or record.status == "active"
        ]
        code_matches = [
            record for record in active if normalize_catalog_key(record.item_code) == key
        ]
        if code_matches:
            return code_matches
        return [
            record
            for record in active
            if key
            in {
                normalize_catalog_key(record.raw_name),
                normalize_catalog_key(record.lookup_name),
                normalize_catalog_key(record.company_display_name),
            }
        ]

    def candidates_in_text(
        self,
        text: str,
        *,
        limit: int = 50,
    ) -> list[CatalogCandidate]:
        haystack = normalize_catalog_key(text)
        found: dict[tuple[str, str | None], CatalogCandidate] = {}
        searchable: list[tuple[str, CatalogCandidate]] = []
        for record in self._load():
            if record.status != "active":
                continue
            names = {
                normalize_catalog_key(record.raw_name),
                normalize_catalog_key(record.lookup_name),
                normalize_catalog_key(record.company_display_name),
            }
            for name in names:
                if len(name) >= 3:
                    searchable.append((name, record))
        for name, record in sorted(searchable, key=lambda item: len(item[0]), reverse=True):
            if name in haystack:
                found[(record.item_code, record.spec)] = record
                if len(found) >= limit:
                    break
        return list(found.values())
