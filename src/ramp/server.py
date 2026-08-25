"""The FastAPI app: OpenAI-compatible passthrough proxy + RAMP control API.

Everything under ``/v1/*`` is transparently forwarded (including SSE
streaming) to whichever backend tier is currently loaded. Every response
carries an ``x-ramp-tier`` header so clients can tell what answered them.
While a tier swap is in progress, requests wait (up to ``queue_timeout_s``)
instead of failing.

Control API:
    GET    /ramp/status      full daemon state (tier, memory, events)
    POST   /ramp/pin/{name}  force a tier and disable the policy
    DELETE /ramp/pin         re-enable automatic calibration
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .controller import Controller

_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-length",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
}

_FWD_REQ_HEADERS = {"content-type", "accept", "authorization"}


def create_app(controller: Controller, cfg: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await controller.start()
        yield
        await controller.close()
        await app.state.client.aclose()

    app = FastAPI(title="RAMP", version="0.1.0", lifespan=lifespan)
    # Long read timeout: LLM generations stream for minutes.
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None)
    )

    # -- control API -----------------------------------------------------

    @app.get("/ramp/status")
    async def status():
        return controller.status()

    @app.post("/ramp/pin/{name}")
    async def pin(name: str):
        try:
            await controller.pin(name)
        except KeyError:
            return JSONResponse({"error": f"unknown tier {name!r}"}, status_code=404)
        return {"pinned": name}

    @app.delete("/ramp/pin")
    async def unpin():
        controller.unpin()
        return {"pinned": None}

    # -- OpenAI passthrough ---------------------------------------------

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        try:
            await asyncio.wait_for(controller.ready.wait(), cfg.queue_timeout_s)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": {"message": "no model loaded (memory too low or swap in progress)", "type": "ramp_unavailable"}},
                status_code=503,
            )

        client: httpx.AsyncClient = request.app.state.client
        headers = {
            k: v for k, v in request.headers.items() if k.lower() in _FWD_REQ_HEADERS
        }
        body_bytes = await request.body()
        # Backends that route by model name (Ollama) need the client's
        # "model" field replaced with the active tier's model.
        serving_model = getattr(controller.backend, "serving_model", None)
        if serving_model and request.method == "POST" and body_bytes:
            try:
                parsed = json.loads(body_bytes)
                if isinstance(parsed, dict) and "model" in parsed:
                    parsed["model"] = serving_model
                    body_bytes = json.dumps(parsed).encode("utf-8")
                    headers.pop("content-length", None)
            except ValueError:
                pass  # not JSON; forward untouched
        upstream_req = client.build_request(
            request.method,
            f"{controller.backend.base_url}/v1/{path}",
            content=body_bytes,
            headers=headers,
        )

        controller.inflight += 1
        try:
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            controller.inflight -= 1
            return JSONResponse(
                {"error": {"message": f"backend unreachable: {e}", "type": "ramp_backend_error"}},
                status_code=502,
            )

        resp_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_HEADERS
        }
        resp_headers["x-ramp-tier"] = controller.tier_name or ""

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                controller.inflight -= 1

        return StreamingResponse(
            body(), status_code=upstream.status_code, headers=resp_headers
        )

    return app
