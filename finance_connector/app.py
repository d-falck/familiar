from __future__ import annotations

import base64
import asyncio
import datetime as dt
import hmac
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET

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
    "/pots",
)

_SAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_EMMA_FIELDS = (
    "ID", "Date", "Amount", "Account", "Bank", "Currency", "Category",
    "Subcategory", "Type", "Tags", "Counterparty", "Custom Name",
    "Merchant", "Additional details", "Notes", "Linked transaction ID",
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
        "emma_export": bool(os.getenv("EMMA_EXPORT_XLSX_PATH")),
    })


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    value = cell.find(f"{{{_XLSX_NS}}}v")
    raw = "" if value is None or value.text is None else value.text
    if kind == "s" and raw:
        return shared[int(raw)]
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{{{_XLSX_NS}}}t"))
    return raw


def _read_emma_export(path: str) -> list[dict[str, str]]:
    """Read Emma's XLSX export without retaining or logging its contents."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in item.iter(f"{{{_XLSX_NS}}}t"))
                for item in root
            ]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
        }
        candidates: list[tuple[str, str]] = []
        for item in workbook.findall(f".//{{{_XLSX_NS}}}sheet"):
            rel_id = item.attrib[f"{{{_XLSX_REL_NS}}}id"]
            target = targets[rel_id].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            candidates.append((item.attrib["name"], target))

        # Emma Live Export currently uses a worksheet named "Primary" and may
        # place a welcome/instructions tab first. Prefer Primary, but retain a
        # header-based fallback so exports remain readable if Emma renames it.
        candidates.sort(key=lambda item: item[0].casefold() != "primary")
        sheet_rows: list[dict[str, str]] | None = None
        for _, target in candidates:
            sheet = ET.fromstring(archive.read(target))
            candidate_rows: list[dict[str, str]] = []
            for row in sheet.findall(f".//{{{_XLSX_NS}}}row"):
                values: dict[str, str] = {}
                for cell in row.findall(f"{{{_XLSX_NS}}}c"):
                    column = "".join(ch for ch in cell.attrib["r"] if ch.isalpha())
                    values[column] = _xlsx_cell_value(cell, shared)
                candidate_rows.append(values)
            if candidate_rows and set(_EMMA_FIELDS).issubset(candidate_rows[0].values()):
                sheet_rows = candidate_rows
                break
        if sheet_rows is None:
            raise ValueError("Emma export has no worksheet with transaction headers")
        rows = sheet_rows
    if not rows:
        return []
    columns = {value: column for column, value in rows[0].items()}
    if not set(_EMMA_FIELDS).issubset(columns):
        missing = sorted(set(_EMMA_FIELDS) - set(columns))
        raise ValueError(f"Emma export is missing columns: {', '.join(missing)}")
    return [
        {field: row.get(columns[field], "") for field in _EMMA_FIELDS}
        for row in rows[1:]
    ]


def _parse_iso_date(value: str, name: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"invalid {name}; expected YYYY-MM-DD") from exc


async def emma_transactions(request: web.Request) -> web.Response:
    path = _required("EMMA_EXPORT_XLSX_PATH")
    start = _parse_iso_date(request.query.get("from", ""), "from")
    end = _parse_iso_date(request.query.get("to", ""), "to")
    if start and end and start > end:
        raise web.HTTPBadRequest(text="from must not be after to")
    bank = request.query.get("bank", "").casefold()
    account = request.query.get("account", "").casefold()
    rows = await asyncio.get_running_loop().run_in_executor(
        None, _read_emma_export, path
    )
    result = []
    for row in rows:
        row_date = dt.date.fromisoformat(row["Date"])
        if start and row_date < start or end and row_date > end:
            continue
        if bank and row["Bank"].casefold() != bank:
            continue
        if account and row["Account"].casefold() != account:
            continue
        result.append(row)
    return web.Response(
        text=json.dumps({"count": len(result), "transactions": result}),
        content_type="application/json",
    )


async def trading212(request: web.Request) -> web.Response:
    tail = "/" + request.match_info["tail"].strip("/")
    if tail not in T212_READ_PATHS:
        raise web.HTTPNotFound(text="read endpoint not allowlisted")
    key, secret = _required("TRADING212_API_KEY"), _required("TRADING212_API_SECRET")
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return await _get_json(
        request, T212_BASE + tail, {"Authorization": f"Basic {auth}"}
    )


def _canonical_read_path(tail: str) -> str | None:
    """Return a safe canonical upstream path, or None for ambiguous input."""
    segments = tail.strip("/").split("/")
    if not segments or any(
        not segment
        or segment in {".", ".."}
        or not _SAFE_PATH_SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        return None
    return "/" + "/".join(segments)


def _valid_ob_path(tail: str) -> bool:
    path = _canonical_read_path(tail)
    if path is None:
        return False
    return any(path == prefix or path.startswith(prefix + "/") for prefix in OB_READ_PREFIXES)


async def open_banking(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    if provider not in {"monzo", "amex"}:
        raise web.HTTPNotFound()
    tail = request.match_info["tail"]
    path = _canonical_read_path(tail)
    if path is None or not _valid_ob_path(tail):
        raise web.HTTPNotFound(text="account-information endpoint not allowlisted")
    prefix = provider.upper()
    base = _required(f"{prefix}_OB_BASE_URL").rstrip("/")
    token = _required(f"{prefix}_OB_ACCESS_TOKEN")
    return await _get_json(
        request,
        f"{base}{path}",
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
    app.router.add_get("/v1/emma/transactions", emma_transactions)
    app.router.add_get("/v1/trading212/{tail:.*}", trading212)
    app.router.add_get("/v1/open-banking/{provider}/{tail:.*}", open_banking)
    app.on_cleanup.append(close_session)
    return app


def main() -> None:
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
