import os, requests
from ghostGex import GhostGex

buf = GhostGex().capture_chart()
requests.post(
    os.environ["INGEST_URL"],
    files={"image": ("gex.png", buf, "image/png")},
    headers={"X-Secret": os.environ["INGEST_SECRET"]},
    timeout=30,
)
