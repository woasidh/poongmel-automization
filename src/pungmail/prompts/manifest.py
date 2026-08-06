from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json


PROMPT_VERSION = "issue2-real-v4"
PROMPT_ORDER = (
    "common.md",
    "classification.md",
    "categories/order.md",
    "categories/upstream_order.md",
    "categories/sample_document_quote.md",
    "categories/punglim_document_request.md",
    "categories/internal_work.md",
    "categories/overseas_work.md",
    "categories/hold.md",
)
CLASSIFICATION_PROMPT_ORDER = ("common.md", "classification.md")
CATEGORY_PROMPT_PATHS = {
    "발주": "categories/order.md",
    "오더": "categories/upstream_order.md",
    "샘플자료견적": "categories/sample_document_quote.md",
    "풍림자료요청": "categories/punglim_document_request.md",
    "사내업무": "categories/internal_work.md",
    "해외업무": "categories/overseas_work.md",
    "보류": "categories/hold.md",
}


@dataclass(frozen=True)
class PromptFile:
    path: str
    sha256: str
    content: str


@dataclass(frozen=True)
class PromptBundle:
    version: str
    sha256: str
    files: tuple[PromptFile, ...]
    content: str

    def manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "files": [
                {"path": item.path, "sha256": item.sha256} for item in self.files
            ],
        }


@dataclass(frozen=True)
class PromptStage:
    name: str
    bundle: PromptBundle
    category: str | None = None

    def manifest(self) -> dict[str, object]:
        return {
            "stage": self.name,
            "category": self.category,
            "sha256": self.bundle.sha256,
            "files": [
                {"path": item.path, "sha256": item.sha256}
                for item in self.bundle.files
            ],
        }


@dataclass(frozen=True)
class PromptTrace:
    version: str
    sha256: str
    stages: tuple[PromptStage, ...]

    def manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "stages": [stage.manifest() for stage in self.stages],
        }


def _build_prompt_bundle(prompt_order: tuple[str, ...]) -> PromptBundle:
    root = files("pungmail.prompts")
    prompt_files: list[PromptFile] = []
    sections: list[str] = []
    for relative_path in prompt_order:
        content = root.joinpath(*relative_path.split("/")).read_text(encoding="utf-8")
        digest = sha256(content.encode("utf-8")).hexdigest()
        prompt_files.append(PromptFile(relative_path, digest, content))
        sections.append(f"<!-- {relative_path} -->\n{content.strip()}")
    combined = "\n\n".join(sections) + "\n"
    return PromptBundle(
        version=PROMPT_VERSION,
        sha256=sha256(combined.encode("utf-8")).hexdigest(),
        files=tuple(prompt_files),
        content=combined,
    )


def build_prompt_bundle() -> PromptBundle:
    """Return the complete prompt catalog for the prompt browser UI."""
    return _build_prompt_bundle(PROMPT_ORDER)


def build_classification_prompt_bundle() -> PromptBundle:
    return _build_prompt_bundle(CLASSIFICATION_PROMPT_ORDER)


def build_category_prompt_bundle(category: object) -> PromptBundle:
    category_value = str(getattr(category, "value", category))
    try:
        category_path = CATEGORY_PROMPT_PATHS[category_value]
    except KeyError as exc:
        raise ValueError(f"unsupported category prompt: {category_value}") from exc
    return _build_prompt_bundle(("common.md", category_path))


def build_prompt_trace(*stages: PromptStage) -> PromptTrace:
    stage_manifest = [stage.manifest() for stage in stages]
    encoded = json.dumps(
        stage_manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return PromptTrace(
        version=PROMPT_VERSION,
        sha256=sha256(encoded).hexdigest(),
        stages=tuple(stages),
    )
