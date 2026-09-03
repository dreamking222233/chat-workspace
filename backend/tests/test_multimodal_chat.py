from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api import workspace as workspace_api
from app.db.session import SessionLocal
from app.models import Asset, Message, ModelChannel
from app.schemas.common import SendMessageRequest, TextMediaInput
from app.schemas.channels import ModelChannelCreate, ModelChannelUpdate
from app.services.providers import ProviderTextRequest, TextResult, build_chat_payload, build_responses_payload


def image_data_url() -> str:
    # JPEG SOI/EOI plus a small payload is enough for the contract's file
    # signature check; provider integration is mocked below.
    return "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfffixture-image\xff\xd9").decode()


def png_data_url(payload: bytes = b"fixture-image") -> str:
    signature = b"\x89PNG\r\n\x1a\n"
    return "data:image/png;base64," + base64.b64encode(signature + payload).decode()


def test_channel_level_visual_policy_arrays_round_trip_without_modality_filtering():
    create = ModelChannelCreate(
        name="visual-policy",
        base_url="https://provider.example/v1",
        modality="text",
        capabilities={
            "supports_input_image": True,
            "supported_input_image_mime_types": ["image/png"],
            "image_url_hosts": ["cdn.example"],
            "_text_endpoint": "responses",
            "vision-model": ["text"],
        },
    )
    assert create.capabilities["supported_input_image_mime_types"] == ["image/png"]
    assert create.capabilities["image_url_hosts"] == ["cdn.example"]
    assert create.capabilities["_text_endpoint"] == "responses"
    assert create.capabilities["vision-model"] == ["text"]

    update = ModelChannelUpdate(capabilities=create.capabilities)
    assert update.capabilities == create.capabilities


def test_automatic_visual_route_falls_back_from_a_text_only_thread_model():
    with SessionLocal() as db:
        suffix = uuid.uuid4().hex
        text_only = ModelChannel(
            name=f"text-only-{suffix}",
            base_url="https://text-only.example/v1",
            modality="text",
            priority=1,
            models_json=[f"plain-{suffix}"],
            capabilities_json={"supports_input_image": False},
        )
        vision = ModelChannel(
            name=f"vision-fallback-{suffix}",
            base_url="https://vision.example/v1",
            modality="text",
            priority=2,
            models_json=[f"vision-{suffix}"],
            capabilities_json={"supports_input_image": True},
        )
        db.add_all([text_only, vision])
        db.commit()
        model, channel = workspace_api.resolve_text_channel(
            db,
            requested_model=None,
            thread_model=text_only.models_json[0],
            channel_id=None,
            require_vision=True,
        )
        assert model == vision.models_json[0]
        assert channel.id == vision.id


def test_automatic_visual_route_checks_later_channel_when_model_id_is_shared():
    with SessionLocal() as db:
        suffix = uuid.uuid4().hex
        model = f"shared-{suffix}"
        text_only = ModelChannel(
            name=f"shared-text-only-{suffix}",
            base_url="https://text-only.example/v1",
            modality="text",
            priority=1,
            models_json=[model],
            capabilities_json={"supports_input_image": False},
        )
        vision = ModelChannel(
            name=f"shared-vision-{suffix}",
            base_url="https://vision.example/v1",
            modality="text",
            priority=2,
            models_json=[model],
            capabilities_json={"supports_input_image": True},
        )
        db.add_all([text_only, vision])
        db.commit()
        selected_model, selected_channel = workspace_api.resolve_text_channel(
            db,
            requested_model=None,
            thread_model=None,
            channel_id=None,
            require_vision=True,
        )
        assert selected_model == model
        assert selected_channel.id == vision.id


def test_asset_upload_creates_a_missing_runtime_directory(monkeypatch, tmp_path: Path):
    from fastapi.testclient import TestClient
    from app.main import app

    storage_dir = tmp_path / "new-runtime-volume" / "assets"
    monkeypatch.setattr(workspace_api.get_settings(), "storage_dir", str(storage_dir))
    with TestClient(app) as client:
        suffix = uuid.uuid4().hex
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"upload-{suffix}@example.com", "password": "password123", "display_name": "Upload"},
        )
        assert registered.status_code == 201
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        uploaded = client.post(
            "/api/v1/assets/upload",
            headers=headers,
            files={"file": ("fixture.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")},
        )
        assert uploaded.status_code == 200
        assert storage_dir.is_dir()
        with SessionLocal() as db:
            asset = db.get(Asset, uploaded.json()["id"])
            assert asset is not None
            assert Path(asset.storage_key).is_file()


def test_text_media_input_validates_data_url_signature_and_size():
    item = TextMediaInput(
        data_url=image_data_url(),
        asset_id=" asset-1 ",
        mime_type="image/jpeg",
        width=640,
        height=480,
    )
    assert item.asset_id == "asset-1"
    assert item.decoded_size > 0
    with pytest.raises(ValidationError):
        TextMediaInput(
            data_url="data:image/jpeg;base64," + base64.b64encode(b"not-an-image").decode(),
            asset_id="asset-1",
            mime_type="image/jpeg",
        )

    with pytest.raises(ValidationError):
        TextMediaInput(data_url=image_data_url(), asset_id="asset-1", mime_type="image/png")


def test_send_message_merges_media_asset_ids_and_enforces_total_encoded_limit():
    first = TextMediaInput(data_url=image_data_url(), asset_id="asset-1", mime_type="image/jpeg")
    request = SendMessageRequest(content="看图", media_inputs=[first])
    assert request.asset_ids == ["asset-1"]

    # Keep every individual item within the per-image bound while exceeding
    # the aggregate bound, matching the browser's multi-image contract.
    raw = b"\xff\xd8\xff" + b"a" * (1_179_648 - 3)
    encoded = base64.b64encode(raw).decode()
    assert len(encoded) == 1_572_864
    large_url = "data:image/jpeg;base64," + encoded
    with pytest.raises(ValidationError, match="3 MiB"):
        SendMessageRequest(
            content="看多图",
            media_inputs=[
                {"data_url": large_url, "asset_id": "asset-1", "mime_type": "image/jpeg"},
                {"data_url": large_url, "asset_id": "asset-2", "mime_type": "image/jpeg"},
                {"data_url": large_url, "asset_id": "asset-3", "mime_type": "image/jpeg"},
            ],
        )


def test_provider_payloads_attach_visual_parts_to_last_user_message():
    request = ProviderTextRequest(
        model="vision-model",
        messages=[
            {"role": "user", "content": "请描述图片"},
        ],
    )
    media = [{"data_url": image_data_url(), "mime_type": "image/jpeg", "detail": "high"}]
    chat_request = ProviderTextRequest(
        model=request.model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": media[0]["data_url"], "detail": "high"}},
                {"type": "text", "text": "请描述图片"},
            ],
        }],
    )
    chat = build_chat_payload(chat_request)
    assert chat["messages"][0]["content"][0]["type"] == "image_url"
    responses = build_responses_payload(ProviderTextRequest(model=request.model, messages=chat["messages"]))
    assert responses["input"][0]["content"][0]["type"] == "input_image"
    assert responses["input"][0]["content"][1] == {"type": "input_text", "text": "请描述图片"}


def test_responses_payload_preserves_multi_turn_function_calls_with_visual_input():
    payload = build_responses_payload(
        ProviderTextRequest(
            model="vision-model",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url(), "detail": "auto"}},
                        {"type": "text", "text": "编辑这张图"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "generate_image", "arguments": '{"prompt":"编辑"}'}}],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
                {"role": "user", "content": "确认结果"},
            ],
        ),
    )
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][3] == {"role": "user", "content": "确认结果"}
    assert payload["input"][0]["content"][0]["type"] == "input_image"
    assert payload["input"][1] == {"type": "function_call", "call_id": "call-1", "name": "generate_image", "arguments": '{"prompt":"编辑"}'}
    assert payload["input"][2] == {"type": "function_call_output", "call_id": "call-1", "output": '{"ok":true}'}


def test_text_stream_forwards_media_to_provider_without_persisting_base64(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        suffix = uuid.uuid4().hex
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": f"vision-{suffix}@example.com", "password": "password123", "display_name": "Vision"},
        )
        assert registered.status_code == 201
        body = registered.json()
        user_headers = {"Authorization": f"Bearer {body['access_token']}"}
        admin_login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        assert client.post(
            f"/api/v1/admin/users/{body['user']['id']}/entitlements",
            headers=admin_headers,
            json={"months": 1},
        ).status_code == 200
        model = f"vision-{suffix}"
        channel = client.post(
            "/api/v1/admin/model-channels",
            headers=admin_headers,
            json={
                "name": f"Vision {suffix}",
                "base_url": "https://provider.example/v1",
                "api_key": "TOKEN",
                "modality": "text",
                "models": [model],
                "capabilities": {model: ["text"], "supports_input_image": True},
            },
        )
        assert channel.status_code == 201
        thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "视觉", "model": model}).json()
        upload = client.post(
            "/api/v1/assets/upload",
            headers=user_headers,
            files={"file": ("fixture.jpg", b"\xff\xd8\xfffixture-image\xff\xd9", "image/jpeg")},
        )
        assert upload.status_code == 200
        asset_id = upload.json()["id"]
        captured: list[dict] = []

        def fake_text(_channel, selected_model, messages, **kwargs):
            captured.append({"model": selected_model, "messages": messages, "kwargs": kwargs})
            from app.services.providers import TextResult
            return TextResult(chunks=iter(["已识别"]), provider_request_id="provider-vision")

        monkeypatch.setattr(workspace_api, "openai_text", fake_text)
        response = client.post(
            f"/api/v1/threads/{thread['id']}/messages/stream",
            headers={**user_headers, "Idempotency-Key": f"vision-{suffix}"},
            json={
                "content": "请描述这张图",
                "model": model,
                "channel_id": channel.json()["id"],
                "asset_ids": [asset_id],
                "media_inputs": [{"data_url": image_data_url(), "asset_id": asset_id, "mime_type": "image/jpeg", "width": 2, "height": 2}],
            },
        )
        assert response.status_code == 200
        assert "已识别" in response.text
        sent = captured[0]["messages"][-1]
        assert sent["content"][0]["type"] == "image_url"
        assert sent["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        # The manifest remains alongside the pixel parts so a later image
        # tool call can refer to the uploaded asset without persisting pixels.
        assert asset_id in sent["content"][-1]["text"]
        with SessionLocal() as db:
            messages = db.scalars(select(Message).where(Message.thread_id == thread["id"]).order_by(Message.sequence.asc())).all()
            user_message = next(item for item in messages if item.role == "user")
            assert image_data_url() not in json.dumps(user_message.content_json or {}, ensure_ascii=False)
        assert client.delete(f"/api/v1/admin/model-channels/{channel.json()['id']}", headers=admin_headers).status_code == 204


def _vision_fixture(client, prefix: str, capabilities: dict):
    """Create an entitled user, a text channel and one uploaded image."""
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": f"{prefix}-{uuid.uuid4().hex}@example.com", "password": "password123", "display_name": prefix.title()},
    )
    assert registered.status_code == 201
    body = registered.json()
    user_headers = {"Authorization": f"Bearer {body['access_token']}"}
    admin_login = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "change-me-now"})
    assert admin_login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    assert client.post(
        f"/api/v1/admin/users/{body['user']['id']}/entitlements",
        headers=admin_headers,
        json={"months": 1},
    ).status_code == 200
    model = f"{prefix}-model-{uuid.uuid4().hex}"
    channel = client.post(
        "/api/v1/admin/model-channels",
        headers=admin_headers,
        json={
            "name": f"{prefix} channel {uuid.uuid4().hex}",
            "base_url": "https://provider.example/v1",
            "api_key": "TOKEN",
            "modality": "text",
            "models": [model],
            "capabilities": {model: ["text"], **capabilities},
        },
    )
    assert channel.status_code == 201
    thread = client.post("/api/v1/threads", headers=user_headers, json={"title": "视觉边界", "model": model}).json()
    upload = client.post(
        "/api/v1/assets/upload",
        headers=user_headers,
        files={"file": ("fixture.jpg", b"\xff\xd8\xfffixture-image\xff\xd9", "image/jpeg")},
    )
    assert upload.status_code == 200
    return body["user"]["id"], user_headers, admin_headers, model, channel.json()["id"], thread["id"], upload.json()["id"]


def test_text_stream_rejects_channel_without_visual_capability(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    provider_called = False

    def fake_text(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return TextResult(chunks=iter(["不应调用"]))

    monkeypatch.setattr(workspace_api, "openai_text", fake_text)
    with TestClient(app) as client:
        _, user_headers, _admin_headers, model, channel_id, thread_id, asset_id = _vision_fixture(
            client,
            "vision-disabled",
            {"supports_input_image": False},
        )
        response = client.post(
            f"/api/v1/threads/{thread_id}/messages/stream",
            headers={**user_headers, "Idempotency-Key": f"disabled-{uuid.uuid4().hex}"},
            json={
                "content": "描述图片",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [asset_id],
                "media_inputs": [{"data_url": image_data_url(), "asset_id": asset_id, "mime_type": "image/jpeg"}],
            },
        )
        assert response.status_code == 422
        assert not provider_called
        assert client.get(f"/api/v1/threads/{thread_id}/messages", headers=user_headers).json() == []


def test_model_level_visual_limits_are_enforced_and_exposed():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        _, user_headers, _admin_headers, model, channel_id, thread_id, asset_id = _vision_fixture(
            client,
            "vision-limits",
            {"supports_input_image": True},
        )
        # Update the channel with a model-keyed policy after the helper has
        # generated the model ID.
        admin_channel = client.get("/api/v1/admin/model-channels", headers=_admin_headers).json()
        current = next(item for item in admin_channel if item["id"] == channel_id)
        policy = {
            **(current.get("capabilities") or {}),
            model: {
                "capabilities": ["text"],
                "supports_input_image": True,
                "max_input_images": 1,
                "input_image_max_bytes": 5,
                "supported_input_image_mime_types": ["image/png"],
            },
        }
        updated = client.patch(f"/api/v1/admin/model-channels/{channel_id}", headers={**_admin_headers, "Content-Type": "application/json"}, json={"capabilities": policy})
        assert updated.status_code == 200
        option = next(item for item in client.get("/api/v1/models", headers=user_headers).json() if item["model"] == model)
        assert option["supports_input_image"] is True
        assert option["max_input_images"] == 1
        assert option["input_image_max_bytes"] == 5
        assert option["supported_input_image_mime_types"] == ["image/png"]

        second = client.post(
            "/api/v1/assets/upload",
            headers=user_headers,
            files={"file": ("second.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")},
        )
        assert second.status_code == 200
        second_id = second.json()["id"]
        too_many = client.post(
            f"/api/v1/threads/{thread_id}/messages/stream",
            headers={**user_headers, "Idempotency-Key": f"too-many-{uuid.uuid4().hex}"},
            json={
                "content": "看两张",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [asset_id, second_id],
                "media_inputs": [
                    {"data_url": png_data_url(b"a"), "asset_id": asset_id, "mime_type": "image/png"},
                    {"data_url": png_data_url(b"b"), "asset_id": second_id, "mime_type": "image/png"},
                ],
            },
        )
        assert too_many.status_code == 422

        wrong_mime = client.post(
            f"/api/v1/threads/{thread_id}/messages/stream",
            headers={**user_headers, "Idempotency-Key": f"wrong-mime-{uuid.uuid4().hex}"},
            json={
                "content": "看 JPEG",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [asset_id],
                "media_inputs": [{"data_url": image_data_url(), "asset_id": asset_id, "mime_type": "image/jpeg"}],
            },
        )
        assert wrong_mime.status_code == 415

        too_large = client.post(
            f"/api/v1/threads/{thread_id}/messages/stream",
            headers={**user_headers, "Idempotency-Key": f"too-large-{uuid.uuid4().hex}"},
            json={
                "content": "看大图",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [second_id],
                "media_inputs": [{"data_url": png_data_url(b"0123456789"), "asset_id": second_id, "mime_type": "image/png"}],
            },
        )
        assert too_large.status_code == 413


def test_text_visual_input_checks_asset_ownership_and_image_type():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        _owner_id, owner_headers, admin_headers, model, channel_id, owner_thread_id, owner_asset_id = _vision_fixture(
            client,
            "vision-owner",
            {},
        )
        other = client.post(
            "/api/v1/auth/register",
            json={"email": f"other-{uuid.uuid4().hex}@example.com", "password": "password123", "display_name": "Other"},
        )
        assert other.status_code == 201
        other_body = other.json()
        other_headers = {"Authorization": f"Bearer {other_body['access_token']}"}
        assert client.post(f"/api/v1/admin/users/{other_body['user']['id']}/entitlements", headers=admin_headers, json={"months": 1}).status_code == 200
        other_thread = client.post("/api/v1/threads", headers=other_headers, json={"title": "越权", "model": model}).json()
        denied = client.post(
            f"/api/v1/threads/{other_thread['id']}/messages/stream",
            headers={**other_headers, "Idempotency-Key": f"owner-{uuid.uuid4().hex}"},
            json={
                "content": "读取别人的图",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [owner_asset_id],
                "media_inputs": [{"data_url": image_data_url(), "asset_id": owner_asset_id, "mime_type": "image/jpeg"}],
            },
        )
        assert denied.status_code == 404

        document = client.post(
            "/api/v1/assets/upload",
            headers=owner_headers,
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )
        assert document.status_code == 200
        document_id = document.json()["id"]
        invalid_type = client.post(
            f"/api/v1/threads/{owner_thread_id}/messages/stream",
            headers={**owner_headers, "Idempotency-Key": f"type-{uuid.uuid4().hex}"},
            json={
                "content": "读取文档",
                "model": model,
                "channel_id": channel_id,
                "asset_ids": [document_id],
                "media_inputs": [{"data_url": image_data_url(), "asset_id": document_id, "mime_type": "image/jpeg"}],
            },
        )
        assert invalid_type.status_code == 415
