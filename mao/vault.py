"""Content-addressed prompt versions with tags, diffs, rollback and gated deploys, recorded in the audit chain."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

from .audit import AuditLog

Ref = Union[int, str, None]


class RefError(LookupError):
    """A version reference matched nothing, or matched ambiguously."""


@dataclass(frozen=True)
class Version:
    name: str
    number: int
    digest: str
    text: str
    note: str = ""


class PromptVault:
    """Versions are immutable. Pass an ``AuditLog`` and every commit, tag and deploy is chained into it."""

    def __init__(self, audit: Optional[AuditLog] = None) -> None:
        self._versions: Dict[str, List[Version]] = {}
        self._tags: Dict[Tuple[str, str], Version] = {}
        self.audit = audit

    def _log(self, event: str, **fields: object) -> None:
        if self.audit is not None:
            self.audit.append(event, **fields)

    def commit(self, name: str, text: str, note: str = "") -> Version:
        """Store a new version. Committing text identical to the latest version is a no-op."""
        history = self._versions.setdefault(name, [])
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        if history and history[-1].digest == digest:
            return history[-1]
        version = Version(name, len(history) + 1, digest, text, note)
        history.append(version)
        self._log("prompt_commit", prompt=name, version=version.number, digest=digest)
        return version

    def get(self, name: str, ref: Ref = None) -> Version:
        history = self._versions.get(name)
        if not history:
            raise RefError(f"unknown prompt: {name}")
        if ref is None:
            return history[-1]
        if isinstance(ref, int):
            if 1 <= ref <= len(history):
                return history[ref - 1]
            raise RefError(f"{name} has no version {ref}")
        if (name, ref) in self._tags:
            return self._tags[(name, ref)]
        matches = [v for v in history if v.digest.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        raise RefError(f"ref '{ref}' matched {len(matches)} versions of {name}")

    def tag(self, name: str, tag: str, ref: Ref = None) -> Version:
        version = self.get(name, ref)
        self._tags[(name, tag)] = version
        self._log("prompt_tag", prompt=name, tag=tag, version=version.number, digest=version.digest)
        return version

    def diff(self, name: str, a: Ref, b: Ref) -> str:
        left, right = self.get(name, a), self.get(name, b)
        return "\n".join(difflib.unified_diff(left.text.splitlines(), right.text.splitlines(),
                                              fromfile=f"{name}@v{left.number}", tofile=f"{name}@v{right.number}", lineterm=""))

    def rollback(self, name: str, ref: Ref) -> Version:
        """Re-commit an old version as the newest. History is never rewritten."""
        old = self.get(name, ref)
        return self.commit(name, old.text, note=f"rollback to v{old.number}")

    def deploy(self, name: str, tag: str, gate: Callable[[Version], bool], ref: Ref = None) -> bool:
        """Move ``tag`` to a version only if ``gate`` passes. A gate that raises is a refusal."""
        version = self.get(name, ref)
        try:
            passed = bool(gate(version))
        except Exception:
            passed = False
        self._log("prompt_deploy", prompt=name, tag=tag, version=version.number, digest=version.digest, passed=passed)
        if passed:
            self._tags[(name, tag)] = version
        return passed
