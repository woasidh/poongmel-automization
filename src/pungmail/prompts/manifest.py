from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files


PROMPT_VERSION = "issue2-real-v2"
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


def build_prompt_bundle() -> PromptBundle:
    root = files("pungmail.prompts")
    prompt_files: list[PromptFile] = []
    sections: list[str] = []
    for relative_path in PROMPT_ORDER:
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
