"""
run.py — Terminal operator console entry point

Usage:
    python run.py                          # LLM via OpenRouter (default)
    AGENT_TYPE=rule-based python run.py   # Deterministic rule-based agent (no API)
    LLM_MODEL=qwen/qwen3-coder python run.py  # Hot-swap the LLM model
"""
from harness.engine import main

if __name__ == "__main__":
    main()
