"""Cosmetic OTC skin-type quiz scorer and SQLite profile persistence.

Stores the user's skin type, sensitivity level, primary skincare goal, and
routine budget in `beauty.db` so the ReAct agent can personalize routines.
"""
import time
from setup_db import get_conn


def ensure_profile_table() -> None:
    """Create the single-row skin_profile table if it does not exist yet."""
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skin_profile (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            skin_type   TEXT NOT NULL,
            sensitivity TEXT NOT NULL,
            concern     TEXT NOT NULL,
            budget      INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def evaluate_skin_quiz(
    post_wash_feel: str,
    midday_shine: str,
    breakout_freq: str,
    reactivity: str,
    concern: str,
    budget: int,
) -> dict:
    """Score cosmetic skin-type answers deterministically and save to SQLite."""
    ensure_profile_table()

    scores = {"Oily": 0, "Dry": 0, "Combination": 0, "Normal": 0}

    # Q1: How skin feels 2 hours after cleansing without moisturizer
    if "Tight" in post_wash_feel:
        scores["Dry"] += 3
    elif "Shiny all over" in post_wash_feel:
        scores["Oily"] += 3
    elif "T-zone" in post_wash_feel:
        scores["Combination"] += 3
    else:
        scores["Normal"] += 3

    # Q2: Midday shine & pore visibility
    if "Whole face" in midday_shine:
        scores["Oily"] += 2
    elif "Forehead & nose" in midday_shine:
        scores["Combination"] += 2
    elif "Flaky" in midday_shine:
        scores["Dry"] += 2
    else:
        scores["Normal"] += 2

    # Q3: Clogged pores / occasional blemishes
    if "Frequently" in breakout_freq:
        scores["Oily"] += 1
        scores["Combination"] += 1
    elif "Rarely" in breakout_freq:
        scores["Dry"] += 1
        scores["Normal"] += 1

    skin_type = max(scores, key=scores.get)

    # Q4: Sensitivity to fragrance or active ingredients
    if "Easily" in reactivity:
        sensitivity = "High (fragrance-free, gentle OTC formulas)"
    elif "Sometimes" in reactivity:
        sensitivity = "Moderate"
    else:
        sensitivity = "Low"

    profile = {
        "skin_type": skin_type,
        "sensitivity": sensitivity,
        "concern": concern,
        "budget": int(budget),
        "updated_at": int(time.time()),
    }

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO skin_profile (id, skin_type, sensitivity, concern, budget, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            skin_type   = excluded.skin_type,
            sensitivity = excluded.sensitivity,
            concern     = excluded.concern,
            budget      = excluded.budget,
            updated_at  = excluded.updated_at
        """,
        (profile["skin_type"], profile["sensitivity"], profile["concern"], profile["budget"], profile["updated_at"]),
    )
    conn.commit()
    conn.close()
    return profile


def get_skin_profile() -> dict | None:
    """Return the saved skin profile, or None if the user hasn't taken the quiz yet."""
    ensure_profile_table()
    conn = get_conn()
    row = conn.execute("SELECT * FROM skin_profile WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def delete_skin_profile() -> None:
    """Delete the saved skin profile row from SQLite."""
    ensure_profile_table()
    conn = get_conn()
    conn.execute("DELETE FROM skin_profile WHERE id = 1")
    conn.commit()
    conn.close()