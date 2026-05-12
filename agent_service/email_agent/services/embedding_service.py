import os

from openai import OpenAI

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def get_embedding(text: str) -> list[float]:
    """Return a 1536-dim embedding for text using text-embedding-3-small."""
    from .metrics_service import metrics
    response = metrics.embed(
        _get_client(),
        operation="embedding:single",
        model="text-embedding-3-small",
        input=text.replace("\n", " "),
    )
    return response.data[0].embedding
