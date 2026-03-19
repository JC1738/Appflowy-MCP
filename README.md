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

### Database Tools
- `appflowy_list_databases(workspace_id: str)` - List databases in a workspace
- `appflowy_get_database_fields(workspace_id: str, database_id: str)` - Get database fields

### Row Tools
- `appflowy_list_rows(workspace_id: str, database_id: str)` - List row IDs
- `appflowy_get_row_details(workspace_id: str, database_id: str, row_ids: str, with_doc: bool = False)` - Get row details
- `appflowy_create_row(workspace_id: str, database_id: str, request: RowCreateRequest)` - Create a new row
- `appflowy_upsert_row(workspace_id: str, database_id: str, request: RowUpdateRequest)` - Update or create row

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
