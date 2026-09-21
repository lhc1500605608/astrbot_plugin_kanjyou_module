"""companion-core 上下文消费（Phase 1 · 契约 v1，向后兼容 v2.4.0）。

只读拉取 ``astrbot_plugin_tcompanion_core`` 的生活 / 关系 / 动机上下文并注入
主动消息 prompt，发送后回执 ``on_proactive_outcome``。插件缺失 / 未激活 /
契约版本不符 / 超时 / 任意异常一律降级：本次不注入、不阻塞发送，行为等价
v2.4.0（fail-closed）。
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT

SUPPORTED_API_VERSION = 1
DEFAULT_COMPANION_PLUGIN_NAME = "astrbot_plugin_tcompanion_core"
COMPANION_BLOCK_HEADER = "【陪伴上下文】"

_FIELD_LABELS = (
    ("life_state", "生活"),
    ("relationship", "关系"),
    ("motivation", "动机"),
)

_LIFE_STATE_TEXT_KEYS = ("activity", "scene", "summary", "as_of", "mood_hint", "source")


def _clean_text(value) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _open_thread_labels(ctx) -> list:
    """Non-empty short labels carried by ``ctx['open_thread_details']``."""
    if not isinstance(ctx, dict):
        return []
    labels = []
    details = ctx.get("open_thread_details")
    if isinstance(details, (list, tuple)):
        for item in details:
            if not isinstance(item, dict):
                continue
            label = _clean_text(item.get("label"))
            if label:
                labels.append(label)
    return labels


def _mentions_open_thread_label(ctx, text: str) -> bool:
    """True when ``text`` embeds a short label from the follow-up candidates.

    Used to keep the motivation field label-free: an open-thread label rides the
    prompt only through the gated follow-up block, never the motivation reason
    (TMEAAA-504).
    """
    return any(label in text for label in _open_thread_labels(ctx))


def _clamp01(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def _clamp_signed(value) -> Optional[float]:
    """Clamp a numeric field into ``[-1, 1]``; ``None`` for non-numeric/NaN."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(-1.0, min(1.0, number))


def _clean_int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sanitize_companion_context(raw) -> Dict:
    """逐字段兜底 + 数值 clamp 到 [0,1]；未知键忽略，缺失字段省略。"""
    if not isinstance(raw, dict):
        return {}
    ctx: Dict = {"api_version": SUPPORTED_API_VERSION}

    life = raw.get("life_state")
    if isinstance(life, dict):
        clean_life: Dict = {}
        for key in _LIFE_STATE_TEXT_KEYS:
            text = _clean_text(life.get(key))
            if text:
                clean_life[key] = text
        energy = _clamp01(life.get("energy"))
        if energy is not None:
            clean_life["energy"] = energy
        if clean_life:
            ctx["life_state"] = clean_life

    relationship = raw.get("relationship")
    if isinstance(relationship, dict):
        clean_rel: Dict = {}
        stage = _clean_text(relationship.get("stage"))
        if stage:
            clean_rel["stage"] = stage
        affinity = _clamp01(relationship.get("affinity"))
        if affinity is not None:
            clean_rel["affinity"] = affinity
        bond = relationship.get("bond")
        if isinstance(bond, bool):
            clean_rel["bond"] = bond
        mode = _clean_text(relationship.get("mode"))
        if mode:
            clean_rel["mode"] = mode
        if clean_rel:
            ctx["relationship"] = clean_rel

    motivation = raw.get("motivation")
    if isinstance(motivation, dict):
        clean_mot: Dict = {}
        reason = _clean_text(motivation.get("reason"))
        if reason:
            clean_mot["reason"] = reason
        score = _clamp01(motivation.get("score"))
        if score is not None:
            clean_mot["score"] = score
        if clean_mot:
            ctx["motivation"] = clean_mot

    details = raw.get("open_thread_details")
    if isinstance(details, (list, tuple)):
        clean_details = []
        for item in details:
            if not isinstance(item, dict):
                continue
            thread_id = _clean_text(item.get("thread_id"))
            label = _clean_text(item.get("label"))
            if not thread_id or not label:
                continue
            clean_item: Dict = {
                "thread_id": thread_id,
                "label": label,
                "kind": _clean_text(item.get("kind")) or "topic",
                "status": _clean_text(item.get("status")) or "open",
            }
            last_seen = _clean_text(item.get("last_seen"))
            if last_seen:
                clean_item["last_seen"] = last_seen
            followup_count = _clean_int(item.get("followup_count"))
            if followup_count is not None and followup_count >= 0:
                clean_item["followup_count"] = followup_count
            confidence = _clamp01(item.get("confidence"))
            if confidence is not None:
                clean_item["confidence"] = confidence
            clean_details.append(clean_item)
        if clean_details:
            ctx["open_thread_details"] = clean_details[:5]

    quota = raw.get("quota")
    if isinstance(quota, dict):
        clean_quota: Dict = {}
        for key in ("hourly_remaining", "daily_remaining"):
            number = _clean_int(quota.get(key))
            if number is not None:
                clean_quota[key] = number
        allow = quota.get("allow")
        if isinstance(allow, bool):
            clean_quota["allow"] = allow
        if clean_quota:
            ctx["quota"] = clean_quota

    streak = _clean_int(raw.get("unanswered_streak"))
    if streak is not None and streak >= 0:
        ctx["unanswered_streak"] = streak

    emotion = raw.get("emotion_state")
    if isinstance(emotion, dict):
        clean_emotion: Dict = {}
        state = _clean_text(emotion.get("state"))
        if state:
            clean_emotion["state"] = state
        valence = _clamp_signed(emotion.get("valence"))
        if valence is not None:
            clean_emotion["valence"] = valence
        last_event = _clean_text(emotion.get("last_event"))
        if last_event:
            clean_emotion["last_event"] = last_event
        as_of = _clean_text(emotion.get("as_of"))
        if as_of:
            clean_emotion["as_of"] = as_of
        if clean_emotion:
            ctx["emotion_state"] = clean_emotion

    expression = raw.get("expression")
    if isinstance(expression, dict):
        clean_expr: Dict = {}
        mode = _clean_text(expression.get("mode"))
        if mode:
            clean_expr["mode"] = mode
        hints = expression.get("style_hints")
        if isinstance(hints, dict):
            clean_hints: Dict = {}
            tone = _clean_text(hints.get("tone"))
            if tone:
                clean_hints["tone"] = tone
            warmth = _clamp01(hints.get("warmth"))
            if warmth is not None:
                clean_hints["warmth"] = warmth
            for key in ("length_bias", "proactive_bias"):
                value = _clamp_signed(hints.get(key))
                if value is not None:
                    clean_hints[key] = value
            if clean_hints:
                clean_expr["style_hints"] = clean_hints
        reason = _clean_text(expression.get("reason"))
        if reason:
            clean_expr["reason"] = reason
        if clean_expr:
            ctx["expression"] = clean_expr

    return ctx


class CompanionContextAdapter:
    """只读桥接到 ``astrbot_plugin_tcompanion_core`` 的公共 API（契约 v1）。

    懒解析 star + 契约版本校验（主版本不符即不可用）+ ≤``timeout_sec`` 超时 +
    任意异常降级，保证主动消息生成 / 发送链路永不被 companion-core 阻塞。
    """

    def __init__(
        self,
        plugin,
        plugin_name: str = DEFAULT_COMPANION_PLUGIN_NAME,
        timeout_sec: float = 1.5,
    ):
        self._plugin = plugin
        self._plugin_name = str(plugin_name or DEFAULT_COMPANION_PLUGIN_NAME)
        self._timeout_sec = max(0.05, float(timeout_sec))
        self._info: Optional[Dict] = None

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
        return getattr(metadata, "star_cls", None)

    def _resolve_method(self, name: str):
        star = self._resolve_star()
        if star is None:
            return None
        method = getattr(star, name, None)
        return method if callable(method) else None

    async def _call(self, method, *args, **kwargs):
        return await asyncio.wait_for(
            method(*args, **kwargs), timeout=self._timeout_sec
        )

    @staticmethod
    def _version_ok(info) -> bool:
        if not isinstance(info, dict):
            return False
        version = info.get("api_version")
        return (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == SUPPORTED_API_VERSION
        )

    async def _contract_ok(self) -> bool:
        info_fn = self._resolve_method("get_contract_info")
        if info_fn is None:
            return False
        try:
            info = await self._call(info_fn)
        except Exception:
            return False
        return self._version_ok(info)

    async def contract_info(self) -> Dict:
        """Fetch + cache ``get_contract_info()``; ``{}`` when unavailable.

        Cached per adapter instance so a single consume cycle probes the
        contract at most once (``fetch_context`` + capability checks).
        """
        if self._info is not None:
            return self._info
        info_fn = self._resolve_method("get_contract_info")
        if info_fn is None:
            self._info = {}
            return self._info
        try:
            info = await self._call(info_fn)
        except Exception:
            info = {}
        self._info = info if isinstance(info, dict) else {}
        return self._info

    async def capabilities(self) -> Dict:
        """Return the contract ``capabilities`` dict; ``{}`` when unavailable."""
        info = await self.contract_info()
        if not self._version_ok(info):
            return {}
        caps = info.get("capabilities")
        return caps if isinstance(caps, dict) else {}

    async def has_capability(self, name: str) -> bool:
        return bool((await self.capabilities()).get(name))

    async def record_emotion_event(
        self,
        umo: str,
        *,
        event_type: str,
        reason: str = "",
        dedupe_key: Optional[str] = None,
    ) -> Optional[Dict]:
        """Report one emotion event; ``None`` when unavailable/degraded.

        Probes ``api_version`` + ``capabilities.emotion`` before calling, so a
        legacy companion-core (no such capability/method) is never called.
        """
        fn = self._resolve_method("record_emotion_event")
        if fn is None:
            return None
        if not await self.has_capability("emotion"):
            return None
        try:
            result = await self._call(
                fn,
                umo,
                event_type=str(event_type or ""),
                reason=str(reason or ""),
                dedupe_key=dedupe_key,
            )
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    async def fetch_context(
        self, umo: str, persona_id: Optional[str] = None
    ) -> Optional[Dict]:
        """拉取并清洗上下文；不可用时返回 ``None``（调用方按不注入处理）。"""
        ctx_fn = self._resolve_method("get_proactive_context")
        if ctx_fn is None:
            return None
        if not await self._contract_ok():
            return None
        try:
            if persona_id:
                raw = await self._call(ctx_fn, umo, persona_id=persona_id)
            else:
                raw = await self._call(ctx_fn, umo)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        ctx = sanitize_companion_context(raw)
        caps = await self.capabilities()
        if not caps.get("emotion"):
            ctx.pop("emotion_state", None)
        if not caps.get("expression"):
            ctx.pop("expression", None)
        if not caps.get("open_threads_followup"):
            ctx.pop("open_thread_details", None)
        return ctx

    async def record_open_thread(
        self,
        umo: str,
        *,
        label: str,
        kind: str,
        reason: str = "",
        dedupe_key: Optional[str] = None,
        confidence: float = 1.0,
        source: str = "",
    ) -> Optional[Dict]:
        """Report one unfinished item; ``None`` when unavailable/degraded.

        Probes ``api_version`` + ``capabilities.open_threads_followup`` before
        calling, so a legacy companion-core is never called.
        """
        fn = self._resolve_method("record_open_thread")
        if fn is None:
            return None
        if not await self.has_capability("open_threads_followup"):
            return None
        try:
            result = await self._call(
                fn,
                umo,
                label=str(label or ""),
                kind=str(kind or ""),
                reason=str(reason or ""),
                dedupe_key=dedupe_key,
                confidence=float(confidence),
                source=str(source or ""),
            )
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    async def open_threads(self, umo: str, limit: int = 3) -> list:
        """Return unfinished items (open + stale); ``[]`` when unavailable."""
        fn = self._resolve_method("get_open_threads")
        if fn is None:
            return []
        if not await self.has_capability("open_threads_followup"):
            return []
        try:
            result = await self._call(fn, umo, limit=int(limit))
        except Exception:
            return []
        return result if isinstance(result, list) else []

    async def close_open_thread(
        self, umo: str, thread_id: str, reason: str = ""
    ) -> Optional[Dict]:
        """Close one unfinished item; ``None`` when unavailable/degraded."""
        fn = self._resolve_method("close_open_thread")
        if fn is None:
            return None
        if not await self.has_capability("open_threads_followup"):
            return None
        try:
            result = await self._call(fn, umo, thread_id, reason=str(reason or ""))
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    async def mark_thread_followup(self, umo: str, thread_id: str) -> Optional[Dict]:
        """Record a follow-up of ``thread_id``; ``None`` when unavailable."""
        fn = self._resolve_method("mark_thread_followup")
        if fn is None:
            return None
        if not await self.has_capability("open_threads_followup"):
            return None
        try:
            result = await self._call(fn, umo, thread_id)
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    async def report_outcome(
        self,
        umo: str,
        *,
        sent: bool,
        reason_code: str,
        replied: bool = False,
    ) -> bool:
        """回执发送结果；失败/不可用返回 ``False``，绝不抛异常。"""
        out_fn = self._resolve_method("on_proactive_outcome")
        if out_fn is None:
            return False
        if not await self._contract_ok():
            return False
        try:
            await self._call(
                out_fn,
                umo,
                sent=bool(sent),
                reason_code=str(reason_code or ""),
                replied=bool(replied),
            )
        except Exception:
            return False
        return True


class CompanionContextUnitsMixin:
    """主动消息生成前拉取 companion-core 上下文并注入；发送后回执。"""

    def _companion_enabled(self) -> bool:
        return self._to_bool(
            self.config.get("companion_enabled"),
            DEFAULT_CONFIG_FLAT["companion_enabled"],
        )

    def _companion_plugin_name(self) -> str:
        name = _clean_text(self.config.get("companion_plugin_name"))
        return name or DEFAULT_CONFIG_FLAT["companion_plugin_name"]

    def _companion_timeout_sec(self) -> float:
        try:
            value = float(
                self.config.get(
                    "companion_timeout_sec",
                    DEFAULT_CONFIG_FLAT["companion_timeout_sec"],
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_CONFIG_FLAT["companion_timeout_sec"]
        return max(0.2, min(value, 5.0))

    def _companion_inject_enabled(self, key: str) -> bool:
        return self._to_bool(self.config.get(key), DEFAULT_CONFIG_FLAT[key])

    def _companion_adapter(self):
        injected = getattr(self, "_companion_adapter_override", None)
        if injected is not None:
            return injected
        return CompanionContextAdapter(
            self,
            plugin_name=self._companion_plugin_name(),
            timeout_sec=self._companion_timeout_sec(),
        )

    def _companion_persona_id(self) -> Optional[str]:
        return _clean_text(self.config.get("persona_id")) or None

    async def _fetch_companion_context(
        self, umo: str, persona_id: Optional[str] = None
    ) -> Dict:
        """可用则返回清洗后的上下文，否则 ``{}``（不注入）。"""
        if not self._companion_enabled():
            return {}
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"companion adapter init failed: {exc}")
            return {}
        try:
            ctx = await asyncio.wait_for(
                adapter.fetch_context(umo, persona_id=persona_id),
                timeout=self._companion_timeout_sec(),
            )
        except asyncio.TimeoutError:
            self._debug(f"companion context timeout umo={umo}")
            return {}
        except Exception as exc:
            self._debug(f"companion context failed umo={umo} err={exc}")
            return {}
        return ctx if isinstance(ctx, dict) else {}

    @staticmethod
    def _companion_life_state_text(life) -> str:
        if not isinstance(life, dict):
            return ""
        summary = _clean_text(life.get("summary"))
        if summary:
            return summary
        parts = []
        scene = _clean_text(life.get("scene"))
        if scene:
            parts.append(f"场景：{scene}")
        activity = _clean_text(life.get("activity"))
        if activity:
            parts.append(f"活动：{activity}")
        energy = life.get("energy")
        if isinstance(energy, (int, float)) and not isinstance(energy, bool):
            parts.append(f"精力 {float(energy):.2f}")
        return "，".join(parts)

    @staticmethod
    def _companion_relationship_text(rel) -> str:
        if not isinstance(rel, dict):
            return ""
        text = ""
        stage = _clean_text(rel.get("stage"))
        if stage:
            text = f"与对方关系：{stage}"
        affinity = rel.get("affinity")
        if isinstance(affinity, (int, float)) and not isinstance(affinity, bool):
            affinity_text = f"好感 {float(affinity):.2f}"
            text = f"{text}（{affinity_text}）" if text else f"与对方关系：{affinity_text}"
        if rel.get("bond"):
            text = f"{text}，已建立专属羁绊" if text else "已建立专属羁绊"
        mode = _clean_text(rel.get("mode"))
        if mode:
            text = f"{text}，相处模式：{mode}" if text else f"相处模式：{mode}"
        return text

    @staticmethod
    def _companion_motivation_text(ctx) -> str:
        if not isinstance(ctx, dict):
            return ""
        motivation = ctx.get("motivation")
        reason = ""
        if isinstance(motivation, dict):
            reason = _clean_text(motivation.get("reason"))
        if not reason:
            return ""
        # Defense-in-depth: an open-thread label may only reach the prompt via
        # the gated follow-up block. Drop a reason that embeds one (TMEAAA-504).
        if _mentions_open_thread_label(ctx, reason):
            return ""
        return f"此刻想主动联系的理由：{reason}"

    def _companion_prompt_fields(self, ctx) -> Dict[str, str]:
        """返回注入用的三个字段文本；关闭注入 / 缺失字段为空串。"""
        fields = {"life_state": "", "relationship": "", "motivation": ""}
        if not isinstance(ctx, dict) or not ctx:
            return fields
        if self._companion_inject_enabled("companion_inject_life_state"):
            fields["life_state"] = self._companion_life_state_text(ctx.get("life_state"))
        if self._companion_inject_enabled("companion_inject_relationship"):
            fields["relationship"] = self._companion_relationship_text(
                ctx.get("relationship")
            )
        if self._companion_inject_enabled("companion_inject_motivation"):
            fields["motivation"] = self._companion_motivation_text(ctx)
        return fields

    @staticmethod
    def _append_companion_block(
        prompt: str, prompt_tpl: str, fields: Dict[str, str]
    ) -> str:
        """模板缺占位符时安全追加独立块；仅追加实际存在的行。"""
        lines = []
        for key, label in _FIELD_LABELS:
            value = _clean_text(fields.get(key))
            if not value:
                continue
            if f"{{{key}}}" in prompt_tpl:
                continue
            lines.append(f"{label}：{value}")
        if not lines:
            return prompt
        return prompt + f"\n{COMPANION_BLOCK_HEADER}\n" + "\n".join(lines) + "\n"

    @staticmethod
    def _companion_quota_allow(ctx) -> Optional[bool]:
        """返回 ctx.quota.allow；缺失/非法返回 ``None``。仅作软闸提示。"""
        if not isinstance(ctx, dict):
            return None
        quota = ctx.get("quota")
        if not isinstance(quota, dict):
            return None
        allow = quota.get("allow")
        return allow if isinstance(allow, bool) else None

    def _companion_quota_soft_gate(self, session) -> bool:
        """一次性读取上一轮拉取的 ``quota.allow``：False 时软降权（非硬闸）。

        现有安全闸始终权威；companion 配额只可能降低主动概率，永不提升。
        """
        if not isinstance(session, dict):
            return False
        return session.pop("companion_quota_allow", None) is False

    async def _report_companion_outcome(
        self,
        umo: str,
        *,
        sent: bool,
        reason_code: str,
        replied: bool = False,
    ) -> bool:
        """发送结果回执；任何失败静默降级，绝不阻塞发送链路。"""
        if not self._companion_enabled() or not umo:
            return False
        try:
            adapter = self._companion_adapter()
        except Exception as exc:
            self._debug(f"companion outcome adapter init failed: {exc}")
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    adapter.report_outcome(
                        umo,
                        sent=sent,
                        reason_code=reason_code,
                        replied=replied,
                    ),
                    timeout=self._companion_timeout_sec(),
                )
            )
        except asyncio.TimeoutError:
            self._debug(f"companion outcome timeout umo={umo}")
            return False
        except Exception as exc:
            self._debug(f"companion outcome failed umo={umo} err={exc}")
            return False
