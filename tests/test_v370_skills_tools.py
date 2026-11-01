"""v3.7.0 MCP tool surface and skill-tool behaviour tests."""

import json
import unittest
from pathlib import Path

from deepseek_mcp import server, skills
from deepseek_mcp.config import load_config
from deepseek_mcp.skill_registry import SkillRegistry
from tests import v370_support as support


class ToolSchemaTests(unittest.TestCase):
    def test_twenty_unique_tools_and_both_registries(self):
        names = [tool["name"] for tool in server.TOOLS]
        self.assertEqual(len(names), 20)
        self.assertEqual(len(set(names)), 20)
        self.assertEqual(len(server.TOOL_IMPL), 20)
        self.assertEqual(len(server.HANDLERS), 20)
        for name in ("list_skills", "read_skill", "approve_skill",
                     "reject_skill", "retire_skill", "rollback_skill"):
            self.assertIn(name, names)
            self.assertIn(name, server.TOOL_IMPL)
            self.assertIn(name, server.HANDLERS)

    def test_six_skill_schemas_have_required_fields(self):
        schemas = {tool["name"]: tool for tool in server.TOOLS}
        self.assertEqual(schemas["read_skill"]["inputSchema"]["required"],
                         ["name"])
        self.assertEqual(schemas["approve_skill"]["inputSchema"]["required"],
                         ["name"])
        self.assertIn("version",
                      schemas["rollback_skill"]["inputSchema"]["properties"])
        self.assertIn("format",
                      schemas["list_skills"]["inputSchema"]["properties"])

    def test_harness_skill_description_mentions_registry_and_none(self):
        text = json.dumps([tool for tool in server.TOOLS
                           if tool["name"] == "harness_deepseek"])
        self.assertIn("registry", text)
        self.assertIn("none", text)


class ToolBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.cfg, self.env, _fake = support.make_root(
            "v370-tools-")
        self.addCleanup(self.tmp.cleanup)
        self.registry = SkillRegistry(self.cfg)
        self.registry.load()

    def create_candidate(self, key="podcast-launch"):
        self.registry.create_or_merge(
            support.valid_draft(key=key), {"score": 9.2, "rounds": 1,
                                           "blocking": 0},
            {"id": "c-test", "baseline": {"median": 8.0, "mean": 8.0,
                                           "n": 3, "blocking_rate": 0.0,
                                           "source": "members"}},
            "auto", "run-create")
        return key

    def test_list_text_json_and_status_filter(self):
        self.create_candidate()
        text = server.tool_list_skills({}, self.cfg)
        self.assertIn("podcast-launch", text)
        self.assertIn("candidate", text)
        payload = json.loads(server.tool_list_skills(
            {"format": "json", "status": "candidate"}, self.cfg))
        self.assertEqual([row["key"] for row in payload], ["podcast-launch"])
        self.assertEqual(payload[0]["status"], "candidate")
        seeds = json.loads(server.tool_list_skills(
            {"format": "json", "status": "seed"}, self.cfg))
        self.assertEqual(len(seeds), 5)

    def test_read_current_version_and_meta(self):
        self.create_candidate()
        content = server.tool_read_skill({"name": "podcast-launch"}, self.cfg)
        self.assertIn("Podcast launch", content)
        versioned = server.tool_read_skill(
            {"name": "podcast-launch", "version": 1}, self.cfg)
        self.assertIn("Podcast launch", versioned)
        meta = server.tool_read_skill(
            {"name": "podcast-launch", "meta": True}, self.cfg)
        self.assertIn("status", meta.lower())
        self.assertIn("candidate", meta)

    def test_approve_reject_retire_and_rollback(self):
        key = self.create_candidate()
        # An unapproved propose candidate can be approved.
        self.registry.set_status(key, "candidate", "propose", approved=False)
        approved = server.tool_approve_skill({"name": key}, self.cfg)
        self.assertIn("approved=true", approved)
        self.assertTrue(self.registry.get(key)["approved"])

        rejected = server.tool_reject_skill({"name": key, "reason": "thin"},
                                            self.cfg)
        self.assertIn("Retired" if "Retired" in rejected else "retired",
                      rejected)
        self.assertEqual(self.registry.get(key)["status"], "retired")

        retired = server.tool_retire_skill({"name": "poster"}, self.cfg)
        self.assertIn("no longer injected", retired)
        self.assertEqual(self.registry.get("poster")["status"], "retired")

        self.registry.add_version(key, self.registry.content(key), "refresh")
        self.registry.set_status(key, "candidate", "manual", approved=True)
        rolled = server.tool_rollback_skill({"name": key}, self.cfg)
        self.assertIn("Rolled back", rolled)

    def test_unknown_key_wrong_status_and_missing_version_are_actionable(self):
        with self.assertRaises(RuntimeError) as caught:
            server.tool_read_skill({"name": "no-such-skill"}, self.cfg)
        self.assertIn("Known skills", str(caught.exception))

        self.create_candidate()
        with self.assertRaises(RuntimeError) as caught:
            server.tool_approve_skill({"name": "poster"}, self.cfg)
        self.assertIn("candidate", str(caught.exception))

        with self.assertRaises(RuntimeError) as caught:
            server.tool_rollback_skill({"name": "podcast-launch",
                                        "version": 4}, self.cfg)
        self.assertIn("no version", str(caught.exception))

    def test_rollback_without_a_previous_version_is_clear(self):
        with self.assertRaises(RuntimeError) as caught:
            server.tool_rollback_skill({"name": "poster"}, self.cfg)
        self.assertIn("no previous version", str(caught.exception))

    def test_off_mode_allows_read_only_and_refuses_writes(self):
        self.env["DSH_AUTO_SKILLS"] = "off"
        cfg = load_config(config_path=self.root / "config.json", env=self.env)
        text = server.tool_list_skills({}, cfg)
        self.assertIn("poster", text)
        self.assertIn("seed", text)
        for call in (server.tool_approve_skill, server.tool_reject_skill,
                     server.tool_retire_skill, server.tool_rollback_skill):
            with self.assertRaises(RuntimeError) as caught:
                call({"name": "poster"}, cfg)
            self.assertIn("auto_skills is off", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
