from django.shortcuts import redirect

class RedirectUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        # Make sure request.user exists safely
        user = getattr(request, "user", None)

        if (
            user
            and user.is_authenticated
            and getattr(user, "user_type", None) == "security"
            and not request.session.get("redirected", False)
            and not request.path.startswith("/access")
        ):
            request.session["redirected"] = True
            return redirect("/access")

        return self.get_response(request)