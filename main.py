from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import string
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from appflowysdk import AppFlowy, ViewLayout

from src.models import (
    FlexiblePayload,
    LoginRequest,
    RefreshTokenRequest,
    RowCreateRequest,
    RowUpdateRequest,
)

load_dotenv()

mcp = FastMCP("appflowy-cloud")

if os.getenv("BASE_URL"):
    client = AppFlowy(
        base_url=str(os.getenv("BASE_URL")),
        email=os.getenv("APPFLOWY_EMAIL"),
        password=os.getenv("APPFLOWY_PASSWORD"),
    )
else:
    client = AppFlowy(
        email=os.getenv("APPFLOWY_EMAIL"),
        password=os.getenv("APPFLOWY_PASSWORD"),
    )


def _require_access_token() -> str:
    token = client.token_store.get_access_token()
    if not token:
        raise Exception("Not authenticated. Please login first.")
    return token


def _payload_dict(request: FlexiblePayload | None) -> dict[str, Any]:
    return request.model_dump(exclude_none=True) if request else {}


def _payload_str(request: FlexiblePayload, key: str) -> str:
    payload = _payload_dict(request)
    value = payload.get(key)
    if not value:
        raise Exception(f"Missing required field: {key}")
    return str(value)


def _payload_layout(request: FlexiblePayload | None) -> ViewLayout:
    payload = _payload_dict(request)
    layout = payload.get("layout", ViewLayout.DOCUMENT)
    if isinstance(layout, ViewLayout):
        return layout
    try:
        return ViewLayout(int(layout))
    except (TypeError, ValueError):
        idx = {"document": 0, "grid": 1, "board": 2, "calendar": 3}.get(
            str(layout).lower(), 0
        )
        return ViewLayout(idx)


# ---- Direct REST helpers (bypass appflowysdk's strict/buggy response models) ----

# Tie to the SDK client's base_url so the REST helper and SDK can never target
# different hosts (avoids replaying the live token against the wrong server).
BASE_URL = str(
    getattr(client, "base_url", None) or os.getenv("BASE_URL") or "https://beta.appflowy.cloud"
).rstrip("/")


def _api(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    _retried: bool = False,
) -> Any:
    token = _require_access_token()
    try:
        resp = httpx.request(
            method,
            f"{BASE_URL}{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30.0,
        )
    except httpx.RequestError as e:
        raise Exception(f"Network error calling {method} {path}: {e}") from e
    # Access tokens are short-lived; refresh once and retry on 401.
    if resp.status_code == 401 and not _retried:
        try:
            client.refresh_token()
        except Exception as e:
            raise Exception(
                f"{method} {path} -> 401 and token refresh failed: {e} "
                "(call appflowy_login first)"
            ) from e
        return _api(method, path, params=params, json_body=json_body, _retried=True)
    if resp.status_code >= 400:
        raise Exception(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        raise Exception(f"{method} {path} returned non-JSON: {resp.text[:200]}")


# ---- Raw binary request + realtime full-sync (CT-13 robustness) -------------
# The persisted /collab doc_state snapshot can LAG the realtime in-memory doc for
# a window after a web-update. A read-modify-write that loads that stale base then
# computes deletes against blocks the live doc no longer has at those positions,
# so the new content is ADDED instead of replacing -> the page DUPLICATES on a
# re-update; read-back also drops inline marks (stale external_ids don't match the
# live structure). The realtime full-sync endpoint (the path the web client uses)
# always returns the live merged state, so we load through it instead.


def _post_raw(path: str, body: bytes, _retried: bool = False) -> httpx.Response:
    """POST raw bytes (protobuf) and return the raw response; 401 -> refresh once."""
    token = _require_access_token()
    try:
        resp = httpx.post(
            f"{BASE_URL}{path}",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            timeout=60.0,
        )
    except httpx.RequestError as e:
        raise Exception(f"Network error calling POST {path}: {e}") from e
    if resp.status_code == 401 and not _retried:
        try:
            client.refresh_token()
        except Exception as e:
            raise Exception(
                f"POST {path} -> 401 and token refresh failed: {e} "
                "(call appflowy_login first)"
            ) from e
        return _post_raw(path, body, _retried=True)
    if resp.status_code >= 400:
        raise Exception(f"POST {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp


# NOTE: the appflowysdk ships full_sync_collab()/CollabDocStateParams but they are
# unusable here — the model has only `doc_state` (the real proto has 5 fields) and
# the method POSTs JSON, while the server decodes protobuf. So we hand-encode the
# protobuf body below instead of calling the SDK.
def _pb_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _encode_collab_doc_state_params(
    object_id: str, sv: bytes, doc_state: bytes, collab_type: int = 0
) -> bytes:
    """Encode CollabDocStateParams (proto3) for the full-sync endpoint.

    Fields: 1 object_id(str), 2 collab_type(int32), 3 compression(enum — omitted
    => NONE, so the request body is uncompressed), 4 sv(bytes), 5 doc_state(bytes).
    """
    ob = object_id.encode("utf-8")
    body = bytearray()
    body += b"\x0a" + _pb_varint(len(ob)) + ob                       # 1 object_id
    body += b"\x10" + _pb_varint(collab_type)                        # 2 collab_type
    body += b"\x22" + _pb_varint(len(sv)) + bytes(sv)               # 4 sv
    body += b"\x2a" + _pb_varint(len(doc_state)) + bytes(doc_state)  # 5 doc_state
    return bytes(body)


def _zstd_decompress(data: bytes) -> bytes:
    import io
    import zstandard  # only imported if a deployment compresses the response

    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(io.BytesIO(data)) as r:
        return r.read()


def _full_sync_doc_state(workspace_id: str, view_id: str) -> bytes:
    """Return the CURRENT (realtime) document as a yrs update via full-sync.

    We send an empty-doc state vector so the server returns the entire live doc,
    and an empty-doc update (a no-op merge — the server rejects a truly empty
    doc_state). The response is a raw yrs update on this deployment; if a build
    zstd-compresses it (detected by the frame magic) we decompress it.
    """
    from pycrdt import Doc

    d0 = Doc()
    sv0 = bytes(d0.get_state())
    upd0 = bytes(d0.get_update())
    body = _encode_collab_doc_state_params(view_id, sv0, upd0, 0)
    resp = _post_raw(
        f"/api/workspace/v1/{workspace_id}/collab/{view_id}/full-sync", body
    )
    data = resp.content
    if data[:4] == b"\x28\xb5\x2f\xfd":  # zstd frame magic
        data = _zstd_decompress(data)
    return data


def _render_delta(delta_json):
    if not delta_json:
        return ""
    try:
        ops = json.loads(delta_json)
    except Exception:
        return str(delta_json)
    out = []
    for op in ops if isinstance(ops, list) else []:
        text = op.get("insert", "")
        attrs = op.get("attributes") or {}
        if attrs.get("code"):
            text = f"`{text}`"
        if attrs.get("bold"):
            text = f"**{text}**"
        if attrs.get("italic"):
            text = f"*{text}*"
        if attrs.get("strikethrough"):
            text = f"~~{text}~~"
        if attrs.get("href"):
            text = f"[{text}]({attrs['href']})"
        out.append(text)
    return "".join(out)


def _document_to_markdown(doc):
    """Render an AppFlowy document (blocks + meta.children_map + meta.text_map) to Markdown."""
    blocks = doc.get("blocks", {})
    meta = doc.get("meta", {})
    children_map = meta.get("children_map", {})
    text_map = meta.get("text_map", {})
    list_types = ("bulleted_list", "numbered_list", "todo_list", "toggle_list")
    out = []  # (markdown_line, is_list_item)

    def render(block_id, depth, seen, sib_counter):
        if not block_id or block_id in seen:  # cycle / shared-child guard
            return
        seen = seen | {block_id}
        block = blocks.get(block_id)
        if not block:
            return
        ty = block.get("ty", "paragraph")
        try:
            data = json.loads(block.get("data") or "{}")
        except Exception:
            data = {}
        ext = block.get("external_id")
        text = _render_delta(text_map.get(ext)) if ext else ""
        if text == "$":  # AppFlowy placeholder for non-text blocks
            text = ""
        indent = "  " * depth
        is_list = ty in list_types

        if ty == "simple_table":
            tbl = _render_simple_table(block_id, blocks, children_map, text_map)
            if tbl:
                out.append((tbl, False))
            return  # table fully rendered; do not recurse into rows/cells
        if ty == "heading":
            out.append(("#" * int(data.get("level", 1)) + " " + text, False))
        elif ty == "todo_list":
            out.append((f"{indent}- [{'x' if data.get('checked') else ' '}] {text}", True))
        elif ty == "bulleted_list":
            out.append((f"{indent}- {text}", True))
        elif ty == "numbered_list":
            sib_counter["n"] += 1
            out.append((f"{indent}{sib_counter['n']}. {text}", True))
        elif ty == "toggle_list":
            out.append((f"{indent}- {text}", True))
        elif ty in ("quote", "callout"):
            out.append((f"{indent}> {text}", False))
        elif ty == "divider":
            out.append(("---", False))
        elif ty == "code":
            out.append((f"```{data.get('language', '')}\n{text}\n```", False))
        elif ty == "image":
            url = data.get("url", "")
            if url:
                out.append((f"{indent}![]({url})", False))
        elif text:  # paragraph + unknown types that actually have text
            out.append((f"{indent}{text}" if depth else text, False))

        child_counter = {"n": 0}  # numbering restarts per sibling group
        for cid in children_map.get(block.get("children"), []):
            if blocks.get(cid, {}).get("ty") != "numbered_list":
                child_counter["n"] = 0  # restart numbering after an interruption
            render(cid, depth + 1 if is_list else depth, seen, child_counter)

    root = blocks.get(doc.get("page_id"), {})
    root_counter = {"n": 0}  # shared across top-level siblings (was reset per item)
    for cid in children_map.get(root.get("children"), []):
        if blocks.get(cid, {}).get("ty") != "numbered_list":
            root_counter["n"] = 0  # restart numbering after an interruption
        render(cid, 0, frozenset(), root_counter)

    # Join: keep consecutive list items on adjacent lines; blank line between block groups.
    result = []
    prev_is_list = False
    for txt, is_list in out:
        if result:
            result.append("\n" if (is_list and prev_is_list) else "\n\n")
        result.append(txt)
        prev_is_list = is_list
    return "".join(result)


def _render_simple_table(table_id, blocks, children_map, text_map):
    """Render a simple_table block (rows -> cells -> paragraphs) as a GFM table.

    AppFlowy simple tables have no header concept; we treat the first row as the
    GFM header so the output is valid Markdown and round-trips back to a table.
    """
    tb = blocks.get(table_id, {})
    rows = []
    for row_id in children_map.get(tb.get("children"), []):
        rb = blocks.get(row_id, {})
        cells = []
        for cell_id in children_map.get(rb.get("children"), []):
            cb = blocks.get(cell_id, {})
            parts = []
            for pid in children_map.get(cb.get("children"), []):
                pj = blocks.get(pid, {})
                ext = pj.get("external_id")
                t = _render_delta(text_map.get(ext)) if ext else ""
                if t and t != "$":
                    parts.append(t)
            text = " ".join(parts).replace("|", "\\|").replace("\n", " ").strip()
            cells.append(text)
        rows.append(cells)
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


# ---- Markdown -> AppFlowy nested-block converter (inverse of _document_to_markdown) ----
# The server's JsonToDocumentParser consumes blocks shaped as:
#   {"type": <ty>, "data": {..., "delta": [ops]}, "children": [<block>, ...]}
# It pulls data.delta out into the doc's text_map and generates ids itself, so we
# only emit type/data/children. Inline marks live in delta-op "attributes"
# (bold/italic/code/strikethrough/href) matching _render_delta on the read side.

# Patterns are tried in order; on a tie for earliest start, the first listed
# wins (so "both"/bold beat italic, image beats link).
_INLINE_PATTERNS = [
    ("both", re.compile(r"\*\*\*(.+?)\*\*\*")),  # bold+italic
    ("code", re.compile(r"`([^`]+)`")),
    ("bold", re.compile(r"\*\*(.+?)\*\*")),
    ("bold", re.compile(r"__(.+?)__")),
    ("strikethrough", re.compile(r"~~(.+?)~~")),
    ("image", re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")),
    ("link", re.compile(r"\[([^\]]*)\]\(([^)]+)\)")),
    ("italic", re.compile(r"\*(.+?)\*")),
    ("italic", re.compile(r"(?<!\w)_(.+?)_(?!\w)")),
]


def _op(insert, attrs):
    op = {"insert": insert}
    if attrs:
        op["attributes"] = dict(attrs)
    return op


def _clean_url(u):
    # Drop an optional Markdown title (   "title" / 'title') and trim whitespace.
    return re.sub(r"""\s+["'].*["']\s*$""", "", u.strip()).strip()


def _inline_to_delta(text, attrs=None):
    """Parse inline markdown into a list of delta ops [{insert, attributes?}].

    Iterative over the remaining text (recurses only into a match's *content*,
    so depth tracks nesting, not the number of inline elements on the line).
    """
    attrs = attrs or {}
    ops = []
    while text:
        best = None  # (kind, match)
        for kind, pat in _INLINE_PATTERNS:
            m = pat.search(text)
            if m and (best is None or m.start() < best[1].start()):
                best = (kind, m)
        if best is None:
            ops.append(_op(text, attrs))
            break
        kind, m = best
        if m.start() > 0:
            ops.append(_op(text[: m.start()], attrs))
        if kind == "code":
            ops.append(_op(m.group(1), dict(attrs, code=True)))  # literal, no recurse
        elif kind == "both":
            ops.extend(_inline_to_delta(m.group(1), dict(attrs, bold=True, italic=True)))
        elif kind in ("link", "image"):
            href = _clean_url(m.group(2))
            label = m.group(1).strip()
            if label:
                ops.extend(_inline_to_delta(m.group(1), dict(attrs, href=href)))
            else:  # empty-text link/image: surface the URL instead of dropping it
                ops.append(_op(href, dict(attrs, href=href)))
        else:
            ops.extend(_inline_to_delta(m.group(1), dict(attrs, **{kind: True})))
        text = text[m.end() :]
    return [o for o in ops if o.get("insert")]


_DIVIDER_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^(\s*)(```|~~~)(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(\[[ xX]\]\s+)?(.*)$")
_IMAGE_RE = re.compile(r"^\s*!\[[^\]]*\]\(([^)]+)\)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def _is_table_start(lines, i):
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and bool(_TABLE_SEP_RE.match(lines[i + 1]))
    )


def _line_starts_block(lines, i):
    line = lines[i]
    if not line.strip():
        return True
    if _FENCE_RE.match(line) or _DIVIDER_RE.match(line) or _HEADING_RE.match(line):
        return True
    if _QUOTE_RE.match(line) or _LIST_RE.match(line) or _IMAGE_RE.match(line):
        return True
    return _is_table_start(lines, i)


def _table_cells(row):
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _parse_table(lines, i):
    rows = [_table_cells(lines[i])]
    i += 2  # skip header row + separator
    n = len(lines)
    while i < n and lines[i].strip() and "|" in lines[i]:
        rows.append(_table_cells(lines[i]))
        i += 1
    ncols = max(len(r) for r in rows)
    children = []
    for r in rows:
        cells = []
        for c in range(ncols):
            txt = r[c] if c < len(r) else ""
            cells.append(
                {
                    "type": "simple_table_cell",
                    "data": {},
                    "children": [
                        {
                            "type": "paragraph",
                            "data": {"delta": _inline_to_delta(txt)},
                            "children": [],
                        }
                    ],
                }
            )
        children.append({"type": "simple_table_row", "data": {}, "children": cells})
    return {"type": "simple_table", "data": {}, "children": children}, i


def _parse_list(lines, i):
    n = len(lines)
    roots = []
    stack = []  # (indent, block)
    while i < n:
        if not lines[i].strip():  # allow blank lines inside a list
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _LIST_RE.match(lines[j]) and not _DIVIDER_RE.match(lines[j]):
                i = j
                continue
            break
        if _DIVIDER_RE.match(lines[i]):
            break
        m = _LIST_RE.match(lines[i])
        if not m:
            break
        indent = len(m.group(1).replace("\t", "  "))
        marker, checkbox, content = m.group(2), m.group(3), m.group(4)
        if checkbox:
            data = {
                "checked": checkbox.strip()[1].lower() == "x",
                "delta": _inline_to_delta(content),
            }
            ty = "todo_list"
        elif marker[0].isdigit():
            ty, data = "numbered_list", {"delta": _inline_to_delta(content)}
        else:
            ty, data = "bulleted_list", {"delta": _inline_to_delta(content)}
        block = {"type": ty, "data": data, "children": []}
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1]["children"] if stack else roots).append(block)
        stack.append((indent, block))
        i += 1
    return roots, i


def _markdown_to_blocks(markdown: str):
    """Convert a Markdown string to a list of AppFlowy nested SerdeBlock dicts."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        mfence = _FENCE_RE.match(line)
        if mfence:
            fence, lang = mfence.group(2), mfence.group(3).strip()
            close = re.compile(r"^\s*" + re.escape(fence) + r"\s*$")
            body, i = [], i + 1
            while i < n and not close.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            data = {"delta": [{"insert": "\n".join(body)}]}
            if lang:
                data["language"] = lang
            blocks.append({"type": "code", "data": data, "children": []})
            continue
        if _DIVIDER_RE.match(line):
            blocks.append({"type": "divider", "data": {}, "children": []})
            i += 1
            continue
        mh = _HEADING_RE.match(line)
        if mh:
            blocks.append(
                {
                    "type": "heading",
                    "data": {
                        "level": len(mh.group(1)),
                        "delta": _inline_to_delta(mh.group(2).strip()),
                    },
                    "children": [],
                }
            )
            i += 1
            continue
        if _is_table_start(lines, i):
            tbl, i = _parse_table(lines, i)
            blocks.append(tbl)
            continue
        if _QUOTE_RE.match(line):
            qlines = []
            while i < n and _QUOTE_RE.match(lines[i]):
                qlines.append(_QUOTE_RE.sub("", lines[i], count=1))
                i += 1
            blocks.append(
                {
                    "type": "quote",
                    "data": {"delta": _inline_to_delta("\n".join(qlines).strip())},
                    "children": [],
                }
            )
            continue
        if _LIST_RE.match(line):
            lst, i = _parse_list(lines, i)
            blocks.extend(lst)
            continue
        mimg = _IMAGE_RE.match(line)
        if mimg:
            blocks.append(
                {
                    "type": "image",
                    "data": {"url": _clean_url(mimg.group(1))},
                    "children": [],
                }
            )
            i += 1
            continue
        # paragraph: gather consecutive plain lines (soft-wrapped) into one block
        para = [line.strip()]
        i += 1
        while i < n and not _line_starts_block(lines, i):
            para.append(lines[i].strip())
            i += 1
        blocks.append(
            {
                "type": "paragraph",
                "data": {"delta": _inline_to_delta(" ".join(para))},
                "children": [],
            }
        )
    return blocks


# ==================== AUTHENTICATION TOOLS ====================


@mcp.tool(
    name="appflowy_login",
    description="Login to AppFlowy Cloud and get access token. Returns access token and refresh token.",
)
def appflowy_login(request: LoginRequest):
    if request.email:
        client.email = request.email
    if request.password:
        client.password = request.password

    if not client.email or not client.password:
        raise Exception(
            "Email and password must be provided either in the request or via APPFLOWY_EMAIL and APPFLOWY_PASSWORD env vars"
        )

    try:
        result = client.login()
        return {
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
        }
    except Exception as e:
        raise Exception(f"Login failed: {str(e)}")


@mcp.tool(
    name="appflowy_refresh_token",
    description="Refresh access token using refresh token.",
)
def appflowy_refresh_token(request: RefreshTokenRequest):
    client.token_store.set_refresh_token(request.refresh_token)
    try:
        result = client.refresh_token()
        return {
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
        }
    except Exception as e:
        raise Exception(f"Token refresh failed: {str(e)}")


# ==================== WORKSPACE TOOLS ====================


@mcp.tool(
    name="appflowy_list_workspaces",
    description="List all workspaces for the authenticated user.",
)
def appflowy_list_workspaces(
    include_member_count: bool | None = None, include_role: bool | None = None
):
    _require_access_token()
    try:
        workspaces = client.get_workspaces(
            include_member_count=include_member_count,
            include_role=include_role,
        )
        return [w.model_dump() for w in workspaces]
    except Exception as e:
        raise Exception(f"Failed to list workspaces: {str(e)}")


@mcp.tool(
    name="appflowy_get_workspace_folder",
    description="Get the workspace folder tree metadata.",
)
def appflowy_get_workspace_folder(
    workspace_id: str, depth: int | None = None, root_view_id: str | None = None
):
    _require_access_token()
    try:
        folder = client.get_workspace_folder(
            workspace_id, depth=depth, root_view_id=root_view_id
        )
        return folder.model_dump()
    except Exception as e:
        raise Exception(f"Failed to get workspace folder: {str(e)}")


# ==================== DATABASE TOOLS ====================


@mcp.tool(
    name="appflowy_list_databases", description="List all databases in a workspace."
)
def appflowy_list_databases(workspace_id: str):
    _require_access_token()
    try:
        databases = client.get_databases(workspace_id)
        return [d.model_dump() for d in databases]
    except Exception as e:
        raise Exception(f"Failed to list databases: {str(e)}")


@mcp.tool(
    name="appflowy_get_database_fields",
    description="Get fields of a specific database.",
)
def appflowy_get_database_fields(workspace_id: str, database_id: str):
    _require_access_token()
    try:
        fields = client.get_database_fields(workspace_id, database_id)
        return [f.model_dump() for f in fields]
    except Exception as e:
        raise Exception(f"Failed to get database fields: {str(e)}")


# AppFlowy FieldType enum -> integer id (confirmed via live data: LastEditedTime=8).
_FIELD_TYPES = {
    "rich_text": 0, "richtext": 0, "text": 0,
    "number": 1, "num": 1,
    "date": 2, "datetime": 2, "date_time": 2,
    "single_select": 3, "singleselect": 3, "select": 3,
    "multi_select": 4, "multiselect": 4,
    "checkbox": 5, "bool": 5, "boolean": 5,
    "url": 6,
    "checklist": 7,
}
_SELECT_FIELD_TYPES = {3, 4}
# SelectOptionColor variant names (from the live "To-dos" grid).
_SELECT_COLORS = [
    "Purple", "Pink", "LightPink", "Orange", "Yellow", "Lime", "Green", "Aqua", "Blue",
]


def _gen_id(n=6):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


@mcp.tool(
    name="appflowy_create_field",
    description=(
        "Add a typed column to a Grid database. field_type one of: rich_text, "
        "number, date, single_select, multi_select, checkbox, url, checklist. "
        "For single_select/multi_select, pass `options` (a list of option "
        "names) to seed choices. Returns the new field id. "
        "NOTE: this AppFlowy version's REST API exposes no field update/delete "
        "endpoint (only create) — edit/remove a column via the web UI."
    ),
)
def appflowy_create_field(
    workspace_id: str,
    database_id: str,
    name: str,
    field_type: str = "rich_text",
    options: list[str] | None = None,
):
    _require_access_token()
    try:
        if not name:
            raise Exception("Missing required field: name")
        key = str(field_type).strip().lower()
        if key not in _FIELD_TYPES:
            raise Exception(
                f"Unknown field_type '{field_type}'. Valid: rich_text, number, "
                "date, single_select, multi_select, checkbox, url, checklist"
            )
        ft = _FIELD_TYPES[key]
        if options and ft not in _SELECT_FIELD_TYPES:
            raise Exception(
                f"`options` is only valid for single_select/multi_select, not '{key}'"
            )
        # Select type options are stored as a *stringified* JSON under "content"
        # (the server parses it back on read); all other types use {} defaults.
        type_option_data: dict = {}
        if ft in _SELECT_FIELD_TYPES and options:
            opts = [
                {
                    "id": _gen_id(),
                    "name": str(n),
                    "color": _SELECT_COLORS[i % len(_SELECT_COLORS)],
                }
                for i, n in enumerate(options)
            ]
            type_option_data = {
                "content": json.dumps({"disable_color": False, "options": opts})
            }
        body = {"name": name, "field_type": ft, "type_option_data": type_option_data}
        field_id = _api(
            "POST",
            f"/api/workspace/{workspace_id}/database/{database_id}/fields",
            json_body=body,
        )
        return {"field_id": field_id.get("data", field_id) if isinstance(field_id, dict) else field_id}
    except Exception as e:
        raise Exception(f"Failed to create field: {str(e)}")


# ==================== ROW TOOLS ====================


@mcp.tool(name="appflowy_list_rows", description="List all row IDs in a database.")
def appflowy_list_rows(workspace_id: str, database_id: str):
    _require_access_token()
    try:
        rows = client.get_database_row_ids(workspace_id, database_id)
        return [r.model_dump() for r in rows]
    except Exception as e:
        raise Exception(f"Failed to list rows: {str(e)}")


@mcp.tool(
    name="appflowy_get_row_details", description="Get details of specific rows by IDs."
)
def appflowy_get_row_details(
    workspace_id: str, database_id: str, row_ids: str, with_doc: bool = False
):
    _require_access_token()
    try:
        ids_list = [item.strip() for item in row_ids.split(",") if item.strip()]
        if not ids_list:
            raise Exception("At least one row ID is required.")
        details = client.get_database_row_details(
            workspace_id, database_id, ids_list, with_doc=with_doc
        )
        return [d.model_dump() for d in details]
    except Exception as e:
        raise Exception(f"Failed to get row details: {str(e)}")


@mcp.tool(name="appflowy_create_row", description="Create a new row in a database.")
def appflowy_create_row(workspace_id: str, database_id: str, request: RowCreateRequest):
    _require_access_token()
    try:
        row_id = client.create_database_row(
            workspace_id,
            database_id,
            cells=request.cells,
            document=request.document,
        )
        return {"id": row_id}
    except Exception as e:
        raise Exception(f"Failed to create row: {str(e)}")


@mcp.tool(
    name="appflowy_upsert_row",
    description="Update existing row or create if it doesn't exist.",
)
def appflowy_upsert_row(workspace_id: str, database_id: str, request: RowUpdateRequest):
    _require_access_token()
    try:
        row_id = client.upsert_database_row(
            workspace_id,
            database_id,
            request.pre_hash or "",
            cells=request.cells,
            document=request.document,
        )
        return {"id": row_id}
    except Exception as e:
        raise Exception(f"Failed to upsert row: {str(e)}")


@mcp.tool(
    name="appflowy_get_updated_rows",
    description="Find updated rows in a database after a specific datetime.",
)
def appflowy_get_updated_rows(workspace_id: str, database_id: str, after: str):
    _require_access_token()
    try:
        updated_rows = client.get_database_row_ids_updated(
            workspace_id, database_id, after=after
        )
        return [r.model_dump() for r in updated_rows]
    except Exception as e:
        raise Exception(f"Failed to get updated rows: {str(e)}")


# ==================== DOCUMENT TOOLS ====================


@mcp.tool(name="appflowy_create_collab", description="Create a new collab object.")
def appflowy_create_collab(workspace_id: str, object_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        client.create_collab(
            workspace_id, object_id, _payload_str(request, "encoded_collab")
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to create collab: {str(e)}")


@mcp.tool(
    name="appflowy_update_collab", description="Update an existing collab object."
)
def appflowy_update_collab(workspace_id: str, object_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        client.update_collab(
            workspace_id, object_id, _payload_str(request, "encoded_collab")
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to update collab: {str(e)}")


@mcp.tool(name="appflowy_get_collab", description="Retrieve encoded collab data.")
def appflowy_get_collab(workspace_id: str, object_id: str):
    _require_access_token()
    try:
        return client.get_collab(workspace_id, object_id).model_dump()
    except Exception as e:
        raise Exception(f"Failed to get collab: {str(e)}")


@mcp.tool(
    name="appflowy_get_collab_json",
    description="Retrieve collab data as JSON (collab_type: 0=Document, 1=Database, 4=DatabaseRow).",
)
def appflowy_get_collab_json(workspace_id: str, object_id: str, collab_type: int = 0):
    _require_access_token()
    try:
        return _api(
            "GET",
            f"/api/workspace/v1/{workspace_id}/collab/{object_id}/json",
            params={"collab_type": collab_type},
        )
    except Exception as e:
        raise Exception(f"Failed to get collab json: {str(e)}")


@mcp.tool(
    name="appflowy_batch_create_collab", description="Bulk create collab objects."
)
def appflowy_batch_create_collab(workspace_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        collabs = _payload_dict(request).get("collabs")
        if not isinstance(collabs, dict):
            raise Exception("Missing required field: collabs")
        client.batch_create_collab(workspace_id, collabs)
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to batch create collab: {str(e)}")


@mcp.tool(
    name="appflowy_full_sync_collab",
    description="Perform a full document state sync and return the binary response as base64.",
)
def appflowy_full_sync_collab(
    workspace_id: str, object_id: str, request: FlexiblePayload
):
    _require_access_token()
    try:
        doc_state = _payload_str(request, "doc_state")
        content = client.full_sync_collab(workspace_id, object_id, doc_state)
        return {
            "content_base64": base64.b64encode(content).decode("utf-8"),
            "content_type": "application/octet-stream",
        }
    except Exception as e:
        raise Exception(f"Failed to full sync collab: {str(e)}")


@mcp.tool(
    name="appflowy_web_update_collab",
    description="Push an update from the web client to a collab object.",
)
def appflowy_web_update_collab(
    workspace_id: str, object_id: str, request: FlexiblePayload
):
    _require_access_token()
    try:
        client.web_update_collab(
            workspace_id, object_id, _payload_str(request, "update")
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to web update collab: {str(e)}")


@mcp.tool(
    name="appflowy_create_page_view",
    description="Create a new document page in the workspace hierarchy.",
)
def appflowy_create_page_view(workspace_id: str, request: FlexiblePayload):
    # Direct REST: appflowysdk's PageResponse model requires a `name` field the
    # server doesn't return, so it errors even though the page is created.
    _require_access_token()
    try:
        payload = _payload_dict(request)
        parent_view_id = payload.get("parent_view_id")
        if not parent_view_id:
            raise Exception("Missing required field: parent_view_id")
        layout_raw = payload.get("layout", 0)
        try:
            layout = int(layout_raw)
        except (TypeError, ValueError):
            layout = {"document": 0, "grid": 1, "board": 2, "calendar": 3}.get(
                str(layout_raw).lower(), 0
            )
        body = {"parent_view_id": str(parent_view_id), "layout": layout}
        if payload.get("name"):
            body["name"] = payload["name"]
        if payload.get("page_data"):
            body["page_data"] = payload["page_data"]
        resp = _api(
            "POST", f"/api/workspace/{workspace_id}/page-view", json_body=body
        )
        return resp.get("data", resp)
    except Exception as e:
        raise Exception(f"Failed to create page: {str(e)}")


@mcp.tool(name="appflowy_get_page_view", description="Get page metadata (use appflowy_get_page_markdown for content).")
def appflowy_get_page_view(workspace_id: str, view_id: str):
    # Direct REST: appflowysdk's PageCollabResponse expects encoded_collab as a
    # string but the server returns raw bytes. Return clean metadata instead.
    _require_access_token()
    try:
        resp = _api("GET", f"/api/workspace/{workspace_id}/page-view/{view_id}")
        data = resp.get("data", resp)
        return {
            "view": data.get("view", {}),
            "note": "Call appflowy_get_page_markdown for the page content.",
        }
    except Exception as e:
        raise Exception(f"Failed to get page view: {str(e)}")


def _crdt_text_deltas(workspace_id: str, view_id: str) -> dict[str, str]:
    """Decode the document's raw CRDT doc_state -> {external_id: delta_json_str}.

    The `/collab/.../json` read endpoint serializes text via yrs `to_json_value()`,
    which flattens formatted YText to plain strings — losing inline bold/italic/
    code/strikethrough and link hrefs. The raw `doc_state` CRDT preserves them, so
    we decode it with pycrdt and rebuild each text node's delta (with attributes).
    Returns {} on any failure so the caller falls back to the flattened text_map.
    """
    try:
        from pycrdt import Doc, Map
    except Exception:
        return {}
    # Read from the persisted /collab snapshot (a plain, side-effect-free GET).
    # We deliberately do NOT full-sync here: full-sync opens a realtime session and
    # submits a (no-op) update, i.e. a WRITE — far too heavy for a hot read path
    # hit on every get_page_markdown. The only downside is that marks read back
    # immediately after an in-place write may be momentarily stale until the
    # snapshot catches up; re-read shortly after to confirm (the write tool also
    # verifies its own result).
    try:
        resp = _api(
            "GET",
            f"/api/workspace/{workspace_id}/collab/{view_id}",
            json_body={
                "workspace_id": workspace_id,
                "inner": {"object_id": view_id, "collab_type": 0},
            },
        )
        doc_state = resp.get("data", {}).get("doc_state")
        if not doc_state:
            return {}
        doc = Doc()
        data = doc.get("data", type=Map)
        doc.apply_update(bytes(doc_state))
        text_map = data["document"]["meta"]["text_map"]
        out: dict[str, str] = {}
        for ext in text_map.keys():
            ops = []
            for insert, attrs in text_map[ext].diff():
                op = {"insert": insert}
                if attrs:
                    op["attributes"] = attrs
                ops.append(op)
            out[ext] = json.dumps(ops)
        return out
    except Exception:
        return {}


@mcp.tool(
    name="appflowy_get_page_markdown",
    description="Get a document page's full content rendered as Markdown.",
)
def appflowy_get_page_markdown(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        resp = _api(
            "GET",
            f"/api/workspace/v1/{workspace_id}/collab/{view_id}/json",
            params={"collab_type": 0},
        )
        doc = resp.get("data", {}).get("collab", {}).get("document", {})
        if not doc:
            raise Exception("No document content found for this view.")
        # The JSON endpoint flattens inline formatting; restore it from the CRDT
        # doc_state (best-effort — falls back to the flattened text on failure).
        deltas = _crdt_text_deltas(workspace_id, view_id)
        if deltas:
            doc.setdefault("meta", {}).setdefault("text_map", {}).update(deltas)
        return {"view_id": view_id, "markdown": _document_to_markdown(doc)}
    except Exception as e:
        raise Exception(f"Failed to get page markdown: {str(e)}")


@mcp.tool(
    name="appflowy_append_block_to_page",
    description="Append blocks to a document page.",
)
def appflowy_append_block_to_page(
    workspace_id: str, view_id: str, request: FlexiblePayload
):
    _require_access_token()
    try:
        blocks = _payload_dict(request).get("blocks")
        if not isinstance(blocks, list):
            raise Exception("Missing required field: blocks")
        client.append_page_blocks(workspace_id, view_id, blocks)
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to append blocks: {str(e)}")


def _fix_append_order(block):
    """Pre-transform a block tree to cancel the append-block endpoint's reordering.

    append-block walks a single global prev_id: when a child carries descendants
    the chain descends into them, so the *next* sibling is inserted at the START
    of its parent — fully reversing any sibling group whose members have children
    (e.g. table rows/cells). Leaf-only groups (flat list items) chain correctly.
    We pre-reverse exactly the groups the server will reverse, cancelling it.
    (Verified empirically against the live server: tables, simple nested lists,
    and multi-section docs round-trip correctly.) Caveat: a *mixed* sibling group
    — a leaf following a branch at the same level, e.g. an irregular nested list —
    can't be corrected this way; use create_page_from_markdown for those.
    """
    children = block.get("children") or []
    for child in children:
        _fix_append_order(child)
    if children and any(c.get("children") for c in children):
        block["children"] = list(reversed(children))
    return block


@mcp.tool(
    name="appflowy_append_markdown",
    description=(
        "Append Markdown to an existing document page. Converts Markdown "
        "(headings, bulleted/numbered/todo lists with nesting, quotes, code "
        "fences, dividers, GFM tables, inline bold/italic/code/strikethrough/"
        "links) into AppFlowy blocks and appends them at the end of the page. "
        "For documents with deeply/irregularly nested lists, prefer "
        "create_page_from_markdown (the append endpoint can't reorder those)."
    ),
)
def appflowy_append_markdown(workspace_id: str, view_id: str, markdown: str):
    _require_access_token()
    try:
        blocks = _markdown_to_blocks(markdown)
        if not blocks:
            return {"ok": True, "appended_blocks": 0}
        # Append each top-level block in its OWN call: append-block's single
        # global prev_id otherwise mis-orders top-level blocks that follow one
        # with children. _fix_append_order cancels the per-level nested reversal.
        for b in blocks:
            _fix_append_order(b)
            _api(
                "POST",
                f"/api/workspace/{workspace_id}/page-view/{view_id}/append-block",
                json_body={"blocks": [b]},
            )
        return {"ok": True, "appended_blocks": len(blocks)}
    except Exception as e:
        raise Exception(f"Failed to append markdown: {str(e)}")


@mcp.tool(
    name="appflowy_create_page_from_markdown",
    description=(
        "Create a new document page from Markdown in one call. Converts the "
        "Markdown (headings, bulleted/numbered/todo lists with nesting, quotes, "
        "code fences, dividers, GFM tables, inline bold/italic/code/"
        "strikethrough/links) into AppFlowy blocks. Returns the new view."
    ),
)
def appflowy_create_page_from_markdown(
    workspace_id: str, parent_view_id: str, name: str, markdown: str
):
    _require_access_token()
    try:
        # NOTE: do NOT _reverse_nested_children here — page_data goes through the
        # server's json_to_document (correct children_map); only the append-block
        # path needs the reversal workaround.
        children = _markdown_to_blocks(markdown)
        body = {
            "parent_view_id": str(parent_view_id),
            "layout": 0,
            "name": name,
            "page_data": {"type": "page", "data": {}, "children": children},
        }
        resp = _api(
            "POST", f"/api/workspace/{workspace_id}/page-view", json_body=body
        )
        return resp.get("data", resp)
    except Exception as e:
        raise Exception(f"Failed to create page from markdown: {str(e)}")


# ---- In-place page-content replace via pycrdt collab surgery (CT-13) ----
# There is no "clear/replace blocks" REST endpoint (only append-block / move /
# trash / icon — verified in src/api/workspace.rs). To replace a page's body
# while preserving its view_id, we mutate the document CRDT directly: load the
# raw doc_state into a pycrdt.Doc, delete every block except the root page block
# (plus its children_map / text_map entries), insert the new blocks, then push
# the resulting *incremental* yrs update to the web-update endpoint (the same
# path the web client uses for edits): POST /api/workspace/v1/{ws}/collab/{view}
# /web-update  body {doc_state:[u8...], collab_type:0}.
#
# Document CRDT schema (confirmed live):
#   data.document = {page_id, blocks, meta:{children_map, text_map}}
#   block = {id, ty, data:<json str, delta excluded>, parent, children:<cm key>,
#            external_id:<tm key|None>, external_type:"text"|None}
#   children_map[key] = [child_block_id, ...]   (empty array for leaves)
#   text_map[external_id] = YText (formatted delta lives here, NOT in block.data)


def _add_block_to_crdt(serde, parent_id, parent_children_key, blocks, cm, tm, Map, Array, Text):
    """Materialize one nested SerdeBlock (and its children) into the CRDT maps.

    `serde` is a {type, data:{...,delta?}, children:[...]} dict from
    _markdown_to_blocks. delta (if present) is moved out of block.data into a new
    text_map YText; block.data keeps only the non-delta fields (level/checked/
    language/...). Block ids are generated here (the server does NOT assign ids on
    this path, unlike the page-view create/append endpoints).
    """
    bid = _gen_id(10)
    ch_key = _gen_id(10)
    data = dict(serde.get("data") or {})
    delta = data.pop("delta", None)
    block = {
        "id": bid,
        "ty": serde["type"],
        "data": json.dumps(data, separators=(",", ":")),
        "parent": parent_id,
        "children": ch_key,
    }
    ext = None
    if delta is not None:
        ext = _gen_id(10)
        block["external_id"] = ext
        block["external_type"] = "text"
    else:
        block["external_id"] = None
        block["external_type"] = None
    blocks[bid] = Map(block)
    cm[ch_key] = Array([])
    if delta is not None:
        text = Text()
        tm[ext] = text
        # Insert the plain text once, then apply each attributed run as a bounded
        # range with format(). Inserting attributed text adjacent to plain text
        # makes yrs *extend* the mark into the neighbour (formatting bleed), so we
        # never pass attributes to insert() — format() on a fixed [start,stop) is
        # the only bleed-free way to write a rich-text delta.
        # IMPORTANT: pycrdt's Text.format() indexes by UTF-8 BYTE offset, not by
        # code point. Computing ranges with len(str) silently misaligns every mark
        # that follows a multi-byte character (em-dash "—", middot "·", arrow "→",
        # accented letters, emoji, …). So accumulate byte offsets via utf-8 encode.
        full = "".join(op.get("insert", "") for op in delta)
        if full:
            text.insert(0, full)
        idx = 0  # byte offset into the UTF-8 encoding of `full`
        for op in delta:
            ins = op.get("insert", "")
            if not ins:
                continue
            blen = len(ins.encode("utf-8"))
            attrs = op.get("attributes") or None
            if attrs:
                text.format(idx, idx + blen, attrs)
            idx += blen
    cm[parent_children_key].append(bid)
    for child in serde.get("children") or []:
        _add_block_to_crdt(child, bid, ch_key, blocks, cm, tm, Map, Array, Text)


@mcp.tool(
    name="appflowy_update_page_from_markdown",
    description=(
        "Replace an existing document page's entire content with blocks "
        "converted from Markdown, IN PLACE — the page's view_id is preserved so "
        "inbound links/bookmarks survive (unlike recreating the page). The old "
        "content is fully removed. Supports the same Markdown as "
        "create_page_from_markdown (headings, bulleted/numbered/todo lists with "
        "nesting, quotes, code fences, dividers, GFM tables, inline bold/italic/"
        "code/strikethrough/links). Pass empty markdown to clear the page. "
        "NOTE: this is a read-modify-write on the page CRDT with no locking, so "
        "it is not safe against another client editing the SAME page "
        "concurrently (intended for agent-owned docs). Reliable for a "
        "single-writer, clean page edited sequentially; if the server's live doc "
        "has diverged (concurrent edit, or a page already duplicated by an earlier "
        "bad write) the replace may not converge — the tool runs a post-write "
        "check and returns ok=false + a warning if the page looks duplicated, in "
        "which case recreate the page via create_page_from_markdown. On ok=true, "
        "re-read with get_page_markdown to confirm."
    ),
)
def appflowy_update_page_from_markdown(workspace_id: str, view_id: str, markdown: str):
    _require_access_token()
    try:
        try:
            from pycrdt import Doc, Map, Array, Text
        except Exception as e:
            raise Exception(
                "pycrdt is required for in-place page updates but is not "
                f"available: {e} (add `--with pycrdt` to the MCP launch args)"
            )
        # Load the CURRENT (realtime) document state via full-sync. The persisted
        # /collab snapshot can lag recent web-update edits; loading a stale base
        # makes the delete step miss the live blocks, so a re-update DUPLICATES the
        # page instead of replacing it. full-sync round-trips the live state so the
        # deletes below reference the live items.
        # LIMITATION (verified): this is safe for a single-writer, clean,
        # freshly-loaded page edited once. It is NOT guaranteed to converge if the
        # server's live doc has diverged from this snapshot (concurrent edit, or a
        # page already duplicated by a prior bad write): the web-update delete set
        # is diffed locally (get_update(state_before)), not against the server's
        # advertised state vector, so deletes can be dropped server-side and
        # content can still stack. The post-write check below catches that case;
        # recreate via create_page_from_markdown if it trips.
        live_update = _full_sync_doc_state(workspace_id, view_id)
        if not live_update:
            raise Exception(
                "full-sync returned no document state (is this a document page?)"
            )
        doc = Doc()
        data = doc.get("data", type=Map)
        doc.apply_update(live_update)
        if "document" not in data:
            raise Exception("Unexpected collab structure (no 'document' map)")
        document = data["document"]
        page_id = document["page_id"]
        blocks = document["blocks"]
        meta = document["meta"]
        cm = meta["children_map"]
        tm = meta["text_map"]
        if page_id not in blocks:
            raise Exception("Unexpected document structure (page block missing)")
        page_children_key = blocks[page_id]["children"]

        # State vector BEFORE the mutation, so get_update() below yields only the
        # incremental change (deletes of old blocks + inserts of new ones) — that
        # is what the web-update endpoint applies on top of the live collab.
        state_before = doc.get_state()
        new_blocks = _markdown_to_blocks(markdown)
        with doc.transaction():
            # Delete every block except the root page block, along with its
            # children_map and text_map entries (captured before the delete).
            for bid in list(blocks.keys()):
                if bid == page_id:
                    continue
                blk = blocks[bid]
                keys = list(blk.keys())
                ck = blk["children"] if "children" in keys else None
                ext = blk["external_id"] if "external_id" in keys else None
                del blocks[bid]
                if ck is not None and ck in cm:
                    del cm[ck]
                if ext is not None and ext in tm:
                    del tm[ext]
            # Clear the page's children ordering array (slice-delete is O(n)).
            del cm[page_children_key][:]
            # Insert the new content under the (unchanged) page block.
            for serde in new_blocks:
                _add_block_to_crdt(
                    serde, page_id, page_children_key, blocks, cm, tm, Map, Array, Text
                )

        update = doc.get_update(state_before)
        _api(
            "POST",
            f"/api/workspace/v1/{workspace_id}/collab/{view_id}/web-update",
            json_body={"doc_state": list(update), "collab_type": 0},
        )

        result = {
            "ok": True,
            "view_id": view_id,
            "blocks": len(new_blocks),
            "note": "update submitted; re-read with get_page_markdown to confirm",
        }
        # Post-write verification: re-read the live doc and confirm the page now has
        # exactly len(new_blocks) top-level children. A mismatch means the delete
        # set didn't apply against the realtime doc (identity divergence) and the
        # content stacked — fail loudly so the caller doesn't trust a duplicated page.
        try:
            verify = _full_sync_doc_state(workspace_id, view_id)
            vdoc = Doc()
            vdata = vdoc.get("data", type=Map)
            vdoc.apply_update(verify)
            vdocument = vdata["document"]
            vpage = vdocument["page_id"]
            vkey = vdocument["blocks"][vpage]["children"]
            live_top = len(list(vdocument["meta"]["children_map"][vkey]))
            if live_top != len(new_blocks):
                result["ok"] = False
                result["warning"] = (
                    f"post-write check FAILED: page has {live_top} top-level blocks "
                    f"but {len(new_blocks)} were written — the in-place replace did "
                    "not fully apply (realtime delete-identity divergence); the page "
                    "is likely duplicated. Recreate via create_page_from_markdown."
                )
            else:
                result["verified_top_level_blocks"] = live_top
        except Exception as e:
            result["note"] += f" (post-write verify skipped: {e})"
        return result
    except Exception as e:
        raise Exception(f"Failed to update page from markdown: {str(e)}")


@mcp.tool(
    name="appflowy_create_orphaned_view",
    description="Create a view without a parent folder or page.",
)
def appflowy_create_orphaned_view(workspace_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        payload = _payload_dict(request)
        view = client.create_orphaned_view(
            workspace_id,
            layout=_payload_layout(request),
            name=payload.get("name"),
        )
        return view.model_dump() if view is not None else {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to create orphaned view: {str(e)}")


@mcp.tool(name="appflowy_duplicate_page", description="Duplicate an existing page.")
def appflowy_duplicate_page(
    workspace_id: str, view_id: str, request: FlexiblePayload | None = None
):
    _require_access_token()
    try:
        parent_view_id = (
            _payload_dict(request).get("parent_view_id") if request else None
        )
        client.duplicate_page(workspace_id, view_id, parent_view_id=parent_view_id)
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to duplicate page: {str(e)}")


@mcp.tool(
    name="appflowy_move_page",
    description=(
        "Move a page to a new parent in the workspace hierarchy (re-parent / "
        "reorder). prev_view_id is the sibling to place this page after under "
        "the new parent (omit to place first)."
    ),
)
def appflowy_move_page(
    workspace_id: str,
    view_id: str,
    new_parent_view_id: str,
    prev_view_id: str | None = None,
):
    _require_access_token()
    try:
        body = {"new_parent_view_id": str(new_parent_view_id)}
        if prev_view_id:
            body["prev_view_id"] = str(prev_view_id)
        _api(
            "POST",
            f"/api/workspace/{workspace_id}/page-view/{view_id}/move",
            json_body=body,
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to move page: {str(e)}")


@mcp.tool(
    name="appflowy_move_page_to_trash",
    description="Move a page to the trash (reversible; restore with appflowy_restore_page_from_trash).",
)
def appflowy_move_page_to_trash(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        _api(
            "POST",
            f"/api/workspace/{workspace_id}/page-view/{view_id}/move-to-trash",
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to move page to trash: {str(e)}")


@mcp.tool(
    name="appflowy_restore_page_from_trash",
    description="Restore a trashed page back into the workspace.",
)
def appflowy_restore_page_from_trash(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        _api(
            "POST",
            f"/api/workspace/{workspace_id}/page-view/{view_id}/restore-from-trash",
        )
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to restore page from trash: {str(e)}")


@mcp.tool(name="appflowy_create_quick_note", description="Create a quick note.")
def appflowy_create_quick_note(workspace_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        title = _payload_str(request, "title")
        content = _payload_str(request, "content")
        return client.create_quick_note(workspace_id, title, content).model_dump()
    except Exception as e:
        raise Exception(f"Failed to create quick note: {str(e)}")


@mcp.tool(name="appflowy_list_quick_notes", description="List user quick notes.")
def appflowy_list_quick_notes(workspace_id: str):
    _require_access_token()
    try:
        return client.list_quick_notes(workspace_id).model_dump()
    except Exception as e:
        raise Exception(f"Failed to list quick notes: {str(e)}")


@mcp.tool(name="appflowy_search_documents", description="Semantic search in documents.")
def appflowy_search_documents(workspace_id: str, request: FlexiblePayload):
    _require_access_token()
    try:
        query = _payload_str(request, "query")
        return [
            item.model_dump() for item in client.search_documents(workspace_id, query)
        ]
    except Exception as e:
        raise Exception(f"Failed to search documents: {str(e)}")


@mcp.tool(name="appflowy_publish_page", description="Make a page public.")
def appflowy_publish_page(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        client.publish_page(workspace_id, view_id)
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to publish page: {str(e)}")


@mcp.tool(
    name="appflowy_unpublish_page", description="Revoke public access for a page."
)
def appflowy_unpublish_page(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        client.unpublish_page(workspace_id, view_id)
        return {"ok": True}
    except Exception as e:
        raise Exception(f"Failed to unpublish page: {str(e)}")


# ==================== IMPORT ====================


@mcp.tool(
    name="appflowy_import_zip",
    description=(
        "Import a Notion-export zip into AppFlowy. Creates a NEW workspace named "
        "from the file (processed async by the worker; allow a minute or two). "
        "Pass the INNER markdown-tree zip — Notion wraps exports as a zip-of-zips "
        "(…Part-1.zip); feed the inner one, not the outer wrapper."
    ),
)
def appflowy_import_zip(file_path: str):
    # Direct multipart POST: AppFlowy's /api/import requires X-Content-Length and
    # X-Content-MD5 (base64 md5) headers matching the upload, which the SDK's
    # import_zip omits (so it 400s on md5 mismatch). 401 -> refresh once + retry.
    _require_access_token()
    path = Path(file_path)
    if not path.exists():
        raise Exception(f"File not found: {file_path}")
    try:
        content = path.read_bytes()
        md5_b64 = base64.b64encode(hashlib.md5(content).digest()).decode()

        def _post():
            token = _require_access_token()
            try:
                return httpx.post(
                    f"{BASE_URL}/api/import",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Content-Length": str(len(content)),
                        "X-Content-MD5": md5_b64,
                    },
                    files={"file": ("import.zip", content, "application/zip")},
                    timeout=120.0,
                )
            except httpx.RequestError as e:
                raise Exception(f"Network error calling POST /api/import: {e}") from e

        resp = _post()
        if resp.status_code == 401:  # short-lived token; refresh once and retry
            try:
                client.refresh_token()
            except Exception as e:
                raise Exception(
                    f"POST /api/import -> 401 and token refresh failed: {e} "
                    "(call appflowy_login first)"
                ) from e
            resp = _post()
        if resp.status_code >= 400:
            raise Exception(f"/api/import -> {resp.status_code}: {resp.text[:300]}")
        return {
            "ok": True,
            "note": "Import queued (async via worker); creates a new workspace "
            "named 'file' — rename it afterward.",
        }
    except Exception as e:
        raise Exception(f"Failed to import zip: {str(e)}")


@mcp.tool(
    name="appflowy_create_import_task",
    description="Create an import task (Notion, etc.).",
)
def appflowy_create_import_task(request: FlexiblePayload):
    _require_access_token()
    try:
        payload = _payload_dict(request)
        import_type = payload.get("import_type")
        data = payload.get("data")
        if not import_type or not isinstance(data, dict):
            raise Exception("Missing required fields: import_type, data")
        return client.create_import_task(str(import_type), data).model_dump()
    except Exception as e:
        raise Exception(f"Failed to create import task: {str(e)}")


if __name__ == "__main__":
    mcp.run()
