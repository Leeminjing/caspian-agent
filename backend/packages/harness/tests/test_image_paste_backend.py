"""粘贴图片后端行为：内容块透传、非视觉模型守卫、uploads 注入不冲掉列表。"""

import unittest

from langchain_core.messages import HumanMessage

from backend.app.gateway.services import (
    _messages_contain_image,
    _resolve_model_config,
)
from backend.packages.harness.caspian.agents.middlewares.uploads_middleware import (
    UploadsMiddleware,
)


class _FakeExecInfo:
    thread_id = "th-1"


class _FakeRuntime:
    execution_info = _FakeExecInfo()
    context = {"user_id": "u-1"}
    tool_call_id = "call-1"


class _FakeModel:
    def __init__(self, name, vision=False):
        self.name = name
        self.vision = vision


class _FakeAppConfig:
    def __init__(self):
        self.models = [_FakeModel("vision-m", True), _FakeModel("text-m", False)]


class ImageContentHelpersTests(unittest.TestCase):
    def test_messages_contain_image_detects_image_url_blocks(self):
        self.assertTrue(
            _messages_contain_image([
                {"role": "user", "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ]}
            ])
        )
        self.assertFalse(_messages_contain_image([{"role": "user", "content": "hi"}]))

    def test_messages_contain_image_empty_input(self):
        self.assertFalse(_messages_contain_image(None))
        self.assertFalse(_messages_contain_image([]))


class ModelVisionResolutionTests(unittest.TestCase):
    def test_resolve_by_name(self):
        cfg = _FakeAppConfig()
        self.assertEqual(_resolve_model_config(cfg, "vision-m").name, "vision-m")
        self.assertEqual(_resolve_model_config(cfg, "text-m").name, "text-m")

    def test_resolve_default_first(self):
        cfg = _FakeAppConfig()
        self.assertEqual(_resolve_model_config(cfg, None).name, "vision-m")


class UploadsMiddlewareListContentTests(unittest.TestCase):
    def test_list_content_keeps_blocks_and_appends_tag(self):
        mw = UploadsMiddleware()
        msg = HumanMessage(
            content=[
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
            additional_kwargs={"files": [{"filename": "a.png", "size": 10}]},
        )
        result = mw._inject_current_uploads({"messages": [msg]}, _FakeRuntime())
        new_msg = result["messages"][0]
        self.assertIsInstance(new_msg.content, list)
        self.assertEqual(new_msg.content[0]["type"], "text")
        self.assertEqual(new_msg.content[1]["type"], "image_url")
        self.assertEqual(new_msg.content[-1]["type"], "text")
        self.assertIn("<current_uploads>", new_msg.content[-1]["text"])

    def test_string_content_still_concatenates(self):
        mw = UploadsMiddleware()
        msg = HumanMessage(
            content="hello",
            additional_kwargs={"files": [{"filename": "a.png", "size": 10}]},
        )
        result = mw._inject_current_uploads({"messages": [msg]}, _FakeRuntime())
        new_msg = result["messages"][0]
        self.assertIsInstance(new_msg.content, str)
        self.assertIn("hello", new_msg.content)
        self.assertIn("<current_uploads>", new_msg.content)


if __name__ == "__main__":
    unittest.main()
