"""One shared text-model helper for modules that need a quick answer from the LLM
(matching listings, fixing spelling, reading look descriptions)."""
import os
import re

from dotenv import load_dotenv

load_dotenv()
_llm = None


def chat(prompt: str) -> str:
    """Send a prompt to the Groq text model and return its answer without any <think> block."""
    global _llm
    if _llm is None:
        from langchain_groq import ChatGroq  # imported lazily so this module always loads
        _llm = ChatGroq(model=os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b"), temperature=0)
    out = _llm.invoke(prompt).content
    return re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()