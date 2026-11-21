"""Public Vera contracts, using only temporary synthetic stores and local mocks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

import tests  # Establish synthetic-only storage before application imports.
import main
from config import settings
from services import accounts, wechat_service


ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = (
    "DEEPSEEK_API_KEY", "MAAS_API_KEY", "TENCENT_SECRET_ID", "TENCENT_SECRET_KEY",
    "SERPER_API_KEY", "SERPAPI_KEY", "HUAWEI_AK", "HUAWEI_SK", "HUAWEI_PROJECT_ID",
    "CAPTCHA_APP_KEY", "CAPTCHA_APP_SECRET", "CAPTCHA_APP_ID", "CAPTCHA_APP_ID_SECRET",
    "DEFAULT_USER_PASSWORD", "JWT_SECRET", "DATABASE_PASSWORD",
)


class PublicConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vera-public-config-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.fixture = self.root / "synthetic-app"
        (self.fixture / "config").mkdir(parents=True)
        (self.fixture / "config" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(ROOT / "config" / "settings.py", self.fixture / "config" / "settings.py")
        # The fixture imports a copied settings module, never a developer .env.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("VERA_", "HOUKONG_")) and key not in SECRET_KEYS
                    and key not in {"ENABLE_FILE_AGENT", "ENABLE_WECHAT", "EMBEDDING_BACKEND"}}
        self.env.update(PYTHONPATH=str(self.fixture), LOCALAPPDATA=str(self.root / "local"))

    def read_settings(self, updates=None):
        script = (
            "import json; from config import settings as s; "
            "print(json.dumps({'data':str(s.DATA_DIR),'chroma':str(s.CHROMA_DIR),"
            "'host':s.HOST,'port':s.PORT,'file_agent':s.ENABLE_FILE_AGENT,"
            "'wechat':s.ENABLE_WECHAT,'backend':s.EMBEDDING_BACKEND,"
            f"'empty_secrets':all(not getattr(s,k) for k in {SECRET_KEYS!r})}}))"
        )
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=self.root,
                                env=self.env | (updates or {}), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_unconfigured_copy_is_offline_and_has_no_default_secrets(self):
        value = self.read_settings()
        self.assertEqual(Path(value["data"]), self.fixture / "data")
        self.assertEqual((value["host"], value["port"], value["backend"]), ("127.0.0.1", 8000, "tfidf"))
        self.assertTrue(value["empty_secrets"])
        self.assertFalse(value["wechat"])
        self.assertFalse(value["file_agent"])
        self.assertFalse((self.fixture / "data").exists())

    def test_vera_environment_controls_paths_and_overrides_legacy_aliases(self):
        value = self.read_settings({"VERA_DATA_DIR": "vera-state", "HOUKONG_DATA_DIR": "legacy-state",
                                    "VERA_CHROMA_DIR": "vera-index", "HOUKONG_CHROMA_DIR": "legacy-index",
                                    "VERA_HOST": "127.0.0.2", "VERA_PORT": "8123",
                                    "ENABLE_FILE_AGENT": "true", "ENABLE_WECHAT": "true"})
        self.assertEqual(Path(value["data"]), self.fixture / "vera-state")
        self.assertEqual(Path(value["chroma"]), self.fixture / "vera-index")
        self.assertEqual((value["host"], value["port"]), ("127.0.0.2", 8123))
        self.assertTrue(value["file_agent"] and value["wechat"])

    def test_environment_takes_precedence_over_checkout_dotenv(self):
        (self.fixture / ".env").write_text("VERA_PORT=8124\nMAAS_API_KEY=synthetic-dotenv-value\n", encoding="utf-8")
        value = self.read_settings({"VERA_PORT": "8125", "MAAS_API_KEY": ""})
        self.assertEqual(value["port"], 8125)
        self.assertTrue(value["empty_secrets"])


class PublicStorageTests(unittest.TestCase):
    def test_bootstrap_and_runtime_share_only_synthetic_storage(self):
        root = tests._test_root.resolve()
        self.assertTrue((root / ".synthetic-fixture").is_file())
        self.assertEqual(Path(settings.DATA_DIR), root / "data")
        self.assertEqual(Path(settings.CHROMA_DIR), root / "chroma")
        self.assertEqual(Path(main.DATA_DIR), settings.DATA_DIR)
        self.assertEqual(Path(accounts.ACCOUNTS_FILE).parent, settings.DATA_DIR)
        for name in ("CONVERSATIONS_FILE", "SESSIONS_FILE", "UPLOAD_DIR", "REVIEW_UPLOAD_DIR"):
            with self.subTest(name=name):
                self.assertTrue(Path(getattr(main, name)).resolve().is_relative_to(root / "data"))
        self.assertFalse(settings.DATA_DIR.is_relative_to(ROOT))

    def test_missing_account_store_has_no_users_or_generated_credentials(self):
        with tempfile.TemporaryDirectory(prefix="vera-empty-accounts-") as folder:
            target = Path(folder) / "accounts.json"
            self.assertEqual(accounts.load_accounts(target), {})
            self.assertFalse(target.exists())
        self.assertEqual(accounts.load_accounts(), {})
        self.assertEqual(main.VALID_ACCOUNTS, {})
        self.assertFalse(main.verify_account_password("synthetic-unprovisioned", "synthetic-password"))
        self.assertFalse(main.is_teacher_account("synthetic-unprovisioned"))
        self.assertFalse(main.is_admin_account("synthetic-unprovisioned"))


class PublicEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_competition_password_bypass_cannot_create_a_session(self):
        with patch.object(main, "create_login_session", new_callable=AsyncMock) as create_session, \
                patch.object(main, "require_captcha", new_callable=AsyncMock) as captcha:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://synthetic") as client:
                for body in (None, {}, {"captcha_ticket": "synthetic", "captcha_randstr": "synthetic"}):
                    response = await client.post("/api/ict-login", json=body)
                    self.assertEqual(response.status_code, 404)
            create_session.assert_not_awaited()
            captcha.assert_not_awaited()

    async def test_public_assets_exist_and_are_served_but_source_is_private(self):
        self.assertTrue((ROOT / "index.html").is_file())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://synthetic") as client:
            self.assertEqual((await client.get("/")).status_code, 200)
            for name in sorted(main.FrontendStaticFiles.PUBLIC_FILES):
                with self.subTest(asset=name):
                    self.assertGreater((ROOT / name).stat().st_size, 0)
                    self.assertEqual((await client.get("/static/" + name)).status_code, 200)
            for name in ("main.py", "config/settings.py", ".env", "data/accounts.json"):
                with self.subTest(source=name):
                    self.assertEqual((await client.get("/static/" + name)).status_code, 404)

    async def test_disabled_wechat_does_not_read_profile_or_start_gateway(self):
        self.assertFalse(settings.ENABLE_WECHAT)
        self.assertFalse(main.ENABLE_WECHAT)
        with patch.object(Path, "is_file", side_effect=AssertionError("Disabled WeChat probed a profile")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Disabled WeChat read a profile")), \
                patch.dict(os.environ, {"HOUKONG_WECHAT_ENV_FILE": "synthetic-unused-profile/.env",
                                        "HOUKONG_WECHAT_SERVICE_TOKEN": "synthetic-unused-token"}):
            self.assertEqual(wechat_service.local_settings(), {})

        attachments = SimpleNamespace(recover=AsyncMock(), stop=AsyncMock())
        account = SimpleNamespace(recover=AsyncMock(), stop=AsyncMock())
        tasks = SimpleNamespace(stop=AsyncMock())
        fixture_app = FastAPI()
        with patch.object(main, "cleanup_expired_sessions", new_callable=AsyncMock), \
                patch.object(main, "warm_file_catalog", new_callable=AsyncMock), \
                patch.object(main, "chat_attachment_processor", return_value=attachments), \
                patch.object(main, "get_task_controller", return_value=tasks), \
                patch.object(main, "account_controller", return_value=account), \
                patch.object(main, "recover_outbox", new_callable=AsyncMock), \
                patch("services.chat_tutor.recover_chat", new_callable=AsyncMock), \
                patch.object(main.wechat_chat, "stop", new_callable=AsyncMock), \
                patch.object(wechat_service, "get_controller", side_effect=AssertionError("Disabled WeChat started a gateway")) as controller:
            async with main._backend_lifespan(fixture_app):
                self.assertFalse(hasattr(fixture_app.state, "wechat_controller"))
            controller.assert_not_called()
        attachments.recover.assert_awaited_once()
        attachments.stop.assert_awaited_once()
        account.stop.assert_awaited_once()
        tasks.stop.assert_awaited_once()
        self.assertFalse(any("/wechat" in getattr(route, "path", "") for route in main.app.routes))


if __name__ == "__main__":
    unittest.main()
