#!/usr/bin/env python3
"""One-time install of the Mac host helper.

Writes `.env` (token and Fernet key, mode 0600) and a LaunchAgent that listens
on 127.0.0.1 only. Per-session terminal commands are not part of this step.
Secrets are not printed.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet  # noqa: E402

from app.dotenv_file import load_env_file, upsert_env_text  # noqa: E402

LABEL = "com.arachne.host-helper"
_PLIST_RELATIVE = Path("Library") / "LaunchAgents" / f"{LABEL}.plist"


@dataclass(frozen=True)
class InstallPlan:
    env_text: str
    plist_text: str
    token: str
    session_key: str
    sessions_dir: str
    port: int


def plan_install(repo: Path, *, environ: dict[str, str] | None = None, existing_env: str = "") -> InstallPlan:
    """Decide token, key, and plist contents. Does not write or print secrets."""
    env = dict(os.environ if environ is None else environ)
    current = load_env_file(repo / ".env")
    if existing_env:
        current = {**current, **_parse_existing(existing_env)}
    token = _first(env, current, "ARACHNE_HOST_HELPER_TOKEN") or _new_token()
    session_key = _first(env, current, "ARACHNE_SESSION_KEY") or Fernet.generate_key().decode("ascii")
    port = _port(env, current)
    sessions = _sessions_dir(repo, env, current)
    updates = {
        "ARACHNE_HOST_HELPER_TOKEN": token,
        "ARACHNE_SESSION_KEY": session_key,
        "ARACHNE_SESSIONS_DIR": sessions,
        "ARACHNE_HOST_HELPER_PORT": str(port),
        "ARACHNE_HOST_HELPER_HOST": "127.0.0.1",
    }
    base = existing_env if existing_env else (repo / ".env").read_text(encoding="utf-8") if (repo / ".env").is_file() else ""
    python = env.get("ARACHNE_HOST_HELPER_PYTHON", sys.executable)
    plist = render_plist(
        python=python,
        script=str((repo / "scripts" / "host_helper.py").resolve()),
        working_directory=str(repo.resolve()),
        updates=updates,
        log_path=str((Path.home() / "Library" / "Logs" / "arachne-host-helper.log")),
    )
    return InstallPlan(
        env_text=upsert_env_text(base, updates),
        plist_text=plist,
        token=token,
        session_key=session_key,
        sessions_dir=sessions,
        port=port,
    )


def render_plist(
    *,
    python: str,
    script: str,
    working_directory: str,
    updates: dict[str, str],
    log_path: str,
) -> str:
    """LaunchAgent XML. The bind host is always 127.0.0.1."""
    env_xml = []
    forced = dict(updates)
    forced["ARACHNE_HOST_HELPER_HOST"] = "127.0.0.1"
    for key in (
        "ARACHNE_HOST_HELPER_TOKEN",
        "ARACHNE_SESSION_KEY",
        "ARACHNE_SESSIONS_DIR",
        "ARACHNE_HOST_HELPER_PORT",
        "ARACHNE_HOST_HELPER_HOST",
    ):
        env_xml.append(f"    <key>{escape(key)}</key>\n    <string>{escape(forced[key])}</string>")
    env_block = "\n".join(env_xml)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(python)}</string>
    <string>{escape(script)}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{escape(working_directory)}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_block}
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
  <key>StandardOutPath</key>
  <string>{escape(log_path)}</string>
  <key>StandardErrorPath</key>
  <string>{escape(log_path)}</string>
</dict>
</plist>
"""


def install(*, repo: Path, home: Path, launch: bool = True, environ: dict[str, str] | None = None) -> int:
    """Write `.env` and the plist. Load launchd only on macOS when `launch` is true."""
    repo = repo.resolve()
    existing = (repo / ".env").read_text(encoding="utf-8") if (repo / ".env").is_file() else ""
    planned = plan_install(repo, environ=environ, existing_env=existing)
    _write_private(repo / ".env", planned.env_text)
    plist_path = home / _PLIST_RELATIVE
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = home / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Re-render the log path under the install home, not Path.home(), so tests stay isolated.
    planned = InstallPlan(
        env_text=planned.env_text,
        plist_text=render_plist(
            python=(environ or os.environ).get("ARACHNE_HOST_HELPER_PYTHON", sys.executable),
            script=str((repo / "scripts" / "host_helper.py").resolve()),
            working_directory=str(repo),
            updates={
                "ARACHNE_HOST_HELPER_TOKEN": planned.token,
                "ARACHNE_SESSION_KEY": planned.session_key,
                "ARACHNE_SESSIONS_DIR": planned.sessions_dir,
                "ARACHNE_HOST_HELPER_PORT": str(planned.port),
                "ARACHNE_HOST_HELPER_HOST": "127.0.0.1",
            },
            log_path=str(log_dir / "arachne-host-helper.log"),
        ),
        token=planned.token,
        session_key=planned.session_key,
        sessions_dir=planned.sessions_dir,
        port=planned.port,
    )
    _write_private(plist_path, planned.plist_text)
    print("Host helper files written. Secrets stayed in .env and the LaunchAgent plist.")
    print(f"Loopback only: http://127.0.0.1:{planned.port}/capture")
    if sys.platform != "darwin":
        print("Not macOS. Start it in the foreground: scripts/arachne-host-helper")
        return 0
    if not launch:
        print("Wrote the LaunchAgent plist. launchd was not loaded.")
        return 0
    return _launchctl(plist_path)


def _launchctl(plist_path: Path) -> int:
    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{LABEL}"],
        check=False,
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print("launchctl bootstrap failed", file=sys.stderr)
        return 1
    print(f"Loaded LaunchAgent {LABEL}.")
    return 0


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _first(env: dict[str, str], current: dict[str, str], key: str) -> str:
    raw = env.get(key, "").strip()
    if raw:
        return raw
    return current.get(key, "").strip()


def _port(env: dict[str, str], current: dict[str, str]) -> int:
    raw = _first(env, current, "ARACHNE_HOST_HELPER_PORT") or "18765"
    try:
        port = int(raw)
    except ValueError:
        return 18765
    if not 1 <= port <= 65535:
        return 18765
    return port


def _sessions_dir(repo: Path, env: dict[str, str], current: dict[str, str]) -> str:
    raw = _first(env, current, "ARACHNE_SESSIONS_DIR")
    path = Path(raw) if raw else repo / "data" / "sessions"
    if not path.is_absolute():
        path = repo / path
    return str(path.resolve())


def _new_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _parse_existing(text: str) -> dict[str, str]:
    from app.dotenv_file import parse_env_text

    return parse_env_text(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the arachne host helper once.")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--no-launch", action="store_true", help="Write files but do not call launchctl")
    args = parser.parse_args(argv)
    return install(repo=args.repo, home=args.home, launch=not args.no_launch)


if __name__ == "__main__":
    raise SystemExit(main())
