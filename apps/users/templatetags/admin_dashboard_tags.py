from django import template
from django.db.models import Count, Q

from apps.users.models import Student


register = template.Library()


@register.simple_tag
def top_violation_students(limit=5):
    return Student.objects.select_related("hostel").filter(violation_flags__gt=0).order_by("-violation_flags", "name")[:limit]


@register.simple_tag
def top_defaulter_students(limit=5):
    return (
        Student.objects.select_related("hostel")
        .annotate(defaulter_count=Count("user__nightpass", filter=Q(user__nightpass__defaulter=True), distinct=True))
        .filter(defaulter_count__gt=0)
        .order_by("-defaulter_count", "name")[:limit]
    )


@register.simple_tag
def top_blocked_students(limit=5):
    return (
        Student.objects.select_related("hostel")
        .filter(violation_flags__gt=0)
        .order_by("-violation_flags", "name")[:limit]
    )
