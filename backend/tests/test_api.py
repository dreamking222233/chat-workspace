from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import uuid
import json
from pathlib import Path
from threading import Event, Lock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api import channels as channels_api
from app.api import workspace as workspace_api
from app.db.session import SessionLocal
from app.main import app
from app.models import AdminAuditLog, Asset, Export, Message, ModelChannel, ModelRequest, Thread, User
from app.services.providers import ImageResult, TextResult
from app.schemas.common import RegenerateRequest, SendMessageRequest


def test_reasoning_effort_contract_accepts_four_levels_only():
    for effort in ("low", "medium", "high", "xhigh"):
        assert SendMessageRequest(content="solve", reasoning_effort=effort).reasoning_effort == effort
        assert RegenerateRequest(reasoning_effort=effort).reasoning_effort == effort
    with pytest.raises(ValidationError):
        SendMessageRequest(content="solve", reasoning_effort="invalid")


def test_health_and_registration():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/api/v1/auth/register", json={"email": f"test-{uuid.uuid4().hex}@example.com", "password": "password123", "display_name": "Test"})
        assert response.status_code == 201
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["role"] == "user"


def test_registration_requires_non_blank_display_name_and_normalizes_it():
    with TestClient(app) as client:
        email = f"display-name-{uuid.uuid4().hex}@example.com"
        credentials = {"email": email, "password": "password123"}

        missing = client.post("/api/v1/auth/register", json=credentials)
        assert missing.status_code == 422

        blank = client.post("/api/v1/auth/register", json={**credentials, "display_name": "   \t\n"})
        assert blank.status_code == 422

        with SessionLocal() as db:
            assert db.scalar(select(User).where(User.email == email)) is None

        registered = client.post(
            "/api/v1/auth/register",
            json={**credentials, "display_name": "  真实用户  "},
        )
        assert registered.status_code == 201
        assert registered.json()["user"]["display_name"] == "真实用户"

        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user is not None
            assert user.display_name == "真实用户"


def test_user_isolation_and_admin_channel():
    with TestClient(app) as client:
        email = f"isolated-{uuid.uuid4().hex}@example.com"
        registered = client.post("/api/v1/auth/register", json={"email": email, "password": "password123", "display_name": "Isolated"})
        assert registered.status_code == 201
        user_body = registered.json()
        user_headers = {"Authorization": f"Bearer {user_body['access_token']}"}
        admin_login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

        project = client.post("/api/v1/projects", headers=user_headers, json={"name": "Private project"})
        assert project.status_code == 201
        assert project.json()["created_at"].endswith("+08:00")
        assert project.json()["updated_at"].endswith("+08:00")
        assert client.get("/api/v1/admin/model-channels", headers=user_headers).status_code == 403
        channel = client.post("/api/v1/admin/model-channels", headers=admin_headers, json={"name": f"Image {uuid.uuid4().hex}", "base_url": "https://api.example.com/v1", "api_key": "sk-test-12345678", "modality": "image", "models": ["gpt-image-1"], "priority": 10, "capabilities": {"gpt-image-1": ["image"], "image_edit_transport": "json"}})
        assert channel.status_code == 201
        assert channel.json()["api_key_masked"].startswith("sk-t")
        assert channel.json()["channel_type"] == "official"
        assert channel.json()["capabilities"]["image_edit_transport"] == "json"
        assert channel.json()["created_at"].endswith("+08:00")
        assert channel.json()["updated_at"].endswith("+08:00")
        users = client.get("/api/v1/admin/users", headers=admin_headers)
        assert users.status_code == 200 and users.json()["total"] >= 2
        assert all(item["created_at"].endswith("+08:00") for item in users.json()["items"])
        # Keep the shared development database free of channel fixtures so
        # the admin page only renders channels created through the real API.
        assert client.delete(f"/api/v1/admin/model-channels/{channel.json()['id']}", headers=admin_headers).status_code == 204


def test_admin_channel_type_can_be_set_to_codex():
    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        created = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Codex {uuid.uuid4().hex}",
                "base_url": "https://codex.example/v1",
                "api_key": "TOKEN",
                "channel_type": "codex",
                "modality": "text",
                "models": ["gpt-codex"],
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]
        assert created.json()["channel_type"] == "codex"
        updated = client.patch(
            f"/api/v1/admin/model-channels/{channel_id}",
            headers=admin_headers,
            json={"channel_type": "official"},
        )
        assert updated.status_code == 200
        assert updated.json()["channel_type"] == "official"
        assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_beijing_usage_filter_uses_an_exclusive_next_midnight_boundary():
    model = f"boundary-{uuid.uuid4().hex}"
    timestamps = [
        datetime(2026, 9, 1, 15, 59, 59, 999999),
        datetime(2026, 9, 1, 16, 0, 0),
        datetime(2026, 9, 2, 15, 59, 59, 999999),
        datetime(2026, 9, 2, 16, 0, 0),
    ]
    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        with SessionLocal() as db:
            admin = db.scalar(select(User).where(User.email == "admin@example.com"))
            rows = [ModelRequest(user_id=admin.id, model=model, modality="text", status="completed", created_at=value) for value in timestamps]
            db.add_all(rows)
            db.commit()
            included_ids = {rows[1].id, rows[2].id}

        response = client.get(
            "/api/v1/admin/usage",
            headers=admin_headers,
            params={
                "model": model,
                "created_after": "2026-09-02T00:00:00.000+08:00",
                "created_before": "2026-09-03T00:00:00.000+08:00",
            },
        )
        assert response.status_code == 200
        assert {item["id"] for item in response.json()} == included_ids
        assert all(item["created_at"].endswith("+08:00") for item in response.json())


def test_workspace_admin_and_export_time_responses_have_beijing_offsets():
    marker = uuid.uuid4().hex
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, f"beijing-{marker[:8]}")
        admin_headers = _admin_headers(client)
        grant = client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=admin_headers, json={"months": 1})
        assert grant.status_code == 200
        assert grant.json()["starts_at"].endswith("+08:00")
        assert grant.json()["expires_at"].endswith("+08:00")

        project = client.post("/api/v1/projects", headers=user_headers, json={"name": "北京时间项目"})
        thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "北京时间会话", "project_id": project.json()["id"]})
        assert project.json()["created_at"].endswith("+08:00")
        assert thread.json()["created_at"].endswith("+08:00")
        assert thread.json()["updated_at"].endswith("+08:00")

        with SessionLocal() as db:
            admin = db.scalar(select(User).where(User.email == "admin@example.com"))
            message = Message(thread_id=thread.json()["id"], user_id=user["id"], role="user", content="时间测试", content_type="text", sequence=0)
            request = ModelRequest(user_id=user["id"], thread_id=thread.json()["id"], model=f"time-{marker}", modality="text", status="completed")
            export = Export(user_id=user["id"], thread_id=thread.json()["id"], format="json", status="completed")
            audit = AdminAuditLog(admin_id=admin.id, target_user_id=user["id"], action=f"time-{marker}")
            db.add_all([message, request, export, audit])
            db.commit()
            message_id, export_id, audit_id = message.id, export.id, audit.id

        messages = client.get(f"/api/v1/threads/{thread.json()['id']}/messages", headers=user_headers).json()
        assert next(item for item in messages if item["id"] == message_id)["created_at"].endswith("+08:00")
        usage = client.get("/api/v1/admin/usage", headers=admin_headers, params={"model": f"time-{marker}"}).json()
        assert len(usage) == 1 and usage[0]["created_at"].endswith("+08:00")
        exported = client.get(f"/api/v1/exports/{export_id}", headers=user_headers).json()
        assert exported["created_at"].endswith("+08:00")
        audits = client.get("/api/v1/admin/audit-logs", headers=admin_headers).json()
        assert next(item for item in audits if item["id"] == audit_id)["created_at"].endswith("+08:00")

        json_export = client.get(f"/api/v1/threads/{thread.json()['id']}/export?format=json", headers=user_headers)
        exported_message = next(item for item in json_export.json()["messages"] if item["content"] == "时间测试")
        assert exported_message["created_at"].endswith("+08:00")


def test_admin_channel_model_sync_persists_remote_models(monkeypatch):
    remote_models = [
        {"id": "text-new", "capabilities": ["text"]},
        {"id": "image-new", "modalities": ["image"]},
    ]
    monkeypatch.setattr(channels_api, "list_remote_models", lambda _channel: remote_models)

    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        created = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Sync {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "both",
                "models": ["manual-alias"],
                "capabilities": {"_text_endpoint": "chat_completions"},
                "priority": 20,
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]
        try:
            remote = client.get(f"/api/v1/admin/model-channels/{channel_id}/remote-models", headers=admin_headers)
            assert remote.status_code == 200
            assert remote.json()["models"] == ["text-new", "image-new"]
            assert remote.json()["capabilities"]["image-new"] == ["image"]
            listed_before_sync = next(item for item in client.get("/api/v1/admin/model-channels", headers=admin_headers).json() if item["id"] == channel_id)
            assert listed_before_sync["models"] == ["manual-alias"]
            synced = client.post(f"/api/v1/admin/model-channels/{channel_id}/sync-models", headers=admin_headers)
            assert synced.status_code == 200
            body = synced.json()
            assert body["ok"] is True
            assert body["models"] == ["text-new", "image-new"]
            assert body["capabilities"]["text-new"] == ["text"]
            assert body["capabilities"]["image-new"] == ["image"]
            assert body["capabilities"]["_text_endpoint"] == "chat_completions"

            listed = client.get("/api/v1/admin/model-channels", headers=admin_headers).json()
            stored = next(item for item in listed if item["id"] == channel_id)
            assert stored["models"] == body["models"]
            assert stored["models_synced_at"] is not None
            assert stored["last_sync_error"] is None

            def fail_remote_models(_channel):
                raise httpx.ConnectError("provider offline", request=httpx.Request("GET", "https://provider.example/v1/models"))

            monkeypatch.setattr(channels_api, "list_remote_models", fail_remote_models)
            failed = client.post(f"/api/v1/admin/model-channels/{channel_id}/sync-models", headers=admin_headers)
            assert failed.status_code == 200
            assert failed.json()["ok"] is False
            assert failed.json()["models"] == body["models"]
            stored_after_failure = next(
                item for item in client.get("/api/v1/admin/model-channels", headers=admin_headers).json()
                if item["id"] == channel_id
            )
            assert stored_after_failure["models"] == body["models"]
            assert stored_after_failure["last_sync_error"] == "ConnectError"
        finally:
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_admin_channel_can_start_without_manual_models(monkeypatch):
    monkeypatch.setattr(channels_api, "list_remote_models", lambda _channel: [{"id": "gpt-text"}, {"id": "gpt-image-2"}])
    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        created = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Auto models {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "both",
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]
        assert created.json()["models"] == []
        try:
            synced = client.post(f"/api/v1/admin/model-channels/{channel_id}/sync-models", headers=admin_headers)
            assert synced.status_code == 200
            assert synced.json()["models"] == ["gpt-text", "gpt-image-2"]
            assert synced.json()["capabilities"] == {"gpt-text": ["text"], "gpt-image-2": ["image"]}
        finally:
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_admin_channel_does_not_reuse_secret_for_a_different_origin():
    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        rejected_credentials = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Credentials {uuid.uuid4().hex}",
                "base_url": "https://user:password@provider.example/v1",
                "api_key": "TOKEN",
                "modality": "text",
                "models": ["text-model"],
            },
        )
        assert rejected_credentials.status_code == 422

        created = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Origin {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "original-secret-token",
                "modality": "text",
                "models": ["text-model"],
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]
        try:
            rejected = client.patch(
                f"/api/v1/admin/model-channels/{channel_id}",
                headers=admin_headers,
                json={"base_url": "https://other.example/v1", "api_key": ""},
            )
            assert rejected.status_code == 422
            assert "API Key" in rejected.json()["message"]

            unchanged = next(
                item for item in client.get("/api/v1/admin/model-channels", headers=admin_headers).json()
                if item["id"] == channel_id
            )
            assert unchanged["base_url"] == "https://provider.example/v1"
            assert unchanged["api_key_masked"].startswith("orig")

            replaced = client.patch(
                f"/api/v1/admin/model-channels/{channel_id}",
                headers=admin_headers,
                json={"base_url": "https://other.example/v1", "api_key": "replacement-secret-token"},
            )
            assert replaced.status_code == 200
            assert replaced.json()["base_url"] == "https://other.example/v1"
            assert replaced.json()["api_key_masked"].startswith("repl")
        finally:
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_admin_channel_concurrent_sync_hits_provider_once(monkeypatch):
    provider_entered = Event()
    release_provider = Event()
    second_request_observed = Event()
    third_request_observed = Event()
    calls_guard = Lock()
    provider_calls = 0

    def slow_remote_models(_channel):
        nonlocal provider_calls
        with calls_guard:
            provider_calls += 1
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return [{"id": "text-concurrent", "capabilities": ["text"]}]

    original_sync_state = channels_api._channel_sync_state
    state_calls = 0

    def observed_sync_state(channel_id):
        nonlocal state_calls
        result = original_sync_state(channel_id)
        with calls_guard:
            state_calls += 1
            if state_calls >= 2:
                second_request_observed.set()
            if state_calls >= 3:
                third_request_observed.set()
        return result

    monkeypatch.setattr(channels_api, "list_remote_models", slow_remote_models)
    monkeypatch.setattr(channels_api, "_channel_sync_state", observed_sync_state)

    with TestClient(app) as client:
        admin_headers = _admin_headers(client)
        created = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Concurrent {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "text",
                "models": ["text-manual"],
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]
        try:
            endpoint = f"/api/v1/admin/model-channels/{channel_id}/sync-models"
            with ThreadPoolExecutor(max_workers=3) as executor:
                first = executor.submit(client.post, endpoint, headers=admin_headers)
                assert provider_entered.wait(timeout=5)
                second = executor.submit(client.post, endpoint, headers=admin_headers)
                third = executor.submit(client.post, endpoint, headers=admin_headers)
                assert second_request_observed.wait(timeout=5)
                assert third_request_observed.wait(timeout=5)
                release_provider.set()
                responses = [first.result(timeout=5), second.result(timeout=5), third.result(timeout=5)]

            assert all(response.status_code == 200 and response.json()["ok"] for response in responses)
            assert provider_calls == 1
            assert sum(response.json()["message"] == "模型已由并发请求同步" for response in responses) == 2
            assert all(response.json()["models"] == ["text-concurrent"] for response in responses)
        finally:
            release_provider.set()
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_stream_events_are_replayable_and_export_is_scoped(monkeypatch):
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex
        registered = client.post("/api/v1/auth/register", json={"email": f"stream-{suffix}@example.com", "password": "password123", "display_name": "Stream"})
        assert registered.status_code == 201
        user_body = registered.json()
        user_headers = {"Authorization": f"Bearer {user_body['access_token']}"}
        admin_login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        entitlement = client.post(f"/api/v1/admin/users/{user_body['user']['id']}/entitlements", headers=admin_headers, json={"months": 1})
        assert entitlement.status_code == 200
        model, channel_id, provider_calls = _create_stub_text_channel(client, monkeypatch)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Replay", "model": model}).json()
            first = client.post(f"/api/v1/threads/{thread['id']}/messages/stream", headers=user_headers, json={"content": "hello", "model": model, "channel_id": channel_id})
            assert first.status_code == 200
            blocks = [item for item in first.text.split("\n\n") if item.strip()]
            ids = [int(next(line for line in block.splitlines() if line.startswith("id: "))[4:]) for block in blocks]
            assert ids == sorted(ids) and ids[0] == 1
            created = next(json.loads(line[6:]) for line in blocks[0].splitlines() if line.startswith("data: "))
            resumed = client.post(f"/api/v1/threads/{thread['id']}/messages/stream?request_id={created['request_id']}", headers={**user_headers, "Last-Event-ID": "1"}, json={"content": "hello"})
            assert resumed.status_code == 200 and "message.completed" in resumed.text
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert len(messages) == 2 and messages[1]["content"]
            usage = client.get("/api/v1/admin/usage", headers=admin_headers, params={"user_id": user_body["user"]["id"]}).json()
            assert usage and usage[0]["status"] == "completed"
            exported = client.get(f"/api/v1/threads/{thread['id']}/export?format=json", headers=user_headers)
            assert exported.status_code == 200 and exported.headers.get("x-export-id")
            export_id = exported.headers["x-export-id"]
            assert client.get(f"/api/v1/exports/{export_id}", headers=user_headers).status_code == 200
            assert len(provider_calls) == 1
        finally:
            _delete_test_channel(client, channel_id)


def _registered_client(client, prefix: str):
    response = client.post("/api/v1/auth/register", json={"email": f"{prefix}-{uuid.uuid4().hex}@example.com", "password": "password123", "display_name": prefix.title()})
    assert response.status_code == 201
    body = response.json()
    return body["user"], {"Authorization": f"Bearer {body['access_token']}"}


def _admin_headers(client):
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_stub_text_channel(client, monkeypatch, reply: str = "Test provider reply", channel_type: str = "official"):
    """Create a real configured channel while replacing only its network edge."""

    calls: list[dict] = []

    def fake_text(_channel, model, messages, **kwargs):
        calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        return TextResult(
            chunks=iter([reply]),
            usage={"prompt_tokens": 3, "completion_tokens": 4},
            provider_request_id=f"provider-{len(calls)}",
        )

    monkeypatch.setattr(workspace_api, "openai_text", fake_text)
    model = f"text-{uuid.uuid4().hex}"
    created = client.post(
        "/api/v1/admin/model-channels",
        headers=_admin_headers(client),
        json={
            "name": f"Text {uuid.uuid4().hex}",
            "base_url": "https://provider.example/v1",
            "api_key": "TOKEN",
            "modality": "text",
            "channel_type": channel_type,
            "models": [model],
            "capabilities": {model: ["text"]},
            "priority": 1,
        },
    )
    assert created.status_code == 201
    return model, created.json()["id"], calls


def _delete_test_channel(client, channel_id: str) -> None:
    """Remove an unused channel or disable one retained by request history."""

    response = client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=_admin_headers(client))
    assert response.status_code == 204


def test_official_search_directive_becomes_activity_and_clean_markdown(monkeypatch):
    raw_reply = (
        'search("\\u4eca\\u5929\\u65b0\\u95fb")'
        'slow|2026年9月3日 今日新闻|1\n'
        '### 今日要闻\n\n- **市场**：保持关注。'
    )
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "search-activity")
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
        model, channel_id, _ = _create_stub_text_channel(client, monkeypatch, raw_reply)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Search", "model": model}).json()
            response = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": f"search-{uuid.uuid4().hex}"},
                json={"content": "今天有什么新闻", "model": model, "channel_id": channel_id},
            )

            assert response.status_code == 200
            assert "event: search.started" in response.text
            assert '"query": "2026年9月3日 今日新闻"' in response.text
            assert "search(" not in response.text
            assert "### 今日要闻" in response.text

            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert messages[-1]["content"] == '### 今日要闻\n\n- **市场**：保持关注。'

            exported = client.get(f"/api/v1/threads/{thread['id']}/export?format=markdown", headers=user_headers)
            assert exported.status_code == 200
            assert "search(" not in exported.text
            assert "### 今日要闻" in exported.text
        finally:
            _delete_test_channel(client, channel_id)


def test_codex_channel_preserves_search_like_normal_text(monkeypatch):
    reply = 'search("term")slow|this is documentation|1\n正文'
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "codex-search-text")
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
        model, channel_id, _ = _create_stub_text_channel(client, monkeypatch, reply, channel_type="codex")
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Codex text", "model": model}).json()
            response = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": f"codex-text-{uuid.uuid4().hex}"},
                json={"content": "解释协议", "model": model, "channel_id": channel_id},
            )
            assert response.status_code == 200
            assert "event: search.started" not in response.text
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert messages[-1]["content"] == reply
        finally:
            _delete_test_channel(client, channel_id)


def test_regeneration_uses_current_content_channel_for_history_and_exports(monkeypatch):
    official_reply = 'search("today")slow|latest news|1\n### 官网正文\n\n- 项目'
    codex_reply = 'search("term")slow|this is documentation|1\nCodex 正文'
    provider_mode = {"fail_codex": False, "block_codex": False}
    codex_started = Event()
    release_codex = Event()

    def fake_text(channel, model, messages, **kwargs):
        reply = official_reply if channel.channel_type == "official" else codex_reply
        if channel.channel_type == "codex" and provider_mode["fail_codex"]:
            def partial_then_fail():
                yield reply
                raise RuntimeError("provider interrupted")

            return TextResult(chunks=partial_then_fail(), usage={}, provider_request_id="provider-failed-codex")
        if channel.channel_type == "codex" and provider_mode["block_codex"]:
            codex_started.set()
            assert release_codex.wait(timeout=5)
        return TextResult(chunks=iter([reply]), usage={}, provider_request_id=f"provider-{channel.channel_type}")

    monkeypatch.setattr(workspace_api, "openai_text", fake_text)
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "regeneration-source")
        admin_headers = _admin_headers(client)
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
        targets: dict[str, tuple[str, str]] = {}
        for channel_type in ("official", "codex"):
            model = f"{channel_type}-{uuid.uuid4().hex}"
            created = client.post(
                "/api/v1/admin/model-channels",
                headers=admin_headers,
                json={
                    "name": f"Regeneration {channel_type} {uuid.uuid4().hex}",
                    "base_url": "https://provider.example/v1",
                    "api_key": "TOKEN",
                    "modality": "text",
                    "channel_type": channel_type,
                    "models": [model],
                    "capabilities": {model: ["text"]},
                    "priority": 1,
                },
            )
            assert created.status_code == 201
            targets[channel_type] = (model, created.json()["id"])

        try:
            # official -> Codex: the current Codex response must stay byte-for-byte
            # intact even though the reused assistant row has an older official request.
            official_model, official_channel_id = targets["official"]
            codex_model, codex_channel_id = targets["codex"]
            first_thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Official to Codex", "model": official_model}).json()
            first = client.post(
                f"/api/v1/threads/{first_thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": f"official-first-{uuid.uuid4().hex}"},
                json={"content": "hello", "model": official_model, "channel_id": official_channel_id},
            )
            assert first.status_code == 200 and "event: search.started" in first.text
            assistant_id = client.get(f"/api/v1/threads/{first_thread['id']}/messages", headers=user_headers).json()[-1]["id"]
            regenerated = client.post(
                f"/api/v1/threads/{first_thread['id']}/regenerate",
                headers={**user_headers, "Idempotency-Key": f"codex-regen-{uuid.uuid4().hex}"},
                json={"assistant_message_id": assistant_id, "model": codex_model, "channel_id": codex_channel_id},
            )
            assert regenerated.status_code == 200 and "event: search.started" not in regenerated.text
            assert client.get(f"/api/v1/threads/{first_thread['id']}/messages", headers=user_headers).json()[-1]["content"] == codex_reply
            # Rows written before response-source metadata was introduced use
            # the latest root request as a deterministic compatibility fallback.
            with SessionLocal() as db:
                legacy_message = db.get(Message, assistant_id)
                assert legacy_message is not None
                legacy_message.content_json = None
                db.commit()
            assert client.get(f"/api/v1/threads/{first_thread['id']}/messages", headers=user_headers).json()[-1]["content"] == codex_reply
            for export_format in ("json", "txt", "markdown"):
                exported = client.get(f"/api/v1/threads/{first_thread['id']}/export", headers=user_headers, params={"format": export_format})
                assert exported.status_code == 200
                if export_format == "json":
                    assert exported.json()["messages"][-1]["content"] == codex_reply
                else:
                    assert codex_reply in exported.text

            # Codex -> official: the latest official response is cleaned and its
            # structured search activity is emitted despite the older Codex request.
            second_thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Codex to Official", "model": codex_model}).json()
            second = client.post(
                f"/api/v1/threads/{second_thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": f"codex-first-{uuid.uuid4().hex}"},
                json={"content": "hello", "model": codex_model, "channel_id": codex_channel_id},
            )
            assert second.status_code == 200
            second_assistant_id = client.get(f"/api/v1/threads/{second_thread['id']}/messages", headers=user_headers).json()[-1]["id"]
            official_regenerated = client.post(
                f"/api/v1/threads/{second_thread['id']}/regenerate",
                headers={**user_headers, "Idempotency-Key": f"official-regen-{uuid.uuid4().hex}"},
                json={"assistant_message_id": second_assistant_id, "model": official_model, "channel_id": official_channel_id},
            )
            assert official_regenerated.status_code == 200 and "event: search.started" in official_regenerated.text
            expected_official = "### 官网正文\n\n- 项目"
            assert client.get(f"/api/v1/threads/{second_thread['id']}/messages", headers=user_headers).json()[-1]["content"] == expected_official
            for export_format in ("json", "txt", "markdown"):
                exported = client.get(f"/api/v1/threads/{second_thread['id']}/export", headers=user_headers, params={"format": export_format})
                assert exported.status_code == 200 and "search(" not in exported.text
                if export_format == "json":
                    assert exported.json()["messages"][-1]["content"] == expected_official
                else:
                    assert expected_official in exported.text

            # A failed Codex regeneration can leave partial provider text. That
            # partial content still belongs to the latest Codex request and must
            # not be cleaned because of the older official request.
            provider_mode["fail_codex"] = True
            failed = client.post(
                f"/api/v1/threads/{second_thread['id']}/regenerate",
                headers={**user_headers, "Idempotency-Key": f"failed-codex-{uuid.uuid4().hex}"},
                json={"assistant_message_id": second_assistant_id, "model": codex_model, "channel_id": codex_channel_id},
            )
            assert failed.status_code == 200 and "event: error" in failed.text
            assert client.get(f"/api/v1/threads/{second_thread['id']}/messages", headers=user_headers).json()[-1]["content"] == expected_official
            for export_format in ("json", "txt", "markdown"):
                exported = client.get(f"/api/v1/threads/{second_thread['id']}/export", headers=user_headers, params={"format": export_format})
                assert exported.status_code == 200 and "search(" not in exported.text
                if export_format == "json":
                    assert exported.json()["messages"][-1]["content"] == expected_official
                else:
                    assert expected_official in exported.text

            # Only one active root request may write a reused assistant row.
            # This prevents cross-channel streams from interleaving content and
            # source metadata in different completion orders.
            provider_mode.update(fail_codex=False, block_codex=True)
            release_codex.clear()
            codex_started.clear()
            with ThreadPoolExecutor(max_workers=1) as executor:
                active = executor.submit(
                    client.post,
                    f"/api/v1/threads/{second_thread['id']}/regenerate",
                    headers={**user_headers, "Idempotency-Key": f"active-codex-{uuid.uuid4().hex}"},
                    json={"assistant_message_id": second_assistant_id, "model": codex_model, "channel_id": codex_channel_id},
                )
                assert codex_started.wait(timeout=5)
                conflicting = client.post(
                    f"/api/v1/threads/{second_thread['id']}/regenerate",
                    headers={**user_headers, "Idempotency-Key": f"conflicting-official-{uuid.uuid4().hex}"},
                    json={"assistant_message_id": second_assistant_id, "model": official_model, "channel_id": official_channel_id},
                )
                assert conflicting.status_code == 409
                release_codex.set()
                active_response = active.result(timeout=5)
            assert active_response.status_code == 200
            assert client.get(f"/api/v1/threads/{second_thread['id']}/messages", headers=user_headers).json()[-1]["content"] == codex_reply
        finally:
            release_codex.set()
            for _, channel_id in targets.values():
                _delete_test_channel(client, channel_id)


def _row_snapshot(row) -> tuple:
    """Copy every persisted column so later ORM mutations cannot hide changes."""

    return tuple(deepcopy(getattr(row, column.name)) for column in row.__table__.columns)


def _thread_persistence_snapshot(thread_id: str) -> dict:
    with SessionLocal() as db:
        thread = db.get(Thread, thread_id)
        assert thread is not None
        messages = db.scalars(select(Message).where(Message.thread_id == thread_id).order_by(Message.sequence.asc())).all()
        requests = db.scalars(select(ModelRequest).where(ModelRequest.thread_id == thread_id).order_by(ModelRequest.created_at.asc(), ModelRequest.id.asc())).all()
        return {
            "thread": _row_snapshot(thread),
            "messages": [_row_snapshot(item) for item in messages],
            "requests": [_row_snapshot(item) for item in requests],
            "message_count": db.scalar(select(func.count(Message.id)).where(Message.thread_id == thread_id)),
            "request_count": db.scalar(select(func.count(ModelRequest.id)).where(ModelRequest.thread_id == thread_id)),
        }


def test_selected_model_without_channel_is_rejected_before_persisting_messages():
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "missing-channel")
        granted = client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1})
        assert granted.status_code == 200
        model = f"unconfigured-{uuid.uuid4().hex}"
        thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Unavailable", "model": model}).json()
        before = _thread_persistence_snapshot(thread["id"])
        response = client.post(f"/api/v1/threads/{thread['id']}/messages/stream", headers=user_headers, json={"content": "hello", "model": model})
        assert response.status_code == 503
        assert model in response.json()["message"]
        assert client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json() == []
        assert _thread_persistence_snapshot(thread["id"]) == before


def test_no_available_text_channel_is_503_and_does_not_mutate_thread():
    with TestClient(app) as client:
        channel_states: dict[str, bool] = {}
        try:
            # Entering TestClient first guarantees the lifespan has created the
            # schema even when this test is selected and run in isolation.
            with SessionLocal() as db:
                for channel in db.scalars(select(ModelChannel)).all():
                    channel_states[channel.id] = channel.enabled
                    channel.enabled = False
                db.commit()

            user, user_headers = _registered_client(client, "no-channel")
            assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "No channel"}).json()
            before = _thread_persistence_snapshot(thread["id"])

            response = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers=user_headers,
                json={"content": "hello"},
            )

            assert response.status_code == 503
            assert "no enabled text model channel" in response.json()["message"]
            assert _thread_persistence_snapshot(thread["id"]) == before
        finally:
            with SessionLocal() as db:
                for channel_id, enabled in channel_states.items():
                    channel = db.get(ModelChannel, channel_id)
                    if channel is not None:
                        channel.enabled = enabled
                db.commit()


def test_explicit_channel_model_mismatch_is_422_and_does_not_mutate_thread(monkeypatch):
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "channel-mismatch")
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
        model, channel_id, provider_calls = _create_stub_text_channel(client, monkeypatch)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Strict channel", "model": model}).json()
            before = _thread_persistence_snapshot(thread["id"])

            response = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers=user_headers,
                json={"content": "hello", "model": f"other-{uuid.uuid4().hex}", "channel_id": channel_id},
            )

            assert response.status_code == 422
            assert "selected channel" in response.json()["message"]
            assert _thread_persistence_snapshot(thread["id"]) == before
            assert provider_calls == []
        finally:
            _delete_test_channel(client, channel_id)


def test_regenerate_reuses_assistant_message_without_duplicate_user_prompt(monkeypatch):
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "regenerate")
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
        model, channel_id, provider_calls = _create_stub_text_channel(client, monkeypatch)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Regenerate", "model": model}).json()
            first = client.post(f"/api/v1/threads/{thread['id']}/messages/stream", headers=user_headers, json={"content": "hello", "model": model, "channel_id": channel_id})
            assert first.status_code == 200 and first.text
            before = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert len(before) == 2 and before[0]["role"] == "user" and before[1]["role"] == "assistant"
            regenerated = client.post(f"/api/v1/threads/{thread['id']}/regenerate", headers=user_headers, json={"assistant_message_id": before[1]["id"], "model": model, "channel_id": channel_id})
            assert regenerated.status_code == 200 and regenerated.text
            after = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert len(after) == 2
            assert after[0]["id"] == before[0]["id"] and after[1]["id"] == before[1]["id"]
            assert after[1]["content"]
            assert len(provider_calls) == 2
        finally:
            _delete_test_channel(client, channel_id)


def test_regenerate_idempotency_survives_process_memory_reset(monkeypatch):
    """A durable request replay must not clear the existing assistant text."""
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "regenerate-replay")
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=_admin_headers(client), json={"months": 1}).status_code == 200
        model, channel_id, provider_calls = _create_stub_text_channel(client, monkeypatch)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Regenerate replay", "model": model}).json()
            key = f"send-{uuid.uuid4().hex}"
            first = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": key},
                json={"content": "hello", "model": model, "channel_id": channel_id},
            )
            assert first.status_code == 200
            before = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            regenerate_key = f"regen-{uuid.uuid4().hex}"
            regenerated = client.post(
                f"/api/v1/threads/{thread['id']}/regenerate",
                headers={**user_headers, "Idempotency-Key": regenerate_key},
                json={"assistant_message_id": before[1]["id"], "model": model, "channel_id": channel_id},
            )
            assert regenerated.status_code == 200
            after_first = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            original_content = after_first[1]["content"]

            # Simulate a worker restart: only database events remain available.
            workspace_api.idempotency_requests.clear()
            workspace_api.stream_states.clear()
            replay = client.post(
                f"/api/v1/threads/{thread['id']}/regenerate",
                headers={**user_headers, "Idempotency-Key": regenerate_key},
                json={"assistant_message_id": before[1]["id"], "model": model, "channel_id": channel_id},
            )
            assert replay.status_code == 200 and "message.completed" in replay.text
            after = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert len(after) == 2
            assert after[1]["content"] == original_content
            assert len(provider_calls) == 2
        finally:
            _delete_test_channel(client, channel_id)


def test_regenerate_without_available_channel_is_503_and_preserves_history(monkeypatch):
    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "regenerate-no-channel")
        admin_headers = _admin_headers(client)
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
        model, channel_id, provider_calls = _create_stub_text_channel(client, monkeypatch)
        try:
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Preserve history", "model": model}).json()
            first = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers=user_headers,
                json={"content": "first prompt", "model": model, "channel_id": channel_id},
            )
            second = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers=user_headers,
                json={"content": "second prompt", "model": model, "channel_id": channel_id},
            )
            assert first.status_code == 200 and second.status_code == 200
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert len(messages) == 4
            assert messages[1]["role"] == "assistant" and messages[1]["content"]
            assert messages[2]["role"] == "user" and messages[3]["role"] == "assistant"
            assert len(provider_calls) == 2

            disabled = client.patch(
                f"/api/v1/admin/model-channels/{channel_id}",
                headers=admin_headers,
                json={"enabled": False},
            )
            assert disabled.status_code == 200 and disabled.json()["enabled"] is False
            before = _thread_persistence_snapshot(thread["id"])

            response = client.post(
                f"/api/v1/threads/{thread['id']}/regenerate",
                headers=user_headers,
                json={"assistant_message_id": messages[1]["id"], "model": model},
            )

            assert response.status_code == 503
            assert model in response.json()["message"]
            assert _thread_persistence_snapshot(thread["id"]) == before
            assert len(provider_calls) == 2
        finally:
            _delete_test_channel(client, channel_id)


def test_prompt_level_image_tool_call_executes_and_hides_protocol(monkeypatch):
    """A provider text tag should enter the native validated image tool loop."""
    channel_id = None
    user_id = None
    stored_paths: list[Path] = []
    provider_messages: list[list[dict]] = []
    image_invocations: list[dict] = []
    provider_turn = 0
    reference_asset_id = None

    def fake_text(_channel, _model, messages, **_kwargs):
        nonlocal provider_turn
        provider_turn += 1
        provider_messages.append(messages)
        if provider_turn == 1:
            assert reference_asset_id
            marker = (
                '<platform_tool_call>{"name":"generate_image","arguments":'
                f'{{"prompt":"把参考图编辑成蓝色直播间界面，16:9 2K 高清","asset_ids":["{reference_asset_id}"]}}'
                '}</platform_tool_call>'
            )
            return TextResult(chunks=iter([marker[:13], marker[13:47], marker[47:]]), usage={"prompt_tokens": 5, "completion_tokens": 8})
        return TextResult(chunks=iter(["图片已经生成。"]), usage={"prompt_tokens": 9, "completion_tokens": 4})

    # Small valid PNG; the asset service also verifies MIME and byte limits.
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c4944415408d763f8ffff3f0005fe02fe0da44d1b0000000049454e44ae426082")

    def fake_image(*args, **kwargs):
        image_invocations.append({"args": args, **kwargs})
        return ImageResult(content=png, mime_type="image/png", provider_request_id="image-request-1", usage={"input_tokens": 2, "output_tokens": 3})

    monkeypatch.setattr(workspace_api, "openai_text", fake_text)
    monkeypatch.setattr(workspace_api, "image_request", fake_image)

    try:
        with TestClient(app) as client:
            user, user_headers = _registered_client(client, "platform-tool")
            user_id = user["id"]
            admin_headers = _admin_headers(client)
            assert client.post(f"/api/v1/admin/users/{user_id}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
            uploaded = client.post(
                "/api/v1/assets/upload",
                headers=user_headers,
                files={"file": ("reference.png", png, "image/png")},
            )
            assert uploaded.status_code == 200
            reference_asset_id = uploaded.json()["id"]
            created_channel = client.post(
                "/api/v1/admin/model-channels",
                headers=admin_headers,
                json={
                    "name": f"Platform Tool {uuid.uuid4().hex}",
                    "base_url": "https://provider.example/v1",
                    "api_key": "TOKEN",
                    "modality": "both",
                    "models": ["text-model", "image-model"],
                    "capabilities": {"text-model": ["text"], "image-model": ["image"]},
                    "priority": 1,
                },
            )
            assert created_channel.status_code == 201
            channel_id = created_channel.json()["id"]
            thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Platform tool", "model": "text-model"}).json()
            response = client.post(
                f"/api/v1/threads/{thread['id']}/messages/stream",
                headers={**user_headers, "Idempotency-Key": f"tool-{uuid.uuid4().hex}"},
                json={
                    "content": "把我上传的参考图编辑成蓝色圆形图",
                    "model": "text-model",
                    "channel_id": channel_id,
                    "asset_ids": [reference_asset_id],
                    "enable_tools": True,
                },
            )
            assert response.status_code == 200
            assert "event: tool.started" in response.text
            assert "event: image.created" in response.text
            assert "event: tool.completed" in response.text
            assert "图片已经生成。" in response.text
            assert "<platform_tool_call>" not in response.text
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert [item["content_type"] for item in messages] == ["text", "text", "image"]
            assert messages[1]["content"] == "图片已经生成。"
            assert provider_turn == 2
            assert "<platform_tool_call>" in provider_messages[0][0]["content"]
            assert any(reference_asset_id in message.get("content", "") for message in provider_messages[0])
            assert "Platform tool results already executed" in provider_messages[1][0]["content"]
            assert all(message.get("role") != "tool" for message in provider_messages[1])
            assert all("tool_calls" not in message for message in provider_messages[1])
            assert len(image_invocations) == 1
            assert image_invocations[0]["args"][3] == "2560x1440"
            assert image_invocations[0]["quality"] == "high"
            assert len(image_invocations[0]["reference_images"]) == 1
    finally:
        with SessionLocal() as db:
            if user_id:
                assets = db.scalars(select(Asset).where(Asset.user_id == user_id)).all()
                stored_paths.extend(Path(item.storage_key) for item in assets)
                user = db.get(User, user_id)
                if user:
                    db.delete(user)
            if channel_id:
                channel = db.get(ModelChannel, channel_id)
                if channel:
                    db.delete(channel)
            db.commit()
        for path in stored_paths:
            path.unlink(missing_ok=True)


def test_stopping_direct_image_discards_late_provider_result(monkeypatch):
    """A late upstream response must not turn a stopped request into an image."""

    provider_entered = Event()
    release_provider = Event()
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c4944415408d763f8ffff3f0005fe02fe0da44d1b0000000049454e44ae426082")

    def slow_image(*_args, **_kwargs):
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return ImageResult(content=png, mime_type="image/png", provider_request_id="late-image")

    monkeypatch.setattr(workspace_api, "image_request", slow_image)

    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "stop-image")
        admin_headers = _admin_headers(client)
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
        created_channel = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Stop Image {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "image",
                "models": ["image-model"],
                "capabilities": {"image-model": ["image"]},
                "priority": 1,
            },
        )
        assert created_channel.status_code == 201
        channel_id = created_channel.json()["id"]
        thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Stop image"}).json()
        endpoint = f"/api/v1/threads/{thread['id']}/image-generations"
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(
                    client.post,
                    endpoint,
                    headers={**user_headers, "Idempotency-Key": f"image-{uuid.uuid4().hex}"},
                    json={"prompt": "生成一张 16:9 2K 高清图片", "model": "image-model", "channel_id": channel_id},
                )
                assert provider_entered.wait(timeout=5)
                stopped = client.post(f"/api/v1/threads/{thread['id']}/messages/stop", headers=user_headers)
                assert stopped.status_code == 200
                assert stopped.json()["stopped"] is True
                release_provider.set()
                result = pending.result(timeout=5)

            assert result.status_code == 409
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert [message["content_type"] for message in messages] == ["text", "text"]
            assert messages[-1]["content"] == "图片生成已停止。"
            with SessionLocal() as db:
                request = db.scalar(
                    select(ModelRequest)
                    .where(ModelRequest.thread_id == thread["id"], ModelRequest.modality == "image")
                    .order_by(ModelRequest.created_at.desc())
                )
                assert request is not None and request.status == "stopped"
                assert db.scalars(select(Asset).where(Asset.user_id == user["id"])).all() == []
        finally:
            release_provider.set()
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_stopping_text_image_tool_discards_late_provider_result(monkeypatch):
    """Stopping a parent text stream also stops its running image-tool child."""

    provider_entered = Event()
    release_provider = Event()
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c4944415408d763f8ffff3f0005fe02fe0da44d1b0000000049454e44ae426082")
    marker = '<platform_tool_call>{"name":"generate_image","arguments":{"prompt":"16:9 2K 高清直播间"}}</platform_tool_call>'

    def tool_call_text(_channel, _model, _messages, **_kwargs):
        return TextResult(chunks=iter([marker]))

    def slow_image(*_args, **_kwargs):
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return ImageResult(content=png, mime_type="image/png", provider_request_id="late-tool-image")

    monkeypatch.setattr(workspace_api, "openai_text", tool_call_text)
    monkeypatch.setattr(workspace_api, "image_request", slow_image)

    with TestClient(app) as client:
        user, user_headers = _registered_client(client, "stop-tool-image")
        admin_headers = _admin_headers(client)
        assert client.post(f"/api/v1/admin/users/{user['id']}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
        created_channel = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Stop Tool Image {uuid.uuid4().hex}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "both",
                "models": ["text-model", "image-model"],
                "capabilities": {"text-model": ["text"], "image-model": ["image"]},
                "priority": 1,
            },
        )
        assert created_channel.status_code == 201
        channel_id = created_channel.json()["id"]
        thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "Stop tool image", "model": "text-model"}).json()
        endpoint = f"/api/v1/threads/{thread['id']}/messages/stream"
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(
                    client.post,
                    endpoint,
                    headers={**user_headers, "Idempotency-Key": f"tool-{uuid.uuid4().hex}"},
                    json={"content": "生成直播间截图", "model": "text-model", "channel_id": channel_id, "enable_tools": True},
                )
                assert provider_entered.wait(timeout=5)
                stopped = client.post(f"/api/v1/threads/{thread['id']}/messages/stop", headers=user_headers)
                assert stopped.status_code == 200
                assert len(stopped.json()["request_ids"]) == 2
                release_provider.set()
                result = pending.result(timeout=5)

            assert result.status_code == 200
            assert '"stopped": true' in result.text
            assert "event: image.created" not in result.text
            messages = client.get(f"/api/v1/threads/{thread['id']}/messages", headers=user_headers).json()
            assert all(message["content_type"] != "image" for message in messages)
            with SessionLocal() as db:
                requests = db.scalars(
                    select(ModelRequest)
                    .where(ModelRequest.thread_id == thread["id"])
                    .order_by(ModelRequest.created_at.asc())
                ).all()
                assert [request.status for request in requests if request.parent_request_id is None] == ["stopped"]
                assert [request.status for request in requests if request.modality == "image"] == ["stopped"]
                assert db.scalars(select(Asset).where(Asset.user_id == user["id"])).all() == []
        finally:
            release_provider.set()
            assert client.delete(f"/api/v1/admin/model-channels/{channel_id}", headers=admin_headers).status_code == 204


def test_archived_project_cannot_receive_or_attach_threads():
    with TestClient(app) as client:
        _, user_headers = _registered_client(client, "archived-project")
        project = client.post("/api/v1/projects", headers=user_headers, json={"name": "Archive me"})
        assert project.status_code == 201
        project_id = project.json()["id"]
        archived = client.patch(f"/api/v1/projects/{project_id}", headers=user_headers, json={"archived": True})
        assert archived.status_code == 200
        assert client.post("/api/v1/threads", headers=user_headers, json={"project_id": project_id}).status_code == 404
        direct = client.post("/api/v1/threads", headers=user_headers, json={"title": "Direct"})
        assert direct.status_code == 201
        assert client.patch(f"/api/v1/threads/{direct.json()['id']}", headers=user_headers, json={"project_id": project_id}).status_code == 404
