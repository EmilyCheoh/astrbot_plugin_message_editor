import asyncio
import copy
import json
import sys
import types
import unittest
from types import SimpleNamespace


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_module = types.ModuleType("astrbot.api.event")
    components_module = types.ModuleType("astrbot.api.message_components")
    provider_module = types.ModuleType("astrbot.api.provider")
    star_module = types.ModuleType("astrbot.api.star")

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def exception(self, *args, **kwargs):
            pass

    class DummyFilter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def _decorator(*args, **kwargs):
            def decorate(func):
                return func

            return decorate

        command = _decorator
        event_message_type = _decorator
        on_llm_request = _decorator

    class Plain:
        def __init__(self, text):
            self.text = text

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        def decorate(cls):
            return cls

        return decorate

    api.logger = Logger()
    event_module.AstrMessageEvent = object
    event_module.filter = DummyFilter()
    components_module.Plain = Plain
    provider_module.ProviderRequest = object
    star_module.Context = object
    star_module.Star = Star
    star_module.register = register

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_module,
            "astrbot.api.message_components": components_module,
            "astrbot.api.provider": provider_module,
            "astrbot.api.star": star_module,
        }
    )


_install_astrbot_stubs()

import main  # noqa: E402


class FakeManager:
    def __init__(self, history):
        self.history = copy.deepcopy(history)
        self.updates = []

    async def get_curr_conversation_id(self, umo):
        return "conversation-id"

    async def get_conversation(self, umo, cid):
        return SimpleNamespace(history=json.dumps(self.history, ensure_ascii=False))

    async def update_conversation(self, umo, cid, history):
        self.history = copy.deepcopy(history)
        self.updates.append(copy.deepcopy(history))


class FakePlatform:
    def __init__(self, *, fail=False):
        self.messages = []
        self.fail = fail

    def meta(self):
        return SimpleNamespace(id="qq-instance")

    async def handle_msg(self, message):
        if self.fail:
            raise RuntimeError("queue failed")
        self.messages.append(message)


class FakeEvent:
    def __init__(self, raw):
        self.message_str = raw
        self.message_obj = SimpleNamespace(
            message_str=raw,
            message=[],
            message_id="original-id",
            raw_message={"message": raw},
        )
        self.unified_msg_origin = "qq:FriendMessage:123"
        self.stopped = False

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return "qq-instance"

    def is_private_chat(self):
        return True

    def plain_result(self, text):
        return text


def make_plugin(history, *, platform=None):
    manager = FakeManager(history)
    platform = platform or FakePlatform()
    context = SimpleNamespace(
        conversation_manager=manager,
        platform_manager=SimpleNamespace(get_insts=lambda: [platform]),
    )
    return main.MessageEditorPlugin(context), manager, platform


class MessageEditorTests(unittest.TestCase):
    def test_parser_is_case_insensitive_and_multiline_greedy(self):
        parsed = main.MessageEditorPlugin._parse_command(
            "/ReSeNd {第一行\n\n{内部大括号}\n最后一行}"
        )
        self.assertEqual(parsed.command, "resend")
        self.assertEqual(parsed.payload, "第一行\n\n{内部大括号}\n最后一行")
        self.assertFalse(parsed.invalid_target)

        parsed = main.MessageEditorPlugin._parse_command("/EDIT A {new}")
        self.assertEqual(parsed.target, "a")

        parsed = main.MessageEditorPlugin._parse_command("/resend f {new}")
        self.assertTrue(parsed.invalid_target)

    def test_replace_text_preserves_non_text_blocks(self):
        message = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "old-1"},
                {"type": "image_url", "image_url": "x"},
                {"type": "text", "text": "old-2"},
            ],
        }
        self.assertTrue(main.MessageEditorPlugin._replace_text(message, "new"))
        self.assertEqual(
            message["content"],
            [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "new"},
                {"type": "image_url", "image_url": "x"},
            ],
        )

    def test_direct_user_edit_updates_without_truncating(self):
        history = [
            {"role": "user", "content": "old <Tag>xml</Tag>"},
            {"role": "assistant", "content": "answer"},
        ]
        plugin, manager, _ = make_plugin(history)
        event = FakeEvent("/edit f {new\n<Tag>kept</Tag>}")

        reply = asyncio.run(plugin._execute_command(event))

        self.assertTrue(event.stopped)
        self.assertIn("改好啦", reply)
        self.assertEqual(len(manager.history), 2)
        self.assertEqual(manager.history[0]["content"], "new\n<Tag>kept</Tag>")

    def test_query_returns_copyable_raw_command(self):
        history = [
            {"role": "user", "content": "raw\n<Tag>xml</Tag>"},
            {"role": "assistant", "content": "answer"},
        ]
        plugin, manager, _ = make_plugin(history)
        event = FakeEvent("/patch")

        reply = asyncio.run(plugin._execute_command(event))

        self.assertIn("/patch f {raw\n<Tag>xml</Tag>}", reply)
        self.assertEqual(manager.updates, [])

    def test_resend_truncates_from_last_user_and_queues_normal_message(self):
        history = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "old answer"},
        ]
        plugin, manager, platform = make_plugin(history)
        event = FakeEvent("/resend {new\ncontent}")

        reply = asyncio.run(plugin._execute_command(event))

        self.assertIn("正在重新发送", reply)
        self.assertEqual(manager.history, history[:2])
        self.assertEqual(len(platform.messages), 1)
        queued = platform.messages[0]
        self.assertEqual(queued.message_str, "【重发】new\ncontent")
        self.assertEqual(queued.message[0].text, queued.message_str)
        self.assertIsNone(queued.raw_message)
        self.assertNotEqual(queued.message_id, "original-id")

    def test_resend_restores_history_when_queue_submission_fails(self):
        history = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "answer"},
        ]
        plugin, manager, _ = make_plugin(history, platform=FakePlatform(fail=True))
        event = FakeEvent("/resend {new}")

        reply = asyncio.run(plugin._execute_command(event))

        self.assertIn("没有成功", reply)
        self.assertEqual(manager.history, history)
        self.assertEqual(len(manager.updates), 2)

    def test_internal_marker_is_removed_from_provider_prompt(self):
        plugin, _, _ = make_plugin([])
        req = SimpleNamespace(prompt="【重发】clean")

        asyncio.run(plugin.strip_internal_resend_marker(FakeEvent("x"), req))

        self.assertEqual(req.prompt, "clean")


if __name__ == "__main__":
    unittest.main()

