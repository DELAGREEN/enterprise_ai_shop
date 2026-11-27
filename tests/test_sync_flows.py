"""Разбор компонентов для синхронизации кубиков Langflow.

``sync_flows.py`` решает проблему, которую не видно в git: кубик в
Langflow — это копия файла, и правка в репозитории на работающий flow не
влияет. В этой сессии так и вышло — в кубике парсера лежал код без
исправления лимита child, а в кубике ингеста значения ``max_embed_chars``
1800 и ``max_parent_chars`` 2000 при дефолтах 3600 и 3000. Код обновили,
значения забыли.

Тесты проверяют две вещи: что дефолты из кода читаются верно и что
сравниваются только лимиты, а не настройки конкретного документа.
"""

import unittest

from langflow_components.sync_flows import (
    WATCHED_VALUES,
    declared_defaults,
    iter_component_nodes,
)

CODE = '''
class Demo(Component):
    inputs = [
        IntInput(
            name="max_child_tokens",
            display_name="Max child tokens",
            value=350,
        ),
        IntInput(
            name="max_parent_chars",
            display_name="Max parent chars",
            value=3000,
        ),
        StrInput(
            name="collection_name",
            display_name="Collection",
            value="gost_chunks",
        ),
        BoolInput(
            name="recreate_collection",
            display_name="Recreate",
            value=False,
        ),
    ]
'''


def flow_with(code: str, node_id: str = "node-1") -> dict:
    return {
        "data": {
            "nodes": [
                {
                    "id": node_id,
                    "data": {"node": {"template": {"code": {"value": code}}}},
                }
            ]
        }
    }


class DeclaredDefaultsTests(unittest.TestCase):
    def test_reads_int_values(self):
        defaults = declared_defaults(CODE)

        self.assertEqual(defaults["max_child_tokens"], 350)
        self.assertEqual(defaults["max_parent_chars"], 3000)

    def test_reads_str_and_bool(self):
        defaults = declared_defaults(CODE)

        self.assertEqual(defaults["collection_name"], "gost_chunks")
        self.assertIs(defaults["recreate_collection"], False)

    def test_missing_inputs_do_not_break(self):
        self.assertEqual(declared_defaults("class X: pass"), {})


class WatchedValuesTests(unittest.TestCase):
    def test_only_model_limits_are_watched(self):
        # collection_name у каждого документа свой — сравнивать его с
        # дефолтом значит показывать ложное расхождение.
        self.assertIn("max_parent_chars", WATCHED_VALUES)
        self.assertIn("max_embed_chars", WATCHED_VALUES)
        self.assertNotIn("collection_name", WATCHED_VALUES)
        self.assertNotIn("top_k", WATCHED_VALUES)


class IterComponentNodesTests(unittest.TestCase):
    def test_finds_known_component(self):
        code = "class EoszStructureParser(Component):\n    pass\n"
        nodes = list(iter_component_nodes(flow_with(code)))

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0][2], "EoszStructureParser")

    def test_ignores_foreign_component(self):
        code = "class SomethingElse(Component):\n    pass\n"
        self.assertEqual(list(iter_component_nodes(flow_with(code))), [])

    def test_ignores_node_without_code(self):
        flow = {"data": {"nodes": [{"id": "x", "data": {"node": {"template": {}}}}]}}
        self.assertEqual(list(iter_component_nodes(flow)), [])


if __name__ == "__main__":
    unittest.main()
