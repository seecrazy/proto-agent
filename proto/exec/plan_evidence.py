"""Bounded text evidence for reviewed tasks, separate from model tools."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import contextmanager


MAX_OUTPUTS = 4
MAX_OUTPUT_BYTES = 16 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")


class EvidenceError(ValueError):
    pass


def valid_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def valid_path(value):
    return (valid_text(value, 512) and "\\" not in value
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and all(part.casefold() not in ("", ".", "..", ".git", ".coordination")
                    for part in value.split("/")))


def valid_sha256(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def valid_steps(steps):
    return (isinstance(steps, list) and 1 <= len(steps) <= 6
            and all(valid_text(step, 500) for step in steps))


def valid_output_paths(paths):
    return (isinstance(paths, list) and len(paths) <= MAX_OUTPUTS and all(valid_path(path) for path in paths)
            and len({path.casefold() for path in paths}) == len(paths))


def valid_approved_plan(plan):
    if (not isinstance(plan, dict) or not isinstance(plan.get("id"), str)
            or not _RUN_ID.fullmatch(plan["id"]) or not valid_sha256(plan.get("sha256"))
            or not valid_steps(plan.get("steps")) or not isinstance(plan.get("outputs"), list)):
        return False
    outputs = plan["outputs"]
    return (len(outputs) <= MAX_OUTPUTS and all(isinstance(output, dict)
            and set(output) == {"path", "before_sha256"}
            and (output["before_sha256"] is None or valid_sha256(output["before_sha256"]))
            for output in outputs) and valid_output_paths([output["path"] for output in outputs]))


@contextmanager
def _directory(root, parts):
    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in parts:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class OutputEvidence:
    def __init__(self, workspace, project_dir):
        self.root = workspace.root.resolve()
        if project_dir != "" and not valid_path(project_dir):
            raise EvidenceError("invalid_project_dir")
        self.project = project_dir.split("/") if project_dir else []
        self.protected = tuple(path.casefold().rstrip("/") for path in workspace.protected_paths)

    def _allowed(self, parts):
        target = str(self.root.joinpath(*parts)).casefold()
        if any(target == path or target.startswith(path + "/") for path in self.protected):
            raise EvidenceError("output_protected_path")

    def require_project(self):
        self._allowed(self.project)
        try:
            with _directory(self.root, self.project):
                pass
        except OSError as exc:
            raise EvidenceError("project_unavailable") from exc

    def _fingerprint(self, path):
        if not valid_path(path):
            raise EvidenceError("invalid_output_path")
        parts = self.project + path.split("/")
        self._allowed(parts)
        try:
            with _directory(self.root, parts[:-1]) as parent:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise EvidenceError("output_not_regular")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        payload = stream.read(MAX_OUTPUT_BYTES + 1)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            raise EvidenceError("output_unsafe_or_unreadable") from exc
        if len(payload) > MAX_OUTPUT_BYTES:
            raise EvidenceError("output_too_large")
        try:
            text = payload.decode("utf-8")
            if "\x00" in text:
                raise EvidenceError("output_not_utf8_text")
        except UnicodeError as exc:
            raise EvidenceError("output_not_utf8_text") from exc
        return hashlib.sha256(payload).hexdigest(), bool(text.strip())

    def snapshot(self, paths):
        return [{"path": path, "before_sha256": self._fingerprint(path)[0]} for path in paths]

    def verify(self, outputs):
        receipts = []
        for output in outputs:
            path, digest, reason = output["path"], None, ""
            try:
                digest, nonblank = self._fingerprint(path)
                if digest is None:
                    reason = "output_missing"
                elif not nonblank:
                    reason = "output_blank"
                elif digest == output["before_sha256"]:
                    reason = "output_unchanged"
            except EvidenceError as exc:
                reason = str(exc)
            receipts.append({"path": path, "sha256": digest, "passed": not reason, "reason": reason})
        return receipts
