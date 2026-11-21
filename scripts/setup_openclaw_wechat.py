"""Install the isolated, pinned local WeChat gateway without printing secrets.

This script never starts WeChat login. Re-running preserves the private token.
"""
from __future__ import annotations

import argparse
import ast
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.wechat_model import (WECHAT_PROVIDER, WECHAT_MODEL, WECHAT_MODEL_REF,
    WECHAT_MODEL_NAME, WECHAT_BASE_URL, WECHAT_KEY_ENV)
PROFILE = "vera"
STATE = Path.home() / (".openclaw-" + PROFILE)
VERSIONS = {"openclaw": "2026.11.3", "weixin": "2.4.10"}


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", identity + ":F", "SYSTEM:F"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        path.chmod(0o600)


def existing_env() -> dict[str, str]:
    env = STATE / ".env"
    if not env.exists():
        return {}
    result = {}
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            result[name] = value
    return result


def read_project_maas_key() -> str:
    from config.settings import MAAS_API_KEY
    if not MAAS_API_KEY:
        raise RuntimeError("Configure MAAS_API_KEY in this project's environment or local .env first.")
    return MAAS_API_KEY


def run_openclaw(*args: str) -> None:
    executable = shutil.which("openclaw.cmd" if os.name == "nt" else "openclaw")
    if not executable:
        raise RuntimeError("OpenClaw executable unavailable after installation.")
    subprocess.run([executable, "--profile", PROFILE, *args], check=True, cwd=ROOT)


def configure() -> None:
    # A profile must not accidentally inherit an explicit foreign state override.
    for name in ("OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"):
        if os.environ.get(name):
            raise RuntimeError(f"Remove the explicit {name} override before setup.")
    values = existing_env()
    values.update({
        WECHAT_KEY_ENV: read_project_maas_key(),
        "HOUKONG_WECHAT_SERVICE_TOKEN": values.get("HOUKONG_WECHAT_SERVICE_TOKEN") or secrets.token_urlsafe(48),
        "HOUKONG_WECHAT_BACKEND_URL": "http://127.0.0.1:8000",
        "HOUKONG_WECHAT_BROKER_PORT": "18790",
        "HOUKONG_WECHAT_STATE_DIR": str(STATE),
        "OPENCLAW_DISABLE_BONJOUR": "1",
        "OPENCLAW_EXEC_SHELL_SNAPSHOT": "0",
    })
    # This isolated profile has one provider and cannot inherit old DeepSeek keys.
    for key in ("DEEPSEEK_API_KEY", "HOUKONG_DEEPSEEK_API_KEY"):
        values.pop(key, None)
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise RuntimeError("Private environment values must be single-line.")
    private_write(STATE / ".env", "\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    path = STATE / "openclaw.json"
    config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    config.setdefault("gateway", {}).update({"mode": "local", "port": 18789, "bind": "loopback"})
    gateway_auth = config["gateway"].setdefault("auth", {})
    gateway_auth["mode"] = "token"
    if not gateway_auth.get("token"):
        gateway_auth["token"] = secrets.token_urlsafe(48)
    config.setdefault("agents", {}).setdefault("defaults", {}).update({
        "workspace": str(STATE / "workspace"),
        "model": {"primary": WECHAT_MODEL_REF, "fallbacks": []},
        "thinkingDefault": "off",
        "maxConcurrent": 3,
    })
    config["agents"]["defaults"]["models"] = {WECHAT_MODEL_REF: {"params": {
        "maxTokens": 8192, "extra_body": {"thinking": {"type": "disabled"}}}}}
    if not config["agents"].get("entries") or isinstance(config["agents"]["entries"], list):
        config["agents"]["entries"] = {"main": {}}
    for agent in config["agents"]["entries"].values():
        agent["model"] = {"primary": WECHAT_MODEL_REF, "fallbacks": []}
    config.setdefault("models", {}).update(mode="replace", providers={WECHAT_PROVIDER: {
        "baseUrl": WECHAT_BASE_URL, "api": "openai-completions", "auth": "api-key",
        "apiKey": {"source": "env", "provider": "default", "id": WECHAT_KEY_ENV},
        "models": [{"id": WECHAT_MODEL, "name": WECHAT_MODEL_NAME, "reasoning": False,
                    "input": ["text"], "contextWindow": 512000, "maxTokens": 8192,
                    "compat": {"supportsTools": True, "supportsStrictMode": False,
                               "supportsReasoningEffort": False, "maxTokensField": "max_tokens"}}],
    }})
    config.setdefault("session", {})["dmScope"] = "per-account-channel-peer"
    config.setdefault("update", {}).setdefault("auto", {})["enabled"] = False
    config["tools"] = {"allow": ["houkong_business"], "deny": ["group:runtime", "group:fs", "group:sessions", "group:ui", "group:automation", "message", "nodes", "browser", "canvas", "web_search", "web_fetch"]}
    config["commands"] = {"native": False, "nativeSkills": False, "text": False, "bash": False, "config": False, "debug": False, "restart": False}
    plugins = config.setdefault("plugins", {})
    plugins["allow"] = ["openclaw-weixin", "houkong-wechat"]
    if "deepseek" in plugins.get("entries", {}):
        plugins["entries"]["deepseek"]["enabled"] = False
    plugins.setdefault("slots", {})["contextEngine"] = "houkong-wechat"
    plugins["slots"]["memory"] = "none"
    for identifier in plugins["allow"]:
        plugins.setdefault("entries", {}).setdefault(identifier, {})["enabled"] = True
    plugins["entries"]["houkong-wechat"]["hooks"] = {"allowConversationAccess": True, "allowPromptInjection": True}
    private_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print("Isolated WeChat profile configured for Huawei openPangu; credentials saved privately.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--skip-core", action="store_true")
    parser.add_argument("--install-daemon", action="store_true")
    args = parser.parse_args()
    if not args.configure_only and not args.skip_core:
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise RuntimeError("Node.js/npm must be installed first.")
        subprocess.run([npm, "install", "-g", "openclaw@" + VERSIONS["openclaw"], "--allow-scripts=openclaw"], check=True)
    if not args.configure_only:
        run_openclaw("plugins", "install", "@tencent-weixin/openclaw-weixin@" + VERSIONS["weixin"])
        from setup_openclaw_wechat_auth_patch import find_package, patch
        patch(find_package())
        configure()
        run_openclaw("plugins", "install", "--link", "--force", "--accept-capabilities", str(ROOT / "integrations/openclaw-wechat"))
    else:
        configure()
    if args.install_daemon:
        run_openclaw("gateway", "install")
        run_openclaw("gateway", "start")
        run_openclaw("gateway", "status", "--json")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Credential-bearing network/config objects must never be printed.
        print(f"Setup failed: {type(exc).__name__}. Review the non-secret setup step.", file=sys.stderr)
        raise SystemExit(1)
