"""Offline evidence and failure-mode tests for the installed-plugin audit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from unitares_host_adapter import hermes_audit as audit


HEAD = "a" * 40
UPSTREAM = "b" * 40


def _plugin(tmp_path: Path) -> Path:
    """Create an importable dependency-free adapter fixture."""
    plugin = tmp_path / "plugin"
    package = plugin / "src" / "unitares_host_adapter"
    (package / "bindings").mkdir(parents=True)
    for name in ("__init__.py", "bindings/__init__.py", "bindings/hermes.py"):
        (package / name).write_text("# fixture\n", encoding="utf-8")
    (plugin / "__init__.py").write_text("# plugin entry\n", encoding="utf-8")
    return plugin


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    plugin: Path,
    *,
    upstream: str = HEAD,
    dirty: str = "",
    network_error: bool = False,
) -> list[list[str]]:
    """Stub only Git; the runtime Python subprocess stays real and offline."""
    real_run = subprocess.run
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return deterministic Git evidence without network access."""
        if command[0] != "git":
            assert "-I" in command and "-B" in command
            # Fixture imports must not depend on this machine's site-packages.
            return real_run([command[0], "-S", *command[1:]], **kwargs)
        calls.append(command)
        if "ls-remote" in command:
            assert audit.DEFAULT_UPSTREAM_URL in command
            assert "origin" not in command
            if network_error:
                return subprocess.CompletedProcess(command, 128, "")
            output = upstream + "\trefs/heads/master\n"
        elif "--show-toplevel" in command:
            output = str(plugin) + "\n"
        elif "rev-parse" in command:
            output = HEAD + "\n"
        elif "status" in command:
            output = dirty
        elif "get-url" in command:
            output = "file:///private/local/clone\n"
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(audit.subprocess, "run", run)
    return calls


def test_good_plugin_fallback_is_not_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare Python without the package can load the installed plugin source."""
    plugin = _plugin(tmp_path)
    calls = _fake_git(monkeypatch, plugin)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "ok"
    assert evidence["git"]["origin_category"] == "local"
    assert evidence["runtime"]["load_mode"] == "plugin_src_fallback"
    assert evidence["runtime"]["source_matches"] is True
    assert evidence["runtime"]["package_path"] == str(
        plugin / "src" / "unitares_host_adapter"
    )
    assert calls
    assert not list(plugin.rglob("*.pyc"))
    assert "file://" not in json.dumps(evidence)


def test_stale_sha_is_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream tip, not a local origin clone, decides freshness."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin, upstream=UPSTREAM)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "drift"
    assert "upstream_sha_mismatch" in evidence["issues"]


@pytest.mark.parametrize(
    "dirty", [" M src/unitares_host_adapter/core.py\0", "?? extra.py\0"]
)
def test_dirty_and_untracked_python_are_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: str
) -> None:
    """Both tracked edits and untracked source must prevent a green audit."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin, dirty=dirty)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "drift"
    assert evidence["git"]["worktree_clean"] is False


@pytest.mark.parametrize("changed_bytes", [False, True])
def test_wrong_runtime_copy_is_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_bytes: bool
) -> None:
    """Both identical shadow copies and different installed bytes are drift."""
    plugin = _plugin(tmp_path)
    wrong = _plugin(tmp_path / "other") / "src"
    if changed_bytes:
        (wrong / "unitares_host_adapter" / "bindings" / "hermes.py").write_text(
            "# older binding\n", encoding="utf-8"
        )
    _fake_git(monkeypatch, plugin)
    real_run = audit.subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Emulate a site-packages installation, without changing test Python."""
        if command[0] != "git":
            command = list(command)
            index = command.index("-c") + 1
            command[index] = (
                f"import sys; sys.path.insert(0, {str(wrong)!r});\n" + command[index]
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", run)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "drift"
    assert evidence["runtime"]["source_matches"] is (not changed_bytes)
    assert evidence["runtime"]["paths_match"] is False
    assert "runtime_path_mismatch" in evidence["issues"]


def test_network_unavailable_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local match cannot turn failed upstream discovery green."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin, network_error=True)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "unknown"
    assert "upstream_unavailable" in evidence["issues"]


def test_missing_plugin_is_unknown(tmp_path: Path) -> None:
    """A nonexistent installation is not a successful empty comparison."""
    evidence = audit.audit_plugin(tmp_path / "absent", sys.executable)
    assert evidence["status"] == "unknown"


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), subprocess.TimeoutExpired("git", 1)]
)
def test_missing_git_or_timeout_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Missing tooling and timed-out discovery yield bounded unknown evidence."""
    plugin = _plugin(tmp_path)
    real_run = subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Fail Git without changing the isolated runtime probe."""
        if command[0] == "git":
            raise failure
        return real_run([command[0], "-S", *command[1:]], **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", run)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "unknown"
    assert len(json.dumps(evidence)) < 8192


def test_cli_json_and_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Automation receives parseable JSON and distinct nonzero failure exits."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin, upstream=UPSTREAM)
    assert audit.main(["--plugin-dir", str(plugin), "--python", sys.executable]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "drift"


def test_cli_rejects_unbounded_ref_without_invoking_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Wildcard refs must not request an arbitrarily large remote ref listing."""
    plugin = _plugin(tmp_path)
    calls = _fake_git(monkeypatch, plugin)
    assert (
        audit.main(
            [
                "--plugin-dir",
                str(plugin),
                "--python",
                sys.executable,
                "--upstream-ref",
                "refs/heads/*",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"
    assert not calls


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), subprocess.TimeoutExpired("python", 1)]
)
def test_runtime_failure_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """An unavailable interpreter or a hung import is never green."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin)
    git_run = audit.subprocess.run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Keep known Git evidence while failing the runtime subprocess."""
        if command[0] != "git":
            raise failure
        return git_run(command, **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", run)
    evidence = audit.audit_plugin(plugin, sys.executable)
    assert evidence["status"] == "unknown"
    assert evidence["runtime"]["status"] == "unknown"
    assert "runtime_unavailable" in evidence["issues"]


def test_custom_upstream_and_secret_origin_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit upstream overrides are supported without exposing credential URLs."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin)
    fake_run = audit.subprocess.run
    custom_url = "https://token:secret@example.invalid/adapter.git"

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Accept a custom upstream and simulate a credential-bearing origin."""
        if "ls-remote" in command:
            assert custom_url in command
            return subprocess.CompletedProcess(
                command, 0, HEAD + "\trefs/heads/master\n"
            )
        if "get-url" in command:
            return subprocess.CompletedProcess(command, 0, custom_url + "\n")
        return fake_run(command, **kwargs)

    monkeypatch.setattr(audit.subprocess, "run", run)
    evidence = audit.audit_plugin(plugin, sys.executable, upstream_url=custom_url)
    assert evidence["status"] == "ok"
    assert evidence["git"]["origin_category"] == "remote"
    assert "secret" not in json.dumps(evidence)
    assert "example.invalid" not in json.dumps(evidence)


def test_cli_good_state_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A fully verified state is the only zero exit."""
    plugin = _plugin(tmp_path)
    _fake_git(monkeypatch, plugin)
    assert audit.main(["--plugin-dir", str(plugin), "--python", sys.executable]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
