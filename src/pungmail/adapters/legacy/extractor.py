from __future__ import annotations

from dataclasses import dataclass, field
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import mimetypes
from pathlib import Path
import re
import sys
from types import ModuleType

from PIL import Image
import pytesseract

from pungmail.config import Settings, get_settings


@dataclass(frozen=True)
class ExtractionResult:
    text: str = ""
    ocr_text: str = ""
    status: str = "NO_TEXT"
    warnings: tuple[str, ...] = field(default_factory=tuple)


def extract_pdf_tables(data: bytes) -> str:
    """PDF 표를 행·열 경계가 보존된 텍스트로 변환한다."""
    import pdfplumber

    sections: list[str] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for table_number, table in enumerate(page.extract_tables(), start=1):
                rows: list[str] = []
                for raw_row in table:
                    cells = [
                        re.sub(r"\s+", " ", str(cell or "")).strip()
                        for cell in raw_row
                    ]
                    if sum(bool(cell) for cell in cells) < 2:
                        continue
                    rows.append("\t".join(cells))
                if rows:
                    sections.append(
                        f"[표 구조: {page_number}페이지 {table_number}번 표]\n"
                        + "\n".join(rows)
                    )
    return "\n\n".join(sections)


class LegacyEvidenceExtractor:
    """운영 프로젝트의 검증된 문서 추출 함수를 읽기 전용으로 재사용한다."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.module = self._load_legacy_module(self.settings.legacy_project_path)
        self.module.OCR_LANGUAGES = self.settings.ocr_languages
        self.module.TESSERACT_CANDIDATES = [
            str(self.settings.tesseract_cmd),
            *list(self.module.TESSERACT_CANDIDATES),
        ]
        self.module.MAX_ARCHIVE_ENTRIES = self.settings.max_archive_entries
        self.module.MAX_ARCHIVE_ENTRY_BYTES = self.settings.max_archive_entry_bytes
        self.module.MAX_ARCHIVE_TOTAL_BYTES = self.settings.max_archive_total_bytes

    @staticmethod
    def _load_legacy_module(project_path: Path) -> ModuleType:
        module_path = project_path / "gmail_monitor" / "gmail_client.py"
        if not module_path.exists():
            raise FileNotFoundError(f"Legacy Gmail extractor not found: {module_path}")
        module_name = "pungmail_legacy_gmail_client"
        existing = sys.modules.get(module_name)
        if existing is not None:
            return existing
        spec = spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load legacy extractor: {module_path}")
        module = module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def extract(self, filename: str, mime_type: str, data: bytes) -> ExtractionResult:
        lowered = filename.casefold()
        mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            if mime.startswith("image/") or lowered.endswith(
                (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
            ):
                ocr = self._ocr_image(data)
                return ExtractionResult(
                    ocr_text=ocr,
                    status="OCR_READ" if ocr else "NO_TEXT",
                )
            if lowered.endswith(".pdf") or mime == "application/pdf":
                return self._extract_pdf(filename, data)
            text = str(self.module._extract_attachment_text(filename, mime, data) or "").strip()
            return ExtractionResult(text=text, status="READ" if text else "NO_TEXT")
        except Exception as exc:
            return ExtractionResult(
                status="READ_FAILED",
                warnings=(f"{type(exc).__name__}: {exc}",),
            )

    def _extract_pdf(self, filename: str, data: bytes) -> ExtractionResult:
        text = ""
        warnings: list[str] = []
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            text = self.module._normalize_whitespace(
                "\n\n".join((page.extract_text() or "") for page in reader.pages)
            )
        except Exception as exc:
            warnings.append(f"PDF text: {type(exc).__name__}: {exc}")

        try:
            table_text = extract_pdf_tables(data)
            if table_text:
                text = f"{text}\n\n{table_text}".strip()
        except Exception as exc:
            warnings.append(f"PDF tables: {type(exc).__name__}: {exc}")

        needs_ocr = len(text.strip()) < 80 or bool(
            self.module._pdf_order_text_needs_ocr(filename, text)
        )
        ocr_text = ""
        if needs_ocr:
            try:
                ocr_text = str(self.module._extract_pdf_ocr_text(data) or "").strip()
            except Exception as exc:
                warnings.append(f"PDF OCR: {type(exc).__name__}: {exc}")
        status = "READ"
        if ocr_text:
            status = "OCR_READ"
        elif not text:
            status = "READ_FAILED" if warnings else "NO_TEXT"
        return ExtractionResult(
            text=text,
            ocr_text=ocr_text,
            status=status,
            warnings=tuple(warnings),
        )

    def _ocr_image(self, data: bytes) -> str:
        if not self.settings.tesseract_cmd.exists():
            return ""
        pytesseract.pytesseract.tesseract_cmd = str(self.settings.tesseract_cmd)
        image = Image.open(BytesIO(data))
        image = self.module._orient_ocr_image(image, pytesseract, "")
        return str(
            pytesseract.image_to_string(image, lang=self.settings.ocr_languages)
        ).strip()
