"""Cost estimation and model pricing for LLM token usage.

Provides pricing information for various AI models and calculates
estimated costs based on token consumption.
"""

import logging

logger = logging.getLogger(__name__)


def estimate_cost(usage: dict, prices: dict) -> float:
    """Calculate estimated cost based on token usage and pricing.
    
    Args:
        usage: Dict with 'input_tokens' and 'output_tokens' keys
        prices: Dict with 'input' and 'output' keys (price per 1M tokens)
    
    Returns:
        Estimated cost in dollars
    """
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    
    return (
        input_tokens / 1_000_000 * prices["input"]
        + output_tokens / 1_000_000 * prices["output"]
    )


# Default pricing for common models ($ per 1M tokens)
# Source: https://openai.com/api/pricing/ (as of January 2026)
MODEL_PRICES = {
    # GPT-5 Series (Flagship models)
    "gpt-5.2": {"input": 1.75, "output": 14.00},
    "gpt-5.2-pro": {"input": 21.00, "output": 168.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    
    # GPT-4.1 Series
    "gpt-4.1": {"input": 3.00, "output": 12.00},
    "gpt-4.1-mini": {"input": 0.80, "output": 3.20},
    "gpt-4.1-nano": {"input": 0.20, "output": 0.80},
    
    # Legacy GPT-4 Series (deprecated, using estimated pricing)
    "gpt-4": {"input": 30.0, "output": 60.0},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    
    # GPT-3.5 Series (legacy)
    "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},
    
    # Default fallback
    "default": {"input": 1.75, "output": 14.00},  # GPT-5.2 pricing
}


def get_model_prices(model: str) -> dict:
    """Get pricing for a specific model.
    
    Args:
        model: Model name
        
    Returns:
        Dict with 'input' and 'output' pricing per 1M tokens
    """
    # Check exact match first
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    
    # Check if model name contains known model as substring
    for model_key in MODEL_PRICES:
        if model_key in model.lower():
            return MODEL_PRICES[model_key]
    
    # Return default pricing
    return MODEL_PRICES["default"]
