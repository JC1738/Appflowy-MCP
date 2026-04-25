from fastmcp import FastMCP
import os
from src.models import (
    Task,
    LoginRequest,
    RefreshTokenRequest,
    AuthResponse,
    Workspace,
    Database,
    RowDetail,
    RowCreateRequest,
    RowUpdateRequest,
)
from dotenv import load_dotenv

from appflowysdk import AppFlowy
from appflowysdk.exceptions import AppFlowyError, LoginError, RefreshTokenError, APIError, ValidationError, NetworkError

load_dotenv()

mcp = FastMCP("appflowy-cloud")

# Global AppFlowy client
client = AppFlowy(
    email=os.getenv("APPFLOWY_EMAIL"),
    password=os.getenv("APPFLOWY_PASSWORD")
)

# ==================== AUTHENTICATION TOOLS ====================

@mcp.tool(
    name="appflowy_login",
    description="Login to AppFlowy Cloud and get access token. Returns access token and refresh token.",
)
def appflowy_login(request: LoginRequest):
    """Login to AppFlowy Cloud. Can use provided credentials or fallback to APPFLOWY_EMAIL/APPFLOWY_PASSWORD env vars."""
    if request.email:
        client.email = request.email
    if request.password:
        client.password = request.password
        
    if not client.email or not client.password:
        raise Exception("Email and password must be provided either in the request or via APPFLOWY_EMAIL and APPFLOWY_PASSWORD env vars")
        
    try:
        result = client.login()
        return {"access_token": result.access_token, "refresh_token": result.refresh_token}
    except Exception as e:
        raise Exception(f"Login failed: {str(e)}")


@mcp.tool(
    name="appflowy_refresh_token",
    description="Refresh access token using refresh token.",
)
def appflowy_refresh_token(request: RefreshTokenRequest):
    """Refresh AppFlowy Cloud access token."""
    client.token_store.set_refresh_token(request.refresh_token)
    try:
        result = client.refresh_token()
        return {"access_token": result.access_token, "refresh_token": result.refresh_token}
    except Exception as e:
        raise Exception(f"Token refresh failed: {str(e)}")


# ==================== WORKSPACE TOOLS ====================

@mcp.tool(
    name="appflowy_list_workspaces",
    description="List all workspaces for the authenticated user.",
)
def appflowy_list_workspaces():
    """List all AppFlowy workspaces."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        workspaces = client.get_workspaces()
        return [w.model_dump() for w in workspaces]
    except Exception as e:
        raise Exception(f"Failed to list workspaces: {str(e)}")


# ==================== DATABASE TOOLS ====================

@mcp.tool(
    name="appflowy_list_databases", description="List all databases in a workspace."
)
def appflowy_list_databases(workspace_id: str):
    """List all databases in a workspace."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

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
    """Get fields of a specific database."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        fields = client.get_database_fields(workspace_id, database_id)
        return [f.model_dump() for f in fields]
    except Exception as e:
        raise Exception(f"Failed to get database fields: {str(e)}")


# ==================== ROW TOOLS ====================

@mcp.tool(name="appflowy_list_rows", description="List all row IDs in a database.")
def appflowy_list_rows(workspace_id: str, database_id: str):
    """List all row IDs in a database."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

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
    """Get details of specific rows. row_ids should be comma-separated UUIDs."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        ids_list = [id.strip() for id in row_ids.split(",") if id.strip()]
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
    """Create a new row in a database."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        row_id = client.create_database_row(
            workspace_id, database_id, cells=request.cells, document=request.document
        )
        return {"id": row_id}
    except Exception as e:
        raise Exception(f"Failed to create row: {str(e)}")


@mcp.tool(
    name="appflowy_upsert_row",
    description="Update existing row or create if it doesn't exist.",
)
def appflowy_upsert_row(workspace_id: str, database_id: str, request: RowUpdateRequest):
    """Update existing row or create if it doesn't exist."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        row_id = client.upsert_database_row(
            workspace_id, 
            database_id, 
            request.pre_hash or "", 
            cells=request.cells, 
            document=request.document
        )
        return {"id": row_id}
    except Exception as e:
        raise Exception(f"Failed to upsert row: {str(e)}")

@mcp.tool(
    name="appflowy_get_updated_rows", 
    description="Find updated rows in a database after a specific datetime."
)
def appflowy_get_updated_rows(workspace_id: str, database_id: str, after: str):
    """Find updated rows after a specific datetime (ISO 8601 string)."""
    if not client.token_store.get_access_token():
        raise Exception("Not authenticated. Please login first.")

    try:
        updated_rows = client.get_database_row_ids_updated(
            workspace_id, database_id, after=after
        )
        return [r.model_dump() for r in updated_rows]
    except Exception as e:
        raise Exception(f"Failed to get updated rows: {str(e)}")


if __name__ == "__main__":
    mcp.run()
