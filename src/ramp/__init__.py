"""RAMP - Resource-Aware Model Proxy.

An elastic local LLM daemon: exposes one OpenAI-compatible endpoint,
continuously watches available system resources (RAM, VRAM, disk), and
transparently moves the loaded model up and down a quality ladder (model
size / quantization / context length) so the assistant degrades gracefully
under pressure and recovers when resources free up.
"""

__version__ = "0.7.0"
