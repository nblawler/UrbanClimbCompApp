import time

# --- Leaderboard cache ---

LEADERBOARD_CACHE_TTL = 10.0  # seconds

# key: normalised category ("all", "male", "female", "inclusive")
# value: (rows, category_label, timestamp)
LEADERBOARD_CACHE: dict = {}


def get_cached_leaderboard(key):
    """
    Return cached leaderboard if still valid.
    """
    entry = LEADERBOARD_CACHE.get(key)
    if not entry:
        return None

    rows, category_label, timestamp = entry
    if (time.time() - timestamp) > LEADERBOARD_CACHE_TTL:
        LEADERBOARD_CACHE.pop(key, None)
        return None

    return rows, category_label


def set_cached_leaderboard(key, rows, category_label):
    """
    Store leaderboard in cache.
    """
    LEADERBOARD_CACHE[key] = (rows, category_label, time.time())


def invalidate_leaderboard_cache():
    """Clear all cached leaderboard entries."""
    LEADERBOARD_CACHE.clear()
