"""Inline SVG brand marks for provider cards.

These are simple, original, brand-colored glyphs rendered as inline SVG so the
UI works offline (no external logo URLs that could be blocked in SageMaker and
no bundling of third-party logo files). Each is a recognizable monogram/mark in
the provider's brand color — not an official logo asset.

LOGOS maps a provider label to an SVG string sized 44x44.
"""

_AWS = "#FF9900"
_COUCHBASE = "#ED2226"
_BEDROCK = "#01A88D"
_BASETEN = "#4F46E5"
_BRAVE = "#FB542B"
_PERPLEXITY = "#20B8CD"
_CLOUDWATCH = "#E7157B"
_DATADOG = "#632CA6"


def _wrap(bg: str, inner: str, radius: int = 12) -> str:
    return (
        f'<svg width="44" height="44" viewBox="0 0 44 44" fill="none" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="44" height="44" rx="{radius}" fill="{bg}"/>' + inner + "</svg>"
    )


def _letter(bg: str, ch: str, fg: str = "#fff") -> str:
    inner = (
        f'<text x="22" y="29" font-family="-apple-system,Segoe UI,sans-serif" '
        f'font-size="20" font-weight="800" fill="{fg}" text-anchor="middle">{ch}</text>'
    )
    return _wrap(bg, inner)


LOGOS = {
    # L1 Data
    "Bedrock KB + DynamoDB (AWS)": _wrap(
        "#161E2D",
        f'<path d="M22 9l11 6v14l-11 6-11-6V15l11-6z" stroke="{_AWS}" stroke-width="2" '
        f'fill="none"/><circle cx="22" cy="22" r="3.2" fill="{_AWS}"/>',
    ),
    "Couchbase": _letter(_COUCHBASE, "C"),

    # L2 Inference
    "Amazon Bedrock (AWS)": _wrap(
        "#0B2027",
        f'<path d="M12 28c4-10 16-10 20 0" stroke="{_BEDROCK}" stroke-width="2.4" fill="none" '
        f'stroke-linecap="round"/><circle cx="22" cy="16" r="3" fill="{_BEDROCK}"/>',
    ),
    "Baseten": _letter(_BASETEN, "B"),

    # L3 Tools (web search)
    "AgentCore Web Search (AWS)": _wrap(
        "#161E2D",
        f'<circle cx="20" cy="20" r="8" stroke="{_AWS}" stroke-width="2.6" fill="none"/>'
        f'<path d="M26 26l7 7" stroke="{_AWS}" stroke-width="3" stroke-linecap="round"/>',
    ),
    "Brave Search": _wrap(
        "#2A140C",
        f'<path d="M22 9l9 5v9c0 6-5 10-9 12-4-2-9-6-9-12v-9l9-5z" fill="{_BRAVE}"/>',
    ),
    "Perplexity Search": _wrap(
        "#07252B",
        f'<circle cx="22" cy="22" r="10" stroke="{_PERPLEXITY}" stroke-width="2.4" fill="none"/>'
        f'<path d="M22 12v20M12 17l10 7 10-7" stroke="{_PERPLEXITY}" stroke-width="2.2" '
        f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    ),

    # L5 Decision
    "Strands Decider (AWS)": _wrap(
        "#161E2D",
        f'<path d="M14 14c6 2 10 12 16 16M14 24c4 1 7 4 10 6" stroke="{_AWS}" '
        f'stroke-width="2.4" fill="none" stroke-linecap="round"/>',
    ),
    "Perplexity Decisions": _wrap(
        "#07252B",
        f'<circle cx="22" cy="22" r="10" stroke="{_PERPLEXITY}" stroke-width="2.4" fill="none"/>'
        f'<path d="M22 12v20M12 17l10 7 10-7" stroke="{_PERPLEXITY}" stroke-width="2.2" '
        f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    ),

    # L4 Security
    "Bedrock Guardrails (AWS)": _wrap(
        "#0B2027",
        f'<path d="M22 9l10 4v8c0 7-5 11-10 14-5-3-10-7-10-14v-8l10-4z" stroke="{_BEDROCK}" '
        f'stroke-width="2.2" fill="none"/><path d="M17 22l3.5 3.5L28 18" stroke="{_BEDROCK}" '
        f'stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    ),

    # L6 Observability
    "CloudWatch (AWS)": _wrap(
        "#2A0A1C",
        f'<path d="M11 27l6-7 5 4 6-9 5 7" stroke="{_CLOUDWATCH}" stroke-width="2.4" fill="none" '
        f'stroke-linecap="round" stroke-linejoin="round"/>',
    ),
    "Datadog": _wrap(
        "#1C0E2E",
        f'<rect x="13" y="24" width="5" height="8" rx="2" fill="{_DATADOG}"/>'
        f'<rect x="19.5" y="19" width="5" height="13" rx="2" fill="{_DATADOG}"/>'
        f'<rect x="26" y="13" width="5" height="19" rx="2" fill="{_DATADOG}"/>',
    ),
}


# Providers that use a real SVG icon file bundled under assets/icons/ instead
# of a hand-drawn mark. File is wrapped in a rounded white tile so the official
# icon reads well on the dark card.
import os

_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")
_ICON_FILES = {
    "Bedrock KB + DynamoDB (AWS)": "amazon-bedrock.svg",
    "Amazon Bedrock (AWS)": "amazon-bedrock.svg",
    "Bedrock Guardrails (AWS)": "amazon-bedrock.svg",
}


def _file_icon(filename: str) -> str:
    """Load an SVG file and return it as-is (served directly as a data URI).

    These vendor icons already carry their own viewBox and background, so we
    use them unchanged and let the card CSS size them. Returning the raw SVG
    (rather than nesting it inside another <svg>) is what renders reliably
    when base64-encoded into an <img src>.
    """
    path = os.path.join(_ICON_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            svg = f.read()
    except OSError:
        return _letter("#2b3a4e", "?")
    # Drop the XML prolog; keep the <svg> root intact.
    if "<?xml" in svg:
        svg = svg[svg.find("?>") + 2:].lstrip()
    return svg


def get_logo(provider: str) -> str:
    """Return an inline SVG mark for the provider, or a neutral placeholder."""
    if provider in _ICON_FILES:
        return _file_icon(_ICON_FILES[provider])
    return LOGOS.get(provider, _letter("#2b3a4e", provider[:1].upper()))


def get_logo_data_uri(provider: str) -> str:
    """Return the provider's SVG mark as a base64 data URI.

    Used as an inline image inside an st.button markdown label (button labels
    support Markdown images), so the whole card itself is the clickable button.
    """
    import base64
    svg = get_logo(provider).encode("utf-8")
    b64 = base64.b64encode(svg).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
