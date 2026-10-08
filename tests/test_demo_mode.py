import asyncio
import unittest

import config
from langflow_client import LangflowClient


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


if __name__ == "__main__":
    unittest.main()
