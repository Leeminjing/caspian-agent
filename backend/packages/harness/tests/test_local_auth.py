"""
本地单用户认证(无注册/登录)单元测试：稳定身份、/me 恒返回本地用户、无 login/logout 路由、CSRF 中间件已移除。
"""

import asyncio
import importlib.util
import unittest
from types import SimpleNamespace

from backend.app.gateway.auth.local import (
    ensure_local_user,
    get_local_user,
    set_local_user,
)
from backend.app.gateway.routers.auth import me, router as auth_router


class LocalAuthTests(unittest.TestCase):
    def test_local_user_identity_is_stable(self):
        saved = get_local_user()
        identity = SimpleNamespace(
            id="u-abc", email="a@b.c", display_name="A", token_version=0
        )
        set_local_user(identity)
        try:
            self.assertIs(get_local_user(), identity)
        finally:
            set_local_user(saved)

    def test_me_returns_local_user(self):
        result = asyncio.run(me(SimpleNamespace()))
        self.assertEqual(result["user"]["id"], str(get_local_user().id))
        self.assertIn("email", result["user"])
        self.assertIn("display_name", result["user"])

    def test_auth_router_has_no_login_or_logout(self):
        paths = {getattr(route, "path", "") for route in auth_router.routes}
        self.assertNotIn("/api/auth/login", paths)
        self.assertNotIn("/api/auth/logout", paths)
        self.assertIn("/api/auth/me", paths)

    def test_csrf_middleware_module_removed(self):
        # 本地单用户模型下移除 CSRF 双提交中间件模块。
        self.assertIsNone(
            importlib.util.find_spec("backend.app.gateway.middleware.csrf")
        )

    def test_ensure_local_user_sets_identity(self):
        identity = asyncio.run(ensure_local_user())
        self.assertTrue(hasattr(identity, "id"))
        self.assertIs(get_local_user(), identity)


if __name__ == "__main__":
    unittest.main()
