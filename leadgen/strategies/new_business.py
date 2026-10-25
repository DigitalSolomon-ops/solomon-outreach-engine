"""#4 new_business (legacy site_filter new_listing): the new-business trigger.

No website pass at all: only map listings with no website and at most max_reviews reviews
(Google omits the count at zero, so missing = 0). The count is a filter, never stored and never
a claim: the writer may cite fact:nosite only and must not say the business just opened.
"""
from .base import Strategy


class NewBusiness(Strategy):
    passes = ("nosite",)
    # New listings rank low: read all 60 results Google gives (3 requests), not just the top 20.
    nosite_depth = 60
    writer_rule = "- Never say or imply that the business just opened or is new."

    def drop_nosite_listing(self, p, c, cand) -> bool:
        return (cand.review_count or 0) > self.params["max_reviews"]


STRATEGY = NewBusiness
