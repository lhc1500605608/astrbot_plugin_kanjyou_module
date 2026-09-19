import asyncio
from typing import Dict, List, Optional

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT


class MemoryRecallAdapter:
    """只读桥接到 ``astrbot_plugin_tmemory`` 的公共召回 API。

    通过 star 注册表懒解析 tmemory 实例并调用 ``recall_for_prompt``；
    插件缺失 / 未激活 / API 不存在 / 超时 / 任意异常一律降级为 ``[]``，
    保证主动消息生成链路永不被记忆召回阻塞。
    """

    def __init__(
        self,
        plugin,
        plugin_name: str = "astrbot_plugin_tmemory",
        timeout_sec: float = 2.0,
    ):
        self._plugin = plugin
        self._plugin_name = plugin_name
        self._timeout_sec = max(0.05, float(timeout_sec))

    def _resolve_star(self):
        context = getattr(self._plugin, "context", None)
        getter = getattr(context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(self._plugin_name)
        except Exception:
            return None
        if metadata is None or not getattr(metadata, "activated", True):
            return None
        star = getattr(metadata, "star_cls", None)
        return star

    async def recall(
        self,
        umo: str,
        query: str,
        session_type: str = "private",
        limit: int = 3,
    ) -> List[str]:
        star = self._resolve_star()
        if star is None:
            return []
        recall_fn = getattr(star, "recall_for_prompt", None)
        if not callable(recall_fn):
            return []
        try:
            result = await asyncio.wait_for(
                recall_fn(umo, query, session_type=session_type, limit=limit),
                timeout=self._timeout_sec,
            )
        except Exception:
            return []
        if not isinstance(result, (list, tuple)):
            return []
        memories: List[str] = []
        for item in result:
            text = str(item or "").strip()
            if text:
                memories.append(text)
        return memories


class MemoryRecallUnitsMixin:
    """主动消息生成前按 persona/session 召回 tmemory 记忆并注入 prompt。"""

    def _memory_recall_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("memory_recall_enabled"),
            DEFAULT_CONFIG_FLAT["memory_recall_enabled"],
        )

    def _memory_recall_limit(self) -> int:
        try:
            value = int(
                self.config.get(
                    "memory_recall_limit", DEFAULT_CONFIG_FLAT["memory_recall_limit"]
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_CONFIG_FLAT["memory_recall_limit"]
        return max(1, min(value, 20))

    def _memory_recall_timeout_sec(self) -> float:
        try:
            value = float(
                self.config.get(
                    "memory_recall_timeout_sec",
                    DEFAULT_CONFIG_FLAT["memory_recall_timeout_sec"],
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_CONFIG_FLAT["memory_recall_timeout_sec"]
        return max(0.1, min(value, 5.0))

    def _memory_recall_private_only(self) -> bool:
        return self._to_bool(
            self.config.get("memory_recall_private_only"),
            DEFAULT_CONFIG_FLAT["memory_recall_private_only"],
        )

    def _memory_recall_group_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("memory_recall_group_enabled"),
            DEFAULT_CONFIG_FLAT["memory_recall_group_enabled"],
        )

    def _memory_recall_plugin_name(self) -> str:
        name = str(
            self.config.get(
                "memory_recall_plugin_name",
                DEFAULT_CONFIG_FLAT["memory_recall_plugin_name"],
            )
            or ""
        ).strip()
        return name or DEFAULT_CONFIG_FLAT["memory_recall_plugin_name"]

    def _memory_recall_adapter(self):
        injected = getattr(self, "_memory_recall_adapter_override", None)
        if injected is not None:
            return injected
        return MemoryRecallAdapter(
            self,
            plugin_name=self._memory_recall_plugin_name(),
            timeout_sec=self._memory_recall_timeout_sec(),
        )

    def _memory_recall_time_span(self, now=None) -> str:
        now = now or self._now()
        hour = now.hour
        if 5 <= hour < 12:
            return "上午"
        if 12 <= hour < 14:
            return "中午"
        if 14 <= hour < 18:
            return "下午"
        if 18 <= hour < 23:
            return "晚上"
        return "深夜"

    def _build_memory_recall_query(
        self,
        session_key: str,
        idle_sec: float,
        env_perception: str = "",
        style_hint: str = "",
    ) -> str:
        # 零额外 LLM 调用：纯拼装时段 / 环境 / 语气关键词。
        session_type = "私聊" if str(session_key).startswith("private:") else "群聊"
        parts = [self._memory_recall_time_span(), session_type]
        if style_hint:
            parts.append(str(style_hint).strip())
        env = str(env_perception or "").strip()
        if env:
            parts.append(env)
        query = " ".join(p for p in parts if p)
        return query[:100]

    async def _recall_memory_for_prompt(
        self,
        unified_msg_origin: str,
        session_key: str,
        idle_sec: float,
        session: Optional[Dict] = None,
        env_perception: str = "",
        style_hint: str = "",
    ) -> str:
        """召回记忆并格式化为 prompt 片段；无记忆 / 不可用时返回 ``"无"``。"""
        if not self._memory_recall_enabled():
            return "无"

        is_group = str(session_key).startswith("group:")
        if is_group and (self._memory_recall_private_only() or not self._memory_recall_group_enabled()):
            return "无"
        session_type = "group" if is_group else "private"

        query = self._build_memory_recall_query(
            session_key, idle_sec, env_perception, style_hint
        )
        if not query:
            return "无"

        try:
            adapter = self._memory_recall_adapter()
        except Exception as exc:
            self._debug(f"memory recall adapter init failed: {exc}")
            return "无"

        try:
            memories = await asyncio.wait_for(
                adapter.recall(
                    unified_msg_origin,
                    query,
                    session_type=session_type,
                    limit=self._memory_recall_limit(),
                ),
                timeout=self._memory_recall_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(
                f"memory recall timeout session={session_key} "
                f"timeout={self._memory_recall_timeout_sec()}s"
            )
            return "无"
        except Exception as exc:
            self._debug(f"memory recall failed session={session_key} err={exc}")
            return "无"

        if not memories:
            return "无"
        return "\n".join(f"- {text}" for text in memories)
