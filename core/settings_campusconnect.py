"""NightPass mounted at /permissions; the default settings retain root hosting."""
from .settings import *  # noqa: F403

FORCE_SCRIPT_NAME = "/permissions"
STATIC_URL = "/permissions/static/"

LOGIN_URL = "/permissions/login/"
LOGOUT_URL = "/permissions/logout/"
LOGIN_REDIRECT_URL = "/permissions/"
LOGOUT_REDIRECT_URL = "/permissions/"

CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, "https://campusconnect.thapar.edu"]
