SIGNATURES = (
    ("chatgpt", ("chatgpt.com", "chat.openai.com", "openai")),
    ("claude-code", ("claude-code", "claude-cli")),
    ("claude", ("claude.ai", "claude-ai", "anthropic")),
    ("copilot", ("copilot", "github.com")),
    ("gemini", ("gemini", "google")),
)


def identify(headers) -> tuple[str, str]:
    headers = headers or {}
    ua = str(headers.get("user-agent", ""))
    origin = str(headers.get("origin", ""))
    haystack = f"{origin} {ua}".lower()

    for label, needles in SIGNATURES:
        if any(n in haystack for n in needles):
            return label, (ua or origin)[:255]
    return "", (ua or origin)[:255]
