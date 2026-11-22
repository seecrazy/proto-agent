"""Workspace-local, paged declaration search. No source text is cached."""
from __future__ import annotations

import ast
import os
import re
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from proto.exec.work_context import clipped, size


PAGE_FILES = 256
PAGE_BYTES = 8 * 1024 * 1024
FILE_BYTES = 512 * 1024
FILE_SYMBOLS = 128
CACHE_FILES = 1024
IGNORED = {"node_modules", "vendor", "build", "dist", "target", "coverage", "Pods", "DerivedData",
           "__pycache__", "venv", "_archived", "Carthage"}
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
PARSED = JS | {".py", ".pyi", ".swift"}
SOURCE = PARSED | {".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm",
                   ".cs", ".rb", ".php", ".sh", ".sql", ".vue", ".svelte"}
_JS_DECL = re.compile(r"^\s*(?:(?:export|default|declare|async|abstract)\s+)*(?:function\s*\*?\s*|"
                      r"(?:class|interface|type|enum|const|let)\s+)([\w$]+)")
_SWIFT_DECL = re.compile(r"^\s*(?:(?:public|private|fileprivate|internal|open|final|static|override|"
                         r"mutating|nonisolated|class)\s+)*(?:func|class|struct|actor|protocol|enum|extension)\s+(\w+)")


def _terms(text):
    words = re.sub(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])", r"\1\3 \2\4", text)
    return set(re.findall(r"[^\W_]+", words.casefold()))


def _python_symbols(tree, prefix=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            if isinstance(node, ast.ClassDef):
                signature = "class " + name
            else:
                args = node.args
                params = [item.arg for item in (*args.posonlyargs, *args.args)]
                if args.vararg or args.kwonlyargs:
                    params.append("*" + (args.vararg.arg if args.vararg else ""))
                params.extend(item.arg for item in args.kwonlyargs)
                if args.kwarg:
                    params.append("**" + args.kwarg.arg)
                signature = name + "(" + ", ".join(params) + ")"
            doc = (ast.get_docstring(node, clean=False) or "").strip()[:128].splitlines()
            yield (name[:160], node.lineno, node.end_lineno, signature[:180], doc[0] if doc else "")
            yield from _python_symbols(node, name + ".")
        elif isinstance(node, (ast.stmt, ast.ExceptHandler)):
            yield from _python_symbols(node, prefix)


def _declarations(path, text):
    suffix = path.suffix.lower()
    if suffix in (".py", ".pyi"):
        yield from _python_symbols(ast.parse(text))
        return
    pattern = _SWIFT_DECL if suffix == ".swift" else _JS_DECL
    for number, line in enumerate(text.splitlines(), 1):
        match = pattern.match(line)
        if match:
            signature = line.strip().split("{", 1)[0].split("=", 1)[0].strip()[:180]
            yield (match[1][:160], number, number, signature, "")


class CodeIndex:
    def __init__(self, root, protected=()):
        self.root = Path(root).resolve()
        self.protected = {Path(path) for path in protected}
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def path(self, relative, *, within=None, follow_final_symlink=False):
        rel = Path(relative)
        if (rel.is_absolute() or ".." in rel.parts or any(ord(char) < 32 for char in str(rel))
                or any(part.startswith(".") or part in IGNORED for part in rel.parts)):
            raise ValueError("invalid_exploration_path")
        target = self.root / rel
        resolved = target.resolve()
        allowed = target if not follow_final_symlink else resolved
        if ((not follow_final_symlink and resolved != target)
                or not allowed.is_relative_to(self.root)
                or (within is not None and not allowed.is_relative_to(within))
                or self.protected.intersection((allowed, *allowed.parents))):
            raise ValueError("exploration_path_outside_scope")
        return target

    @contextmanager
    def open_file(self, path, *, follow_final_symlink=False):
        target = self.path(path.relative_to(self.root), follow_final_symlink=follow_final_symlink)
        if follow_final_symlink:
            target = target.resolve()
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not_regular_source")
            yield stream

    def files(self, base, after=(), *, follow_symlinks=False):
        def walk(directory):
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
            for entry in entries:
                if (entry.name.startswith(".") or entry.name in IGNORED
                        or entry.name.endswith((".min.js", ".min.css", ".generated.swift"))):
                    continue
                path = Path(entry.path)
                key = path.relative_to(self.root).parts
                if path in self.protected or (key <= after and after[:len(key)] != key):
                    continue
                # Only final-file links may be followed. Directory links can loop
                # or move an entire subtree outside the workspace.
                if entry.is_dir(follow_symlinks=False):
                    yield from walk(path)
                elif key > after and entry.is_file(follow_symlinks=follow_symlinks):
                    yield path
        yield from walk(base)

    def _symbols(self, path, info):
        stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
        with self.lock:
            cached = self.cache.get(path)
            if cached and cached[0] == stamp:
                self.cache.move_to_end(path)
                return cached[1]
        rows, mode = [], "indexed"
        try:
            with self.open_file(path) as stream:
                raw = stream.read(FILE_BYTES + 1)
            if b"\0" in raw or len(raw) > FILE_BYTES:
                return (), "skipped"
            for row in _declarations(path, raw.decode("utf-8-sig")):
                if len(rows) == FILE_SYMBOLS:
                    mode = "limited"
                    break
                rows.append(row)
        except (SyntaxError, RecursionError):
            rows, mode = [], "file_only"
        except (OSError, ValueError):
            return (), "skipped"
        with self.lock:
            self.cache[path] = stamp, (rows, mode)
            self.cache.move_to_end(path)
            while len(self.cache) > CACHE_FILES:
                self.cache.popitem(last=False)
        return rows, mode

    def search(self, query, cwd="", cursor="", limit=6):
        base = self.path(cwd)
        if not base.is_dir() or not query.strip():
            raise ValueError("expected_directory_and_nonempty_query")
        after = self.path(cursor, within=base).relative_to(self.root).parts if cursor else ()
        terms = _terms(query)
        ranked = []
        coverage = dict(visited=0, indexed=0, file_only=0, limited=0, skipped=0)
        consumed, previous, following = 0, "", ""
        for path in self.files(base, after):
            relative = path.relative_to(self.root).as_posix()
            suffix = path.suffix.lower()
            try:
                info = path.lstat()
            except OSError:
                info = None
            cost = info.st_size if info and suffix in PARSED and info.st_size <= FILE_BYTES else 0
            if coverage["visited"] and (coverage["visited"] >= PAGE_FILES or consumed + cost > PAGE_BYTES):
                following = previous
                break
            coverage["visited"] += 1
            previous, consumed = relative, consumed + cost
            if not info or suffix not in SOURCE or not stat.S_ISREG(info.st_mode):
                coverage["skipped"] += 1
                continue
            rows, mode = self._symbols(path, info) if suffix in PARSED and info.st_size <= FILE_BYTES else ((), "file_only")
            coverage[mode] += 1
            if mode == "skipped":
                continue
            path_score = len(terms & _terms(relative))
            for name, start, end, signature, doc in rows or [("", 1, 1, "", "")]:
                score = (path_score + 6 * len(terms & _terms(name)) + 2 * len(terms & _terms(signature))
                         + len(terms & _terms(doc)) + 12 * (name.casefold() == query.strip().casefold()))
                if score:
                    ranked.append((-score, relative, start, end, name, signature))
            if len(ranked) > limit:
                ranked = sorted(ranked)[:limit]
        hits = [{"path": path, "line": start, "end_line": end, "symbol": name, "signature": signature}
                for _, path, start, end, name, signature in sorted(ranked)[:limit]]
        return {"next_cursor": following or None, "coverage": coverage, "hits": hits}

    def read_window(self, path, base, offset, limit):
        target = self.path(path, within=base)
        if target.suffix.lower() not in SOURCE or target.name.endswith((".min.js", ".generated.swift")):
            raise ValueError("not_explorable_source")
        lines, budget = [], 2400
        with self.open_file(target) as stream:
            if os.fstat(stream.fileno()).st_size > FILE_BYTES or b"\0" in stream.peek(4096):
                raise ValueError("source_binary_or_too_large")
            for number, raw in enumerate(stream, 1):
                if number <= offset:
                    continue
                line = clipped(raw.decode("utf-8").rstrip("\r\n"), budget)
                lines.append(line)
                budget -= size(line) + 1
                if len(lines) >= limit or budget < 8:
                    break
        if not lines:
            raise ValueError("empty_source_window")
        return {"path": path, "line": offset + 1, "end_line": offset + len(lines)}, "\n".join(lines)
