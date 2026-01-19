# utils/prompts.py
from __future__ import annotations

from pathlib import Path


def load_text_prompt(relative_path: str) -> str:
    """
    Load a prompt file relative to the project root.
    Example: load_text_prompt("prompts/scout_instructions.txt")
    """
    root = Path(__file__).resolve().parents[1]  # project root
    path = root / relative_path
    return path.read_text(encoding="utf-8").strip()
