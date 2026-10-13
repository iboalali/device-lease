import json
import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from devicelease import paths
from devicelease.project import Project

DEFAULT_EXPIRY_MINUTES = 30
REPO_FILE = ".devicelease.json"


class PolicyError(Exception):
    pass


@dataclass
class Rules:
    allow: list[str] | None = None
    deny: list[str] = field(default_factory=list)


@dataclass
class Policy:
    expiry_minutes: float = DEFAULT_EXPIRY_MINUTES
    deny: list[str] = field(default_factory=list)
    projects: dict[str, Rules] = field(default_factory=dict)
    defaults: Rules = field(default_factory=lambda: Rules(allow=["*"]))
    repo: Rules | None = None
    source: str | None = None
    repo_source: str | None = None

    def project_rules(self, root: str, t3_project: str | None = None) -> tuple[str | None, Rules]:
        """The project entry that applies: `t3:<project id>` first, then the longest matching
        path, then the defaults."""
        if t3_project and f"t3:{t3_project}" in self.projects:
            key = f"t3:{t3_project}"
            return key, self.projects[key]
        best_key, best_len = None, -1
        for key in self.projects:
            if key.startswith("t3:"):
                continue
            path = os.path.realpath(os.path.expanduser(key))
            if (root == path or root.startswith(path + os.sep)) and len(path) > best_len:
                best_key, best_len = key, len(path)
        if best_key is None:
            return None, self.defaults
        return best_key, self.projects[best_key]

    def check(self, root: str, device_id: str, name: str | None = None,
              t3_project: str | None = None) -> str | None:
        """Returns None when the device is usable in this project, otherwise the reason it is not."""
        names = [device_id] + ([name] if name else [])

        def hit(patterns):
            return next((p for p in patterns if any(fnmatchcase(n, p) for n in names)), None)

        if pattern := hit(self.deny):
            return f"global deny list ({pattern})"
        key, rules = self.project_rules(root, t3_project)
        label = f"project {key}" if key else "defaults"
        if pattern := hit(rules.deny):
            return f"{label} deny list ({pattern})"
        if not hit(rules.allow or []):
            return f"not in {label} allow list"
        if self.repo is not None:
            if pattern := hit(self.repo.deny):
                return f"{REPO_FILE} deny list ({pattern})"
            if self.repo.allow is not None and not hit(self.repo.allow):
                return f"not in {REPO_FILE} allow list"
        return None


def _patterns(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise PolicyError(f"{where} must be a list of strings")
    return value


def _rules(value, where: str, default_allow) -> Rules:
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be an object")
    allow = value.get("allow", default_allow)
    return Rules(
        allow=None if allow is None else _patterns(allow, f"{where}.allow"),
        deny=_patterns(value.get("deny", []), f"{where}.deny"),
    )


def _read(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise PolicyError(f"cannot read {path}: {e}") from e


def load_policy(project: Project) -> Policy:
    policy = Policy()
    path = paths.policy_path()
    data = _read(path)
    if data is not None:
        if not isinstance(data, dict):
            raise PolicyError(f"{path} must contain a JSON object")
        policy.source = str(path)
        expiry = data.get("expiryMinutes", DEFAULT_EXPIRY_MINUTES)
        if not isinstance(expiry, (int, float)) or expiry <= 0:
            raise PolicyError(f"{path}: expiryMinutes must be a positive number")
        policy.expiry_minutes = expiry
        policy.deny = _patterns(data.get("deny", []), f"{path}: deny")
        projects = data.get("projects", {})
        if not isinstance(projects, dict):
            raise PolicyError(f"{path}: projects must be an object")
        # A listed project without its own allow list falls back to the default allow list.
        defaults = _rules(data.get("defaults", {"allow": ["*"]}), f"{path}: defaults", ["*"])
        policy.defaults = defaults
        policy.projects = {
            key: _rules(value, f"{path}: projects.{key}", defaults.allow)
            for key, value in projects.items()
        }

    repo_path = Path(project.checkout) / REPO_FILE
    repo = _read(repo_path)
    if repo is not None:
        policy.repo = _rules(repo, str(repo_path), None)
        policy.repo_source = str(repo_path)
    return policy
