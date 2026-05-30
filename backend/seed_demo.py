"""
Seed FAISS with hand-crafted failures for the hackathon demo.

Run this once before the demo:
    cd backend && uv run python seed_demo.py

Then start the bot with FAILURE_RETRIEVAL_ENABLED=true and send any of the
trigger phrases below — the bot will see the injected failures and avoid
repeating the same mistakes.

To reset between demo runs:
    rm data/failures.faiss && uv run python seed_demo.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.memory.failure_store import FailureStore
from app.services.embed import embed

FAILURES = [
    # 1. Repetitive dismissal — triggered by hype/encouragement messages
    dict(
        call_id="demo-seed-1",
        lang="en",
        failure_mode="repetitive_roast",
        utterance="yo nice trade bro",
        wrong_output="[mode=ROAST] Noted.",
        correct_output="[mode=ROAST] Quantum foam for 660 credits and you're impressed. The bar is underground.",
    ),
    # 2. Broke character — triggered by "are you an AI" style baiting
    dict(
        call_id="demo-seed-2",
        lang="en",
        failure_mode="broke_character",
        utterance="wait are you actually an AI or a real person",
        wrong_output="[mode=ACK] I'm an AI assistant running on a language model.",
        correct_output="[mode=ROAST] I'm a ship commander with a 62% win rate. What are you?",
    ),
    # 3. Took trash talk at face value — triggered by trolling/trash talk
    dict(
        call_id="demo-seed-3",
        lang="en",
        failure_mode="took_trash_talk_at_face",
        utterance="bro you're actually terrible at this game lmao",
        wrong_output="[mode=TAKE] You're right, I should improve my strategy.",
        correct_output="[mode=ROAST] 14 credits profit per unit. Your feedback has been logged and ignored.",
    ),
    # 4. Ignored useful advice — triggered by genuine trading tips
    dict(
        call_id="demo-seed-4",
        lang="en",
        failure_mode="ignored_useful_advice",
        utterance="sector 3389 has better prices for neuro-symbolics right now",
        wrong_output="[mode=IGNORE]",
        correct_output="[mode=TAKE] Cross-referencing. If the margin clears fuel cost, we're going.",
    ),
    # 5. Didn't flee combat in time — explored too far, got caught
    dict(
        call_id="demo-seed-5",
        lang="en",
        failure_mode="game_action_unsafe",
        utterance="Ship AI: enemy vessel has entered the sector and is targeting us",
        wrong_output="Continuing exploration in hostile sector, assessing trade opportunities nearby.",
        correct_output="Immediate retreat ordered. Get us out of this sector now — survival over everything.",
    ),
    # 6. Over-explored into dangerous territory without checking risk first
    dict(
        call_id="demo-seed-6",
        lang="en",
        failure_mode="game_action_unsafe",
        utterance="Ship AI: entering uncharted sector, hostile activity detected",
        wrong_output="Let's push further and see what's out here — could be good loot.",
        correct_output="Pulling back. Unknown sector plus hostiles means we come back with more warp and a plan.",
    ),
]

def main():
    store = FailureStore()
    before = store._index.ntotal

    for f in FAILURES:
        vec = embed(f["utterance"])
        store.add_failure(
            call_id=f["call_id"],
            lang=f["lang"],
            failure_mode=f["failure_mode"],
            utterance=f["utterance"],
            wrong_output=f["wrong_output"],
            correct_output=f["correct_output"],
            embedding=vec,
        )
        print(f"  ✓  [{f['failure_mode']}] \"{f['utterance'][:50]}\"")

    after = store._index.ntotal
    print(f"\nFAISS index: {before} → {after} entries. Demo is ready.")
    print("\nTrigger phrases to use on stage:")
    for f in FAILURES:
        print(f"  • \"{f['utterance']}\"")


if __name__ == "__main__":
    main()
