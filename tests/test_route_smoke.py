import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import admin, api, auth, chat, embed, public


def build_test_app() -> FastAPI:
    app = FastAPI(title="route-smoke-test")
    app.include_router(public.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(embed.router)
    app.include_router(api.router)
    return app


class RouteSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_test_app()
        cls.client = TestClient(cls.app)

    def assert_route_registered(self, path: str, methods=None):
        matches = [r for r in self.app.routes if getattr(r, "path", None) == path]
        self.assertTrue(matches, f"Route {path!r} is not registered")
        if methods:
            actual_methods = set().union(*(getattr(r, "methods", set()) for r in matches))
            self.assertTrue(
                set(methods).issubset(actual_methods),
                f"Route {path!r} does not expose methods {methods}; actual methods: {sorted(actual_methods)}",
            )

    def test_public_auth_routes_registered(self):
        self.assert_route_registered("/", {"GET"})
        self.assert_route_registered("/auth/login", {"GET", "POST"})
        self.assert_route_registered("/auth/logout", {"GET"})
        self.assert_route_registered("/api/favorites/toggle", {"POST"})

    def test_admin_routes_registered(self):
        for path in [
            "/admin",
            "/admin/publications",
            "/admin/history",
            "/admin/settings",
            "/admin/groups",
            "/admin/users",
            "/admin/embeds",
            "/admin/integrations",
        ]:
            self.assert_route_registered(path, {"GET"})

        for path in [
            "/admin/publish",
            "/admin/flow/edit",
            "/admin/groups/create",
            "/admin/groups/edit",
            "/admin/groups/delete",
            "/admin/user/groups",
            "/admin/user/disable",
            "/admin/agent/groups",
            "/admin/embeds/create",
            "/admin/embeds/toggle",
            "/admin/embeds/delete",
            "/admin/integrations/create",
            "/admin/integrations/toggle",
            "/admin/integrations/delete",
        ]:
            self.assert_route_registered(path, {"POST"})

    def test_chat_and_embed_routes_registered(self):
        for path in [
            "/chat/{flow_id}",
            "/chat/{flow_id}/{chat_id}",
            "/chat/{flow_id}/{chat_id}/send",
            "/chat/{flow_id}/{chat_id}/rename",
            "/chat/{flow_id}/{chat_id}/delete",
            "/chat/{flow_id}/new",
            "/embed/chat/{flow_id}",
            "/embed/chat/{flow_id}/{chat_id}/send",
            "/embed/sso/{flow_id}/{chat_id}",
        ]:
            self.assert_route_registered(path)

    def test_api_routes_registered(self):
        for path in [
            "/api/v1/me",
            "/api/v1/flows",
            "/api/v1/chat/{flow_id}/send",
            "/api/v1/chat/{flow_id}/{chat_id}",
            "/api/v1/chat/{flow_id}",
        ]:
            self.assert_route_registered(path)

    def test_auth_login_page_responds(self):
        response = self.client.get("/auth/login")
        self.assertIn(response.status_code, (200, 302, 401))

    def test_admin_page_has_route_response(self):
        response = self.client.get("/admin")
        self.assertIn(response.status_code, (200, 302, 401))


if __name__ == "__main__":
    unittest.main()
