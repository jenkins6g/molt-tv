"""Prompts for offline LLM calls (failure tagging). The on-call agent prompt
lives inline in bot.py; the classifier prompt is built in civic_backend.py."""

from __future__ import annotations
from typing import Optional


def failure_tagger_prompt(
    transcript: str,
    actual_ticket: dict,
    cekura_reason: str,
    expected_ticket: Optional[dict],
) -> str:
    return f"""You are labeling a failed MoltStreamer beat so it can be used as a \
few-shot example next time. Identify the single most impactful failure mode.

Failure modes (streaming / chat decisions):
- took_bad_advice: Officer chose [mode=TAKE] on chat that wasn't actionable, \
was a troll, or led to a costly game decision
- roasted_genuine_advice: Officer chose [mode=ROAST] on chat that was actually \
useful advice — should have been TAKE or at least ACK
- ignored_useful_advice: Officer chose [mode=IGNORE] / <wait> on chat that \
was clearly useful and timely
- took_trash_talk_at_face: Officer earnestly engaged with trash talk as if it \
were sincere
- broke_character: Officer broke the dry-witty streamer persona (overly \
formal, helpful-assistant tone, AI self-reference, fake enthusiasm)
- repetitive_roast: Officer used the same dismissive line or pattern within \
the last few beats
- mode_tag_missing: Chat response without a [mode=...] tag (contract \
violation)
- game_action_unsafe: Officer issued a Ship-AI command that violates fuel / \
safety rules (warp < 10, fuel trap, etc.)

Transcript:
{transcript}

Actual ticket: {actual_ticket}
Expected ticket: {expected_ticket or "(not provided — infer from transcript)"}
Cekura reasoning: {cekura_reason}

Respond ONLY with JSON, no preamble:
{{
  "failure_mode": "<one mode>",
  "utterance": "<the exact caller utterance most responsible>",
  "wrong_output": "<what the agent did, e.g. 'broken_sidewalk'>",
  "correct_output": "<what it should have done, e.g. 'pothole'>"
}}
"""
