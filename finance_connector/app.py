from __future__ import annotations

import base64
import hmac
import os
from typing import Any

import aiohttp
from aiohttp import web


T212_BASE = "https://live.trading212.com/api/v0"
T212_READ_PATHS = {
    "/equity/account/summary",
    "/equity/positions",
    "/equity/orders",
    "/equity/history/orders",
    "/equity/history/dividends",
    "/equity/history/transactions",
    "/equity/history/exports",
    "/equity/metadata/instruments",
    "/equity/metadata/exchanges",
}

# UK Open Banking Account Information endpoints. Payment-initiation paths are
# intentionally absent. The upstream URL can be a licensed aggregator that
# exposes the standard OBIE resource paths for both Monzo and Amex.
OB_READ_PREFIXES = (
    "/accounts",
    "/balances",
    "/transactions",
    "/beneficiaries",
    "/direct-debits",
    "/standing-orders",
    "/scheduled-payments",
    "/statements",
    "/offers",
    "/party",
    "/parties",
    "/products",
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _authorised(request: web.Request) -> bool:
    expected = request.app["connector_secret"]
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@web.middleware
async def security_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    if request.method != "GET":
        raise web.HTTPMethodNotAllowed(request.method, ["GET"])
    if not _authorised(request):
        raise web.HTTPUnauthorized(text="unauthorised")
    response = await handler(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
    return response


async def _get_json(
    request: web.Request, url: str, headers: dict[str, str]
) -> web.Response:
    timeout = aiohttp.ClientTimeout(total=20)
    session = request.app.get("session")
    if session is None:
        session = request.app["session"] = aiohttp.ClientSession()
    async with session.get(
        url, params=request.query, headers=headers, timeout=timeout
    ) as upstream:
        body = await upstream.read()
        # Do not log bodies: they contain financial data.
        return web.Response(
            status=upstream.status,
            body=body,
            content_type=upstream.content_type or "application/json",
        )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def providers(request: web.Request) -> web.Response:
    return web.json_response({
        "trading212": bool(os.getenv("TRADING212_API_KEY") and os.getenv("TRADING212_API_SECRET")),
        "monzo": bool(os.getenv("MONZO_OB_ACCESS_TOKEN")),
        "amex": bool(os.getenv("AMEX_OB_ACCESS_TOKEN")),
    })


async def trading212(request: web.Request) -> web.Response:
    tail = "/" + request.match_info["tail"].strip("/")
    if tail not in T212_READ_PATHS:
        raise web.HTTPNotFound(text="read endpoint not allowlisted")
    key, secret = _required("TRADING212_API_KEY"), _required("TRADING212_API_SECRET")
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return await _get_json(
        request, T212_BASE + tail, {"Authorization": f"Basic {auth}"}
    )


def _valid_ob_path(tail: str) -> bool:
    path = "/" + tail.strip("/")
    return any(path == prefix or path.startswith(prefix + "/") for prefix in OB_READ_PREFIXES)


async def open_banking(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    if provider not in {"monzo", "amex"}:
        raise web.HTTPNotFound()
    tail = request.match_info["tail"]
    if not _valid_ob_path(tail):
        raise web.HTTPNotFound(text="account-information endpoint not allowlisted")
    prefix = provider.upper()
    base = _required(f"{prefix}_OB_BASE_URL").rstrip("/")
    token = _required(f"{prefix}_OB_ACCESS_TOKEN")
    return await _get_json(
        request,
        f"{base}/{tail.strip('/')}",
        {"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )


async def close_session(app: web.Application) -> None:
    session = app.get("session")
    if session is not None:
        await session.close()


def build_app(*, connector_secret: str | None = None) -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=1024)
    app["connector_secret"] = connector_secret or _required("FINANCE_CONNECTOR_SECRET")
    app["session"] = None
    app.router.add_get("/health", health)
    app.router.add_get("/v1/providers", providers)
    app.router.add_get("/v1/trading212/{tail:.*}", trading212)
    app.router.add_get("/v1/open-banking/{provider}/{tail:.*}", open_banking)
    app.on_cleanup.append(close_session)
    return app


def main() -> None:
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
