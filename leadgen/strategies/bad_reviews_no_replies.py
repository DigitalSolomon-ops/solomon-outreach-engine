"""#7 bad_reviews_no_replies: rated under max_rating, and no owner reply on any recent review.

The rating is a filter used in the moment, never stored or cited. Owner replies come from
HasData's Google Maps reviews (newest first); only whether each review has a reply leaves the
adapter. The writer may cite one count ("none of the last N reviews has a reply"), never review
text, ratings or reviewer names.
"""
from __future__ import annotations

from .base import Strategy

MIN_REVIEWS = 3


class BadReviewsNoReplies(Strategy):

    def drop_listing(self, p, c, cand):
        if cand.rating is None:
            return "no rating on the listing"
        if float(cand.rating) >= self.params["max_rating"]:
            return f"rated {self.params['max_rating']:g} or higher"
        if not cand.place_id:
            return "no listing id to read reviews with"
        reviews, _ = p.e.signal("reviews").call("recent_replies", cand.place_id, self.params["recent"])
        if len(reviews) < MIN_REVIEWS:
            return "too few recent reviews"
        if any(r["has_reply"] for r in reviews):
            return "the owner replies to reviews"
        cand.raw = {**(cand.raw or {}), "unreplied": len(reviews)}
        return None

    def facts(self, p, c, cand, read):
        n = (cand.raw or {}).get("unreplied")
        if not n:
            return []
        return [{"id": "no_review_replies", "pillar": "Conversation",
                 "text": f"None of the {n} most recent reviews on their Google listing has a reply from the business."}]


STRATEGY = BadReviewsNoReplies
