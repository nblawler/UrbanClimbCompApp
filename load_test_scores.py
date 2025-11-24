"""
Load test for Urban Climb Comp App
Simulates hundreds of competitors logging climbs at once.
"""

import asyncio
import random
import time
import aiohttp

# -----------------------------
# CONFIG — ADJUST IF NEEDED
# -----------------------------
BASE_URL = "http://127.0.0.1:5001"

# Competitor range (fake seeded ones)
MIN_COMP_ID = 12
MAX_COMP_ID = 511

# Your real configured climb numbers
CLIMB_NUMBERS = [1, 2, 3, 5, 6, 9, 10, 14]

# Total POST requests to send
TOTAL_REQUESTS = 2000

# How many run simultaneously
MAX_CONCURRENT = 150   # try 150 first; can raise to 300+


# -----------------------------
# Load test functions
# -----------------------------
async def submit_score(session, competitor_id, climb_number):
    attempts = random.randint(1, 6)
    topped = random.random() < 0.7

    payload = {
        "competitor_id": competitor_id,
        "climb_number": climb_number,
        "attempts": attempts,
        "topped": topped
    }

    try:
        async with session.post(f"{BASE_URL}/api/score", json=payload) as resp:
            text = await resp.text()
            if resp.status != 200:
                print(f"[ERROR {resp.status}] {payload} :: {text[:200]}")
            return resp.status
    except Exception as e:
        print(f"[EXCEPTION] {e} :: {payload}")
        return None


async def worker(name, session, task_queue):
    while True:
        item = await task_queue.get()
        if item is None:
            task_queue.task_done()
            break

        competitor_id, climb_number = item
        await submit_score(session, competitor_id, climb_number)
        task_queue.task_done()


async def main():
    task_queue = asyncio.Queue()

    # Generate all simulated requests
    for _ in range(TOTAL_REQUESTS):
        cid = random.randint(MIN_COMP_ID, MAX_COMP_ID)
        climb = random.choice(CLIMB_NUMBERS)
        await task_queue.put((cid, climb))

    # Add sentinel None tasks to close workers
    for _ in range(MAX_CONCURRENT):
        await task_queue.put(None)

    async with aiohttp.ClientSession() as session:
        workers = [
            asyncio.create_task(worker(f"worker-{i}", session, task_queue))
            for i in range(MAX_CONCURRENT)
        ]

        print(f"Sending {TOTAL_REQUESTS} requests with concurrency {MAX_CONCURRENT}...")
        start = time.time()

        await task_queue.join()
        end = time.time()

        for w in workers:
            await w

        print(f"Completed in {end - start:.2f} seconds")


if __name__ == "__main__":
    asyncio.run(main())
