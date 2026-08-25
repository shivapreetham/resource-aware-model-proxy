"""RAMP - RAM-Aware Model Proxy.

An elastic local LLM daemon: exposes one OpenAI-compatible endpoint,
continuously watches available system memory, and transparently moves the
loaded model up and down a quality ladder (model size / quantization /
context length) so the assistant degrades gracefully under memory pressure
and recovers when memory frees up.
"""

__version__ = "0.1.0"
