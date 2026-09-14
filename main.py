"""QQ message editor for AstrBot v4.24.5.

Commands:
- /resend {content}: remove the last turn and re-dispatch edited content.
- /edit [f|a] [{content}]: inspect or replace the last user/assistant text.
- /patch [f|a] [{content}]: alias of /edit.
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


RESEND_PREFIX = "【重发】"

COMMAND_RE = re.compile(
    r"^\s*/(?P<command>resend|edit|patch)(?P<tail>(?:\s+[\s\S]*)?)$",
    flags=re.IGNORECASE,
)
PAYLOAD_RE = re.compile(r"\{([\s\S]*)\}\s*$")

TARGET_ALIASES = {
    "": "f",
    "f": "f",
    "a": "a",
}

RESEND_USAGE = (
    "主人，把修改后的完整内容放进大括号里交给咪吧：🐈‍⬛"
)
RESEND_TARGET_USAGE = (
    "主人，`/resend` 只会重发主人的消息，不用写 f：\n"
    "/resend {新内容} 🐾"
)
RESEND_ACCEPTED = (
    "收到啦，主人。咪已经把上一轮收回来，正在重新发送……🐾"
)
RESEND_FAILED = (
    "呜，咪没有成功收回上一轮，原来的对话没有变化……💦"
)
EMPTY_CONTENT = "主人，大括号里还是空的，咪没有改动原来的消息……🐾"
NO_CONVERSATION = "呜，咪没有找到当前对话，原来的内容没有变化……💦"
NO_USER_MESSAGE = "呜，咪没有找到可以修改的主人消息……💦"
NO_ASSISTANT_MESSAGE = "呜，咪没有找到可以修改的回复……💦"
UNSUPPORTED_PLATFORM = "主人，这组指令目前只在 OneBot QQ 私聊里工作哦。🐈‍⬛"
EDIT_FAILED = "呜，咪没有改成功，原来的消息没有变化……💦"


@dataclass
class ParsedCommand:
    command: str
    original_command: str
    target: str | None
    payload: str | None
    has_payload: bool
    invalid_target: bool = False


@dataclass
class ConversationState:
    cid: str
    history: list[dict[str, Any]]


@register(
    "astrbot_plugin_message_editor",
    "Noir & Felis Abyssalis",
    "在 QQ 中查看、修改或重发最后一轮对话",
    "1.0.1",
    "https://github.com/EmilyCheoh/astrbot_plugin_message_editor",
)
class MessageEditorPlugin(Star):
    """Edit the active QQ conversation without bypassing AstrBot's pipeline."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.context = context

    # Standard command handlers keep the commands visible to AstrBot's command
    # registry. The high-priority fallback below handles mixed-case spellings.
    @filter.command("resend")
    async def resend_command(self, event: AstrMessageEvent):
        """撤回最后一轮，并用大括号中的新内容重新生成。"""
        reply = await self._execute_command(event)
        if reply:
            yield event.plain_result(reply)

    @filter.command("edit", alias={"patch"})
    async def edit_command(self, event: AstrMessageEvent):
        """查看或直接修改最后一条主人/Abyss消息，不触发模型回复。"""
        reply = await self._execute_command(event)
        if reply:
            yield event.plain_result(reply)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=11000)
    async def mixed_case_command_fallback(self, event: AstrMessageEvent):
        """Handle /Edit, /PATCH and other mixed-case spellings."""
        parsed = self._parse_command(self._raw_message(event))
        if not parsed or parsed.original_command == parsed.command:
            return

        reply = await self._execute_command(event, parsed=parsed)
        if reply:
            yield event.plain_result(reply)

    @filter.on_llm_request(priority=-499)
    async def strip_internal_resend_marker(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """Ensure the internal resend marker never reaches the model/history.

        Lorebook consumes this marker at priority -498 to rewind its turn state.
        This later fallback also removes it when Lorebook is disabled or empty.
        """
        if isinstance(req.prompt, str) and req.prompt.startswith(RESEND_PREFIX):
            req.prompt = req.prompt[len(RESEND_PREFIX) :]

    async def _execute_command(
        self,
        event: AstrMessageEvent,
        *,
        parsed: ParsedCommand | None = None,
    ) -> str | None:
        parsed = parsed or self._parse_command(self._raw_message(event))
        if not parsed:
            return None

        # Stop the command itself from falling through to the LLM/history.
        event.stop_event()

        if event.get_platform_name() != "aiocqhttp" or not event.is_private_chat():
            return UNSUPPORTED_PLATFORM

        if parsed.command == "resend":
            return await self._handle_resend(event, parsed)
        return await self._handle_direct_edit(event, parsed)

    async def _handle_resend(
        self,
        event: AstrMessageEvent,
        parsed: ParsedCommand,
    ) -> str:
        if not parsed.has_payload:
            await self._send_resend_usage(event)
            return ""
        if parsed.invalid_target or parsed.target not in (None, "f"):
            return RESEND_TARGET_USAGE
        if parsed.payload is None or not parsed.payload.strip():
            return EMPTY_CONTENT

        try:
            state = await self._load_conversation(event)
            if not state:
                return NO_CONVERSATION

            last_user_idx = self._find_last_role(state.history, "user")
            if last_user_idx is None:
                return NO_USER_MESSAGE

            original_history = state.history
            truncated_history = original_history[:last_user_idx]

            manager = self.context.conversation_manager
            await manager.update_conversation(
                event.unified_msg_origin,
                state.cid,
                history=truncated_history,
            )

            try:
                await self._redispatch(event, RESEND_PREFIX + parsed.payload)
            except Exception:
                # Do not leave the conversation truncated when event creation or
                # queue submission fails.
                await manager.update_conversation(
                    event.unified_msg_origin,
                    state.cid,
                    history=original_history,
                )
                raise

            logger.info(
                "MessageEditor: resend queued for %s; history %d -> %d",
                event.unified_msg_origin,
                len(original_history),
                len(truncated_history),
            )
            return RESEND_ACCEPTED
        except Exception as exc:
            logger.exception("MessageEditor: resend failed: %s", exc)
            return RESEND_FAILED

    async def _handle_direct_edit(
        self,
        event: AstrMessageEvent,
        parsed: ParsedCommand,
    ) -> str:
        command = parsed.command
        if parsed.invalid_target or parsed.target not in ("f", "a"):
            return self._target_usage(command)

        try:
            state = await self._load_conversation(event)
            if not state:
                return NO_CONVERSATION

            target_role = "user" if parsed.target == "f" else "assistant"
            target_idx = self._find_last_text_message(state.history, target_role)
            if target_idx is None:
                return NO_USER_MESSAGE if parsed.target == "f" else NO_ASSISTANT_MESSAGE

            target_message = state.history[target_idx]
            if target_role == "user":
                # AstrBot may keep plugin-generated data, such as the time tag,
                # in later text blocks. Only the first block is the editable
                # user body (including XML injected into that same block).
                raw_text = self._extract_first_text(target_message)
            else:
                raw_text = self._extract_text(target_message)

            if not parsed.has_payload:
                await self._send_raw_text_reply(
                    event,
                    command,
                    parsed.target,
                    raw_text,
                )
                return ""

            if parsed.payload is None or not parsed.payload.strip():
                return EMPTY_CONTENT

            if target_role == "user":
                replaced = self._replace_first_text(
                    target_message,
                    parsed.payload,
                )
            else:
                replaced = self._replace_text(target_message, parsed.payload)

            if not replaced:
                return EDIT_FAILED

            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                state.cid,
                history=state.history,
            )

            logger.info(
                "MessageEditor: edited %s message at index %d in %s",
                target_role,
                target_idx,
                state.cid,
            )
            if parsed.target == "f":
                return "改好啦，主人。最后一条主人消息已经替换完成。✨"
            return "改好啦，主人。Abyss的最后一条回复已经替换完成。✨"
        except Exception as exc:
            logger.exception("MessageEditor: direct edit failed: %s", exc)
            return EDIT_FAILED

    async def _load_conversation(
        self,
        event: AstrMessageEvent,
    ) -> ConversationState | None:
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        if not cid:
            return None

        conversation = await manager.get_conversation(
            event.unified_msg_origin,
            cid,
        )
        if not conversation:
            return None

        history = json.loads(conversation.history or "[]")
        if not isinstance(history, list):
            return None
        if not all(isinstance(item, dict) for item in history):
            return None
        return ConversationState(cid=cid, history=history)

    async def _redispatch(self, event: AstrMessageEvent, content: str) -> None:
        platform = self._find_platform(event.get_platform_id())
        if platform is None or not hasattr(platform, "handle_msg"):
            raise RuntimeError("matching OneBot platform instance was not found")

        message = copy.copy(event.message_obj)
        message.message_str = content
        message.message = [Plain(content)]
        message.message_id = uuid.uuid4().hex
        message.timestamp = int(time.time())
        # Sending replies does not require the original aiocqhttp Event; the
        # numeric private-session ID is sufficient and avoids stale command data.
        message.raw_message = None

        await platform.handle_msg(message)

    def _find_platform(self, platform_id: str):
        for platform in self.context.platform_manager.get_insts():
            try:
                meta = platform.meta()
            except Exception:
                continue
            if getattr(meta, "id", None) == platform_id:
                return platform
        return None

    @staticmethod
    def _raw_message(event: AstrMessageEvent) -> str:
        raw = getattr(event.message_obj, "message_str", None)
        if isinstance(raw, str):
            return raw
        return event.message_str if isinstance(event.message_str, str) else ""

    @staticmethod
    def _parse_command(raw: str) -> ParsedCommand | None:
        match = COMMAND_RE.fullmatch(raw)
        if not match:
            return None

        original_command = match.group("command")
        command = original_command.casefold()
        tail = match.group("tail") or ""
        payload_match = PAYLOAD_RE.search(tail)

        if payload_match:
            before_payload = tail[: payload_match.start()].strip().casefold()
            payload = payload_match.group(1)
            has_payload = True
        else:
            before_payload = tail.strip().casefold()
            payload = None
            has_payload = False

        invalid_target = before_payload not in TARGET_ALIASES
        target = TARGET_ALIASES.get(before_payload)

        # Resend has no target. Any token before the opening brace, including
        # the old "f" spelling, is rejected with a focused hint.
        if command == "resend":
            invalid_target = bool(before_payload)
            target = None

        return ParsedCommand(
            command=command,
            original_command=original_command,
            target=target,
            payload=payload,
            has_payload=has_payload,
            invalid_target=invalid_target,
        )

    @staticmethod
    def _find_last_role(history: list[dict[str, Any]], role: str) -> int | None:
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("role") == role:
                return index
        return None

    @classmethod
    def _find_last_text_message(
        cls,
        history: list[dict[str, Any]],
        role: str,
    ) -> int | None:
        for index in range(len(history) - 1, -1, -1):
            message = history[index]
            if message.get("role") == role and cls._has_text_slot(message):
                return index
        return None

    @staticmethod
    def _is_text_block(block: Any) -> bool:
        return (
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text"}
            and isinstance(block.get("text"), str)
        )

    @classmethod
    def _has_text_slot(cls, message: dict[str, Any]) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return any(cls._is_text_block(block) for block in content)
        return False

    @classmethod
    def _extract_text(cls, message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block["text"] for block in content if cls._is_text_block(block)
            )
        return ""

    @classmethod
    def _extract_first_text(cls, message: dict[str, Any]) -> str:
        """Read only the editable body of a user message.

        Plugin-owned text blocks after the first one (for example
        <current_date_and_time>) are intentionally excluded.
        """
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if cls._is_text_block(block):
                    return block["text"]
        return ""

    @classmethod
    def _replace_first_text(
        cls,
        message: dict[str, Any],
        new_text: str,
    ) -> bool:
        """Replace the user body while preserving every later block."""
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = new_text
            return True

        if not isinstance(content, list):
            return False

        for index, block in enumerate(content):
            if not cls._is_text_block(block):
                continue
            updated = dict(block)
            updated["text"] = new_text
            content[index] = updated
            return True
        return False

    @classmethod
    def _replace_text(cls, message: dict[str, Any], new_text: str) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = new_text
            return True

        if not isinstance(content, list):
            return False

        replaced = False
        new_blocks: list[Any] = []
        for block in content:
            if cls._is_text_block(block):
                if not replaced:
                    updated = dict(block)
                    updated["text"] = new_text
                    new_blocks.append(updated)
                    replaced = True
                # Drop later text blocks: the supplied text replaces the full
                # visible/raw text while image/thinking blocks remain intact.
                continue
            new_blocks.append(block)

        if replaced:
            message["content"] = new_blocks
        return replaced

    @staticmethod
    def _target_usage(command: str) -> str:
        return (
            "主人，咪只认识 f（主人）和 a（Abyss）：\n"
            f"/{command} f {{新内容}}\n"
            f"/{command} a {{新内容}} 🐾"
        )

    @staticmethod
    async def _send_raw_text_reply(
        event: AstrMessageEvent,
        command: str,
        target: str,
        raw_text: str,
    ) -> None:
        if target == "f":
            who = "主人最后一条消息"
        else:
            who = "Abyss最后一条回复"

        intro = (
            f"主人，咪把{who}在数据库里的正文原文拿来了。"
            "修改大括号里的内容，再把整条指令发回来就好——🐈‍⬛"
        )
        await event.send(event.plain_result(intro))
        editable_command = f"/{command} {target} {{{raw_text}}}"
        await event.send(event.plain_result(editable_command))

    @staticmethod
    async def _send_resend_usage(event: AstrMessageEvent) -> None:
        await event.send(event.plain_result(RESEND_USAGE))
        await event.send(event.plain_result("/resend {新内容}"))

    async def terminate(self) -> None:
        """No persistent resources to release."""
