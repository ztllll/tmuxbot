"""Capability descriptors for providers and communication channels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    name: str
    supports_hooks: bool = False
    supports_structured_transcript: bool = True
    supports_incremental_text: bool = False
    supports_resume: bool = False
    supports_continue: bool = False
    supports_tasks: bool = False
    supports_plans: bool = False
    supports_usage: bool = False
    supports_interactive_pickers: bool = False


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    name: str
    supports_edit: bool = False
    supports_actions: bool = False
    supports_threads: bool = False
    supports_cards: bool = False
    supports_images: bool = True
    supports_files: bool = True
    supports_typing: bool = False
    supports_replies: bool = False
    max_text_length: int = 4096
