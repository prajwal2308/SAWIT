"""The shape of a note.

One schema covers every category rather than a per-category union. Category
*guidance* lives in the extraction prompt instead, which keeps the JSON schema
strict-mode friendly and means a reel that straddles two topics still lands
somewhere sensible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "finance",
    "travel",
    "food",
    "tech",
    "news",
    "fitness",
    "education",
    "shopping",
    "other",
]


class KeyFact(BaseModel):
    """A single extracted datum: a number, price, place, dose, ingredient."""

    label: str = Field(description="What this value is, in 1-4 words.")
    value: str = Field(description="The value itself, with units or currency.")


class ReelNote(BaseModel):
    title: str = Field(description="A specific, scannable title. Not the creator's clickbait.")
    category: Category
    one_liner: str = Field(description="One sentence: what this reel actually tells you.")
    takeaways: list[str] = Field(description="3-5 bullets. The substance, not a description.")
    key_facts: list[KeyFact] = Field(description="Concrete numbers/names. Empty list if none.")
    steps: list[str] = Field(
        description=(
            "The procedure or calculation in order, if the reel contains one. "
            "This is the field that saves a rewatch. Empty list if not applicable."
        )
    )
    caveats: list[str] = Field(
        description="Unsupported claims, missing context, or anything to verify. May be empty."
    )
    tags: list[str] = Field(description="2-5 lowercase single-word tags for retrieval.")
