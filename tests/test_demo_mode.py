import asyncio
import unittest

from starlette.requests import Request

import config
from langflow_client import LangflowClient
from routers import admin
from session import create_session


class DemoModeTests(unittest.TestCase):
    def test_demo_mode_returns_generated_flow(self):
        original = config.ENABLE_DEMO_MODE
        original_flow_id = config.DEMO_FLOW_ID
        original_flow_name = config.DEMO_FLOW_NAME
        try:
            config.ENABLE_DEMO_MODE = True
            config.DEMO_FLOW_ID = "demo-flow"
            config.DEMO_FLOW_NAME = "Demo assistant"

            flows = asyncio.run(LangflowClient().get_all_flows())
            self.assertTrue(flows)
            self.assertEqual(flows[0]["id"], "demo-flow")
        finally:
            config.ENABLE_DEMO_MODE = original
            config.DEMO_FLOW_ID = original_flow_id
            config.DEMO_FLOW_NAME = original_flow_name

    def test_demo_mode_returns_generated_text(self):
        original = config.ENABLE_DEMO_MODE
        original_flow_id = config.DEMO_FLOW_ID
        try:
            config.ENABLE_DEMO_MODE = True
            config.DEMO_FLOW_ID = "demo-flow"

            result = asyncio.run(LangflowClient().run_flow("demo-flow", "Привет"))
            self.assertIn("demo", str(result).lower())
        finally:
            config.ENABLE_DEMO_MODE = original
            config.DEMO_FLOW_ID = original_flow_id

    def test_demo_mode_can_be_toggled_via_admin_route(self):
        original = config.ENABLE_DEMO_MODE
        original_flow_id = config.DEMO_FLOW_ID
        token = create_session({"username": "admin", "is_admin": True, "is_system_admin": True})

        class FakeResult:
            def scalar_one_or_none(self):
                return None

        class FakeSession:
            async def execute(self, stmt):
                return FakeResult()

            async def commit(self):
                pass

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/admin/settings/demo-mode/toggle",
            "headers": [(b"cookie", f"session={token}".encode())],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("testserver", 80),
        })

        try:
            config.ENABLE_DEMO_MODE = False
            config.DEMO_FLOW_ID = "demo-flow"
            result = asyncio.run(admin.admin_toggle_demo_mode({"enabled": True}, request, FakeSession()))
            self.assertTrue(result["enabled"])
            self.assertEqual(result["flow_id"], "demo-flow")
        finally:
            config.ENABLE_DEMO_MODE = original
            config.DEMO_FLOW_ID = original_flow_id


if __name__ == "__main__":
    unittest.main()
