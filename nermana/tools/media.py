from __future__ import annotations

import base64
from pathlib import Path

from nermana.config import AppConfig
from nermana.http_client import post_json
from nermana.tooling import Tool, ToolRegistry
from nermana.tools.files import _safe_path


def register_media_tools(registry: ToolRegistry, config: AppConfig) -> None:
    def image_available() -> tuple[bool, str]:
        if not config.providers.image_enabled:
            return False, "image provider disabled"
        if not config.providers.image_endpoint:
            return False, "set image endpoint"
        return True, "configured"

    def vision_available() -> tuple[bool, str]:
        if not config.providers.vision_enabled:
            return False, "vision provider disabled"
        if not config.providers.vision_endpoint:
            return False, "set vision endpoint"
        return True, "configured"

    def generate_image(payload: dict) -> dict:
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            return {"ok": False, "error": "prompt is required"}
        headers = _auth_header(config.providers.image_api_key)
        response = post_json(
            config.providers.image_endpoint,
            {"prompt": prompt, "size": payload.get("size", "1024x1024")},
            timeout=config.providers.timeout_seconds,
            headers=headers,
        )
        if not response.ok:
            return {"ok": False, "error": f"image provider unavailable: {response.error}"}
        return {"ok": True, "provider_response": response.data}

    def vision_analyze(payload: dict) -> dict:
        question = str(payload.get("question", "Describe this image.")).strip()
        image_url = str(payload.get("image_url", "")).strip()
        image_path = str(payload.get("path", "")).strip()
        body = {"question": question}
        if image_url:
            body["image_url"] = image_url
        elif image_path:
            path = _safe_path(config, image_path)
            body["image_base64"] = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            body["filename"] = Path(path).name
        else:
            return {"ok": False, "error": "image_url or path is required"}
        response = post_json(
            config.providers.vision_endpoint,
            body,
            timeout=config.providers.timeout_seconds,
            headers=_auth_header(config.providers.vision_api_key),
        )
        if not response.ok:
            return {"ok": False, "error": f"vision provider unavailable: {response.error}"}
        return {"ok": True, "provider_response": response.data}

    registry.register(
        Tool(
            name="generate_image",
            description="Generate an image through a configured provider.",
            provider="image",
            input_schema={"type": "object", "properties": {"prompt": {"type": "string"}, "size": {"type": "string"}}},
            online_required=True,
            risk="read",
            timeout_seconds=config.providers.timeout_seconds,
            handler=generate_image,
            availability=image_available,
        )
    )
    registry.register(
        Tool(
            name="vision_analyze",
            description="Analyze an image through a configured vision provider.",
            provider="vision",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "image_url": {"type": "string"}, "question": {"type": "string"}},
            },
            online_required=True,
            risk="read",
            timeout_seconds=config.providers.timeout_seconds,
            handler=vision_analyze,
            availability=vision_available,
        )
    )


def _auth_header(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}
