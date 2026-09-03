# Chat Workspace API

## Local run

```bash
cd backend
conda activate project
pip install -e .
uvicorn app.main:app --reload --port 8000
```

The development default is the local MySQL database `chat_workspace` at `127.0.0.1:3306`. Set `CHAT_DATABASE_URL` when using another server. The initial administrator is created from `CHAT_ADMIN_EMAIL` and `CHAT_ADMIN_PASSWORD`.

For a clean local database, start the included MySQL service with `docker compose up -d mysql`, then set `CHAT_DATABASE_URL=mysql+pymysql://chat_app:local_chat_password@127.0.0.1:3307/chat_workspace?charset=utf8mb4`. Existing local MySQL credentials can be supplied through environment variables.

Schema management is available through Alembic:

```bash
alembic upgrade head
```

Set `CHAT_ENCRYPTION_KEY` to a Fernet key generated with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
The development fallback derives the key from `CHAT_JWT_SECRET` for compatibility
with existing local records.

The local asset volume can be cleaned after a backup with:

```bash
python scripts/cleanup_assets.py
```

## Model channels

`/api/v1/admin/model-channels` manages OpenAI-compatible text/image channels. A channel stores a Base URL, API key, modality (`text`, `image`, or `both`), model names, priority, and enabled state. API keys are never returned in full by the API.

The channel editor accepts a JSON capability map for per-model modality overrides and
transport flags. For example, `{"gpt-image-2":["image"],"_text_endpoint":"responses"}`
selects the Responses transport for text calls while keeping the image model separate;
`"image_edit_transport":"json"` enables a gateway-specific JSON edit payload.

The provider adapter normalizes a Base URL to an API root, synchronizes models from
`<base>/models`, streams text from `<base>/chat/completions`, generates images from
`<base>/images/generations`, and edits images through standard multipart
`<base>/images/edits`. The image endpoints accept `prompt` (and retain `content` as a
legacy alias), `asset_ids`, `mask_asset_id`, `size`, `quality`, and single-output
`n=1`. Multi-image values are rejected until the workspace has gallery persistence.

Before either a direct image request or a `generate_image` tool call reaches the
provider, the server resolves output hints in the prompt. For the configured image
channel, `16:9 2K` becomes `size=2560x1440`, `9:16 4K` becomes
`size=2160x3840`, and Chinese/English high-quality wording becomes
`quality=high`. Explicit bounded `WIDTHxHEIGHT` values are also accepted. Prompt
hints take precedence over request defaults so direct generation, edits, native
function calls, and prompt-level tool calls behave consistently. GPT Image quality
values are `auto/low/medium/high`; legacy client values `standard/hd` are normalized
to `medium/high` before the provider request.

While `/admin/model-channels` is open, the admin UI immediately synchronizes every
enabled channel through `POST /api/v1/admin/model-channels/{channel_id}/sync-models`
and repeats the operation every 60 seconds. Requests remain server-side so provider
keys are never sent to the browser. The interval is cleared when the page unmounts,
overlapping scheduled runs are skipped, and automatic/manual requests share a per-channel
in-flight lock. The API also uses a per-channel condition/single-flight result so concurrent
requests wait for and reuse one provider call. The last check/sync status is shown in
the channel table. Manual test, sync, edit, enable/disable, and delete actions remain
available. A Base URL containing URL credentials is rejected; changing its origin requires
the administrator to enter the API key again instead of forwarding the stored key to a new
host.

Text streams include a bounded `generate_image` function tool whenever an image model
is enabled. Tool turns emit `tool.started`, `image.created`, and `tool.completed` SSE
events; tool-call/result rows are persisted for provider replay but hidden from the
user message list. Configure `CHAT_MODEL_TOOL_MAX_ROUNDS`,
`CHAT_MODEL_TOOL_MAX_CALLS`, and `CHAT_MODEL_MAX_REFERENCE_BYTES` for limits.
`CHAT_MODEL_MAX_IMAGE_BYTES` caps decoded provider output before it is written to disk.
Stopping a thread marks both the root text request and any active image-tool child as
stopped. A late synchronous provider response is checked under a database row lock and
discarded instead of creating an asset or changing the request back to completed.

Providers that ignore native function calling also receive a platform System Prompt.
For an image request it asks the text model to emit one strict envelope:

```text
<platform_tool_call>{"name":"generate_image","arguments":{"prompt":"...","size":"1024x1024"}}</platform_tool_call>
```

The server parses this envelope across arbitrary SSE chunk boundaries, removes it from
visible assistant text, validates the arguments with the same image-tool schema, and
runs the existing bounded tool loop. Native OpenAI `tool_calls` take precedence when
both transports appear. Prompt-level calls are replayed to gateways as ordinary
assistant/result context because some compatible gateways reject `role=tool`; native
calls retain the standard OpenAI replay structure. Uploaded reference IDs are appended
as server-generated `<platform_attachments>` metadata so an edit request can reuse the
exact user-owned assets without allowing the model to bypass ownership checks.

If a provider exposes only the Responses API, set
`capabilities_json["_text_endpoint"]` to `responses` for that channel; the adapter
translates chat messages and function tools to `/responses` and parses its delta events.
