"""RN-001, RN-003 and RN-004: the package, the command and the product text are named kerbtap."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import kerbtap
from kerbtap.cli import build_parser

TOOL_DIR = Path(__file__).resolve().parent.parent
REPO = TOOL_DIR.parent


def _subcommands() -> list[str]:
    """The subcommand names, read from the real parser."""
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("the parser has no subcommands")


# A `governor` CLI command: the old module entry point, a subcommand of the real parser, or a flag.
OLD_COMMAND = re.compile(
    r"python3? -m governor\b"
    r"|(?<![\w./@-])governor (" + "|".join(re.escape(n) for n in _subcommands()) + r")\b"
    r"|(?<![\w./@-])governor -"
)
# The old brand name. `Governor-Review` is the commit trailer, a wire string the proposal keeps.
OLD_BRAND = re.compile(r"Governor(?!-Review)")


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *patterns], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split("\0") if p]


def _hits(files: list[Path], pattern: re.Pattern[str]) -> list[str]:
    found = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for number, line in enumerate(lines, 1):
            if pattern.search(line):
                found.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
    return found


def test_package_is_kerbtap():
    meta = tomllib.loads((TOOL_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["name"] == "kerbtap"
    assert meta["project"]["version"] == "0.5.0"
    assert meta["project"]["scripts"] == {"kerbtap": "kerbtap.cli:main"}
    assert any(dep.startswith("setuptools") for dep in meta["project"]["optional-dependencies"]["dev"])
    assert kerbtap.__version__ == "0.5.0"
    assert not (TOOL_DIR / "governor").exists()


def test_package_wheel_installs_the_kerbtap_command(tmp_path):
    """Build the wheel offline, install it in a new venv, run the command. A missing backend is a failure."""
    wheels = tmp_path / "wheels"
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(wheels), str(TOOL_DIR)],
        capture_output=True, text=True,
    )
    assert built.returncode == 0, f"the wheel did not build (is setuptools installed?):\n{built.stdout[-800:]}\n{built.stderr[-800:]}"
    wheel = next(wheels.glob("kerbtap-0.5.0-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
        licence = [n for n in names if ".dist-info/" in n and n.rsplit("/", 1)[-1].startswith("LICENSE")]
        assert licence, f"no licence file in the wheel: {names[:5]}"
        licence_text = archive.read(licence[0]).decode()
    assert "License-Expression: Apache-2.0" in metadata, metadata[:600]
    assert "Apache License" in licence_text and "Version 2.0" in licence_text
    env_dir = tmp_path / "venv"
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}  # a source path hides the install
    subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    installed = subprocess.run(
        [str(bindir / "python"), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
        capture_output=True, text=True, env=clean,
    )
    assert installed.returncode == 0, installed.stderr[-800:]
    ran = subprocess.run([str(bindir / "kerbtap"), "--version"], capture_output=True, text=True, env=clean)
    assert ran.returncode == 0, ran.stderr
    assert "0.5.0" in ran.stdout
    assert not list(bindir.glob("governor*"))


def test_no_governor_command_in_product_text():
    files = _tracked(
        "starter", "global", "tool/kerbtap", "README.md", "INSTALL.md", "USER-MANUAL.md",
    )
    hits = _hits(files, OLD_COMMAND)
    assert not hits, "a governor command is left:\n" + "\n".join(hits)


def test_no_governor_brand_in_messages_or_user_docs():
    files = _tracked(
        "starter/.claude/hooks/*.py", "global/hooks/*.py",
        "README.md", "INSTALL.md", "USER-MANUAL.md", "starter/README.md",
    )
    assert files, "no file matched; the test would prove nothing"
    hits = _hits(files, OLD_BRAND)
    assert not hits, "the old brand name is left:\n" + "\n".join(hits)


# RN-005: an old plugin, agent or plugin id. Lowercase only: `GOVERNOR:BEGIN` and the other wire strings stay.
OLD_PLUGIN_NAME = re.compile(r"governor:[a-z]|governor-research|@governor\b")
# The cutover text names the old marketplace and its old ids on a line that also names the command that removes them.
OLD_MARKETPLACE_REMOVAL = "marketplace remove governor"


def test_no_old_plugin_name_in_product_text():
    files = _tracked(
        "starter", "global", "research", ".claude-plugin", "tool/kerbtap",
        "README.md", "INSTALL.md", "USER-MANUAL.md", "CLAUDE.md", ".claude/skills/*/SKILL.md",
    )
    assert files, "no file matched; the test would prove nothing"
    hits = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OLD_PLUGIN_NAME.search(line) and OLD_MARKETPLACE_REMOVAL not in line:
                hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()[:100]}")
    assert not hits, "an old plugin name is left:\n" + "\n".join(hits)
