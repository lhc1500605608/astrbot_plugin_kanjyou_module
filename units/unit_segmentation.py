"""被动回复语义分段（方案 B）单元。

与 `_dispatch_reply_segments` 的主动/内部路径解耦：本片段接管 AstrBot 通用
被动回复（LLM 结果），在发送前按语义拆成 1-N 条、逐条自发送并抑制原结果。

关键前提（Phase 0 实测）：流式输出时钩子只拿到 `STREAMING_FINISH`，无法分段；
因此启用分段时须在 `event_message_type(ALL)` 阶段关闭本事件的流式。
"""

import asyncio
import random
import re
from typing import Dict, List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

try:
    from ..config import DEFAULT_CONFIG_FLAT
except ImportError:
    from config import DEFAULT_CONFIG_FLAT


class SegmentationUnitsMixin:
    _OUTPUT_SEGMENT_MODES = ("semantic", "native_compat", "off")
    _SEG_TOKEN_RE = re.compile(r"\x00KJSEG\d+\x00")
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
    _INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
    _IMAGE_TOKEN_RE = re.compile(r"\[\[IMAGE\]\][^\n]*")
    _URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。！？；、）)】\]]+")
    _QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")

    def _output_segment_mode(self) -> str:
        raw = str(
            self.config.get(
                "output_segment_mode", DEFAULT_CONFIG_FLAT["output_segment_mode"]
            )
            or ""
        ).strip().lower()
        return raw if raw in self._OUTPUT_SEGMENT_MODES else "semantic"

    def _output_segment_active(self) -> bool:
        if not self._output_segment_enabled():
            return False
        return self._output_segment_mode() == "semantic"

    def _output_segment_private_only(self) -> bool:
        return self._to_bool(
            self.config.get("output_segment_private_only"),
            DEFAULT_CONFIG_FLAT["output_segment_private_only"],
        )

    def _output_segment_delay_min_ms(self) -> int:
        return max(
            0,
            int(
                self.config.get(
                    "output_segment_delay_min_ms",
                    DEFAULT_CONFIG_FLAT["output_segment_delay_min_ms"],
                )
            ),
        )

    def _output_segment_delay_max_ms(self) -> int:
        hi = max(
            0,
            int(
                self.config.get(
                    "output_segment_delay_max_ms",
                    DEFAULT_CONFIG_FLAT["output_segment_delay_max_ms"],
                )
            ),
        )
        return max(hi, self._output_segment_delay_min_ms())

    def _output_segment_delay_ms(self, text: str) -> int:
        lo = self._output_segment_delay_min_ms()
        hi = self._output_segment_delay_max_ms()
        if hi <= lo:
            return lo
        length = len((text or "").strip())
        ratio = min(1.0, length / 40.0)
        base = lo + (hi - lo) * ratio
        jittered = base * random.uniform(0.85, 1.15)
        return int(max(float(lo), min(float(hi), jittered)))

    def _output_segment_in_scope(self, event: AstrMessageEvent) -> bool:
        if not self._output_segment_private_only():
            return True
        return self._session_key(event).startswith("private:")

    async def _evt_segment_prepare(self, event: AstrMessageEvent):
        """`event_message_type(ALL)` 阶段：分段启用时关闭本事件流式（须最早生效）。"""
        try:
            if not self._output_segment_active():
                return
            if not self._output_segment_in_scope(event):
                return
            event.set_extra("enable_streaming", False)
        except Exception as exc:
            self._debug(f"segment prepare failed: {exc}")

    async def _evt_on_decorating_result(self, event: AstrMessageEvent):
        """发送前钩子：仅接管纯文本 LLM 结果，逐条自发送并抑制原结果。"""
        original = None
        try:
            if not self._output_segment_active():
                return
            if not self._output_segment_in_scope(event):
                return
            if event.get_extra("_kanjyou_output_seg_done"):
                return
            result = event.get_result()
            if result is None or not getattr(result, "chain", None):
                return
            is_llm = getattr(result, "is_llm_result", None)
            if not callable(is_llm) or not is_llm():
                return
            text = self._plain_chain_text(result.chain)
            if not text or not text.strip():
                return
            parts = self._build_output_segments(text)
            if len(parts) < 2:
                return
            event.set_extra("_kanjyou_output_seg_done", True)
            original = result
            event.clear_result()
            await self._send_output_segments(event, parts, original)
        except Exception as exc:
            self._debug(f"output segment failed: {exc}")
            if original is not None and event.get_result() is None:
                try:
                    event.set_result(original)
                except Exception:
                    pass

    def _plain_chain_text(self, chain) -> str:
        parts: List[str] = []
        for comp in chain or []:
            if not isinstance(comp, Plain):
                return ""
            parts.append(str(getattr(comp, "text", "") or ""))
        return "".join(parts)

    def _mask_protected(self, text: str) -> Tuple[str, Dict[str, str]]:
        mapping: Dict[str, str] = {}

        def _repl(match: re.Match) -> str:
            token = f"\x00KJSEG{len(mapping)}\x00"
            mapping[token] = match.group(0)
            return token

        out = text or ""
        for pattern in (
            self._CODE_FENCE_RE,
            self._INLINE_CODE_RE,
            self._IMAGE_TOKEN_RE,
            self._URL_RE,
            self._QUOTE_LINE_RE,
        ):
            out = pattern.sub(_repl, out)
        return out, mapping

    def _restore_protected(self, text: str, mapping: Dict[str, str]) -> Optional[str]:
        out = text or ""
        for token, value in mapping.items():
            out = out.replace(token, value)
        if self._SEG_TOKEN_RE.search(out):
            return None
        return out

    def _has_explicit_separators(self, text: str) -> bool:
        raw = text or ""
        if "||" in raw:
            return True
        return len([line for line in raw.splitlines() if line.strip()]) >= 2

    def _apply_sentence_budget(self, parts: List[str], text: str) -> List[str]:
        max_parts = self._output_segment_max_parts()
        if self._has_explicit_separators(text):
            # The model already split the reply on purpose: the complexity
            # budget may only cap the part count, never merge it down to one.
            return self._trim_reply_segments(parts, max_parts)
        level = self._complexity_level(text)
        budget = 1 if level == "simple" else (2 if level == "complex" else 3)
        cap = max(1, min(max_parts, budget))
        if len(parts) <= cap:
            return parts
        return parts[: cap - 1] + ["".join(parts[cap - 1 :])]

    def _build_output_segments(self, text: str) -> List[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        masked, mapping = self._mask_protected(raw)
        parts = self._split_reply_segments(masked)
        if not parts:
            return [raw]
        parts = self._apply_sentence_budget(parts, raw)
        parts = self._trim_reply_segments(parts, self._output_segment_max_parts())
        restored: List[str] = []
        for part in parts:
            value = self._restore_protected(part, mapping)
            if value is None:
                return [raw]
            value = value.strip()
            if value:
                restored.append(value)
        if len(restored) < 2:
            return [raw]
        joined = "".join(restored)
        # `||` is an explicit delimiter consumed by the split, not content.
        expected = raw.replace("||", "")
        if re.sub(r"\s+", "", joined) != re.sub(r"\s+", "", expected):
            return [raw]
        return restored

    async def _send_output_segments(
        self, event: AstrMessageEvent, parts: List[str], original
    ) -> None:
        sent = 0
        for index, part in enumerate(parts):
            try:
                await event.send(event.plain_result(part))
                sent += 1
            except Exception as exc:
                self._debug(f"send output segment {index} failed: {exc}")
                break
            if index < len(parts) - 1:
                delay_ms = self._output_segment_delay_ms(part)
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
        if sent == 0:
            try:
                event.set_result(original)
            except Exception:
                pass
