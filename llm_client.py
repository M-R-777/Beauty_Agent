"""One shared text-model helper for modules that need a quick answer from the LLM
(matching listings, fixing spelling, reading look descriptions)."""
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()
_llm = None


def chat(prompt: str) -> str:
    """Send a prompt to the Groq text model and return its answer without any <think> block."""
    global _llm
    if _llm is None:
        from langchain_groq import ChatGroq  # imported lazily so this module always loads
        _llm = ChatGroq(model=os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b"), temperature=0)

    last_err = None
    for attempt in range(3):
        try:
            out = _llm.invoke(prompt).content
            cleaned = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
            if cleaned:
                return cleaned
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if last_err:
        raise last_err
    return ""