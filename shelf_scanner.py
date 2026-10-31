"""Placeholder for the future AI shelf-photo scanner.

Plan: send a shelf photo to a vision model, get back guessed {title, author} per
spine, then show the user a checklist to confirm/fix before saving via POST /api/books
(source='shelf-photo'). Keep a per-user monthly spend cap.

To implement: set ENABLED = True (or read an env var such as VISION_API_KEY) and
make read_spines() return a list of dicts: [{"title": ..., "author": ..., "confidence": 0-1}].
"""
import os
ENABLED = False

def read_spines(image_bytes: bytes) -> list[dict]:
    raise NotImplementedError("AI shelf scanner not built yet")
