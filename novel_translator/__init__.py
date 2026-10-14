"""Gemini EPUB translator EN -> ID, version 2.6.0."""
from .models import TranslationOptions, TranslationResult
from .pipeline import translate_novel

__version__ = "2.6.0"
__all__ = ["TranslationOptions", "TranslationResult", "translate_novel"]
