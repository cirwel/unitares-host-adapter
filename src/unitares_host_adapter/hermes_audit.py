"""Read-only installed Hermes plugin audit, independent of UNITARES identity.

Run with ``python -m unitares_host_adapter.hermes_audit --plugin-dir PATH
--python HERMES_PYTHON``. Exit codes: 0 verified, 1 drift, 2 unknown. The runtime
probe models a fresh plugin load; it does not inspect an already running agent.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_UPSTREAM_URL = "https://github.com/CIRWEL/unitares-host-adapter.git"
_TIMEOUT_SECONDS = 15
_MAX_OUTPUT = 65536
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,240}\Z")

# Mirror the directory entry point's import-first / src-fallback behavior, but
# never register hooks or construct an adapter. -I excludes cwd/PYTHONPATH and
# -B prevents writing bytecode; installed site-packages remain visible.
_RUNTIME_PROBE = r'''
import contextlib
import hashlib
import json
import os
import sys
from pathlib import Path


def manifest(root):
    """Hash a bounded Python source tree, without following directory symlinks."""
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            with path.open("rb") as source:
                data = source.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024 or len(result) >= 256:
                raise ValueError("source limit")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    if "__init__.py" not in result or "bindings/hermes.py" not in result:
        raise ValueError("incomplete source")
    return result


def probe():
    """Report paths and source matches from a fresh, identity-neutral import."""
    expected = Path(sys.argv[1]).resolve() / "src" / "unitares_host_adapter"
    with open(os.devnull, "w") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            mode = "environment"
            try:
                from unitares_host_adapter.bindings import hermes
            except ImportError:
                mode = "plugin_src_fallback"
                sys.path.insert(0, str(expected.parent))
                from unitares_host_adapter.bindings import hermes
            import unitares_host_adapter as package
    actual = Path(package.__file__).resolve().parent
    expected_sources, actual_sources = manifest(expected), manifest(actual)
    paths_match = actual == expected
    sources_match = expected_sources == actual_sources
    loaded_count = 0
    for name, module in list(sys.modules.items()):
        if name != "unitares_host_adapter" and not name.startswith("unitares_host_adapter."):
            continue
        loaded_count += 1
        if loaded_count > 256:
            raise ValueError("module limit")
        path = Path(module.__file__).resolve()
        parts = name.split(".")[1:]
        relative = Path(*parts, "__init__.py") if hasattr(module, "__path__") else Path(*parts).with_suffix(".py")
        paths_match = paths_match and path == expected / relative
        with path.open("rb") as source:
            data = source.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("source limit")
        sources_match = sources_match and expected_sources.get(relative.as_posix()) == hashlib.sha256(data).hexdigest()
    return {
        "load_mode": mode,
        "package_path": str(actual)[:512],
        "binding_path": str(Path(hermes.__file__).resolve())[:512],
        "paths_match": paths_match,
        "source_matches": sources_match,
        "source_files": len(expected_sources),
        "loaded_modules": loaded_count,
        "installed_source_sha256": hashlib.sha256(json.dumps(expected_sources, sort_keys=True).encode()).hexdigest(),
        "loaded_source_sha256": hashlib.sha256(json.dumps(actual_sources, sort_keys=True).encode()).hexdigest(),
    }


try:
    result = probe()
except Exception:
    result = {"error": "runtime_probe_failed"}
print(json.dumps(result, sort_keys=True))
'''


def _run(command: list[str], plugin: Path) -> str | None:
    """Run a bounded read-only probe; never return command stderr or secrets."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("UNITARES_")
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=5",
        }
    )
    try:
        result = subprocess.run(
            command,
            cwd=plugin,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode or len(result.stdout) > _MAX_OUTPUT:
        return None
    return result.stdout


def _origin_category(origin: str | None) -> str:
    """Classify origin without publishing URLs, usernames, or embedded tokens."""
    if not origin or not origin.strip():
        return "unknown"
    value = origin.strip()
    if value.startswith("file:") or (":" not in value and "://" not in value):
        return "local"
    return "remote"


def _valid_ref(ref: str) -> bool:
    """Accept only one exact ref, never wildcard listings or Git options."""
    return (
        bool(_REF.fullmatch(ref))
        and not any(item in ref for item in ("..", "//", "/."))
        and not ref.endswith(("/", ".", ".lock"))
    )


def _runtime_evidence(raw: str | None) -> dict[str, Any] | None:
    """Validate the small probe schema instead of echoing arbitrary output."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("load_mode") not in ("environment", "plugin_src_fallback"):
        return None
    for key in ("paths_match", "source_matches"):
        if type(data.get(key)) is not bool:
            return None
    for key in ("source_files", "loaded_modules"):
        if type(data.get(key)) is not int or not 1 <= data[key] <= 256:
            return None
    for key in ("package_path", "binding_path"):
        if not isinstance(data.get(key), str) or len(data[key]) > 512:
            return None
    for key in ("installed_source_sha256", "loaded_source_sha256"):
        if not isinstance(data.get(key), str) or not re.fullmatch(
            r"[0-9a-f]{64}", data[key]
        ):
            return None
    return {
        key: data[key]
        for key in (
            "load_mode",
            "paths_match",
            "source_matches",
            "source_files",
            "loaded_modules",
            "package_path",
            "binding_path",
            "installed_source_sha256",
            "loaded_source_sha256",
        )
    }


def audit_plugin(
    plugin_dir: Path | str,
    python: str,
    *,
    upstream_ref: str = "refs/heads/master",
    upstream_url: str = DEFAULT_UPSTREAM_URL,
) -> dict[str, Any]:
    """Compare installed Git/source and fresh plugin imports, without writes.

    Upstream is explicitly configured, never inferred from the installed origin:
    catalog installations may have a stale local clone as their origin.
    """
    evidence: dict[str, Any] = {
        "status": "unknown",
        "issues": [],
        "git": {
            "head": None,
            "upstream_head": None,
            "worktree_clean": None,
            "origin_category": "unknown",
        },
        "runtime": {
            "scope": "isolated_plugin_load_not_live_process",
            "status": "unknown",
        },
    }
    issues = evidence["issues"]
    if not _valid_ref(upstream_ref) or not upstream_url or upstream_url.startswith("-"):
        issues.append("invalid_upstream")
        return evidence
    evidence["git"]["upstream_ref"] = upstream_ref
    evidence["git"]["upstream_source"] = (
        "canonical" if upstream_url == DEFAULT_UPSTREAM_URL else "explicit"
    )
    try:
        plugin = Path(plugin_dir).expanduser().resolve()
        present = (
            plugin.is_dir() and (plugin / "src" / "unitares_host_adapter").is_dir()
        )
    except (OSError, ValueError, RuntimeError):
        issues.append("plugin_unavailable")
        return evidence
    if not present:
        issues.append("plugin_unavailable")
        return evidence

    def git(*args: str) -> str | None:
        """Disable optional Git index locks and interactive credential helpers."""
        return _run(
            ["git", "--no-optional-locks", "-c", "credential.helper=", *args], plugin
        )

    drift = False
    unknown = False
    root = git("rev-parse", "--show-toplevel")
    try:
        root_matches = root is not None and Path(root.strip()).resolve() == plugin
    except (OSError, ValueError, RuntimeError):
        root_matches = False
    if not root_matches:
        issues.append("git_unavailable_or_wrong_root")
        unknown = True
    else:
        head = git("rev-parse", "--verify", "HEAD")
        if head is None or not _SHA.fullmatch(head.strip()):
            issues.append("installed_head_unavailable")
            unknown = True
        else:
            evidence["git"]["head"] = head.strip()
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        if status is None:
            issues.append("worktree_status_unavailable")
            unknown = True
        else:
            evidence["git"]["worktree_clean"] = not bool(status)
            if status:
                issues.append("worktree_dirty")
                drift = True
        origin = git("remote", "get-url", "origin")
        evidence["git"]["origin_category"] = _origin_category(origin)

    remote = git("ls-remote", "--exit-code", "--refs", "--", upstream_url, upstream_ref)
    fields = remote.split() if remote is not None else []
    if len(fields) != 2 or not _SHA.fullmatch(fields[0]) or fields[1] != upstream_ref:
        issues.append("upstream_unavailable")
        unknown = True
    else:
        evidence["git"]["upstream_head"] = fields[0]
        if evidence["git"]["head"] and evidence["git"]["head"] != fields[0]:
            issues.append("upstream_sha_mismatch")
            drift = True

    runtime = _runtime_evidence(
        _run([python, "-I", "-B", "-c", _RUNTIME_PROBE, str(plugin)], plugin)
    )
    if runtime is None:
        issues.append("runtime_unavailable")
        unknown = True
    else:
        evidence["runtime"].update(runtime)
        runtime_drift = not runtime["paths_match"] or not runtime["source_matches"]
        evidence["runtime"]["status"] = "drift" if runtime_drift else "ok"
        if not runtime["paths_match"]:
            issues.append("runtime_path_mismatch")
        if not runtime["source_matches"]:
            issues.append("runtime_source_mismatch")
        drift = drift or runtime_drift
    evidence["status"] = "drift" if drift else "unknown" if unknown else "ok"
    return evidence


def main(argv: list[str] | None = None) -> int:
    """Emit bounded JSON evidence and a fail-closed automation exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--upstream-ref", default="refs/heads/master")
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL)
    args = parser.parse_args(argv)
    evidence = audit_plugin(
        args.plugin_dir,
        args.python,
        upstream_ref=args.upstream_ref,
        upstream_url=args.upstream_url,
    )
    print(json.dumps(evidence, sort_keys=True))
    return {"ok": 0, "drift": 1, "unknown": 2}[evidence["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
