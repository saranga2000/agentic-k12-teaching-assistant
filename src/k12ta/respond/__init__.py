"""Applying the feedback policy filter and rendering student-facing text.

Student-facing rendering only. Never import this package from `k12ta.keys` --
parent screens (key confirmation, enrollment setup, any future digest) render
full detail directly from the store, unfiltered, on purpose. See
`docs/ARCHITECTURE.md`'s module table: `k12ta.digest` "must not reuse
student-facing renderers" for the same reason in the other direction.
"""

from __future__ import annotations
