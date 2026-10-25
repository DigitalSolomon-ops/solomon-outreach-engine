"""#3 weak_or_no_site: the weak-site pass, then businesses with NO website at all.

No-website leads are keyed gbp-<listing id>; their email is printed on their own BBB profile
page, never guessed, and they are cited only as fact:nosite.
"""
from .weak_site import WeakSite


class WeakOrNoSite(WeakSite):
    passes = ("site", "nosite")


STRATEGY = WeakOrNoSite
