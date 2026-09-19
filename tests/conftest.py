"""Test bootstrap for astrbot_plugin_kanjyou_module.

Installs a minimal AstrBot stub (4.23.2 API surface used by the plugin) so the
compatibility tests run without a full AstrBot installation, then loads the
plugin package from the repo root.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _StubPlain:
    def __init__(self, text: str = ""):
        self.text = text
        self.type = "plain"


class _StubImage:
    def __init__(self, file: str = ""):
        self.file = file
        self.type = "image"


class _StubMessageChain:
    """Mirrors the AstrBot 4.23.2 MessageChain contract used by the plugin."""

    def __init__(self):
        self.chain = []
        self.last_method = ""

    def message(self, message: str):
        self.chain.append(_StubPlain(message))
        self.last_method = "message"
        return self

    def url_image(self, url: str):
        self.chain.append(_StubImage(file=url))
        self.last_method = "url_image"
        return self

    def file_image(self, path: str):
        self.chain.append(_StubImage(file=path))
        self.last_method = "file_image"
        return self


class _StubAstrMessageEvent:
    def __init__(self, message_str: str = "", message_obj=None, umo: str = ""):
        self.message_str = message_str
        self.message_obj = message_obj
        self.unified_msg_origin = umo
        self.call_llm = False
        self._stopped = False

    def get_sender_id(self) -> str:
        sender = getattr(self.message_obj, "sender", None)
        return str(getattr(sender, "user_id", "") or "")

    def plain_result(self, text: str):
        return _StubPlain(text)

    async def send(self, chain):
        return None

    def stop_event(self):
        self._stopped = True

    def should_call_llm(self, value: bool):
        self.call_llm = value


class _StubFilter:
    class EventMessageType:
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"
        OTHER_MESSAGE = "other"
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    @staticmethod
    def _decorator_factory(*_args, **_kwargs):
        def decorator(func):
            return func

        return decorator

    event_message_type = _decorator_factory
    after_message_sent = _decorator_factory
    command = _decorator_factory
    permission_type = _decorator_factory
    regex = _decorator_factory


def _install_astrbot_stubs() -> None:
    for name in list(sys.modules):
        if name == "astrbot" or name.startswith("astrbot."):
            del sys.modules[name]

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.__path__ = []
    api_mod = types.ModuleType("astrbot.api")
    api_mod.__path__ = []
    event_mod = types.ModuleType("astrbot.api.event")
    message_components_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class _StubAstrBotConfig(dict):
        def save_config(self, replace_config=None):
            return None

    class _StubContext:
        def __init__(self):
            self.persona_manager = None
            self.provider_manager = None
            self.registered_web_apis = []

        def register_web_api(self, route, view_handler, methods, desc):
            self.registered_web_apis.append((route, view_handler, methods, desc))

    class _StubStar:
        def __init__(self, context, config=None):
            self.context = context

        async def text_to_image(self, text: str, return_url: bool = True) -> str:
            return ""

    def _register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    api_mod.logger = logging.getLogger("astrbot")
    api_mod.AstrBotConfig = _StubAstrBotConfig
    event_mod.AstrMessageEvent = _StubAstrMessageEvent
    event_mod.MessageChain = _StubMessageChain
    event_mod.filter = _StubFilter()
    message_components_mod.Plain = _StubPlain
    star_mod.Context = _StubContext
    star_mod.Star = _StubStar
    star_mod.register = _register

    web_mod = types.ModuleType("astrbot.api.web")

    class _StubPluginMultiDict:
        def __init__(self, data=None):
            self._data = data or {}

        def get(self, key, default=None, type=None):
            value = self._data.get(key, default)
            if type is not None and value is not default:
                try:
                    return type(value)
                except (TypeError, ValueError):
                    return default
            return value

    class _StubPluginRequest:
        """Minimal request body/query surface used by plugin Web handlers."""

        def __init__(self, payload=None, query=None):
            self._payload = payload if isinstance(payload, dict) else {}
            self.query = _StubPluginMultiDict(query)

        async def json(self, default=None):
            return self._payload if self._payload else default

    def _stub_json_response(data=None, **_kwargs):
        return data

    def _stub_error_response(message, **_kwargs):
        return {"status": "error", "message": message}

    def _stub_stream_response(content, **_kwargs):
        return {
            "__stream__": content,
            "content_type": _kwargs.get("content_type"),
        }

    web_mod.request = _StubPluginRequest()
    web_mod.json_response = _stub_json_response
    web_mod.error_response = _stub_error_response
    web_mod.stream_response = _stub_stream_response

    astrbot_mod.api = api_mod
    astrbot_mod.logger = logging.getLogger("astrbot")

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = message_components_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.web"] = web_mod


def _load_package_module(module_name: str, file_name: str):
    pkg_name = "astrbot_plugin_kanjyou_module"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = package

    full_name = f"{pkg_name}.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, ROOT / file_name)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin_module():
    _install_astrbot_stubs()
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    return _load_package_module("main", "main.py")


@pytest.fixture()
def plugin(plugin_module, tmp_path):
    instance = plugin_module.KanjyouIdleProactivePlugin(
        context=plugin_module.Context(), config={}
    )
    instance._state_path = tmp_path / "idle_state.json"
    return instance
