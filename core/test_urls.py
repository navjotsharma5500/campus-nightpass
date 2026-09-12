"""URL mounting regressions; no database or live OAuth requests required."""
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import get_script_prefix, reverse, set_script_prefix
from django.utils import timezone

from apps.users.views import gauth, get_post_login_redirect, oauth_callback
from core.middleware import RedirectUserMiddleware


class DeploymentURLTests(SimpleTestCase):
    def test_both_settings_modules(self):
        for module_name, prefix, host in (
            ("core.settings", "", "permissions.thapar.edu"),
            ("core.settings_campusconnect", "/permissions", "campusconnect.thapar.edu"),
        ):
            module = import_module(module_name)
            overrides = {name: getattr(module, name) for name in (
                "STATIC_URL", "LOGIN_URL", "LOGOUT_URL", "LOGIN_REDIRECT_URL",
                "LOGOUT_REDIRECT_URL", "CSRF_TRUSTED_ORIGINS",
            )}
            overrides["FORCE_SCRIPT_NAME"] = getattr(module, "FORCE_SCRIPT_NAME", None)
            previous_prefix = get_script_prefix()
            try:
                with self.subTest(settings=module_name), override_settings(**overrides):
                    # Django sets this during setup and request handling; override_settings
                    # alone does not update the thread-local script prefix.
                    set_script_prefix(prefix or "/")
                    self.check_deployment(prefix, host)
            finally:
                set_script_prefix(previous_prefix)

    def check_deployment(self, prefix, host):
        paths = {
            "home": "/", "login": "/login/", "logout": "/logout/",
            "google_login": "/accounts/google/login/",
            "google_callback": "/accounts/google/login/callback/",
            "scanner": "/access/", "admin_dashboard": "/access/admin-dashboard/",
            "admin:index": "/admin/", "cancel_pass": "/cancel/",
        }
        for name, path in paths.items():
            self.assertEqual(reverse(name), prefix + path)
        self.assertEqual(static("thapar_logo.png"), prefix + "/static/thapar_logo.png")
        from django.conf import settings
        self.assertEqual(settings.STATIC_URL, prefix + "/static/")
        self.assertIn("https://permissions.thapar.edu", settings.CSRF_TRUSTED_ORIGINS)
        if prefix:
            self.assertIn("https://campusconnect.thapar.edu", settings.CSRF_TRUSTED_ORIGINS)

        # PATH_INFO excludes the mount point, as supplied by the front-end server.
        kwargs = {"SCRIPT_NAME": prefix, "HTTP_HOST": host, "secure": True}
        with patch("core.middleware.cache.get", return_value=timezone.now()):
            response = self.client.get("/", **kwargs)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, prefix + "/login/?next=" + prefix + "/")
            response = self.client.get("/login/", **kwargs)
            self.assertContains(response, 'action="' + prefix + '/login/"')
            response = self.client.get("/admin/", **kwargs)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, prefix + "/admin/login/?next=" + prefix + "/admin/")
            response = self.client.get("/admin/login/", **kwargs)
            self.assertContains(response, prefix + "/static/admin/css/base.css")
            self.assertContains(response, 'action="' + prefix + '/admin/login/"')

        for template, expected in (
            ("base.html", prefix + "/logout/"),
            ("info.html", prefix + "/access/extension/fetchuser/performtask/"),
            ("info.html", prefix + "/static/validation/beep.mp3"),
            ("lmao.html", prefix + "/access/get_status_json/"),
            ("lmao.html", prefix + "/book/__resource__"),
            ("admin/superuser_student_list.html", prefix + "/admin/"),
            ("admin/global_settings/normalize_specific_date.html", prefix + "/admin/global_settings/settings/"),
        ):
            self.assertIn(expected, render_to_string(template, {"user": SimpleNamespace(is_authenticated=True)}))

        user = SimpleNamespace(user_type="student")
        self.assertEqual(get_post_login_redirect(user), prefix + "/")
        user.user_type = "admin"
        self.assertEqual(get_post_login_redirect(user), prefix + "/access/admin-dashboard/")
        user.user_type = "security"
        user.is_authenticated = True
        user.groups = Mock()
        user.groups.filter.return_value.exists.return_value = False
        self.assertEqual(get_post_login_redirect(user), prefix + "/access/")
        user.groups.filter.return_value.exists.return_value = True
        self.assertEqual(get_post_login_redirect(user), prefix + "/access/admin-dashboard/")

        factory = RequestFactory()
        middleware = RedirectUserMiddleware(lambda request: HttpResponse("ok"))
        with patch("core.middleware.cache.get", return_value=timezone.now()):
            for path in ("/access/", "/logout/", "/admin/logout/"):
                request = factory.get(path, **kwargs)
                request.user = user
                self.assertEqual(middleware(request).status_code, 200)
            request = factory.get("/", **kwargs)
            request.user = user
            self.assertEqual(middleware(request).url, prefix + "/access/")

        callback = "https://" + host + prefix + "/accounts/google/login/callback/"
        request = factory.get("/accounts/google/login/", **kwargs)
        response = gauth(request)
        self.assertEqual(parse_qs(urlsplit(response.url).query)["redirect_uri"], [callback])
        request = factory.get("/accounts/google/login/callback/", {"code": "test-code"}, **kwargs)
        with patch("apps.users.views.requests.post") as post, patch("apps.users.views.messages.error"):
            post.return_value.ok = False
            response = oauth_callback(request)
            self.assertEqual(post.call_args.kwargs["data"]["redirect_uri"], callback)
            self.assertEqual(response.url, prefix + "/")
