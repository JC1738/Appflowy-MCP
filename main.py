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
import httpx
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("appflowy-cloud")

# In-memory token storage (could be replaced with Redis/DB for production)
token_store = {"access_token": None, "refresh_token": None}

# ==================== AUTHENTICATION TOOLS ====================


@mcp.tool(
    name="appflowy_login",
    description="Login to AppFlowy Cloud and get access token. Returns access token and refresh token.",
)
def appflowy_login(request: LoginRequest):
    """Login to AppFlowy Cloud using email and password."""
    url = "https://beta.appflowy.cloud/gotrue/token?grant_type=password"
    headers = {
        "Content-Type": "application/json",
    }
    data = {"email": request.email, "password": request.password}

    response = httpx.post(url, headers=headers, json=data)
    if response.status_code == 200:
        result = response.json()
        # Store tokens in memory
        token_store["access_token"] = result.get("access_token")
        token_store["refresh_token"] = result.get("refresh_token")
        return result
    else:
        raise Exception(f"Login failed: {response.status_code} - {response.text}")


@mcp.tool(
    name="appflowy_refresh_token",
    description="Refresh access token using refresh token.",
)
def appflowy_refresh_token(request: RefreshTokenRequest):
    """Refresh AppFlowy Cloud access token."""
    url = "https://beta.appflowy.cloud/gotrue/token?grant_type=refresh_token"
    headers = {
        "Content-Type": "application/json",
    }
    data = {"refresh_token": request.refresh_token}

    response = httpx.post(url, headers=headers, json=data)
    if response.status_code == 200:
        result = response.json()
        # Update stored tokens
        token_store["access_token"] = result.get("access_token")
        token_store["refresh_token"] = result.get("refresh_token")
        return result
    else:
        raise Exception(
            f"Token refresh failed: {response.status_code} - {response.text}"
        )


# ==================== WORKSPACE TOOLS ====================


@mcp.tool(
    name="appflowy_list_workspaces",
    description="List all workspaces for the authenticated user.",
)
def appflowy_list_workspaces():
    """List all AppFlowy workspaces."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = "https://beta.appflowy.cloud/api/workspace"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to list workspaces: {response.status_code} - {response.text}"
        )


# ==================== DATABASE TOOLS ====================


@mcp.tool(
    name="appflowy_list_databases", description="List all databases in a workspace."
)
def appflowy_list_databases(workspace_id: str):
    """List all databases in a workspace."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to list databases: {response.status_code} - {response.text}"
        )


@mcp.tool(
    name="appflowy_get_database_fields",
    description="Get fields of a specific database.",
)
def appflowy_get_database_fields(workspace_id: str, database_id: str):
    """Get fields of a specific database."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database/{database_id}/fields"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to get database fields: {response.status_code} - {response.text}"
        )


# ==================== ROW TOOLS ====================


@mcp.tool(name="appflowy_list_rows", description="List all row IDs in a database.")
def appflowy_list_rows(workspace_id: str, database_id: str):
    """List all row IDs in a database."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database/{database_id}/row"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to list rows: {response.status_code} - {response.text}"
        )


@mcp.tool(
    name="appflowy_get_row_details", description="Get details of specific rows by IDs."
)
def appflowy_get_row_details(
    workspace_id: str, database_id: str, row_ids: str, with_doc: bool = False
):
    """Get details of specific rows. row_ids should be comma-separated UUIDs."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database/{database_id}/row/detail"
    params = {"ids": row_ids, "with_doc": str(with_doc).lower()}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.get(url, headers=headers, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to get row details: {response.status_code} - {response.text}"
        )


@mcp.tool(name="appflowy_create_row", description="Create a new row in a database.")
def appflowy_create_row(workspace_id: str, database_id: str, request: RowCreateRequest):
    """Create a new row in a database."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database/{database_id}/row"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.post(url, headers=headers, json=request.dict())
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to create row: {response.status_code} - {response.text}"
        )


@mcp.tool(
    name="appflowy_upsert_row",
    description="Update existing row or create if it doesn't exist.",
)
def appflowy_upsert_row(workspace_id: str, database_id: str, request: RowUpdateRequest):
    """Update existing row or create if it doesn't exist."""
    access_token = token_store.get("access_token")
    if not access_token:
        raise Exception("Not authenticated. Please login first.")

    url = f"https://beta.appflowy.cloud/api/workspace/{workspace_id}/database/{database_id}/row"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    response = httpx.put(url, headers=headers, json=request.dict())
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(
            f"Failed to upsert row: {response.status_code} - {response.text}"
        )


if __name__ == "__main__":
    mcp.run()
