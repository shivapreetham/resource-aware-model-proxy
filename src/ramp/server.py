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
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

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

    # Browser-based clients (Open WebUI, LibreChat, custom web front-ends)
    # can't call RAMP at all without CORS. Permissive by default because the
    # daemon binds to localhost; narrow it with `cors_origins` if you expose
    # it on a network.
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-ramp-tier"],
        )

    # -- control API -----------------------------------------------------

    @app.get("/ramp/status")
    async def status():
        return controller.status()

    @app.post("/ramp/shutdown")
    async def shutdown():
        """Ask the daemon to exit cleanly.

        A clean exit is not a nicety here: transparent mode has moved
        someone's model server to another port, and only an orderly shutdown
        runs the code that puts it back. `ramp stop` calls this and falls
        back to killing the process, which is why the arrangement is also
        recorded on disk.
        """
        server = getattr(app.state, "server", None)
        if server is None:
            return JSONResponse(
                {"error": "this process was not started in a way that can "
                          "shut itself down; stop it with Ctrl+C"},
                status_code=501,
            )
        server.should_exit = True
        return {"stopping": True}

    @app.get("/ramp/metrics")
    async def metrics():
        return PlainTextResponse(
            controller.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

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

    # -- compatibility surface -------------------------------------------

    @app.get("/health")
    async def health():
        """Liveness for orchestrators, Docker healthchecks, and clients that
        probe before connecting. 200 whenever RAMP can serve."""
        ready = controller.ready.is_set()
        return JSONResponse(
            {"status": "ok" if ready else "loading", "tier": controller.tier_name},
            status_code=200 if ready else 503,
        )

    @app.get("/v1/models")
    async def list_models():
        """A stable model list, independent of what's loaded right now.

        Clients populate dropdowns from this and cache it, so returning the
        physical backend model would make the menu change under the user
        every time RAMP swaps. Instead we advertise the virtual name "auto"
        plus the configured tiers, which never change while running.
        """
        created = int(time.time())
        data = [
            {"id": "auto", "object": "model", "created": created, "owned_by": "ramp"}
        ]
        data += [
            {
                "id": t.name,
                "object": "model",
                "created": created,
                "owned_by": "ramp",
            }
            for t in cfg.tiers
        ]
        return {"object": "list", "data": data}

    # -- OpenAI passthrough ---------------------------------------------

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(path: str, request: Request):
        return await _forward(request, f"/v1/{path}")

    # Ollama's native API, for tools written against Ollama rather than the
    # OpenAI shape. Only meaningful with the ollama backend; harmless (404s
    # from upstream) otherwise.
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_native(path: str, request: Request):
        return await _forward(request, f"/api/{path}")

    async def _forward(request: Request, upstream_path: str):
        gate_t0 = time.monotonic()
        try:
            await asyncio.wait_for(controller.ready.wait(), cfg.queue_timeout_s)
        except asyncio.TimeoutError:
            controller.metrics.requests_rejected += 1
            return JSONResponse(
                {
                    "error": {
                        "message": "no model loaded (resources too low or swap in progress)",
                        "type": "ramp_unavailable",
                    }
                },
                status_code=503,
            )
        controller.metrics.request_started(time.monotonic() - gate_t0)

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
            f"{controller.backend.base_url}{upstream_path}",
            content=body_bytes,
            headers=headers,
            params=dict(request.query_params),
        )

        controller.inflight += 1
        try:
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            controller.inflight -= 1
            controller.metrics.requests_failed += 1
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
