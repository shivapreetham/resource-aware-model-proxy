"""A tiny fake OpenAI-compatible server used by the mock backend.

Lets the whole daemon be demoed and integration-tested without downloading
models. Identifies itself (its tier name) in every reply so you can SEE
which tier answered. ``--ballast-mb`` allocates real memory so the process
footprint resembles a model of that size in demos.

Run directly:  python -m ramp.mock_llm --port 9000 --name small --ballast-mb 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

_ballast = bytearray(0)  # kept alive for the process lifetime


def build_app(name: str) -> FastAPI:
    app = FastAPI(title=f"mock-llm:{name}")

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": name}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "ramp-mock"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        last_user = ""
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                content = m.get("content", "")
                last_user = content if isinstance(content, str) else str(content)
                break
        text = f"[{name}] you said: {last_user}"
        created = int(time.time())

        if body.get("stream"):
            async def gen():
                base = {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": name,
                }
                first = {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
                yield f"data: {json.dumps(first)}\n\n"
                for word in text.split(" "):
                    chunk = {**base, "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    await asyncio.sleep(0.02)
                last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(last)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": created,
            "model": name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    return app


def main() -> None:
    global _ballast
    p = argparse.ArgumentParser(prog="ramp.mock_llm")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--name", default="mock")
    p.add_argument("--ballast-mb", type=int, default=0)
    args = p.parse_args()

    if args.ballast_mb > 0:
        _ballast = bytearray(args.ballast_mb * 1024 * 1024)
        # Touch pages so the memory is actually committed.
        for i in range(0, len(_ballast), 4096):
            _ballast[i] = 1

    uvicorn.run(build_app(args.name), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
