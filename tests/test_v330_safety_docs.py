"""3.15.0 docs contract: per-OS limits match what the boundary suite is designed to prove."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def limits_bullets():
    text = read("README.md")
    start = text.index("## Limits")
    end = text.index("\n## ", start + 1)
    return [line for line in text[start:end].splitlines() if line.startswith("- ")]


class SafetyDocsTest(unittest.TestCase):
    def test_readme_limits_are_four_per_os_bullets(self):
        bullets = limits_bullets()
        self.assertEqual(len(bullets), 4, bullets)
        body = "\n".join(bullets).lower()
        for word in ("macos", "linux", "windows", "reviewer"):
            self.assertIn(word, body)
        for word in ("seatbelt", "landlock", "write-restricted",
                     "unsandboxed", "require"):
            self.assertIn(word, body)

    def test_old_linux_windows_sentence_is_gone(self):
        self.assertNotIn("Linux and Windows rely", read("README.md"))

    def test_readme_stays_short_and_plain(self):
        text = read("README.md")
        self.assertLessEqual(len(text.splitlines()), 110)
        self.assertNotIn("$", text)
        self.assertIn("CrewForeman 3.15.0", text)

    def test_limitations_has_full_per_os_text(self):
        text = read("docs/LIMITATIONS.md")
        for word in ("macOS", "Linux", "Windows", "Seatbelt", "Landlock",
                     "write-restricted", "UNSANDBOXED", "require",
                     "delete_outside=unverified", "UNC", "ABI 1-2",
                     "network", "Reviewer"):
            self.assertIn(word, text)
        self.assertIn("## Platform boundaries (3.15.0)", text)

    def test_install_documents_linux_and_windows(self):
        text = " ".join(read("docs/INSTALL.md").lower().split())
        for word in ("linux", "windows", "landlock", "write-restricted",
                     "kernel 6.2", "standard user", "unc"):
            self.assertIn(word, text)

    def test_operations_documents_modes_and_keys(self):
        text = read("docs/OPERATIONS.md")
        for word in ("auto", "off", "require", "UNSANDBOXED", "degraded",
                     "linux_sandbox", "linux_min_abi", "windows_sandbox",
                     "sandbox_extra_writes", "sandbox_probe_ttl",
                     "sandbox_canary", "DEEPSEEK_MCP_LINUX_SANDBOX",
                     "DEEPSEEK_MCP_WINDOWS_SANDBOX",
                     "DEEPSEEK_MCP_SANDBOX_EXTRA_WRITES",
                     "DEEPSEEK_MCP_SANDBOX_PROBE_TTL",
                     "DEEPSEEK_MCP_SANDBOX_CANARY"):
            self.assertIn(word, text)

    def test_windows_ci_claim_is_admin_scoped_and_not_pending(self):
        limitations = read("docs/LIMITATIONS.md")
        operations = read("docs/OPERATIONS.md")
        install = read("docs/INSTALL.md")
        self.assertIn("windows-2025", limitations)
        self.assertIn("Administrator", limitations)
        self.assertIn("delete_outside=unverified", limitations)
        for text in (limitations, operations, install):
            self.assertNotIn("Windows CI is PENDING", text)

    def test_changelog_has_current_release(self):
        text = read("CHANGELOG.md")
        self.assertIn("## 3.15.0", text)
        self.assertIn("## 3.14.3", text)
        self.assertIn("## 3.13.1", text)
        self.assertLess(text.index("## 3.15.0"), text.index("## 3.14.3"))
        self.assertLess(text.index("## 3.14.3"), text.index("## 3.13.1"))
        self.assertLess(text.index("## 3.13.1"), text.index("## 3.12.2"))
        self.assertIn("windows-2025", text)
        for word in ("Windows", "Linux", "corrupt", "cleanup", "zero"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()
