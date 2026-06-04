from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

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
    base_url = str(os.getenv("BASE_URL"))
    client = AppFlowy(
        base_url=base_url,
        email=os.getenv("APPFLOWY_EMAIL"),
        password=os.getenv("APPFLOWY_PASSWORD"),
    )
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
    return layout if isinstance(layout, ViewLayout) else ViewLayout(int(layout))


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


@mcp.tool(name="appflowy_get_collab_json", description="Retrieve collab data as JSON.")
def appflowy_get_collab_json(workspace_id: str, object_id: str):
    _require_access_token()
    try:
        return client.get_collab_json(workspace_id, object_id).model_dump()
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
    _require_access_token()
    try:
        payload = _payload_dict(request)
        parent_view_id = payload.get("parent_view_id")
        if not parent_view_id:
            raise Exception("Missing required field: parent_view_id")
        page = client.create_page(
            workspace_id,
            str(parent_view_id),
            layout=_payload_layout(request),
            name=payload.get("name"),
            page_data=payload.get("page_data"),
        )
        return page.model_dump()
    except Exception as e:
        raise Exception(f"Failed to create page: {str(e)}")


@mcp.tool(name="appflowy_get_page_view", description="Get page data and metadata.")
def appflowy_get_page_view(workspace_id: str, view_id: str):
    _require_access_token()
    try:
        return client.get_page(workspace_id, view_id).model_dump()
    except Exception as e:
        raise Exception(f"Failed to get page view: {str(e)}")


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
    description="Upload and import a zip file into AppFlowy Cloud.",
)
def appflowy_import_zip(file_path: str):
    _require_access_token()
    path = Path(file_path)
    if not path.exists():
        raise Exception(f"File not found: {file_path}")
    try:
        client.import_zip(path.read_bytes())
        return {"ok": True}
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
