# utils/prompts.py
from __future__ import annotations

import os
from pathlib import Path


def load_text_prompt(relative_path: str) -> str:
    """
    Load a prompt file relative to the project root.
    
    For production (Render), first checks RENDER_SECRET_FILE_PATH environment variable.
    Falls back to git-tracked file for local development.
    
    Example: load_text_prompt("prompts/scout_instructions.txt")
    
    On Render, set RENDER_SECRET_FILE_PATH=scout_instructions.txt
    and upload the file via Render's Secret Files feature.
    """
    # Check if running on Render with a secret file path
    secret_file = os.getenv("RENDER_SECRET_FILE_PATH")
    if secret_file:
        # Render mounts secret files at /etc/secrets/<filename>
        secret_path = Path("/etc/secrets") / secret_file
        if secret_path.exists():
            return secret_path.read_text(encoding="utf-8").strip()
    
    # Fallback to git-tracked file (for local development)
    root = Path(__file__).resolve().parents[1]  # project root
    path = root / relative_path
    return path.read_text(encoding="utf-8").strip()
