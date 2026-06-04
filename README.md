# AppFlowy Cloud MCP Server

A Model Context Protocol (MCP) server for interacting with AppFlowy Cloud API, providing tools for workspace, database, and row operations.

## Features

- **Authentication**: Login and refresh token management
- **Workspace Operations**: List all workspaces
- **Database Operations**: List databases, get database fields
- **Row Operations**: List rows, get row details, create rows, upsert rows

## Authentication

The server uses in-memory token storage. To authenticate:

1. Use `appflowy_login` with your email and password
2. The tokens are stored automatically
3. Use `appflowy_refresh_token` when the access token expires

## Available Tools

### Authentication Tools
- `appflowy_login(request: LoginRequest)` - Login to AppFlowy Cloud
- `appflowy_refresh_token(request: RefreshTokenRequest)` - Refresh access token

### Workspace Tools
- `appflowy_list_workspaces()` - List all workspaces
- `appflowy_get_workspace_folder(workspace_id: str, depth: int | None = None, root_view_id: str | None = None)` - Get workspace folder metadata

### Database Tools
- `appflowy_list_databases(workspace_id: str)` - List databases in a workspace
- `appflowy_get_database_fields(workspace_id: str, database_id: str)` - Get database fields

### Row Tools
- `appflowy_list_rows(workspace_id: str, database_id: str)` - List row IDs
- `appflowy_get_row_details(workspace_id: str, database_id: str, row_ids: str, with_doc: bool = False)` - Get row details
- `appflowy_create_row(workspace_id: str, database_id: str, request: RowCreateRequest)` - Create a new row
- `appflowy_upsert_row(workspace_id: str, database_id: str, request: RowUpdateRequest)` - Update or create row

### Document Tools
- `appflowy_create_collab(...)`
- `appflowy_update_collab(...)`
- `appflowy_get_collab(...)`
- `appflowy_get_collab_json(...)`
- `appflowy_batch_create_collab(...)`
- `appflowy_full_sync_collab(...)`
- `appflowy_web_update_collab(...)`
- `appflowy_create_page_view(...)`
- `appflowy_get_page_view(...)`
- `appflowy_append_block_to_page(...)`
- `appflowy_create_orphaned_view(...)`
- `appflowy_duplicate_page(...)`
- `appflowy_create_quick_note(...)`
- `appflowy_list_quick_notes(...)`
- `appflowy_search_documents(...)`
- `appflowy_publish_page(...)`
- `appflowy_unpublish_page(...)`
- `appflowy_import_zip(file_path: str)`
- `appflowy_create_import_task(...)`

## Running the Server

```bash
uv run python main.py
```

## Usage Example

1. Login:
```python
request = LoginRequest(email="your@example.com", password="your_password")
response = appflowy_login(request)
```

2. List workspaces:
```python
workspaces = appflowy_list_workspaces()
```

3. Get database fields:
```python
fields = appflowy_get_database_fields("workspace_id", "database_id")
```

4. Create a row:
```python
row_request = RowCreateRequest(cells={"Field_Name": "Value"}, document="Optional markdown")
result = appflowy_create_row("workspace_id", "database_id", row_request)
```

## Note

The server maintains tokens in memory. For production use, consider adding persistent storage (Redis, database, etc.) and proper error handling.

## Self hosting 
if you are slef hosting and you want the mcp to work just added as env in where you get you AI
