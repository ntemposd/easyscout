# utils/prompts.py
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def load_text_prompt(relative_path: str) -> str:
    """
    Load a prompt file relative to the project root.
    
    For production (Render), checks for secret files at /etc/secrets/<filename>.
    Falls back to git-tracked file for local development.
    
    Example: load_text_prompt("prompts/scout_instructions.txt")
    
    On Render, upload files via Render's Secret Files feature:
    - scout_instructions.txt
    - stats_refresh.txt
    (They will be mounted at /etc/secrets/<filename>)
    """
    # Extract just the filename from the relative path
    filename = Path(relative_path).name
    
    # Check if running on Render with a secret file path
    secret_path = Path("/etc/secrets") / filename
    if secret_path.exists():
        logger.info(f"[PROMPTS] Loading {filename} from /etc/secrets (Render production)")
        return secret_path.read_text(encoding="utf-8").strip()
    
    # Fallback to git-untracked file (for local development)
    logger.info(f"[PROMPTS] Loading {filename} from local file system")
    root = Path(__file__).resolve().parents[1]  # project root
    path = root / relative_path
    try:
        content = path.read_text(encoding="utf-8").strip()
        logger.info(f"[PROMPTS] Successfully loaded {filename} ({len(content)} chars)")
        return content
    except FileNotFoundError:
        logger.error(f"[PROMPTS] ERROR: {filename} not found at {path}. This will cause LLM calls to fail or return empty responses.")
        raise
