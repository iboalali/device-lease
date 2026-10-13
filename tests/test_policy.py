import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devicelease.policy import PolicyError, load_policy
from devicelease.project import Project

POLICY = {
    "expiryMinutes": 15,
    "deny": ["Pixel_rooted"],
    "projects": {
        "~/Projects/shop-app": {"allow": ["0A1B2C3D4E", "shop_api36_*"]},
        "~/Projects/ui-tests": {"allow": ["uitest_*"], "deny": ["uitest_pixel"]},
        "~/Projects/ui-tests/nested": {"allow": ["Pixel_*"]},
        "~/Projects/open": {"deny": ["Pixel_5"]},
    },
    "defaults": {"allow": ["*"]},
}


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.policy_file = self.home / "policy.json"
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "DEVICELEASE_POLICY": str(self.policy_file),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def project(self, relative: str, repo_file: dict | None = None) -> Project:
        root = self.home / relative
        root.mkdir(parents=True, exist_ok=True)
        if repo_file is not None:
            (root / ".devicelease.json").write_text(json.dumps(repo_file))
        return Project(str(root), str(root))

    def load(self, project: Project, policy=POLICY):
        if policy is not None:
            self.policy_file.write_text(json.dumps(policy))
        return load_policy(project)

    def test_missing_policy_allows_everything(self):
        project = self.project("Projects/any")
        policy = self.load(project, None)
        self.assertIsNone(policy.check(project.root, "Pixel_rooted"))
        self.assertEqual(policy.expiry_minutes, 30)

    def test_global_deny_wins_over_project_allow(self):
        project = self.project("Projects/ui-tests")
        policy = self.load(project, {**POLICY, "deny": ["uitest_api37"]})
        self.assertIn("global deny", policy.check(project.root, "uitest_api37"))

    def test_project_allow_list(self):
        project = self.project("Projects/shop-app")
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "0A1B2C3D4E"))
        self.assertIsNone(policy.check(project.root, "shop_api36_rooted"))
        self.assertIn("allow list", policy.check(project.root, "uitest_api37"))

    def test_project_deny_beats_project_allow(self):
        project = self.project("Projects/ui-tests")
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "uitest_api36"))
        self.assertIn("deny list", policy.check(project.root, "uitest_pixel"))

    def test_unlisted_project_uses_defaults(self):
        project = self.project("Projects/other")
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "uitest_api37"))
        self.assertIn("global deny", policy.check(project.root, "Pixel_rooted"))

    def test_project_without_allow_uses_default_allow(self):
        project = self.project("Projects/open")
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "Pixel_6a"))
        self.assertIn("deny list", policy.check(project.root, "Pixel_5"))

    def test_longest_project_path_wins(self):
        project = self.project("Projects/ui-tests/nested")
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "Pixel_6a"))
        self.assertIsNotNone(policy.check(project.root, "uitest_api36"))

    def test_patterns_match_display_name(self):
        project = self.project("Projects/other")
        policy = self.load(project, {"deny": ["*XR*"]})
        self.assertIn("global deny", policy.check(project.root, "XR_Headset", "XR Headset"))
        self.assertIn("global deny", policy.check(project.root, "0A1B", "XR test phone"))

    def test_repo_file_narrows_allow_list(self):
        project = self.project("Projects/shop-app", {"allow": ["shop_*"]})
        policy = self.load(project)
        self.assertIsNone(policy.check(project.root, "shop_api36_rooted"))
        self.assertIn(".devicelease.json allow", policy.check(project.root, "0A1B2C3D4E"))

    def test_repo_file_cannot_widen_user_policy(self):
        project = self.project("Projects/shop-app", {"allow": ["*"]})
        policy = self.load(project)
        self.assertIsNotNone(policy.check(project.root, "uitest_api37"))
        self.assertIsNotNone(policy.check(project.root, "Pixel_rooted"))

    def test_repo_file_adds_deny(self):
        project = self.project("Projects/shop-app", {"deny": ["shop_api36_plain"]})
        policy = self.load(project)
        self.assertIn(".devicelease.json deny", policy.check(project.root, "shop_api36_plain"))
        self.assertIsNone(policy.check(project.root, "shop_api36_rooted"))

    def test_t3_project_key_wins_over_path(self):
        project = self.project("Projects/shop-app")
        policy = self.load(project, {**POLICY, "projects": {
            **POLICY["projects"], "t3:project-9": {"allow": ["uitest_*"]},
        }})
        self.assertIsNone(policy.check(project.root, "uitest_api37", t3_project="project-9"))
        self.assertIsNotNone(policy.check(project.root, "0A1B2C3D4E", t3_project="project-9"))
        self.assertIsNone(policy.check(project.root, "0A1B2C3D4E", t3_project="project-1"))
        self.assertIsNone(policy.check(project.root, "0A1B2C3D4E"))

    def test_invalid_policy_is_an_error(self):
        project = self.project("Projects/other")
        with self.assertRaises(PolicyError):
            self.load(project, {"deny": "Pixel_5"})
        self.policy_file.write_text("{not json")
        with self.assertRaises(PolicyError):
            load_policy(project)


if __name__ == "__main__":
    unittest.main()
