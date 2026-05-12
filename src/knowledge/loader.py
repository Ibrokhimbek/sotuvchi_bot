from __future__ import annotations

import json
from pathlib import Path


def load_knowledge_base(directory: Path) -> str:
    """Linko-POS JSON fayllarini bitta katta matnga aylantirib qaytaradi.

    Gemini system_instruction ichiga to'g'ridan-to'g'ri joylashtiriladi.
    """
    chunks: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        chunks.append(_format_file(path.stem, data))
    return "\n\n".join(chunks)


def _format_file(name: str, data: object) -> str:
    header = f"### {name.replace('_', ' ')}"
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return f"{header}\n{body}"
