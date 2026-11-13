"""Tests for deepseek_mcp.mimo_permissions (per-run mimocode.jsonc, W2).

Pure stdlib unittest, Python 3.9-safe.  The suite never writes outside the
temp dirs it creates under TMPDIR and never touches ``~/.config``: the global
config is only stat'ed to prove write_run_config leaves it alone.
"""

import json
import os
import shutil
import tempfile
import unittest
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace

from deepseek_mcp import mimo_permissions as mp

MUTATING_COMMANDS = (
    "rm -rf /",
    "rm -rf ~",
    "rm x",
    "mv a b",
    "cp -r a b",
    "tee out",
    "dd if=/dev/zero",
    "chmod 777 x",
    "git push origin main",
    "git subtree push",
    "git checkout main",
    "git commit -m x",
    "git commit --amend",
    "git rebase -i HEAD~2",
    "git reset --hard HEAD",
    "git filter-branch --tree-filter x",
    "git gc --aggressive",
    "git tag v1",
    "git config --global user.name x",
    "git config --system init.defaultBranch main",
    "sudo ls",
    "gh pr create",
    "curl example.com",
    "python -c 'open(\"x\",\"w\")'",
    "npm install",
    "echo hi > out",
    "make",
)

READ_ONLY_SAFE_COMMANDS = (
    "ls",
    "ls -la src",
    "cat setup.py",
    "head -n 5 x",
    "tail -f log",
    "grep -n foo x",
    "find . -name '*.py'",
    "wc -l x",
    "pwd",
    "which python",
    "git status",
    "git status --short",
    "git log --oneline -5",
    "git diff HEAD~1",
    "git show HEAD",
)


class Cfg(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__


def _tmpdir():
    return tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None)


def _parse(text):
    """Parse rendered config (output is pure JSON by contract)."""
    return json.loads(text)


def _decision(bash, command):
    """Evaluate a bash glob map: last matching glob wins (mirrors the
    repo-root mimocode.jsonc shape: catch-all first, specifics after)."""
    verdict = None
    for glob, value in bash.items():
        if fnmatchcase(command, glob):
            verdict = value
    return verdict


def _workspace_root(workspace):
    return os.path.abspath(workspace).replace("\\", "/").rstrip("/") or "/"


class RenderParseTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_presets_constant(self):
        self.assertEqual(mp.PRESETS,
                         ("read-only", "workspace-write", "danger-full-access"))

    def test_round_trip_each_preset(self):
        for preset in mp.PRESETS:
            text = mp.render_mimocode_jsonc(preset, self.ws)
            self.assertTrue(text.endswith("\n"), preset)
            doc = _parse(text)  # pure JSON: no comment stripping needed
            self.assertEqual(doc["$schema"], mp.SCHEMA_URL)
            self.assertEqual(
                sorted(doc["permission"].keys()),
                sorted(["read", "edit", "glob", "grep", "list", "lsp", "task",
                        "actor", "skill", "webfetch", "external_directory",
                        "bash"]),
                preset)

    def test_permission_values_per_preset(self):
        expected = {
            "read-only": {"read": "allow", "edit": "deny", "glob": "allow",
                          "grep": "allow", "list": "allow", "lsp": "allow",
                          "task": "allow", "actor": "allow", "skill": "allow",
                          "webfetch": "allow"},
            "workspace-write": {"read": "allow", "edit": "allow",
                                "glob": "allow", "grep": "allow",
                                "list": "allow", "lsp": "allow",
                                "task": "allow", "actor": "allow",
                                "skill": "allow", "webfetch": "allow"},
            "danger-full-access": {"read": "allow", "edit": "allow",
                                   "glob": "allow", "grep": "allow",
                                   "list": "allow", "lsp": "allow",
                                   "task": "allow", "actor": "allow",
                                   "skill": "allow", "webfetch": "allow"},
        }
        for preset, values in expected.items():
            perm = _parse(
                mp.render_mimocode_jsonc(preset, self.ws))["permission"]
            for key, want in values.items():
                self.assertEqual(perm[key], want, "%s/%s" % (preset, key))

    def test_read_only_bash_map_is_exact_allowlist(self):
        perm = _parse(mp.render_mimocode_jsonc("read-only", self.ws))["permission"]
        expected = [
            ("*", "deny"),
            ("ls*", "allow"), ("cat*", "allow"), ("head*", "allow"),
            ("tail*", "allow"), ("grep*", "allow"), ("find*", "allow"),
            ("wc*", "allow"), ("pwd", "allow"), ("which*", "allow"),
            ("git status*", "allow"), ("git log*", "allow"),
            ("git diff*", "allow"), ("git show*", "allow"),
        ]
        self.assertEqual(list(perm["bash"].items()), expected)

    def test_workspace_write_bash_map_is_exact(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "workspace-write", self.ws))["permission"]
        expected = [
            ("*", "allow"),
            ("git push*", "deny"), ("git * push*", "deny"), ("gh *", "deny"),
            ("git config --global*", "deny"),
            ("git config --system*", "deny"),
            ("git commit --amend*", "deny"), ("git rebase*", "deny"),
            ("git reset --hard*", "deny"), ("git filter-branch*", "deny"),
            ("git gc*", "deny"), ("git tag*", "deny"),
            ("sudo *", "deny"), ("rm -rf /*", "deny"), ("rm -rf ~*", "deny"),
            ("*--dangerously-skip-permissions*", "deny"),
            ("*--yolo*", "deny"),
        ]
        self.assertEqual(list(perm["bash"].items()), expected)

    def test_danger_bash_all_allow(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "danger-full-access", self.ws))["permission"]
        self.assertEqual(list(perm["bash"].items()), [("*", "allow")])

    def test_invalid_preset_and_workspace_rejected(self):
        for bad in ("banana", "", None, 5, "sandbox"):
            self.assertRaises(ValueError, mp.render_mimocode_jsonc, bad,
                              self.ws)
        self.assertRaises(ValueError, mp.render_mimocode_jsonc, "read-only", "")
        self.assertRaises(ValueError, mp.render_mimocode_jsonc, "read-only",
                          "   ")


class ReadOnlyStrictnessTest(unittest.TestCase):
    """A read-only reviewer must not edit or run mutating bash."""

    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.perm = _parse(
            mp.render_mimocode_jsonc("read-only", self.ws))["permission"]

    def test_edit_has_explicit_deny(self):
        self.assertEqual(self.perm["edit"], "deny")

    def test_bash_has_catch_all_deny(self):
        self.assertEqual(self.perm["bash"]["*"], "deny")

    def test_no_allow_glob_covers_mutating_commands(self):
        allow_globs = [g for g, v in self.perm["bash"].items()
                       if v == "allow"]
        self.assertEqual(sorted(allow_globs),
                         sorted(["ls*", "cat*", "head*", "tail*", "grep*",
                                 "find*", "wc*", "pwd", "which*",
                                 "git status*", "git log*", "git diff*",
                                 "git show*"]))
        for cmd in MUTATING_COMMANDS:
            hits = [g for g in allow_globs if fnmatchcase(cmd, g)]
            self.assertEqual(hits, [], "%r covered by %r" % (cmd, hits))
            self.assertEqual(_decision(self.perm["bash"], cmd), "deny", cmd)

    def test_safe_inspection_commands_still_allowed(self):
        for cmd in READ_ONLY_SAFE_COMMANDS:
            self.assertEqual(_decision(self.perm["bash"], cmd), "allow", cmd)

    def test_mutating_tools_denied_by_value(self):
        self.assertNotEqual(self.perm["edit"], "allow")


class WorkspaceWriteDenialsTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.perm = _parse(mp.render_mimocode_jsonc(
            "workspace-write", self.ws))["permission"]

    def test_catch_all_allow_with_explicit_denies(self):
        self.assertEqual(self.perm["bash"]["*"], "allow")
        for glob in ("git push*", "git * push*", "gh *",
                     "git config --global*", "git config --system*",
                     "git commit --amend*", "git rebase*",
                     "git reset --hard*", "git filter-branch*", "git gc*",
                     "git tag*", "sudo *", "rm -rf /*", "rm -rf ~*",
                     "*--dangerously-skip-permissions*", "*--yolo*"):
            self.assertEqual(self.perm["bash"].get(glob), "deny", glob)

    def test_dangerous_commands_denied(self):
        for cmd in ("git push", "git push origin main", "git subtree push",
                    "gh pr create", "sudo rm -rf /",
                    "git config --global user.name x",
                    "git config --system init.defaultBranch main",
                    "git commit --amend --no-edit", "git rebase -i HEAD~1",
                    "git reset --hard HEAD~1",
                    "git filter-branch --tree-filter x HEAD",
                    "git gc --aggressive", "git tag v1",
                    "rm -rf /", "rm -rf ~", "rm -rf ~/tmp",
                    "mimo run --dangerously-skip-permissions",
                    "dsh --yolo", "x--yolo"):
            self.assertEqual(_decision(self.perm["bash"], cmd), "deny", cmd)

    def test_normal_builder_commands_allowed(self):
        for cmd in ("git status", "git commit -m msg", "git fetch origin",
                    "python -m pytest", "make test", "echo hi",
                    "npm test", "cat x"):
            self.assertEqual(_decision(self.perm["bash"], cmd), "allow", cmd)

    def test_edit_allowed(self):
        self.assertEqual(self.perm["edit"], "allow")


class DangerFullAccessTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.perm = _parse(mp.render_mimocode_jsonc(
            "danger-full-access", self.ws))["permission"]

    def test_all_allow(self):
        for key in ("read", "edit", "glob", "grep", "list", "lsp", "task",
                    "actor", "skill", "webfetch"):
            self.assertEqual(self.perm[key], "allow", key)
        self.assertEqual(self.perm["bash"], {"*": "allow"})
        self.assertEqual(self.perm["external_directory"], {"*": "allow"})


class ExternalDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_scoped_to_workspace(self):
        want = {"*": "deny",
                _workspace_root(self.ws) + "/**": "allow"}
        for preset in ("read-only", "workspace-write"):
            perm = _parse(
                mp.render_mimocode_jsonc(preset, self.ws))["permission"]
            self.assertEqual(perm["external_directory"], want, preset)

    def test_trailing_slash_normalised(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "read-only", self.ws + os.sep))["permission"]
        ext = perm["external_directory"]
        self.assertEqual(ext["*"], "deny")
        self.assertEqual(list(ext.keys()),
                         ["*", _workspace_root(self.ws) + "/**"])
        self.assertNotIn("//", list(ext.keys())[1][:-3])

    def test_path_workspace_accepted(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "read-only", Path(self.ws)))["permission"]
        self.assertIn(_workspace_root(self.ws) + "/**",
                      perm["external_directory"])


class ExtraDenyTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_extra_deny_appended_for_every_preset(self):
        for preset in mp.PRESETS:
            perm = _parse(mp.render_mimocode_jsonc(
                preset, self.ws,
                extra_deny=["nuke *", "zap"]))["permission"]
            self.assertEqual(perm["bash"].get("nuke *"), "deny", preset)
            self.assertEqual(perm["bash"].get("zap"), "deny", preset)

    def test_extra_deny_single_string_and_ordering(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "workspace-write", self.ws,
            extra_deny="frobnicate *"))["permission"]
        self.assertEqual(perm["bash"].get("frobnicate *"), "deny")
        self.assertEqual(list(perm["bash"].keys())[-1], "frobnicate *")

    def test_extra_deny_on_read_only_stays_deny(self):
        perm = _parse(mp.render_mimocode_jsonc(
            "read-only", self.ws, extra_deny=["ls *"]))["permission"]
        self.assertEqual(perm["bash"].get("ls *"), "deny")
        self.assertEqual(_decision(perm["bash"], "ls foo"), "deny")

    def test_bad_extra_deny_rejected(self):
        self.assertRaises(TypeError, mp.render_mimocode_jsonc,
                          "workspace-write", self.ws, extra_deny=[1])


class NormalizePresetTest(unittest.TestCase):
    def test_canonical_passthrough(self):
        for preset in mp.PRESETS:
            self.assertEqual(mp.normalize_preset(preset), preset)

    def test_aliases_match_safety_convention(self):
        cases = {
            "read-only": ("read-only", "readonly", "read_only", "ro",
                          "Read-Only", " READ_ONLY ", "RO"),
            "workspace-write": ("workspace-write", "workspace_write",
                                "write", "workspace", "rw",
                                "WRITE"),
            "danger-full-access": ("danger-full-access", "danger_full_access",
                                   "dangerfullaccess", "full", "danger",
                                   "none", "DANGER"),
        }
        for want, aliases in cases.items():
            for alias in aliases:
                self.assertEqual(mp.normalize_preset(alias), want, alias)

    def test_invalid_raises_without_echoing(self):
        for bad in ("banana", "", None, 3, "read only books"):
            try:
                mp.normalize_preset(bad)
            except ValueError as exc:
                if bad:
                    self.assertNotIn(str(bad), str(exc))
            else:
                self.fail("expected ValueError for %r" % (bad,))


class SkipPermissionsArgsTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(mp.skip_permissions_args(None), [])
        self.assertEqual(mp.skip_permissions_args(Cfg()), [])
        self.assertEqual(mp.skip_permissions_args(SimpleNamespace()), [])

    def test_falsy_values_stay_off(self):
        for value in (False, 0, None, "", "0", "false", "FALSE", "no",
                      "off", "maybe", 2, 0.0):
            cfg = Cfg({"mimo_skip_permissions": value})
            self.assertEqual(mp.skip_permissions_args(cfg), [], repr(value))

    def test_opt_in_returns_skip_flag(self):
        for value in (True, 1, "1", "true", "True", " true ", "TRUE"):
            cfg = Cfg({"mimo_skip_permissions": value})
            self.assertEqual(mp.skip_permissions_args(cfg),
                             ["--dangerously-skip-permissions"], repr(value))

    def test_attribute_style_cfg(self):
        cfg = SimpleNamespace(mimo_skip_permissions="true")
        self.assertEqual(mp.skip_permissions_args(cfg),
                         ["--dangerously-skip-permissions"])
        cfg = SimpleNamespace(mimo_skip_permissions="false")
        self.assertEqual(mp.skip_permissions_args(cfg), [])

    def test_never_yolo(self):
        samples = [None, Cfg(), Cfg({"mimo_skip_permissions": True}),
                   Cfg({"mimo_skip_permissions": "true"}),
                   SimpleNamespace(mimo_skip_permissions=True)]
        for cfg in samples:
            for arg in mp.skip_permissions_args(cfg):
                self.assertNotIn("yolo", arg)
                self.assertNotEqual(arg, "--yolo")


class PresetForRoleTest(unittest.TestCase):
    def test_read_only_roles(self):
        for role in ("reviewer", "audit", "probe", "preflight", "Reviewer",
                     " PREFLIGHT "):
            self.assertEqual(mp.preset_for_role(role), "read-only", role)

    def test_workspace_write_roles(self):
        for role in ("builder", "crosscheck", "finisher", "revision",
                     "rescue", "other", "", "something-else", None):
            self.assertEqual(mp.preset_for_role(role), "workspace-write",
                             role)

    def test_explicit_cfg_override_wins(self):
        cases = {"ro": "read-only", "read-only": "read-only",
                 "write": "workspace-write", "workspace_write":
                 "workspace-write", "danger": "danger-full-access",
                 "danger-full-access": "danger-full-access"}
        for raw, want in cases.items():
            cfg = Cfg({"mimo_sandbox": raw})
            self.assertEqual(mp.preset_for_role("reviewer", cfg), want, raw)
            self.assertEqual(mp.preset_for_role("builder", cfg), want, raw)

    def test_invalid_override_falls_back_to_role(self):
        for raw in ("banana", "sandbox", "danger!!"):
            cfg = Cfg({"mimo_sandbox": raw})
            self.assertEqual(mp.preset_for_role("reviewer", cfg), "read-only")
            self.assertEqual(mp.preset_for_role("builder", cfg),
                             "workspace-write")
        cfg = Cfg({"mimo_sandbox": ""})
        self.assertEqual(mp.preset_for_role("reviewer", cfg), "read-only")

    def test_attribute_style_cfg_override(self):
        cfg = SimpleNamespace(mimo_sandbox="workspace-write")
        self.assertEqual(mp.preset_for_role("reviewer", cfg),
                         "workspace-write")


class WriteRunConfigTest(unittest.TestCase):
    def setUp(self):
        self.ws = _tmpdir()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_writes_into_workspace(self):
        target = mp.write_run_config("workspace-write", self.ws)
        self.assertTrue(target.is_absolute())
        self.assertEqual(target, Path(self.ws) / "mimocode.jsonc")
        self.assertTrue(target.is_file())
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text, mp.render_mimocode_jsonc(
            "workspace-write", self.ws))
        doc = json.loads(text)
        self.assertEqual(doc["permission"]["edit"], "allow")
        leftovers = [p for p in os.listdir(self.ws)
                     if p != "mimocode.jsonc"]
        self.assertEqual(leftovers, [])  # atomic write leaves no tmp files

    def test_path_containment(self):
        target = mp.write_run_config("read-only", self.ws)
        try:
            target.relative_to(Path(os.path.abspath(self.ws)))
        except ValueError:
            self.fail("target escaped the workspace: %r" % target)

    def test_nested_path_created(self):
        target = mp.write_run_config(
            "read-only", self.ws,
            path=os.path.join(self.ws, "sub", "deep", "run.jsonc"))
        self.assertTrue(target.is_file())
        self.assertEqual(target,
                         Path(self.ws) / "sub" / "deep" / "run.jsonc")

    def test_relative_path_stays_in_workspace(self):
        target = mp.write_run_config("read-only", self.ws,
                                     path="conf/mimocode.jsonc")
        self.assertEqual(target, Path(self.ws) / "conf" / "mimocode.jsonc")
        self.assertTrue(target.is_file())

    def test_path_outside_workspace_rejected(self):
        other = _tmpdir()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        outside = os.path.join(other, "mimocode.jsonc")
        self.assertRaises(ValueError, mp.write_run_config, "read-only",
                          self.ws, path=outside)
        self.assertFalse(os.path.exists(outside))
        escape = os.path.join(self.ws, "..", "escaped.jsonc")
        self.assertRaises(ValueError, mp.write_run_config, "read-only",
                          self.ws, path=escape)
        self.assertFalse(os.path.exists(os.path.abspath(escape)))

    def test_never_touches_global_config(self):
        home = Path(os.path.expanduser("~"))
        watched = [home / ".config",
                   home / ".config" / "mimocode",
                   home / ".config" / "mimocode" / mp.CONFIG_NAME]
        before = self._snapshot(watched)  # stat only, never write there
        target = mp.write_run_config("danger-full-access", self.ws)
        self.assertEqual(self._snapshot(watched), before)
        self.assertEqual(target, Path(self.ws) / mp.CONFIG_NAME)

    def test_invalid_preset_writes_nothing(self):
        self.assertRaises(ValueError, mp.write_run_config, "banana", self.ws)
        self.assertEqual(os.listdir(self.ws), [])

    @staticmethod
    def _snapshot(paths):
        out = []
        for path in paths:
            try:
                st = path.stat()
                out.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(path), None, None))
        return out


if __name__ == "__main__":
    unittest.main()
