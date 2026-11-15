"""Prints the Groq models your API key can use. Run: python list_models.py"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()
resp = requests.get(
    "https://api.groq.com/openai/v1/models",
    headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
    timeout=30,
)
resp.raise_for_status()
for m in sorted(resp.json()["data"], key=lambda m: m["id"]):
    print(m["id"])