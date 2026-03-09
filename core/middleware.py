from django.shortcuts import redirect
from django.core.cache import cache
from django.utils import timezone

class RedirectUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        throttle_key = "nightpass:deadline-eval:last-run"
        now = timezone.now()
        last_run = cache.get(throttle_key)
        if not last_run or (now - last_run).total_seconds() >= 60:
            from apps.users.services.deadline_evaluator import evaluate_active_pass_deadlines

            evaluate_active_pass_deadlines(now=now)
            cache.set(throttle_key, now, 60)

        # Make sure request.user exists safely
        user = getattr(request, "user", None)

        if (
            user
            and user.is_authenticated
            and getattr(user, "user_type", None) == "security"
            and not request.path.startswith("/access")
            and not request.path.startswith("/logout")
            and not request.path.startswith("/admin/logout")
        ):
            return redirect("/access")

        return self.get_response(request)
