"""xAI (Grok) Responses API helpers: request body and answer extraction.

Stdlib only, so the wire format can be unit-tested without httpx. Reference (checked against docs.x.ai):
POST https://api.x.ai/v1/responses  with  {model, input, instructions, max_output_tokens, store, ...}
The reply has an ``output`` array of typed items: ``message`` items hold ``content`` parts of type
``output_text`` (field ``text``) or ``refusal``; reasoning models also emit ``reasoning`` items, which are ignored.
"""

XAI_URL = "https://api.x.ai/v1/responses"


class XAIResponseError(ValueError):
    """The reply was well-formed JSON but holds no usable answer (refusal, error object, empty output)."""


def build_request(model: str, system: str, user_text: str, max_output_tokens: int = 800) -> dict:
    """Body for /v1/responses.

    - ``instructions`` carries the system prompt, ``input`` the question plus sources.
    - ``store: false``: without it xAI keeps every question and its sources for 30 days.
    - No ``tools`` / ``search_parameters``: answers must come only from the sources MAVE supplies,
      not from xAI's live web/X search (which would also be billed per fetched item).
    """
    return {
        "model": model,
        "instructions": system,
        "input": user_text,
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def extract_text(data: object) -> str:
    """Return the assistant's visible answer, or raise XAIResponseError."""
    if not isinstance(data, dict):
        raise XAIResponseError("unexpected response shape")
    texts: list[str] = []
    refused = False
    output = data.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue  # reasoning / tool-call items are not part of the answer
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "refusal":
                refused = True
    text = "".join(texts).strip()
    if text:
        return text  # also covers status "incomplete" (token limit hit): a partial answer beats none
    if refused:
        raise XAIResponseError("model declined to answer")
    if data.get("error"):
        raise XAIResponseError("provider reported an error")
    raise XAIResponseError("empty answer")


def error_hint(data: object, secret: str = "") -> str:
    """Short, key-free description of an error reply, for server logs only (never sent to clients)."""
    msg = ""
    if isinstance(data, dict):
        err = data.get("error", data.get("message", data.get("detail")))
        msg = err.get("message", "") if isinstance(err, dict) else (err if isinstance(err, str) else "")
    msg = " ".join(str(msg).split())[:200]
    return msg.replace(secret, "[redacted]") if secret else msg
