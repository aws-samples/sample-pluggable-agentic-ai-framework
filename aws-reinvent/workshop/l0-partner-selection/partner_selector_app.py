"""AnyCompany Pluggable Agentic AI Framework - Partner Selection UI

A workshop configuration wizard with a modern, sophisticated interface:

- A docked left navigation rail (framework layers with live status) built on
  Streamlit's sidebar, styled as a glass panel over a deep indigo gradient.
- A main detail panel per layer: provider options, a credential field (stored
  in SSM as a SecureString), and the code snippet to copy into the SageMaker
  notebook.

Run with: streamlit run partner_selector_app.py

Modeled on l5-application/chatbot_app.py (same SSM + cache patterns).
"""

import html
import os
import boto3
import streamlit as st

from snippets import (
    LAYERS, SECTIONS, get_snippet,
    INFERENCE_ROLES, MODEL_PROVIDERS, inference_snippet,
    STRUCTURED_PROVIDERS, STRUCTURED_STAGES, structured_snippet,
    UNSTRUCTURED_PROVIDERS, UNSTRUCTURED_STAGES, unstructured_snippet,
    DATADOG_SITES,
    DECISION_PROVIDERS, DECISION_STAGES, decision_snippet,
    WEBSEARCH_PROVIDERS, WEBSEARCH_STAGES, websearch_snippet,
    OBSERVABILITY_STAGES, observability_snippet,
    PERPLEXITY_KEY_PARAM, cred_param,
    CONFIG_PREFIX as _CFG,
)
from logos import get_logo_data_uri

# --- Configuration ---
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SSM_PREFIX = "/anycompany/agentcore"
CONFIG_PREFIX = f"{SSM_PREFIX}/config"


@st.dialog("Vector store architecture", width="large")
def _show_kb_arch_dialog(svg_uri: str):
    """Show the vector store ingest/retrieve diagram in a page-level modal."""
    st.html(f'<img src="{svg_uri}" alt="Vector store ingest and retrieval flow" '
            f'style="width:100%;height:auto;"/>')


@st.dialog("Architecture", width="large")
def _show_diagram_dialog(svg_uri: str, alt: str):
    """Show a layer's architecture diagram full-size in a page-level modal."""
    st.html(f'<img src="{svg_uri}" alt="{html.escape(alt)}" style="width:100%;height:auto;"/>')


# ---------------------------------------------------------------------------
# Layer architecture diagrams
#
# Each layer page shows an SVG diagram as a clickable <img> (data URI): the
# button IS the image, and clicking it opens _show_diagram_dialog. CSS
# animations run inside the image. Shared vocabulary, so every diagram moves
# the same way:
#   .flow           dashes travel along a connector
#   .pkt            a small packet slides 26 units along a short connector
#   .pulse          an element breathes in and out
#   .s2a/.s2b       a highlight that alternates between 2 options (6 s)
#   .s3a/.s3b/.s3c  a highlight that cycles through 3 options (9 s)
# The "pluggable slot" (dashed box + PLUGGABLE badge + cycling highlight) shows
# that a layer has interchangeable providers. prefers-reduced-motion stops all
# motion and leaves no option highlighted.
# ---------------------------------------------------------------------------
_DIAGRAM_DEFS = """
  <defs>
    <linearGradient id="dgg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#6d5cff"/><stop offset="1" stop-color="#0891b2"/>
    </linearGradient>
    <marker id="dga" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
      <path d="M0,0 L9,4.5 L0,9 Z" fill="#94a3b8"/>
    </marker>
    <marker id="dgp" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
      <path d="M0,0 L9,4.5 L0,9 Z" fill="#6d5cff"/>
    </marker>
    <marker id="dgt" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
      <path d="M0,0 L9,4.5 L0,9 Z" fill="#0891b2"/>
    </marker>
  </defs>
  <style>
    .flow { stroke-dasharray: 6 5; animation: dgdash 1.1s linear infinite; }
    @keyframes dgdash { to { stroke-dashoffset: -22; } }
    .pkt { animation: dgmove 2.4s ease-in-out infinite; opacity: 0; }
    @keyframes dgmove {
      0% { transform: translateX(0); opacity: 0; } 15% { opacity: 1; }
      80% { opacity: 1; } 100% { transform: translateX(26px); opacity: 0; }
    }
    .pulse { animation: dgpulse 2.4s ease-in-out infinite; }
    @keyframes dgpulse { 0%, 100% { opacity: .35; } 45% { opacity: 1; } }
    .s2a { animation: dgs2a 6s ease-in-out infinite; }
    .s2b { animation: dgs2b 6s ease-in-out infinite; opacity: 0; }
    @keyframes dgs2a { 0%, 42% { opacity: 1; } 50%, 92% { opacity: 0; } 100% { opacity: 1; } }
    @keyframes dgs2b { 0%, 42% { opacity: 0; } 50%, 92% { opacity: 1; } 100% { opacity: 0; } }
    .s3a { animation: dgs3a 9s ease-in-out infinite; }
    .s3b { animation: dgs3b 9s ease-in-out infinite; opacity: 0; }
    .s3c { animation: dgs3c 9s ease-in-out infinite; opacity: 0; }
    @keyframes dgs3a { 0%, 28% { opacity: 1; } 33%, 95% { opacity: 0; } 100% { opacity: 1; } }
    @keyframes dgs3b { 0%, 28% { opacity: 0; } 33%, 61% { opacity: 1; } 66%, 100% { opacity: 0; } }
    @keyframes dgs3c { 0%, 61% { opacity: 0; } 66%, 95% { opacity: 1; } 100% { opacity: 0; } }
    @media (prefers-reduced-motion: reduce) {
      .flow, .pkt, .pulse, .s2a, .s2b, .s3a, .s3b, .s3c { animation: none; }
      .pkt { opacity: 1; } .pulse { opacity: .8; }
      .s2a, .s2b, .s3a, .s3b, .s3c { opacity: 0; }
    }
  </style>
"""


def _pluggable_header(x: int, y: int, title: str, hint: str, title_w: int) -> str:
    """Title + PLUGGABLE badge + hint for a pluggable slot. ``title_w`` is the
    approximate rendered width of the title, so the badge sits right after it."""
    bx = x + title_w + 10
    return (
        f'<text x="{x}" y="{y}" fill="#0f172a" font-size="14" font-weight="800">{title}</text>'
        f'<rect x="{bx}" y="{y - 12}" width="74" height="17" rx="8.5" fill="#eef2ff"/>'
        f'<text x="{bx + 37}" y="{y}" text-anchor="middle" fill="#6d5cff" font-size="9.5" '
        f'font-weight="800" letter-spacing="1">PLUGGABLE</text>'
        f'<text x="{bx + 84}" y="{y}" fill="#64748b" font-size="11">{hint}</text>'
    )


def _provider_tag(kind: str) -> str:
    """The one label every provider card and diagram uses: "aws" -> AWS SERVICE,
    anything else (a partner, with or without an API key) -> PARTNER SOLUTION."""
    return "AWS SERVICE" if kind == "aws" else "PARTNER SOLUTION"


# Same colors as the provider-card pills (st-key-pcard-aws- / st-key-pcard-key-).
_TAG_COLORS = {"aws": ("#eff6ff", "#2563eb"), "partner": ("#fef3c7", "#b45309")}
_TAG_WIDTH = {"aws": 70, "partner": 100}


def _tag_pill(x: int, baseline: float, kind: str) -> tuple[str, int]:
    """An AWS SERVICE / PARTNER SOLUTION pill whose text sits on ``baseline``.
    Returns (svg, width) so callers can place text after it."""
    bg, fg = _TAG_COLORS[kind]
    w = _TAG_WIDTH[kind]
    label = _provider_tag("aws" if kind == "aws" else "key")
    return (f'<rect x="{x}" y="{baseline - 10}" width="{w}" height="14" rx="7" fill="{bg}"/>'
            f'<text x="{x + w / 2}" y="{baseline}" text-anchor="middle" fill="{fg}" font-size="8" '
            f'font-weight="800" letter-spacing="0.5">{label}</text>'), w


def _option_card(x: int, y: int, w: int, h: int, sel_class: str, title: str,
                 lines: list[str], tag: str) -> str:
    """A selectable provider option: base card, cycling highlight, radio, title,
    then an AWS SERVICE / PARTNER SOLUTION pill (``tag`` = "aws" | "partner")
    leading the first detail line."""
    cy = y + 18
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#ffffff" stroke="#e2e8f0"/>',
        f'<rect class="{sel_class}" x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="#eef2ff" stroke="#6d5cff" stroke-width="1.5"/>',
        f'<circle cx="{x + 15}" cy="{cy}" r="5.5" fill="#ffffff" stroke="#94a3b8" stroke-width="1.3"/>',
        f'<circle class="{sel_class}" cx="{x + 15}" cy="{cy}" r="3" fill="#6d5cff"/>',
        f'<text x="{x + 28}" y="{cy + 4}" fill="#0f172a" font-size="12.5" font-weight="700">{title}</text>',
    ]
    for i, line in enumerate(lines):
        base = cy + 21 + i * 15
        tx = x + 28
        if i == 0:
            pill, pw = _tag_pill(tx, base, tag)
            out.append(pill)
            tx += pw + 6
        if line:
            out.append(f'<text x="{tx}" y="{base}" fill="#64748b" font-size="10.5">{line}</text>')
    return "".join(out)


def _or_chip(cx: int, cy: int) -> str:
    return (f'<rect x="{cx - 14}" y="{cy - 7}" width="28" height="14" rx="7" fill="#ffffff" stroke="#cbd5e1"/>'
            f'<text x="{cx}" y="{cy + 3.5}" text-anchor="middle" fill="#64748b" font-size="9" '
            f'font-weight="800">OR</text>')


def _diagram_svg(body: str, title: str, desc: str) -> str:
    """Wrap a diagram body in the shared 980x330 SVG frame (defs + animations)."""
    return (
        '<svg viewBox="0 0 980 330" xmlns="http://www.w3.org/2000/svg" '
        'font-family="-apple-system,Segoe UI,Roboto,sans-serif" width="980" height="330" '
        'role="img" aria-labelledby="dgTitle dgDesc">'
        f'<title id="dgTitle">{html.escape(title)}</title>'
        f'<desc id="dgDesc">{html.escape(desc)}</desc>'
        f'{_DIAGRAM_DEFS}{body}</svg>'
    )


def _intro_with_diagram(key: str, heading: str, note_html: str, svg: str, alt: str) -> None:
    """'How it works' text on the left, clickable animated diagram on the right."""
    import base64
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    intro_col, diagram_col = st.columns([1, 1.25], gap="large")
    with intro_col:
        st.markdown(f'<div class="section-label">{heading}</div>'
                    f'<div class="ssm-note">{note_html}</div>', unsafe_allow_html=True)
    with diagram_col:
        # Key must start with "arch_click" so the clickable-diagram CSS applies.
        if st.button(f"![{alt}]({uri})", key=f"arch_click_{key}", use_container_width=True):
            _show_diagram_dialog(uri, alt)
        st.caption("Click the diagram to enlarge")


def _doc_link(url: str, label: str) -> str:
    return (f' <a href="{url}" target="_blank" rel="noopener noreferrer">{label} ↗</a>')


# ---------------------------------------------------------------------------
# Save-to-SSM credential form, used by every layer that needs partner
# credentials, so they all look and behave the same:
#   <Provider> credentials                         (section label)
#   one line on where values are stored            (note)
#   label | input | ✓ masked saved value            (one row per field)
#   [ Save to SSM ]  ✓ Saved                        (one button per form)
# Secrets are SecureStrings and never sent back to the browser (inputs start
# blank; the badge shows a masked value). Non-secret fields pre-fill.
# ---------------------------------------------------------------------------
def _credential_form(form_id: str, title: str, fields: list[dict], *, note: str = "",
                     help_html: str = "", validate=None, after_save=None) -> bool:
    """Render a credential form and save it to SSM. Returns True when every
    field already has a stored value.

    fields: [{"key", "label", "param", "secure"=True, "placeholder"="", "options"=None}]
    help_html: optional "where to get these" instructions shown above the fields
               (trusted, static markup from this file only).
    validate(values) -> error message or None, checked before saving.
    after_save() runs after a successful save (e.g. also record the provider).
    """
    st.markdown(f'<div class="section-label">{html.escape(title)}</div>', unsafe_allow_html=True)
    if help_html:
        st.markdown(f'<div class="ssm-note">{help_html}</div>', unsafe_allow_html=True)
    any_secret = any(f.get("secure", True) for f in fields)
    st.markdown(
        '<div class="ssm-note">Saved to SSM Parameter Store in your account'
        + (", with keys encrypted as SecureStrings." if any_secret else ".")
        + (f" {note}" if note else "") + "</div>",
        unsafe_allow_html=True,
    )
    values, stored = {}, {}
    for f in fields:
        secure = f.get("secure", True)
        existing = get_param(f["param"])
        stored[f["key"]] = existing
        lcol, icol, scol = st.columns([1.3, 3, 1.6], gap="small")
        with lcol:
            st.markdown(f'<div style="padding-top:0.55rem;font-weight:600;">{html.escape(f["label"])}</div>',
                        unsafe_allow_html=True)
        with icol:
            options = f.get("options")
            if options:
                # Allowlisted field: a dropdown, so only a known-good value is possible.
                values[f["key"]] = st.selectbox(
                    f["label"], options, index=options.index(existing) if existing in options else 0,
                    key=f"cred_{form_id}_{f['key']}", label_visibility="collapsed")
            else:
                values[f["key"]] = st.text_input(
                    f["label"], value="" if secure else (existing or ""),
                    type="password" if secure else "default",
                    key=f"cred_{form_id}_{f['key']}",
                    placeholder=("••••••••  (stored)" if (secure and existing) else f.get("placeholder", "")),
                    label_visibility="collapsed")
        with scol:
            if existing:
                # Stored values are untrusted (SSM can be written outside this UI): mask or escape.
                shown = _mask(existing) if secure else html.escape(existing)
                st.markdown(f'<div style="padding-top:12px;"><span class="badge-ok">✓ {shown}</span></div>',
                            unsafe_allow_html=True)

    all_saved = all(stored.values())
    bcol, mcol = st.columns([1, 3])
    with bcol:
        clicked = st.button("Save to SSM", key=f"save_{form_id}", type="primary",
                            use_container_width=True)
    with mcol:
        if all_saved:
            st.markdown('<div style="padding-top:12px;"><span class="badge-ok">✓ Saved</span></div>',
                        unsafe_allow_html=True)
    if clicked:
        missing = [f["label"] for f in fields
                   if not (str(values[f["key"]]).strip() or stored[f["key"]])]
        error = validate(values) if validate else None
        if missing:
            st.warning("Missing: " + ", ".join(missing))
        elif error:
            st.warning(error)
        else:
            try:
                for f in fields:
                    v = str(values[f["key"]]).strip()
                    if v:
                        put_param(f["param"], v, secure=f.get("secure", True))
                if after_save:
                    after_save()
                st.toast("✅ Saved to SSM", icon="✅")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to save: {e}")
    return all_saved


def _lock_hint() -> None:
    """Shown under a credential form while it still has unsaved fields."""
    st.markdown('<div class="snippet-lock-hint">Save your credentials above to unlock the '
                'code below.</div>', unsafe_allow_html=True)


def _role_box(x: int, y: int, w: int, h: int, title: str, sub: str, stroke: str = "#e2e8f0") -> str:
    cx = x + w // 2
    width = ' stroke-width="1.5"' if stroke != "#e2e8f0" else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#ffffff" stroke="{stroke}"{width}/>'
            f'<text x="{cx}" y="{y + h // 2 - 2}" text-anchor="middle" fill="#0f172a" font-size="14" '
            f'font-weight="700">{title}</text>'
            f'<text x="{cx}" y="{y + h // 2 + 15}" text-anchor="middle" fill="#64748b" font-size="11">{sub}</text>')


def _route(d: str, sel_class: str) -> str:
    """A connector into one option: static grey base + animated highlight overlay."""
    return (f'<path d="{d}" stroke="#cbd5e1" stroke-width="1.6" fill="none" marker-end="url(#dga)"/>'
            f'<path class="{sel_class} flow" d="{d}" stroke="#6d5cff" stroke-width="2.2" fill="none" '
            f'marker-end="url(#dgp)"/>')


def _inference_diagram() -> str:
    """Agent roles -> models. The orchestrator Harness and the specialists go through
    LiteLLM to any provider; the evaluation judge always uses Claude Haiku 5.5 on Bedrock."""
    body = (
        '<text x="24" y="36" fill="#64748b" font-size="12" font-weight="700" letter-spacing="1.5">AGENT ROLES</text>'
        # Roles in workshop order: 1 specialists, 2 orchestrator, 3 evaluator.
        + _role_box(24, 52, 188, 56, "Specialist Agents", "Order · Refund · Research", "#6d5cff")
        + _role_box(24, 150, 188, 56, "Orchestrator", "AgentCore Harness", "#6d5cff")
        + _role_box(24, 248, 188, 56, "Evaluator (judge)", "L7 custom evaluators")
        # LiteLLM, shared by the specialists and the orchestrator
        + '<rect x="290" y="97" width="140" height="64" rx="14" fill="url(#dgg)"/>'
          '<text x="360" y="125" text-anchor="middle" fill="#ffffff" font-size="17" font-weight="800">LiteLLM</text>'
          '<text x="360" y="144" text-anchor="middle" fill="#e5e7ff" font-size="11">one invoke() interface</text>'
        # Specialists -> LiteLLM
        + '<path class="flow" d="M212 80 L 250 80 C 270 80, 268 114, 286 118" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
          '<rect class="pkt" x="216" y="75" width="10" height="10" rx="2.5" fill="#6d5cff"/>'
        # Orchestrator -> LiteLLM (the Harness's liteLlmModelConfig)
        + '<path class="flow" d="M212 178 L 250 178 C 270 178, 268 144, 286 140" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
          '<rect class="pkt" x="216" y="173" width="10" height="10" rx="2.5" fill="#6d5cff"/>'
          '<text x="222" y="200" fill="#6d5cff" font-size="11" font-weight="600">liteLlmModelConfig</text>'
        # Evaluator: fixed to Claude Haiku 5.5 on Bedrock (set in L7, not a UI choice)
        + '<path class="flow" d="M212 276 L 596 276" stroke="#94a3b8" stroke-width="2" fill="none" marker-end="url(#dga)"/>'
          '<text x="404" y="268" text-anchor="middle" fill="#64748b" font-size="11">AgentCore Evaluations · Claude Haiku 5.5 (fixed)</text>'
        # LiteLLM -> the model-provider box: one arrow; the model_id picks the provider
        + '<path class="flow" d="M430 129 L 592 129" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
          '<rect class="pkt" x="440" y="124" width="10" height="10" rx="2.5" fill="#6d5cff"/>'
          '<text x="512" y="119" text-anchor="middle" fill="#64748b" font-size="11">model_id picks the provider</text>'
        # pluggable provider slot
        + '<rect x="600" y="28" width="356" height="292" rx="14" fill="#f8fafc" stroke="#6d5cff" '
          'stroke-width="1.5" stroke-dasharray="7 5"/>'
        + _pluggable_header(620, 52, "Model providers", "per role", 122)
        # Partner first, level with the LiteLLM arrow
        + _option_card(620, 105, 316, 48, "s2b", "Baseten", ["baseten/ provider · via LiteLLM"], "partner")
        # Bedrock, below the partner (tall: every role can use it)
        + '<rect x="620" y="184" width="316" height="128" rx="10" fill="#ffffff" stroke="#e2e8f0"/>'
          '<rect class="s2a" x="620" y="184" width="316" height="128" rx="10" fill="#eef2ff" stroke="#6d5cff" stroke-width="1.5"/>'
          '<circle cx="635" cy="202" r="5.5" fill="#ffffff" stroke="#94a3b8" stroke-width="1.3"/>'
          '<circle class="s2a" cx="635" cy="202" r="3" fill="#6d5cff"/>'
          '<text x="648" y="206" fill="#0f172a" font-size="12.5" font-weight="700">Amazon Bedrock</text>'
        + _tag_pill(648, 223, "aws")[0]
        + '<text x="724" y="223" fill="#64748b" font-size="10.5">works for every role</text>'
          '<g font-size="10" fill="#475569">'
          '<rect x="648" y="232" width="56" height="17" rx="8.5" fill="#ffffff" stroke="#c7d2fe"/><text x="676" y="244" text-anchor="middle">Claude</text>'
          '<rect x="710" y="232" width="46" height="17" rx="8.5" fill="#ffffff" stroke="#c7d2fe"/><text x="733" y="244" text-anchor="middle">Kimi</text>'
          '<rect x="762" y="232" width="46" height="17" rx="8.5" fill="#ffffff" stroke="#c7d2fe"/><text x="785" y="244" text-anchor="middle">GPT</text>'
          '</g>'
          '<text x="648" y="276" fill="#64748b" font-size="10.5">The judge always uses Claude Haiku 5.5.</text>'
          '<text x="648" y="292" fill="#64748b" font-size="10.5">Specialists and orchestrator reach it through LiteLLM.</text>'
    )
    return _diagram_svg(
        body, "Inference: a model per agent role",
        "The specialist agents and the orchestrator Harness call their model through "
        "LiteLLM, so they can use Amazon Bedrock or Baseten, chosen by the "
        "model id. The evaluation judge always uses Claude Haiku 5.5 on Amazon Bedrock.",
    )


def _websearch_diagram() -> str:
    """Research Agent -> AgentCore Gateway (MCP) -> one of three web-search providers."""
    body = (
        _role_box(24, 112, 168, 96, "Research Agent", "web_search · ask_web", "#6d5cff")
        # agent <-> gateway
        + '<path class="flow" d="M192 146 L 268 146" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
          '<text x="230" y="136" text-anchor="middle" fill="#6d5cff" font-size="10.5" font-weight="600">tools/call</text>'
          '<path class="flow" d="M272 178 L 196 178" stroke="#0891b2" stroke-width="2" fill="none" marker-end="url(#dgt)"/>'
          '<text x="232" y="198" text-anchor="middle" fill="#0891b2" font-size="10.5" font-weight="600">results + URLs</text>'
          '<text x="232" y="232" text-anchor="middle" fill="#94a3b8" font-size="10">Cognito token</text>'
        # gateway
        + '<rect x="272" y="96" width="176" height="128" rx="14" fill="url(#dgg)"/>'
          '<text x="360" y="146" text-anchor="middle" fill="#ffffff" font-size="16" font-weight="800">AgentCore</text>'
          '<text x="360" y="166" text-anchor="middle" fill="#ffffff" font-size="16" font-weight="800">Gateway</text>'
          '<text x="360" y="188" text-anchor="middle" fill="#e5e7ff" font-size="11">one MCP endpoint</text>'
          '<text x="360" y="203" text-anchor="middle" fill="#e5e7ff" font-size="11">handles outbound auth</text>'
        # gateway -> each provider
        + _route("M448 132 C 510 132, 520 101, 576 101", "s3a")
        + _route("M448 160 C 510 160, 520 181, 576 181", "s3b")
        + _route("M448 188 C 510 188, 520 261, 576 261", "s3c")
        # slot
        + '<rect x="560" y="28" width="396" height="276" rx="14" fill="#f8fafc" stroke="#6d5cff" '
          'stroke-width="1.5" stroke-dasharray="7 5"/>'
        + _pluggable_header(580, 52, "Web-search provider", "choose one", 150)
        + _option_card(580, 66, 356, 70, "s3a", "AgentCore Web Search",
                       ["managed connector, Amazon web index",
                        "auth: the Gateway's IAM role · no API key"], "aws")
        + _option_card(580, 146, 356, 70, "s3b", "Brave Search",
                       ["open source MCP server",
                        "on AgentCore Runtime · SigV4 · key in Secrets Manager"], "partner")
        + _option_card(580, 226, 356, 70, "s3c", "Perplexity Search",
                       ["hosted MCP server · search + ask",
                        "auth: AgentCore API-key credential provider"], "partner")
        + '<text x="490" y="322" text-anchor="middle" fill="#475569" font-size="11.5">Same agent code and '
          'tools for every provider. Switch by running a different Connect cell.</text>'
    )
    return _diagram_svg(
        body, "Tools: pluggable web search",
        "The Research Agent calls one MCP endpoint on the AgentCore Gateway with a Cognito token. "
        "The Gateway routes the call to the provider you connected: AgentCore Web Search, Brave "
        "Search or Perplexity Search, and returns results with source URLs.",
    )


def _observability_diagram() -> str:
    """Agents emit OTel spans -> CloudWatch or Datadog; logs always -> CloudWatch.

    CloudZero mode is not drawn (it would need a third card): it is CloudWatch plus
    the specialists' model-call spans to CloudZero, as the caption says."""
    rows = ["Orchestrator (Harness)", "Order Agent", "Refund Agent", "Research Agent"]
    agents = "".join(
        f'<rect x="38" y="{80 + i * 40}" width="160" height="32" rx="8" fill="#f8fafc" stroke="#e2e8f0"/>'
        f'<text x="118" y="{100 + i * 40}" text-anchor="middle" fill="#0f172a" font-size="11.5" '
        f'font-weight="700">{t}</text>'
        for i, t in enumerate(rows)
    )
    body = (
        '<rect x="24" y="44" width="188" height="256" rx="12" fill="#ffffff" stroke="#e2e8f0"/>'
        '<text x="118" y="68" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Your agents</text>'
        + agents
        + '<text x="118" y="258" text-anchor="middle" fill="#64748b" font-size="10.5">OpenTelemetry spans for</text>'
          '<text x="118" y="273" text-anchor="middle" fill="#64748b" font-size="10.5">model calls, tools, tokens</text>'
        # spans -> slot
        + '<path class="flow" d="M212 128 L 436 128" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
          '<rect class="pulse" x="270" y="112" width="110" height="20" rx="10" fill="#eef2ff"/>'
          '<text x="325" y="126" text-anchor="middle" fill="#6d5cff" font-size="10.5" font-weight="700">spans &amp; traces</text>'
        # logs -> CloudWatch Logs
        + '<path class="flow" d="M212 274 L 436 274" stroke="#0891b2" stroke-width="2" fill="none" marker-end="url(#dgt)"/>'
          '<text x="325" y="266" text-anchor="middle" fill="#0891b2" font-size="10.5" font-weight="700">logs</text>'
        # trace backend slot
        + '<rect x="440" y="28" width="516" height="200" rx="14" fill="#f8fafc" stroke="#6d5cff" '
          'stroke-width="1.5" stroke-dasharray="7 5"/>'
        + _pluggable_header(460, 52, "Trace backend", "choose one", 104)
        + _option_card(460, 66, 220, 146, "s2a", "Amazon CloudWatch",
                       ["", "stays in your account", "GenAI Observability",
                        "CloudWatch Omni (L6)", "X-Ray Transaction Search", "no external credentials"], "aws")
        + _or_chip(698, 139)
        + _option_card(716, 66, 220, 146, "s2b", "Datadog",
                       ["", "OTLP direct", "Agent Observability",
                        "one session per conversation"], "partner")
        # logs box (always CloudWatch)
        + '<rect x="440" y="248" width="516" height="52" rx="12" fill="#ffffff" stroke="#0891b2" stroke-width="1.5"/>'
          '<text x="460" y="270" fill="#0f172a" font-size="12.5" font-weight="700">Amazon CloudWatch Logs</text>'
          '<text x="460" y="287" fill="#64748b" font-size="10.5">application and usage logs · always, whichever trace backend you pick</text>'
        + '<text x="490" y="322" text-anchor="middle" fill="#475569" font-size="11.5">Pick where traces go. '
          'Logs always land in CloudWatch, so nothing is lost when you switch.</text>'
    )
    return _diagram_svg(
        body, "Agent observability: pluggable trace backend",
        "The orchestrator and the Order, Refund and Research agents emit OpenTelemetry spans. "
        "Spans and traces go to the backend you pick, Amazon CloudWatch or Datadog. With "
        "CloudZero they stay on CloudWatch, and the Order, Refund and Research agents also send "
        "each model call's span to CloudZero. Logs always go to Amazon CloudWatch Logs.",
    )


def _decision_diagram() -> str:
    """Customer message -> decision model (Strands or Perplexity) -> typed
    probabilities -> the chatbot's action."""
    answers = [("in scope", "yes/no", "0.97"), ("wants a human", "yes/no", "0.12"),
               ("urgency", "score", "Elevated"), ("sentiment", "choice", "negative · 0.81")]
    ans = "".join(
        f'<rect x="602" y="{76 + i * 40}" width="152" height="32" rx="8" fill="#f8fafc" stroke="#e2e8f0"/>'
        f'<text x="612" y="{90 + i * 40}" fill="#0f172a" font-size="11" font-weight="700">{q}</text>'
        f'<text x="612" y="{103 + i * 40}" fill="#94a3b8" font-size="9.5">{k}</text>'
        f'<text class="pulse" x="746" y="{97 + i * 40}" text-anchor="end" fill="#0891b2" font-size="11" '
        f'font-weight="800" style="animation-delay:{i * 0.4:.1f}s">{v}</text>'
        for i, (q, k, v) in enumerate(answers)
    )
    acts = [("s3a", "Answer", "orchestrator replies"), ("s3b", "Flag for a human", "escalation badge"),
            ("s3c", "Canned reply", "confidently off-topic")]
    act = "".join(
        f'<rect x="828" y="{76 + i * 62}" width="120" height="52" rx="10" fill="#ffffff" stroke="#e2e8f0"/>'
        f'<rect class="{c}" x="828" y="{76 + i * 62}" width="120" height="52" rx="10" fill="#eef2ff" '
        f'stroke="#6d5cff" stroke-width="1.5"/>'
        f'<text x="888" y="{98 + i * 62}" text-anchor="middle" fill="#0f172a" font-size="11.5" font-weight="700">{t}</text>'
        f'<text x="888" y="{114 + i * 62}" text-anchor="middle" fill="#64748b" font-size="9.5">{s}</text>'
        for i, (c, t, s) in enumerate(acts)
    )
    body = (
        _role_box(24, 112, 168, 96, "Customer message", '"Where is my order?!"')
        + '<path class="flow" d="M192 160 L 236 160" stroke="#94a3b8" stroke-width="2" fill="none" marker-end="url(#dga)"/>'
          '<rect class="pkt" x="196" y="155" width="10" height="10" rx="2.5" fill="#6d5cff"/>'
        # decision model slot
        + '<rect x="240" y="28" width="320" height="262" rx="14" fill="#f8fafc" stroke="#6d5cff" '
          'stroke-width="1.5" stroke-dasharray="7 5"/>'
        + _pluggable_header(260, 52, "Decision model", "choose one", 112)
        + _option_card(260, 68, 280, 88, "s2a", "Strands Decider",
                       ["open source model", "on an Amazon SageMaker endpoint",
                        "IAM auth · GPU first, CPU fallback"], "aws")
        + _or_chip(400, 168)
        + _option_card(260, 182, 280, 88, "s2b", "Perplexity Decisions",
                       ["hosted Decisions API", "pplx-decider models",
                        "API key (shared with Tools)"], "partner")
        + '<path class="flow" d="M560 160 L 594 160" stroke="#0891b2" stroke-width="2" fill="none" marker-end="url(#dgt)"/>'
        # typed answers
        + '<rect x="588" y="28" width="180" height="262" rx="12" fill="#ffffff" stroke="#e2e8f0"/>'
          '<text x="678" y="56" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Typed answers</text>'
        + ans
        + '<text x="678" y="256" text-anchor="middle" fill="#64748b" font-size="10.5">probabilities, not free text</text>'
          '<text x="678" y="271" text-anchor="middle" fill="#64748b" font-size="10.5">all questions in one call</text>'
        + '<path class="flow" d="M768 160 L 816 160" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#dgp)"/>'
        # chatbot actions
        + '<rect x="816" y="28" width="144" height="262" rx="12" fill="#ffffff" stroke="#e2e8f0"/>'
          '<text x="888" y="56" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Chatbot acts</text>'
        + act
        + '<text x="490" y="318" text-anchor="middle" fill="#475569" font-size="11.5">Example answers. Thresholds '
          'live in the chatbot code, not the model.</text>'
    )
    return _diagram_svg(
        body, "Decision models: pluggable triage step",
        "Each customer message goes to the decision model you pick, Strands Decider on Amazon "
        "SageMaker or Perplexity Decisions. It returns typed answers with probabilities: in scope, "
        "wants a human, urgency and sentiment. The chatbot uses thresholds to answer, flag a human, "
        "or send a canned reply for confidently off-topic messages.",
    )


@st.cache_resource
def get_ssm():
    return boto3.client("ssm", region_name=REGION)


# Cache SSM reads. Without this, every rerun (e.g. clicking a tile) fires a
# dozen+ blocking get_parameter calls to AWS from the main panel - which makes each click
# feel slow. Caching collapses those to in-memory lookups; a short TTL keeps
# values fresh, and put_param clears the cache so a just-saved key shows up.
@st.cache_data(ttl=30, show_spinner=False)
def _get_param_cached(name: str) -> str | None:
    ssm = get_ssm()
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    except Exception:
        return None


def get_param(name: str, default: str = "") -> str:
    val = _get_param_cached(name)
    return default if val is None else val


def put_param(name: str, value: str, secure: bool = False) -> None:
    get_ssm().put_parameter(
        Name=name, Value=value,
        Type="SecureString" if secure else "String", Overwrite=True,
    )
    # Invalidate cached reads so the new value is reflected immediately.
    _get_param_cached.clear()


def _mask(value: str) -> str:
    """Mask a secret for display: first 4 + … + last 4 (never echo in full)."""
    if not value:
        return ""
    return "…" if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


# --- Framework logo: sidebar brand + browser tab icon ---
_PAF_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons", "paf-logo.svg")
with open(_PAF_LOGO_PATH, "rb") as _f:
    import base64 as _logo_b64
    _PAF_LOGO_URI = "data:image/svg+xml;base64," + _logo_b64.b64encode(_f.read()).decode("ascii")

# --- Page config ---
st.set_page_config(
    page_title="Partner Selection - Pluggable Agentic AI Framework",
    page_icon=_PAF_LOGO_PATH,
    layout="wide",
    initial_sidebar_state="expanded",
)

# =====================================================================
# Design system
# =====================================================================
st.markdown(
    """
    <style>
      :root {
        --bg:        #ffffff;
        --bg-soft:   #f4f5f8;
        --panel:     #ffffff;
        --panel-brd: rgba(15,23,42,0.12);
        --txt:       #0f172a;
        --txt-dim:   #475569;
        --txt-faint: #94a3b8;
        --accent:    #6d5cff;   /* indigo */
        --accent-2:  #0891b2;   /* cyan (darker for white bg) */
        --ok:        #059669;
        --warn:      #d97706;
        --info:      #2563eb;
      }

      html { font-size: 17px; }

      /* --- global button reset: kill Streamlit's default red on all buttons --- */
      .stButton > button,
      .stButton > button:hover,
      .stButton > button:focus,
      .stButton > button:active,
      .stButton > button:focus:not(:active),
      .stButton > button:focus-visible {
        color: var(--txt);
        border-color: var(--panel-brd);
        box-shadow: none;
        outline: none !important;
      }
      /* focus ring in accent, never red */
      .stButton > button:focus-visible { box-shadow: 0 0 0 3px rgba(124,108,255,.25) !important; }
      /* neutralize red on inputs/widgets' focus accents too */
      :root { --primary-color: #7c6cff; }

      .stApp {
        background:
          radial-gradient(900px 600px at 85% -5%, rgba(124,108,255,.14), transparent 60%),
          radial-gradient(700px 500px at 10% 110%, rgba(34,211,238,.08), transparent 55%),
          var(--bg);
      }
      html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
                     "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
        -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
        color: var(--txt);
      }
      .block-container { padding-top: 2.2rem; padding-bottom: 3.5rem; max-width: 1300px; }
      header[data-testid="stHeader"] { background: transparent; }
      #MainMenu, footer { visibility: hidden; }

      /* ===================== SIDEBAR / NAV RAIL ===================== */
      section[data-testid="stSidebar"] {
        width: 350px !important;
        background: #f8f9fb;
        border-right: 1px solid var(--panel-brd);
      }
      /* Kill Streamlit's default top padding above the sidebar content.
         The padded element is stSidebarUserContent in current Streamlit; the
         older "> div" reset alone misses it, so target both. */
      section[data-testid="stSidebar"] > div,
      section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        padding-top: 0 !important;
      }

      /* The sidebar header row only holds the collapse arrow; keep it short so
         the brand sits near the top. */
      section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
        height: 2rem !important; min-height: 0 !important;
        padding-top: 0.25rem !important; padding-bottom: 0 !important;
        margin-bottom: 0 !important;
      }
      .brand {
        padding: 4px 24px 18px;
        border-bottom: 1px solid var(--panel-brd);
      }
      .brand .mark {
        display:inline-flex; align-items:center; gap:13px;
      }
      /* Framework logo (assets/icons/paf-logo.svg); transparent background. */
      .brand .logo {
        width:56px; height:auto; flex-shrink:0; display:block;
      }
      .brand .name { font-size:1.28rem; font-weight:800; letter-spacing:-.015em; color:var(--txt);
                     line-height:1.3; }
      .brand .name span {
        background: linear-gradient(90deg, var(--accent), var(--accent-2));
        -webkit-background-clip:text; -webkit-text-fill-color:transparent;
      }
      .brand .tag {
        color: var(--txt-faint); font-size:.82rem; margin-top:10px; font-weight:500;
        letter-spacing:.02em;
      }

      .progress-wrap { padding: 18px 24px 6px; }
      .progress-top {
        display:flex; justify-content:space-between; align-items:baseline;
        color: var(--txt-dim); font-size:.82rem; font-weight:600; margin-bottom:9px;
      }
      .progress-top b { color:var(--txt); font-size:1rem; }
      .track { height:7px; background: rgba(15,23,42,.08); border-radius:999px; overflow:hidden; }
      .track .fill {
        height:100%; border-radius:999px; transition: width .4s cubic-bezier(.4,0,.2,1);
        background: linear-gradient(90deg, var(--accent), var(--accent-2));
        box-shadow: 0 0 14px rgba(124,108,255,.5);
      }

      .nav-head {
        color: var(--txt-faint); font-size:.74rem; font-weight:700; letter-spacing:.14em;
        text-transform:uppercase; padding: 22px 26px 10px;
      }

      /* section heading above each group of layers */
      .nav-section {
        color: var(--txt-dim); font-size:.8rem; font-weight:700; letter-spacing:.08em;
        text-transform:uppercase; padding: 10px 26px 6px;
      }
      /* nav items are full-width buttons with a two-line markdown label */
      section[data-testid="stSidebar"] .stButton { margin: 0 14px 4px; }
      /* single-line nav item: one left-aligned label, balanced height */
      section[data-testid="stSidebar"] .stButton > button {
        width:100%; display:flex; align-items:center;
        justify-content:flex-start !important; text-align:left !important;
        border:1px solid transparent; border-radius:10px;
        background: var(--panel); color: var(--txt-dim);
        padding: 10px 16px; min-height:0;
        box-shadow:none; transition: all .16s cubic-bezier(.4,0,.2,1);
      }
      /* Force the label (and any Streamlit-injected wrapper) to the left edge. */
      section[data-testid="stSidebar"] .stButton > button > * {
        width:100% !important; justify-content:flex-start !important; text-align:left !important;
      }
      section[data-testid="stSidebar"] .stButton > button [data-testid="stMarkdownContainer"] {
        width:100%; text-align:left !important;
      }
      section[data-testid="stSidebar"] .stButton > button p {
        margin:0; text-align:left !important;
        font-size:.95rem; font-weight:500; line-height:1.4; color: var(--txt);
      }
      /* layer name (first line of each nav item): navy blue */
      section[data-testid="stSidebar"] .stButton > button p:first-child,
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] p:first-child {
        color: #1e3a8a;
      }
      /* chosen technology: the lines after the layer name, smaller and dimmer */
      section[data-testid="stSidebar"] .stButton > button p + p {
        font-size:.76rem; font-weight:400; color: var(--txt-faint); margin-top:2px;
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] p + p {
        font-weight:400; color: var(--txt-dim);
      }
      section[data-testid="stSidebar"] .stButton > button:hover {
        background: rgba(109,92,255,.06); border-color: rgba(109,92,255,.3);
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, rgba(109,92,255,.20), rgba(8,145,178,.12));
        border-color: rgba(109,92,255,.75);
        border-left:4px solid var(--accent);
        box-shadow: 0 6px 18px rgba(109,92,255,.22), inset 0 0 0 1px rgba(109,92,255,.14);
      }
      section[data-testid="stSidebar"] .stButton > button[kind="primary"] p { color: var(--txt); font-weight:600; }

      /* ===================== TABS (Inference roles) ===================== */
      /* Folder tabs: a baseline runs under the tab row; the selected tab is
         an open-bottomed card (accent top edge) that merges into the panel
         below; unselected tabs are plain text on the baseline. */
      div[data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 2px; padding: 0 4px; margin-bottom: 0;
        background: transparent;
        border-bottom: 1px solid var(--panel-brd);
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"] {
        height: auto; padding: 12px 22px 11px;
        background: transparent;
        border: 1px solid transparent; border-bottom: none;
        border-radius: 10px 10px 0 0;
        margin-bottom: -1px;               /* sit on the baseline */
        transition: color .15s ease, background .15s ease;
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"] p {
        font-size: 1rem; font-weight: 600; color: var(--txt-dim); margin: 0;
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"]:hover {
        background: rgba(109,92,255,.05);
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"]:hover p { color: var(--txt); }
      div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        background: var(--panel);
        border-color: var(--panel-brd);
        border-top: 3px solid var(--accent);
        box-shadow: 0 1px 0 var(--panel);  /* hides the baseline under it */
      }
      div[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] p {
        color: var(--txt); font-weight: 700;
      }
      /* the tab's content panel: same card, attached to the selected tab */
      div[data-testid="stTabs"] [data-baseweb="tab-panel"] {
        background: var(--panel);
        border: 1px solid var(--panel-brd); border-top: none;
        border-radius: 0 0 14px 14px;
        padding: 4px 24px 18px;
      }
      /* drop Streamlit's own underline; the open card marks the selection */
      div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
      div[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }

      /* ===================== MAIN DETAIL ===================== */
      /* Hero: compact card with the step pill + title on one row. */
      .hero {
        background: var(--panel); border:1px solid var(--panel-brd);
        border-radius: 16px; padding: 16px 22px; margin-bottom: 18px;
        backdrop-filter: blur(12px);
        box-shadow: 0 6px 20px rgba(15,23,42,.07);
      }
      .hero-row {
        display:flex; align-items:center; gap:12px; margin-bottom:6px;
      }
      .hero .step {
        display:inline-block; font-size:.70rem; font-weight:700; letter-spacing:.09em;
        text-transform:uppercase; color: var(--accent-2);
        background: rgba(34,211,238,.16); border:1px solid rgba(34,211,238,.36);
        padding: 2px 10px; border-radius:999px; line-height:1.5;
      }
      /* Title: "Section › Page" on one baseline, same font family and size */
      .hero-title {
        display:flex; align-items:baseline; flex-wrap:wrap; gap:0 10px;
        font-family: inherit; font-size:1.65rem; line-height:1.25;
        letter-spacing:-.015em; margin:0;
      }
      .hero-title .crumb { font-weight:600; color: var(--txt-dim); }
      .hero-title .sep   { font-weight:400; color: var(--txt-faint); }
      .hero-title .page  { font-weight:800; color: var(--txt); }
      .hero p  { color: var(--txt); font-size:1rem; line-height:1.5; margin:0; font-weight:500; }
      .hero-note {
        margin-top:6px;
        color: var(--txt-dim); font-size:.98rem; line-height:1.45;
        font-style: italic;
      }
      .hero-note-icon { display:none; }

      /* section heading: accent bar + mixed-case title (replaces the old
         tiny uppercase label) */
      .section-label {
        display:flex; align-items:center; gap:10px;
        color: var(--txt); font-size:1.12rem; font-weight:700; letter-spacing:-.01em;
        margin: 32px 0 14px; padding-left:2px;
      }
      .section-label::before {
        content:""; flex:none; width:4px; height:20px; border-radius:999px;
        background: linear-gradient(180deg, var(--accent), var(--accent-2));
      }
      /* Modifier for secondary/action headers (Choose model, Copy this into
         your notebook): same font weight but no accent bar, so the primary
         section headers above them read as the dominant hierarchy. */
      .section-label--plain::before { content: none !important; }
      .section-label--plain { padding-left: 0; }

      /* Step header: numbered circle badge + large title. Used for the L2
         inference role headers (1 · Sub-agents / 2 · Orchestrator / 3 ·
         Evaluator). Signals a sequential workflow more clearly than the
         plain accent bar. */
      .step-header {
        display: flex;
        align-items: center;
        gap: 14px;
        margin: 36px 0 10px;
      }
      .step-badge {
        flex: none;
        width: 40px; height: 40px;
        border-radius: 999px;
        background: linear-gradient(135deg, var(--accent), var(--accent-2));
        color: #ffffff;
        font-weight: 800;
        font-size: 1.2rem;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 4px 14px rgba(109,92,255,.35);
      }
      .step-title {
        color: var(--txt);
        font-size: 1.55rem;
        font-weight: 800;
        letter-spacing: -.02em;
        line-height: 1.1;
      }

      /* ---- provider cards (Option A): horizontal cards ----
         Streamlit adds class "st-key-<key>" to every keyed widget, so we scope
         to keys starting with "pcard-". Nothing else in the app is affected. */
      div[data-testid="stHorizontalBlock"]:has([class*="st-key-pcard-"]) { gap:16px; }
      [class*="st-key-pcard-"] button {
        width:100% !important; min-height:118px !important; height:auto !important;
        padding:18px 20px !important; text-align:left !important;
        justify-content:flex-start !important; align-items:center !important;
        background:#ffffff !important; color:var(--txt) !important;
        border:1px solid #e2e8f0 !important; border-radius:16px !important;
        box-shadow:0 1px 2px rgba(15,23,42,.04) !important;
        transition:border-color .15s ease, box-shadow .15s ease, transform .15s ease;
        position:relative;
      }
      [class*="st-key-pcard-"] button:hover {
        border-color:rgba(109,92,255,.55) !important;
        box-shadow:0 8px 20px rgba(15,23,42,.08) !important; transform:translateY(-1px);
      }
      /* selected card */
      [class*="st-key-pcard-"] button[kind="primary"],
      [class*="st-key-pcard-"] button[data-testid="stBaseButton-primary"] {
        border:2px solid var(--accent) !important; background:rgba(109,92,255,.05) !important;
        box-shadow:0 8px 22px rgba(109,92,255,.15) !important;
      }
      [class*="st-key-pcard-"] button[kind="primary"]::after,
      [class*="st-key-pcard-"] button[data-testid="stBaseButton-primary"]::after {
        content:"✓"; position:absolute; top:12px; right:12px;
        width:24px; height:24px; border-radius:999px; background:var(--accent);
        color:#fff; font-size:.8rem; font-weight:800;
        display:flex; align-items:center; justify-content:center;
      }
      /* label layout: logo on the left spanning 3 rows, text on the right */
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] {
        display:grid !important; grid-template-columns:64px 1fr;
        column-gap:16px; row-gap:2px; align-items:center; width:100%;
      }
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] p {
        margin:0 !important; text-align:left !important;
      }
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] p:nth-child(1) {
        grid-column:1; grid-row:1 / span 3; line-height:0;
      }
      [class*="st-key-pcard-"] button img {
        width:64px !important; height:64px !important; max-height:64px !important;
        border-radius:14px; object-fit:cover;
      }
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] p:nth-child(2) {
        grid-column:2; font-size:1.08rem; font-weight:800; color:var(--txt);
      }
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] p:nth-child(3) {
        grid-column:2; font-size:.88rem; font-weight:500; color:var(--txt-dim); line-height:1.35;
      }
      [class*="st-key-pcard-"] button [data-testid="stMarkdownContainer"] p:nth-child(4) {
        grid-column:2; justify-self:start; margin-top:6px !important;
        font-size:.68rem; font-weight:800; letter-spacing:.06em; text-transform:uppercase;
        padding:2px 9px; border-radius:999px;
      }
      [class*="st-key-pcard-aws-"] button [data-testid="stMarkdownContainer"] p:nth-child(4) {
        background:#eff6ff; color:#2563eb;
      }
      [class*="st-key-pcard-key-"] button [data-testid="stMarkdownContainer"] p:nth-child(4) {
        background:#fef3c7; color:#b45309;
      }

      .provider-desc {
        background: linear-gradient(135deg, rgba(124,108,255,.10), rgba(34,211,238,.04));
        border:1px solid rgba(124,108,255,.38); border-left:4px solid var(--accent);
        border-radius:16px;
        padding: 18px 22px; color: var(--txt); font-size:1.04rem; line-height:1.6;
        margin: 2px 0 6px;
        box-shadow: 0 4px 14px rgba(109,92,255,.08);
      }
      .provider-desc b { color: var(--txt); font-weight:700; }
      .provider-desc a {
        color: var(--accent); font-weight:600; text-decoration: underline;
        text-underline-offset: 3px; white-space: nowrap;
      }
      .provider-desc a:hover, .provider-desc a:focus-visible { text-decoration-thickness: 2px; }

      .ssm-note { color: var(--txt-dim); font-size:1rem; margin-bottom:10px; }
      .ssm-path {
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
        color: var(--accent-2); font-size:.95rem;
        background: rgba(34,211,238,.08); padding:2px 8px; border-radius:6px;
      }
      .badge-ok {
        display:inline-block; font-size:.9rem; font-weight:700; padding:5px 14px;
        border-radius:999px; background: rgba(52,211,153,.14); color: var(--ok);
        border:1px solid rgba(52,211,153,.35);
      }

      /* hide the 'Press Enter to apply' hint overlay */
      .stTextInput [data-testid="InputInstructions"] { display: none !important; }

      /* text input */
      .stTextInput input {
        font-size:1.02rem; padding:13px 16px; border-radius:12px !important;
        background: var(--bg-soft) !important; border:1px solid var(--panel-brd) !important;
        color: var(--txt) !important;
      }
      .stTextInput input:focus { border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(124,108,255,.2) !important; }

      /* selectbox: match the text-input visual (border, radius, bg) so the
         dropdown has a clear outer line and reads as a form field, not plain text */
      div[data-baseweb="select"] > div {
        background: var(--bg-soft) !important;
        border: 1px solid var(--panel-brd) !important;
        border-radius: 12px !important;
        min-height: 46px;
      }
      div[data-baseweb="select"] > div:hover {
        border-color: rgba(124,108,255,.55) !important;
      }
      div[data-baseweb="select"] > div[aria-expanded="true"] {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px rgba(124,108,255,.2) !important;
      }

      /* Code snippet: cap the rendered code block height so expanding a
         snippet in one role doesn't push the roles below it off-screen.
         The code is still fully accessible via internal scroll + the
         always-visible copy-to-clipboard button on the top-right. */
      [data-testid="stCode"] pre,
      [data-testid="stCodeBlock"] pre {
        max-height: 320px !important;
        overflow-y: auto !important;
      }



      /* code block */
      div[data-testid="stCode"], pre { border-radius:16px !important; font-size:.95rem !important; }
      /* always show the "Copy to clipboard" button on code blocks (default is hover-only) */
      [data-testid="stCode"] button,
      [data-testid="stCodeBlock"] button,
      [data-testid="stCodeCopyButton"],
      [data-testid="stCopyToClipboardButton"] {
        opacity: 1 !important;
      }
      /* locked snippet: the summary row stays visible but is non-interactive,
         and the content body is hidden so neither the code nor the copy
         button are reachable until the key is saved to SSM.
         The wrapper's st-key-* class ends in "-locked". */
      [class*="st-key-snip-wrap-"][class*="-locked"] [data-testid="stExpander"] summary {
        pointer-events: none !important;
        opacity: 0.5 !important;
        cursor: not-allowed !important;
      }
      [class*="st-key-snip-wrap-"][class*="-locked"] [data-testid="stExpander"] details > div,
      [class*="st-key-snip-wrap-"][class*="-locked"] [data-testid="stExpander"] details > *:not(summary) {
        display: none !important;
      }

      /* _lock_hint(): "Save your credentials above..." shown under a credential form. */
      .snippet-lock-hint {
        color: var(--warn);
        font-size: .92rem; font-weight: 500;
        margin: 4px 0 10px 0;
      }
      .snippet-lock-hint::before {
        content: "🔒 "; font-size: 1rem;
      }

      /* Snippet expanders ("1 · Ingest the knowledge", "2 · Test retrieval")
         get a left accent bar so they read as the primary output of this page,
         not generic disclosures. */
      div[data-testid="stExpander"] {
        border:1px solid var(--panel-brd) !important;
        border-left:4px solid var(--accent) !important;
        border-radius:14px !important;
        background: var(--panel);
        box-shadow: 0 4px 14px rgba(15,23,42,.05);
        margin: 8px 0;
      }
      div[data-testid="stExpander"] summary {
        font-weight:700 !important; font-size:1.02rem !important; color: var(--txt) !important;
      }

      /* clickable architecture diagram: the button IS the image */
      [class*="st-key-arch_click"] button {
        padding:4px !important; border:1px solid var(--panel-brd) !important;
        background:#fff !important; border-radius:12px !important; height:auto !important;
        box-shadow:0 2px 10px rgba(15,23,42,.06) !important; cursor:zoom-in !important;
        transition:border-color .15s ease, box-shadow .15s ease !important;
      }
      [class*="st-key-arch_click"] button:hover {
        border-color:var(--accent) !important; box-shadow:0 8px 22px rgba(109,92,255,.18) !important;
      }
      [class*="st-key-arch_click"] button img {
        width:100% !important; height:auto !important; max-height:none !important;
        border-radius:9px !important; display:block !important;
      }
      [class*="st-key-arch_click"] button > div, [class*="st-key-arch_click"] button p {
        width:100% !important; margin:0 !important;
      }
      /* Streamlit wraps the label in shrink-to-fit span/div layers; stretch them
         so the image scales to the button width instead of its intrinsic size. */
      [class*="st-key-arch_click"] button span,
      [class*="st-key-arch_click"] button div {
        width:100% !important; max-width:100% !important;
      }
      code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace !important; }

      /* Save/action buttons in the main area -> gradient. Scoped to the main
         container; the provider-tile rules above are more specific (they
         include the stHorizontalBlock+column chain) so tiles keep their look. */
      div[data-testid="stMain"] .stButton > button[kind="primary"],
      div[data-testid="stMain"] .stButton > button[kind="primary"] p {
        color:#ffffff !important;
      }
      div[data-testid="stMain"] .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, var(--accent), #6366f1) !important;
        border:none !important; font-weight:700; font-size:1.04rem;
        border-radius:13px; padding:14px 20px;
        box-shadow: 0 10px 30px rgba(124,108,255,.35);
        transition: all .16s ease;
      }
      div[data-testid="stMain"] .stButton > button[kind="primary"]:hover {
        transform: translateY(-1px); box-shadow: 0 14px 38px rgba(124,108,255,.5);
      }
      /* secondary buttons in the main area (e.g. "Save key"): readable dark
         text on a light surface */
      div[data-testid="stMain"] .stButton > button[kind="secondary"],
      div[data-testid="stMain"] .stButton > button[kind="secondary"] p {
        color: var(--txt) !important;
      }
      div[data-testid="stMain"] .stButton > button[kind="secondary"] {
        background:#ffffff !important; border:1px solid var(--panel-brd) !important;
        border-radius:11px; font-weight:600;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Layers flagged "hidden" in snippets.py are left out of the UI entirely
# (nav, step numbers, progress). Remove the flag to bring a layer back.
LAYERS = [lyr for lyr in LAYERS if not lyr.get("hidden")]

# --- State ---
if "active_layer" not in st.session_state or st.session_state.active_layer not in {
        lyr["id"] for lyr in LAYERS}:
    st.session_state.active_layer = LAYERS[0]["id"]
if "selections" not in st.session_state:
    st.session_state.selections = {}

LAYERS_BY_ID = {layer["id"]: layer for layer in LAYERS}

def _chosen(layer: dict) -> str:
    return st.session_state.selections.get(layer["id"], list(layer["providers"].keys())[0])


def _nav_choices(layer: dict) -> list:
    """Lines shown under a nav item: the technology chosen on that page.

    The page's first option is the default: "Default: X" until the participant
    picks a card (or saved a choice earlier, for the pages that read it back
    from SSM), then "Selected: X".
    """
    def short(name: str) -> str:
        return name.replace(" (AWS)", "")

    if layer["id"] == "inference":
        # One line for both roles: the provider(s), e.g. "Default: Amazon Bedrock"
        # or "Selected: Amazon Bedrock + Baseten" when the roles differ.
        names = list(MODEL_PROVIDERS.keys())
        provs, any_picked = [], False
        for role in INFERENCE_ROLES:
            rid = role["id"]
            prov = st.session_state.selections.get(f"inference_{rid}_provider")
            if prov not in names:
                saved = get_param(f"{_CFG}/inference/{rid}/provider")
                prov = saved if saved in names else None
            any_picked = any_picked or prov is not None
            provs.append(prov or names[0])
        unique = list(dict.fromkeys(short(p) for p in provs))
        return [f"{'Selected' if any_picked else 'Default'}: {' + '.join(unique)}"]

    names = list(layer["providers"].keys())
    choice = st.session_state.selections.get(layer["id"])
    if choice in names:
        return [f"Selected: {short(choice)}"]
    if layer["id"] == "observability":   # the saved choice is read back on its page too
        saved = get_param(f"{_CFG}/observability/provider")
        by_value = {m.get("value", n): n for n, m in layer["providers"].items()}
        if saved in by_value:
            return [f"Selected: {short(by_value[saved])}"]
    return [f"Default: {short(names[0])}"]


def _creds(prov: dict) -> list:
    """Normalize a provider's credential definition to a list of field dicts.

    Supports both the single-field "credential" shape (most layers) and the
    multi-field "credentials" list (observability / Datadog)."""
    if prov.get("credentials"):
        return prov["credentials"]
    if prov.get("credential"):
        return [prov["credential"]]
    return []


def _staged_provider_page(layer_id, providers, stages, snippet_fn, *, heading, intro,
                          status_fn=None, diagram=None):
    """Provider cards -> description -> API key (if any) -> status -> paste snippets.

    Used by layers whose notebook is provider-neutral and only the pasted
    Connect cell differs (Decision Models, Tools). ``providers`` maps a label to
    {logo, tag, tagline, description, key_param}; ``key_param`` is the SSM path
    of the partner key, or None for AWS options. Snippets stay locked until the
    key is saved. ``status_fn(choice, meta)`` may return an HTML status line.
    ``diagram`` is an optional (svg, alt) pair shown to the right of the intro.
    """
    if diagram:
        _intro_with_diagram(layer_id, heading, intro, diagram[0], diagram[1])
    else:
        st.markdown(f'<div class="section-label">{heading}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="ssm-note">{intro}</div>', unsafe_allow_html=True)

    names = list(providers.keys())
    cur = st.session_state.selections.get(layer_id)
    if cur not in names:
        cur = names[0]

    cols = st.columns(len(names))
    for col, opt in zip(cols, names):
        meta = providers[opt]
        kind = "key" if meta.get("key_param") else "aws"
        label = (f"![logo]({get_logo_data_uri(meta['logo'])})\n\n"
                 f"**{opt.replace(' (AWS)', '')}**\n\n{meta['tagline']}\n\n{_provider_tag(kind)}")
        with col:
            if st.button(label, key=f"pcard-{kind}-{layer_id}-{opt}",
                         type="primary" if opt == cur else "secondary",
                         use_container_width=True):
                st.session_state.selections[layer_id] = opt
                st.rerun()

    meta = providers[cur]
    st.markdown(f'<div class="provider-desc">{meta["description"]}</div>',
                unsafe_allow_html=True)

    snippet_state = "open"
    key_param = meta.get("key_param")
    if key_param:
        brand = cur.replace(" (AWS)", "").split()[0]          # "Brave", "Perplexity"
        key_stored = _credential_form(
            f"{layer_id}_{brand.lower()}", f"{brand} credentials",
            [{"key": "api_key", "label": f"{brand} API key", "param": key_param,
              "placeholder": "Paste your API key"}],
            note=("The same key is used by Tools and Decision Models."
                  if key_param == PERPLEXITY_KEY_PARAM else ""),
        )
        snippet_state = "open" if key_stored else "locked"
        if not key_stored:
            _lock_hint()

    note = status_fn(cur, meta) if status_fn else None
    if note:
        st.markdown(f'<div class="ssm-note">{note}</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-label">Code for your notebook cells</div>',
                unsafe_allow_html=True)
    for stage in stages:
        with st.container(key=f"snip-wrap-{layer_id}-{stage['id']}-{snippet_state}"):
            with st.expander(f"📋 {stage['label']}", expanded=False):
                st.caption(stage["help"])
                st.code(snippet_fn(cur, stage["id"]), language="python")


# ========================= LEFT NAV RAIL =========================
with st.sidebar:
    st.markdown(
        '<div class="brand"><div class="mark">'
        f'<img class="logo" src="{_PAF_LOGO_URI}" alt="Pluggable Agentic AI Framework logo"/>'
        '<span class="name">Pluggable Agentic <span>AI Framework</span></span>'
        '</div><div class="tag">Partner Selection UI</div></div>',
        unsafe_allow_html=True,
    )

    # Position in the flow: current layer number out of total (starts at 1/6).
    current_pos = next(
        (i for i, lyr in enumerate(LAYERS, 1) if lyr["id"] == st.session_state.active_layer),
        1,
    )
    pct = int(current_pos / len(LAYERS) * 100)
    st.markdown(
        f'<div class="progress-wrap">'
        f'<div class="progress-top"><span>Setup progress</span>'
        f'<b>{current_pos}/{len(LAYERS)}</b></div>'
        f'<div class="track"><div class="fill" style="width:{pct}%"></div></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="nav-head">Framework Layers</div>', unsafe_allow_html=True)
    # Every section is a heading with its layers listed underneath.
    for section in SECTIONS:
        members = [lyr for lyr in LAYERS if lyr["section"] == section]
        if not members:
            continue
        st.markdown(f'<div class="nav-section">{section}</div>', unsafe_allow_html=True)
        for layer in members:
            is_active = layer["id"] == st.session_state.active_layer
            # Layer name, then the chosen technology in smaller type (CSS below).
            label = f"**{layer['label']}**" + "".join(f"\n\n{line}" for line in _nav_choices(layer))
            key = f"nav_{layer['id']}"
            if st.button(
                label, key=key, use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                st.session_state.active_layer = layer["id"]
                st.rerun()


# ========================= MAIN DETAIL PANEL =========================
layer = LAYERS_BY_ID[st.session_state.active_layer]
layer_id = layer["id"]
layer_num = next(i for i, lyr in enumerate(LAYERS, 1) if lyr["id"] == layer_id)
options = list(layer["providers"].keys())
current = _chosen(layer)

page_label = layer["label"]

_note_html = (
    f'<div class="hero-note"><span class="hero-note-icon">ℹ</span>'
    f'<span>{layer["note"]}</span></div>'
    if layer.get("note") else ""
)
st.markdown(
    f'<div class="hero">'
    f'<div class="hero-row">'
    f'<span class="step">Step {layer_num} of {len(LAYERS)}</span>'
    # One title element (not <h1>) so Streamlit's own heading styles and anchor
    # icon don't apply; the section and layer share one font and baseline.
    + f'<div class="hero-title" role="heading" aria-level="1">'
      f'<span class="crumb">{layer["section"]}</span>'
      f'<span class="sep" aria-hidden="true">›</span>'
      f'<span class="page">{page_label}</span>'
      f'</div>'
    f'</div>'
    f'<p>{layer.get("help", "")}</p>'
    f'{_note_html}'
    f'</div>',
    unsafe_allow_html=True,
)

if layer_id == "inference":
    # =====================================================================
    # Inference layer: choose a model provider + model PER AGENT ROLE
    # (Orchestrator agent vs Sub-agents). Each role is stored separately.
    # =====================================================================
    # --- "How it works" text (left) + animated architecture diagram (right) ---
    _intro_with_diagram(
        "inference", "How model selection works",
        "The agents call their model through LiteLLM, an open source library that puts "
        "100+ providers behind one API. Pick a model for each agent role below.",
        _inference_diagram(), "Inference: a model per agent role",
    )

    # Short tagline + visual kind for each provider card, keyed by the same
    # name as MODEL_PROVIDERS. "kind" drives the pill color (aws = blue,
    # key = yellow); "tag" is the visible label on the card.
    INFERENCE_PROVIDER_META = {
        "Amazon Bedrock (AWS)": {
            "tagline": "Claude, Kimi and GPT models",
            "kind":    "aws",
            "tag":     "AWS SERVICE",
        },
        "Baseten": {
            "tagline": "Hosted open models: Kimi, GLM and more",
            "kind":    "key",
            "tag":     "PARTNER SOLUTION",
        },
    }

    # Provider description shown under the cards in every role tab.
    INFERENCE_PROVIDER_DESC = {
        "Amazon Bedrock (AWS)": (
            "<b>Frontier and open models behind one AWS API.</b> Amazon Bedrock serves Claude, "
            "Kimi and GPT models with IAM auth, so there are no API keys to manage. "
            "Both agent roles can use it, and the L7 evaluation judge always runs on it "
            "(Claude Haiku 5.5). The orchestrator Harness and the "
            "specialists reach it through LiteLLM."
            + _doc_link("https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html",
                        "Amazon Bedrock documentation")
        ),
        "Baseten": (
            "<b>Hosted open models, with nothing to deploy.</b> Baseten Model APIs serve models "
            "such as Kimi K3.1 Pro and GLM 5.3. LiteLLM calls them through its baseten/ "
            "provider with the same code as Bedrock; only the model id and your API key change. Available for the specialist agents and the orchestrator."
            + _doc_link("https://docs.baseten.co/development/model-apis/overview",
                        "Baseten Model APIs documentation")
        ),
    }
    # Roles that must use a Bedrock model, and why (shown next to the lone card).
    # The orchestrator is not here: the AgentCore Harness calls every model through
    # liteLlmModelConfig (L3 3_orchestrator_agent.ipynb, Step 3.2).
    # The evaluator (L7 judge) has no tab: L7 always uses Claude Haiku 5.5 on Bedrock.
    BEDROCK_ONLY_ROLES = {}

    # One tab per role (same order as the L2 notebook), so each role's
    # cards, model, key and snippet sit in a short view of their own.
    # `default` reopens the tab the user last acted in instead of jumping to the first.
    # Display names live here (keyed by the stable role id) so they don't depend
    # on the label text in snippets.py.
    _role_names = {"subagents": "Specialist Agents",
                   "orchestrator": "Orchestrator Agent"}
    _role_name = lambda r: _role_names.get(r["id"], r["label"].split(" · ", 1)[-1])
    _tab_labels = {r["id"]: _role_name(r) for r in INFERENCE_ROLES}
    _active_rid = st.session_state.get("inf_active_tab", INFERENCE_ROLES[0]["id"])
    _tabs = st.tabs(list(_tab_labels.values()),
                    default=_tab_labels.get(_active_rid))
    for role, _tab in zip(INFERENCE_ROLES, _tabs):
        with _tab:
            rid = role["id"]
            rbase = f"{_CFG}/inference/{rid}"

            # The evaluator is Bedrock-only: L7's custom LLM-as-judge evaluators pass
            # evaluator_model_id as bedrockEvaluatorModelConfig.modelId. The specialists
            # and the orchestrator (both through LiteLLM; the Harness uses
            # liteLlmModelConfig) show all three cards.
            if rid in BEDROCK_ONLY_ROLES:
                prov_names = [p for p in MODEL_PROVIDERS.keys() if p == "Amazon Bedrock (AWS)"]
            else:
                prov_names = list(MODEL_PROVIDERS.keys())

            # Role header: numbered circle badge + title. Split "1 · Sub-agents"
            # into the step number and the role name so each lives in its own
            # element (circle + heading) — reads as a stepped workflow.
            step_num = role["label"].split(" · ", 1)[0]
            step_name = _role_name(role)
            st.markdown(
                f'<div class="step-header">'
                f'<span class="step-badge">{step_num}</span>'
                f'<span class="step-title">{step_name}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.markdown(f'<div class="ssm-note">{role["help"]}</div>',
                        unsafe_allow_html=True)

            # ---- Provider cards (same visual as L1 data cards) ----
            # Reuses the st-key-pcard-* CSS block from the data-layer cards, so
            # the "AWS SERVICE" pill goes blue and "PARTNER SOLUTION" goes yellow
            # automatically. Session state tracks the selection per role.
            sel_key = f"inference_{rid}_provider"
            cur_prov = st.session_state.selections.get(sel_key)
            if cur_prov not in prov_names:
                cur_prov = get_param(f"{rbase}/provider", prov_names[0])
                if cur_prov not in prov_names:
                    cur_prov = prov_names[0]

            # The evaluator shows only the Bedrock card. Lay it out on the same
            # 3-column grid as the other tabs so the card keeps the same size.
            cols = st.columns(len(MODEL_PROVIDERS))
            for col, opt in zip(cols, prov_names):
                is_sel    = opt == cur_prov
                card_meta = INFERENCE_PROVIDER_META[opt]
                logo      = get_logo_data_uri(opt)
                label     = (
                    f"![logo]({logo})\n\n"
                    f"**{opt.replace(' (AWS)', '')}**\n\n"
                    f"{card_meta['tagline']}\n\n"
                    f"{card_meta['tag']}"
                )
                with col:
                    if st.button(label,
                                 key=f"pcard-{card_meta['kind']}-inference-{rid}-{opt}",
                                 type="primary" if is_sel else "secondary",
                                 use_container_width=True):
                        st.session_state.selections[sel_key] = opt
                        st.session_state.inf_active_tab = rid
                        st.rerun()
            if rid in BEDROCK_ONLY_ROLES:
                with cols[1]:
                    st.markdown(
                        f'<div class="ssm-note" style="padding-top:28px;">'
                        f'{BEDROCK_ONLY_ROLES[rid]}</div>',
                        unsafe_allow_html=True,
                    )

            prov = st.session_state.selections.get(sel_key, cur_prov)
            if prov not in prov_names:
                prov = cur_prov

            st.markdown(f'<div class="provider-desc">{INFERENCE_PROVIDER_DESC[prov]}</div>',
                        unsafe_allow_html=True)

            meta = MODEL_PROVIDERS[prov]

            # Model dropdown — use the same accent-bar section-label as the rest of
            # the page (uniform visual hierarchy) and collapse the Streamlit label.
            # Key includes `prov` so switching the provider card resets the
            # dropdown instead of carrying a stale model name.
            role_models = meta["models"].get(rid, {})
            model_names = list(role_models.keys())
            st.markdown('<div class="section-label section-label--plain">Choose model</div>',
                        unsafe_allow_html=True)
            model_label = st.selectbox("Model", model_names,
                                       key=f"inf_model_{rid}_{prov}",
                                       label_visibility="collapsed")
            model_id = role_models[model_label]

            # Partner providers need an API key; Bedrock never does.
            key_saved = True
            if meta["needs_key"]:
                def _keep_tab(rid=rid):
                    # Reopen this role's tab after the save rerun.
                    st.session_state.inf_active_tab = rid
                key_saved = _credential_form(
                    f"inf_{rid}_{prov}", f"{prov} credentials",
                    [{"key": "api_key", "label": f"{prov} API key", "param": f"{rbase}/api_key",
                      "placeholder": "Paste your API key"}],
                    after_save=_keep_tab,
                )

            snippet_state = "open" if key_saved else "locked"
            if not key_saved:
                _lock_hint()

            # Match the L1 pattern: a section-label header right before the snippet
            # so participants know where to go next after picking a model.
            st.markdown('<div class="section-label section-label--plain">Copy this into your notebook</div>',
                        unsafe_allow_html=True)
            with st.container(key=f"snip-wrap-{rid}-{prov}-{snippet_state}"):
                with st.expander(f"📋 {_role_name(role)} code snippet", expanded=False):
                    st.code(
                        inference_snippet(rid, role["label"], prov, model_label, model_id),
                        language="python",
                    )

            st.write("")

elif layer_id == "data_kb":
    # =====================================================================
    # Data layer - Unstructured data (return-policy knowledge).
    # ONE notebook (1_knowledge_base.ipynb) loads the shared POLICY_DOCS. The
    # provider-specific ingest + test code is generated here as paste snippets;
    # provider-specific infra (S3 upload for Bedrock KB, cluster provisioning
    # for Couchbase) lives inside the snippets, not the notebook skeleton.
    # =====================================================================
    # --- Intro text (left) + animated ingest/retrieve diagram (right) ---
    # The diagram is an SVG rendered as an <img> data URI (same pattern as the
    # inference page). Its CSS animations run inside the image: chunks travel
    # down the ingest pipeline, vectors light up in the store, and the
    # retrieval paths flow. A prefers-reduced-motion rule turns all motion off.
    _kb_vec_dots = "".join(
        f'<circle class="vec" cx="{892 + c * 20}" cy="{92 + r * 19}" r="4.5" '
        f'style="animation-delay:{((r * 3 + c) * 0.17) % 2.4:.2f}s"/>'
        for r in range(5) for c in range(3)
    )
    _kb_arch_svg = """
<svg viewBox="0 0 980 330" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,Segoe UI,Roboto,sans-serif" width="980" height="330" role="img" aria-labelledby="kbTitle kbDesc">
  <title id="kbTitle">Vector store: ingest and retrieve</title>
  <desc id="kbDesc">Ingest: return-policy documents are split into passages, embedded with Amazon Titan Text Embeddings V2, and indexed in a pluggable vector store: choose either Amazon Bedrock Knowledge Bases or Couchbase Capella. Retrieve: when a customer asks about returns, the Refund Agent runs a semantic search and gets back the top matching passages.</desc>
  <defs>
    <linearGradient id="kbg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#6d5cff"/><stop offset="1" stop-color="#0891b2"/>
    </linearGradient>
    <marker id="kbarr" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
      <path d="M0,0 L9,4.5 L0,9 Z" fill="#94a3b8"/>
    </marker>
    <marker id="kbarr2" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
      <path d="M0,0 L9,4.5 L0,9 Z" fill="#0891b2"/>
    </marker>
  </defs>
  <style>
    .flow { stroke-dasharray: 6 5; animation: kbdash 1.1s linear infinite; }
    @keyframes kbdash { to { stroke-dashoffset: -22; } }
    .pkt { animation: kbmove 2.4s ease-in-out infinite; opacity: 0; }
    @keyframes kbmove {
      0%   { transform: translateX(0);    opacity: 0; }
      15%  { opacity: 1; }
      80%  { opacity: 1; }
      100% { transform: translateX(26px); opacity: 0; }
    }
    .vec { fill: #0891b2; animation: kbpulse 2.4s ease-in-out infinite; opacity: .25; }
    @keyframes kbpulse { 0%, 100% { opacity: .2; } 45% { opacity: 1; } }
    /* The selection alternates between the two providers to show the slot is swappable. */
    .selA { animation: kbselA 6s ease-in-out infinite; }
    .selB { animation: kbselB 6s ease-in-out infinite; opacity: 0; }
    @keyframes kbselA { 0%, 42% { opacity: 1; } 50%, 92% { opacity: 0; } 100% { opacity: 1; } }
    @keyframes kbselB { 0%, 42% { opacity: 0; } 50%, 92% { opacity: 1; } 100% { opacity: 0; } }
    @media (prefers-reduced-motion: reduce) {
      .flow, .pkt, .vec, .selA, .selB { animation: none; }
      .pkt { opacity: 1; }
      .vec { opacity: .8; }
      .selA, .selB { opacity: 0; }
    }
  </style>

  <!-- lane labels -->
  <text x="24" y="36" fill="#64748b" font-size="12" font-weight="700" letter-spacing="1.5">INGEST</text>
  <text x="88" y="36" fill="#94a3b8" font-size="11">once, in the L1 notebook</text>
  <text x="24" y="250" fill="#64748b" font-size="12" font-weight="700" letter-spacing="1.5">RETRIEVE</text>
  <text x="24" y="267" fill="#94a3b8" font-size="11">on every policy question</text>

  <!-- 1. policy documents -->
  <rect x="24" y="70" width="168" height="96" rx="12" fill="#ffffff" stroke="#e2e8f0"/>
  <rect x="100" y="80" width="18" height="23" rx="2.5" fill="#eef2ff" stroke="#6d5cff" stroke-width="1.2"/>
  <rect x="94"  y="84" width="18" height="23" rx="2.5" fill="#ffffff" stroke="#6d5cff" stroke-width="1.2"/>
  <line x1="98" y1="91" x2="108" y2="91" stroke="#6d5cff" stroke-width="1.2"/>
  <line x1="98" y1="96" x2="108" y2="96" stroke="#6d5cff" stroke-width="1.2"/>
  <line x1="98" y1="101" x2="105" y2="101" stroke="#6d5cff" stroke-width="1.2"/>
  <text x="108" y="128" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Policy documents</text>
  <text x="108" y="146" text-anchor="middle" fill="#64748b" font-size="11">return &amp; refund policy</text>

  <!-- 2. chunk -->
  <rect x="238" y="70" width="150" height="96" rx="12" fill="#ffffff" stroke="#e2e8f0"/>
  <rect x="294" y="81" width="38" height="6" rx="3" fill="#c7d2fe"/>
  <rect x="294" y="90" width="38" height="6" rx="3" fill="#a5b4fc"/>
  <rect x="294" y="99" width="38" height="6" rx="3" fill="#818cf8"/>
  <text x="313" y="128" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Chunk</text>
  <text x="313" y="146" text-anchor="middle" fill="#64748b" font-size="11">split into passages</text>

  <!-- 3. embed -->
  <rect x="434" y="70" width="150" height="96" rx="14" fill="url(#kbg)"/>
  <text x="509" y="106" text-anchor="middle" fill="#ffffff" font-size="15" font-weight="800">Embed</text>
  <text x="509" y="126" text-anchor="middle" fill="#e5e7ff" font-size="11">Titan Text</text>
  <text x="509" y="140" text-anchor="middle" fill="#e5e7ff" font-size="11">Embeddings V2</text>
  <text x="509" y="155" text-anchor="middle" fill="#e5e7ff" font-size="10.5">1024-dim vectors</text>

  <!-- 4. vector store: a pluggable slot with one provider chosen -->
  <rect x="630" y="44" width="326" height="152" rx="14" fill="#f8fafc" stroke="#6d5cff" stroke-width="1.5" stroke-dasharray="7 5"/>
  <text x="650" y="68" fill="#0f172a" font-size="14" font-weight="800">Vector store</text>
  <rect x="748" y="56" width="74" height="17" rx="8.5" fill="#eef2ff"/>
  <text x="785" y="68" text-anchor="middle" fill="#6d5cff" font-size="9.5" font-weight="800" letter-spacing="1">PLUGGABLE</text>
  <text x="832" y="68" fill="#64748b" font-size="11">choose one</text>

  <!-- option A: Bedrock Knowledge Bases -->
  <rect x="650" y="82" width="222" height="36" rx="10" fill="#ffffff" stroke="#e2e8f0"/>
  <rect class="selA" x="650" y="82" width="222" height="36" rx="10" fill="#eef2ff" stroke="#6d5cff" stroke-width="1.5"/>
  <circle cx="665" cy="100" r="5.5" fill="#ffffff" stroke="#94a3b8" stroke-width="1.3"/>
  <circle class="selA" cx="665" cy="100" r="3" fill="#6d5cff"/>
  <text x="678" y="97" fill="#0f172a" font-size="12" font-weight="700">Bedrock Knowledge Bases</text>
  {PILL_AWS}
  <text x="754" y="111" fill="#64748b" font-size="10.5">OpenSearch Serverless</text>

  <!-- OR divider -->
  <line x1="660" y1="128" x2="747" y2="128" stroke="#e2e8f0"/>
  <line x1="775" y1="128" x2="862" y2="128" stroke="#e2e8f0"/>
  <rect x="747" y="121" width="28" height="14" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
  <text x="761" y="131.5" text-anchor="middle" fill="#64748b" font-size="9" font-weight="800">OR</text>

  <!-- option B: Couchbase Capella -->
  <rect x="650" y="138" width="222" height="36" rx="10" fill="#ffffff" stroke="#e2e8f0"/>
  <rect class="selB" x="650" y="138" width="222" height="36" rx="10" fill="#eef2ff" stroke="#6d5cff" stroke-width="1.5"/>
  <circle cx="665" cy="156" r="5.5" fill="#ffffff" stroke="#94a3b8" stroke-width="1.3"/>
  <circle class="selB" cx="665" cy="156" r="3" fill="#6d5cff"/>
  <text x="678" y="153" fill="#0f172a" font-size="12" font-weight="700">Couchbase Capella</text>
  {PILL_PARTNER}
  <text x="784" y="167" fill="#64748b" font-size="10.5">vector search</text>

  <text x="793" y="188" text-anchor="middle" fill="#94a3b8" font-size="10">same documents · same embeddings · swap anytime</text>
  {DOTS}

  <!-- ingest arrows + traveling chunks -->
  <path class="flow" d="M192 118 L 234 118" stroke="#94a3b8" stroke-width="2" fill="none" marker-end="url(#kbarr)"/>
  <path class="flow" d="M388 118 L 430 118" stroke="#94a3b8" stroke-width="2" fill="none" marker-end="url(#kbarr)"/>
  <path class="flow" d="M584 118 L 626 118" stroke="#0891b2" stroke-width="2" fill="none" marker-end="url(#kbarr2)"/>
  <rect class="pkt" x="194" y="113" width="10" height="10" rx="2.5" fill="#6d5cff" style="animation-delay:0s"/>
  <rect class="pkt" x="390" y="113" width="10" height="10" rx="2.5" fill="#818cf8" style="animation-delay:.8s"/>
  <circle class="pkt" cx="591" cy="118" r="5" fill="#0891b2" style="animation-delay:1.6s"/>

  <!-- retrieve lane -->
  <rect x="238" y="226" width="150" height="56" rx="12" fill="#ffffff" stroke="#e2e8f0"/>
  <text x="313" y="250" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Customer question</text>
  <text x="313" y="268" text-anchor="middle" fill="#64748b" font-size="11">"Can I return this?"</text>
  <path class="flow" d="M388 254 L 430 254" stroke="#94a3b8" stroke-width="2" fill="none" marker-end="url(#kbarr)"/>
  <rect x="434" y="226" width="150" height="56" rx="12" fill="#ffffff" stroke="#6d5cff" stroke-width="1.5"/>
  <text x="509" y="250" text-anchor="middle" fill="#0f172a" font-size="14" font-weight="700">Refund Agent</text>
  <text x="509" y="268" text-anchor="middle" fill="#64748b" font-size="11">refund_policy tool</text>

  <!-- query up to the store, passages back down -->
  <path class="flow" d="M584 240 L 760 240 L 760 200" stroke="#6d5cff" stroke-width="2" fill="none" marker-end="url(#kbarr)"/>
  <text x="672" y="232" text-anchor="middle" fill="#6d5cff" font-size="11" font-weight="600">semantic search</text>
  <path class="flow" d="M800 196 L 800 268 L 588 268" stroke="#0891b2" stroke-width="2" fill="none" marker-end="url(#kbarr2)"/>
  <text x="694" y="285" text-anchor="middle" fill="#0891b2" font-size="11" font-weight="600">top matching passages</text>

  <text x="490" y="318" text-anchor="middle" fill="#475569" font-size="11.5">The vector store is pluggable: pick either provider, and swap later by pasting a different snippet.</text>
</svg>
""".replace("{DOTS}", _kb_vec_dots) \
     .replace("{PILL_AWS}", _tag_pill(678, 111, "aws")[0]) \
     .replace("{PILL_PARTNER}", _tag_pill(678, 167, "partner")[0])
    import base64 as _b64
    _kb_svg_uri = ("data:image/svg+xml;base64,"
                   + _b64.b64encode(_kb_arch_svg.encode("utf-8")).decode("ascii"))
    _kb_intro_col, _kb_diagram_col = st.columns([1, 1.25], gap="large")
    with _kb_intro_col:
        st.markdown(
            '<div class="section-label">How the vector store works</div>'
            '<div class="ssm-note">'
            'The policy documents are split into passages and stored as vectors. A '
            'question is turned into a vector too, and the store returns the closest '
            'passages. Both providers below work this way.'
            '</div>',
            unsafe_allow_html=True,
        )
    with _kb_diagram_col:
        # Same clickable-diagram pattern as the inference page.
        if st.button(f"![Vector store ingest and retrieval flow]({_kb_svg_uri})",
                     key="arch_click_kb", use_container_width=True):
            _show_kb_arch_dialog(_kb_svg_uri)
        st.caption("Click the diagram to enlarge")

    prov_names = list(UNSTRUCTURED_PROVIDERS.keys())
    cur_kb = st.session_state.selections.get("data_kb")
    if cur_kb not in prov_names:
        cur_kb = prov_names[0]

    kb_taglines = {
        "Bedrock KB (AWS)": "Managed RAG on OpenSearch Serverless",
        "Couchbase":        "Vector search in Couchbase Capella",
    }
    kb_descriptions = {
        "Bedrock KB (AWS)": "<b>Fully managed RAG, with no pipeline to run.</b> Point "
                            "Amazon Bedrock Knowledge Bases at the policy docs in S3, and "
                            "it handles chunking, Titan embeddings (1024-dim), and indexing "
                            "in OpenSearch Serverless for you. Agents get semantic search "
                            "through one Retrieve API secured by IAM, with no extra "
                            "credentials. It's wired to the Refund Agent's "
                            "<code>refund_policy</code> tool out of the box. "
                            "<b>Setup ≈ 6 min</b> on first run, while the vector collection "
                            "comes online. "
                            '<a href="https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html" '
                            'target="_blank" rel="noopener noreferrer">'
                            "Amazon Bedrock Knowledge Bases documentation ↗</a>",
        "Couchbase":        "<b>Vectors live next to your operational data.</b> Couchbase "
                            "Capella stores each policy chunk as a document, with its text "
                            "and its Titan embedding (1024-dim), in a regular collection "
                            "with a cosine-similarity vector index. Semantic search is an "
                            "ordinary database query, so it can sit alongside filters and "
                            "joins on the same data. "
                            "<b>Setup ≈ 6 min</b> on first run, while a free-tier Capella "
                            "cluster deploys. Add your Capella credentials below first. "
                            '<a href="https://docs.couchbase.com/cloud/vector-index/vectors-and-indexes-overview.html" '
                            'target="_blank" rel="noopener noreferrer">'
                            "Couchbase Capella vector index documentation ↗</a>",
    }

    cols = st.columns(len(prov_names))
    for col, opt in zip(cols, prov_names):
        is_sel = opt == cur_kb
        meta   = UNSTRUCTURED_PROVIDERS[opt]
        logo   = get_logo_data_uri(meta["logo"])
        kind   = meta.get("kind", "aws")
        tag    = meta.get("tag",  "AWS SERVICE")
        label  = (f"![logo]({logo})\n\n**{opt.replace(' (AWS)', '')}**\n\n"
                  f"{kb_taglines.get(opt, '')}\n\n{tag}")
        with col:
            if st.button(label, key=f"pcard-{kind}-data_kb-{opt}",
                         type="primary" if is_sel else "secondary",
                         use_container_width=True):
                st.session_state.selections["data_kb"] = opt
                st.rerun()

    kb_choice = st.session_state.selections.get("data_kb", cur_kb)
    if kb_choice not in prov_names:
        kb_choice = cur_kb
    if kb_descriptions.get(kb_choice):
        st.markdown(f'<div class="provider-desc">{kb_descriptions[kb_choice]}</div>',
                    unsafe_allow_html=True)

    # Lock state for the paste snippets. Bedrock KB needs no credentials, so
    # it defaults to open; the Couchbase branch flips this to "locked" until
    # org_id, project_id, and api_token are all saved to SSM (mirrors the L2
    # inference partner-key lock pattern).
    snippet_state = "open"

    # Couchbase prerequisite: Capella credentials. The helpers/couchbase_kb.provision()
    # function reads these from SSM on first use to deploy the free-tier cluster,
    # create the bucket + scopes + collections, and generate a DB credential.
    if kb_choice == "Couchbase":
        cb_prefix = "/anycompany/agentcore/couchbase"
        cb_org_name   = f"{cb_prefix}/org_id"
        cb_proj_name  = f"{cb_prefix}/project_id"
        cb_tok_name   = f"{cb_prefix}/api_token"
        cb_help = (
            'One-time setup, takes ~2 min.'
            '<ol style="margin:0.4rem 0 0.4rem 1.1rem;padding:0;">'
            '<li>A free Capella account is enough. Sign in at '
            '<a href="https://cloud.couchbase.com" target="_blank" rel="noopener noreferrer">'
            'cloud.couchbase.com</a>.</li>'
            '<li><b>Organization ID</b> is the value of the '
            '<span class="ssm-path">oid=</span> query parameter in your URL, e.g. '
            '<span class="ssm-path">cloud.couchbase.com/databases?oid=&lt;UUID&gt;</span>.</li>'
            '<li>Open a project. <b>Project ID</b> is the value of the '
            '<span class="ssm-path">pid=</span> query parameter in the URL '
            'once you are inside a project.</li>'
            '<li>Inside the project: <b>Settings → API Keys → Generate Key</b>. '
            'Role must be <b>Project Owner</b>. '
            'Copy the <b>API Secret</b> right away; Capella shows it only once.</li>'
            '</ol>'
        )
        # All three must be in SSM before the ingest/retrieval snippets unlock,
        # since those snippets read these parameters at notebook runtime.
        cb_creds_saved = _credential_form(
            "couchbase", "Capella credentials",
            [{"key": "org_id", "label": "Organization ID", "param": cb_org_name, "secure": False,
              "placeholder": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
             {"key": "project_id", "label": "Project ID", "param": cb_proj_name, "secure": False,
              "placeholder": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
             {"key": "api_token", "label": "API Secret", "param": cb_tok_name,
              "placeholder": "Paste the secret (shown once in Capella)"}],
            help_html=cb_help,
        )
        snippet_state = "open" if cb_creds_saved else "locked"
        if not cb_creds_saved:
            _lock_hint()

    # Per-stage provider snippets → paste into the notebook's paste cells.
    # Each snippet expander is wrapped in a container whose key ends in
    # "-locked" or "-open"; the existing CSS targets
    # [class*="st-key-snip-wrap-"][class*="-locked"] and hides the body
    # + makes the summary non-interactive while locked.
    st.markdown('<div class="section-label section-label--plain">Copy this into your notebook</div>',
                unsafe_allow_html=True)
    for stage in UNSTRUCTURED_STAGES:
        with st.container(key=f"snip-wrap-data_kb-{stage['id']}-{snippet_state}"):
            with st.expander(f"📋 {stage['label']}", expanded=False):
                st.caption(stage["help"])
                st.code(unstructured_snippet(kb_choice, stage["id"]), language="python")

    # Note: no "Save selection" button here. The paste snippet writes the real
    # provider record (/anycompany/agentcore/kb_provider) when the participant
    # runs the ingest cell, which is the actual commitment.

elif layer_id == "data":
    # =====================================================================
    # Data layer - Structured data (customer orders): DynamoDB vs Couchbase.
    # ONE notebook owns the constant skeleton (region, load ORDERS from
    # sample-data). The provider-specific load + test code is generated here
    # as copy-paste snippets. Unstructured/knowledge data comes next.
    # =====================================================================
    st.markdown('<div class="section-label">Structured data · customer orders</div>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="ssm-note">Pick the store for the structured customer orders. '
        'Both expose the same capability - load the sample orders and query them '
        'back. The notebook owns the shared skeleton (region + loading '
        '<span class="ssm-path">ORDERS</span> from sample-data); the provider code '
        'below fills the paste cells.</div>',
        unsafe_allow_html=True,
    )

    prov_names = list(STRUCTURED_PROVIDERS.keys())
    cur = st.session_state.selections.get("data_structured")
    if cur not in prov_names:
        cur = prov_names[0]

    taglines = {
        "DynamoDB (AWS)": "Managed key-value orders store",
        "Couchbase": "Operational JSON + SQL++ queries",
    }
    descriptions = {
        "DynamoDB (AWS)": "Amazon DynamoDB CustomerOrders table with a composite key "
                          "and GSIs for order-id and status lookups.",
        "Couchbase": "Couchbase Capella collection (retail.support.orders) with "
                     "SQL++ query indexes, in place of DynamoDB.",
    }

    cols = st.columns(len(prov_names))
    for col, opt in zip(cols, prov_names):
        is_sel = opt == cur
        logo = get_logo_data_uri(STRUCTURED_PROVIDERS[opt]["logo"])
        kind = "key" if STRUCTURED_PROVIDERS[opt].get("needs_key") else "aws"
        tag = "PARTNER SOLUTION" if kind == "key" else "AWS SERVICE"
        label = (f"![logo]({logo})\n\n**{opt.replace(' (AWS)', '')}**\n\n"
                 f"{taglines.get(opt, '')}\n\n{tag}")
        with col:
            if st.button(label, key=f"pcard-{kind}-structured-{opt}",
                         type="primary" if is_sel else "secondary",
                         use_container_width=True):
                st.session_state.selections["data_structured"] = opt
                st.rerun()

    choice = st.session_state.selections.get("data_structured", cur)
    if choice not in prov_names:
        choice = cur
    if descriptions.get(choice):
        st.markdown(f'<div class="provider-desc">{descriptions[choice]}</div>',
                    unsafe_allow_html=True)

    # API key for providers that need one (Couchbase).
    if STRUCTURED_PROVIDERS.get(choice, {}).get("needs_key"):
        key_name = f"{_CFG}/data/api_key"
        existing = get_param(key_name)
        st.markdown('<div class="section-label">API Key</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="ssm-note">Stored as a SecureString at '
            f'<span class="ssm-path">{key_name}</span></div>', unsafe_allow_html=True)
        kv = st.text_input(f"{choice} API key", type="password", key="data_api_key",
                           placeholder="••••••••  (stored)" if existing else "Enter API key",
                           label_visibility="collapsed")
        # Save button + "Credentials saved" status badge, side by side.
        save_col, status_col = st.columns([1.5, 3])
        with save_col:
            if kv and st.button(f"Save {choice} key to SSM", key="data_savekey",
                                type="primary", use_container_width=True):
                try:
                    put_param(key_name, kv, secure=True)
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to save: {e}")
        with status_col:
            if existing:
                st.markdown(
                    '<div style="padding-top:12px;">'
                    '<span class="badge-ok">✓ Credentials saved</span></div>',
                    unsafe_allow_html=True,
                )

    # Per-stage provider snippets → paste into the notebook's paste cells.
    st.markdown('<div class="section-label section-label--plain">Copy this into your notebook</div>',
                unsafe_allow_html=True)
    for stage in STRUCTURED_STAGES:
        with st.expander(f"📋 {stage['label']}", expanded=False):
            st.caption(stage["help"])
            st.code(structured_snippet(choice, stage["id"]), language="python")

    # Persist the structured-data provider selection.
    st.write("")
    if st.button(f"Set “{choice}” as the structured-data provider",
                 key="savesel_data_structured", type="primary",
                 use_container_width=True):
        try:
            put_param(f"{_CFG}/data/structured_provider", choice, secure=False)
            # Toast survives the st.rerun() below.
            st.toast(f"✅ {choice} saved as structured-data provider", icon="✅")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to save: {e}")

elif layer_id == "observability":
    # =====================================================================
    # Observability layer - CloudWatch (AWS) vs Datadog vs CloudZero.
    # Logs ALWAYS route to CloudWatch; only spans/traces branch by provider.
    # The provider is chosen ONCE here and applies to all agents: the L3 agent
    # notebooks (2-5) read config/observability/provider when they deploy, and
    # Step 6 of l3-orchestration/1_gateway_tools.ipynb confirms the choice.
    #   - CloudWatch needs no credentials; clicking its card saves the provider id.
    #   - Datadog is OTEL-direct (no sidecar/ddtrace) and captures THREE creds:
    #     api_key + app_key (SecureString) and site (String). Saving them also
    #     saves the provider id.
    #   - CloudZero keeps CloudWatch for all traces and logs; the specialists also
    #     send LiteLLM model-call spans to CloudZero. It captures api_key
    #     (SecureString) and username (String) under config/cloudzero/. Saving
    #     them also saves the provider id.
    # The provider id stored in SSM is the lowercase provider["value"]
    # ("cloudwatch"/"datadog"/"cloudzero"), not the UI label.
    # =====================================================================
    _intro_with_diagram(
        "observability", "How agent observability works",
        "The agents emit OpenTelemetry spans for every model call and tool call. The "
        "traces go to the backend you pick, and logs always go to CloudWatch. With "
        "CloudZero, traces stay on CloudWatch and the Order, Refund and Research agents "
        "also send each model call's span (tokens and cost, no prompt text) to CloudZero; "
        "the orchestrator is not sent.",
        _observability_diagram(), "Agent observability: pluggable trace backend",
    )

    # The saved choice (what the L3 notebooks will deploy with). Open the page on
    # it, so a participant who saved Datadog earlier sees Datadog selected.
    _provider_param = f"{CONFIG_PREFIX}/{layer_id}/provider"
    _saved_obs = get_param(_provider_param)                      # "cloudwatch" | "datadog" | "cloudzero" | ""
    _by_value = {m.get("value", n): n for n, m in layer["providers"].items()}
    if layer_id not in st.session_state.selections and _saved_obs in _by_value:
        st.session_state.selections[layer_id] = _by_value[_saved_obs]
    current = _chosen(layer)

    cols = st.columns(len(options))
    for i, (col, opt) in enumerate(zip(cols, options)):
        meta = layer["providers"][opt]
        is_sel = opt == current
        kind = meta.get("kind") or ("key" if _creds(meta) else "aws")
        tagline = meta.get("tagline") or meta.get("description", "").split(".")[0]
        name = opt.replace(" (AWS)", "")
        label = (
            f"![logo]({get_logo_data_uri(opt)})\n\n"
            f"**{name}**\n\n{tagline}\n\n{_provider_tag(kind)}"
        )
        with col:
            if st.button(label, key=f"pcard-{kind}-observability-{i}",
                         type="primary" if is_sel else "secondary",
                         use_container_width=True):
                st.session_state.selections[layer_id] = opt
                # A provider with nothing to fill in (CloudWatch) is saved on click.
                # Datadog is saved together with its credentials, below.
                if not _creds(meta):
                    try:
                        put_param(_provider_param, meta.get("value", opt), secure=False)
                        st.toast(f"✅ {name} saved as your trace backend", icon="✅")
                    except Exception as e:
                        st.session_state["obs_save_error"] = str(e)
                st.rerun()
    if st.session_state.get("obs_save_error"):
        st.error(f"Failed to save: {st.session_state.pop('obs_save_error')}")

    choice = _chosen(layer)
    provider = layer["providers"][choice]
    provider_value = provider.get("value", choice)
    choice_name = choice.replace(" (AWS)", "")
    creds = _creds(provider)

    if provider.get("description"):
        st.markdown(f'<div class="provider-desc">{provider["description"]}</div>',
                    unsafe_allow_html=True)

    # Status: what the agents will use, and when the choice matters.
    if _saved_obs == provider_value:
        _status = f'<span class="badge-ok">✓ {html.escape(choice_name)} is your trace backend</span>'
    elif not creds and not _saved_obs:
        _status = (f'<span class="badge-ok">✓ {html.escape(choice_name)} is your trace '
                   f'backend (the default)</span>')
    else:
        _status = (f"Save your {html.escape(choice_name)} credentials below to switch to "
                   f"{html.escape(choice_name)}.")
    st.markdown(
        f'<div class="ssm-note">{_status}<br>One choice for all agents. Make it before Step 6 '
        f'of the Gateway Tools notebook, which confirms it, and before you deploy the agents. '
        f'Switching later means redeploying them.</div>',
        unsafe_allow_html=True,
    )

    # Partner credentials (Datadog or CloudZero). Saving them also makes that
    # provider the trace backend.
    if creds:
        portal = provider.get("portal")
        dd_help = (f'Get your credentials, then paste them below: '
                   f'<a href="{portal["url"]}" target="_blank" rel="noopener noreferrer">'
                   f'{portal["label"]}</a>.' if portal else "")

        def _site_ok(values):
            # Defence in depth behind the dropdown: the site is interpolated into the
            # OTLP endpoint the ingest key is sent to, and SSM can be written directly.
            site = (values.get("site") or "").strip()
            if site and site not in DATADOG_SITES:
                return "Unsupported Datadog site. Choose one of: " + ", ".join(DATADOG_SITES)
            return None

        def _make_active():
            put_param(_provider_param, provider_value, secure=False)

        keys_saved = _credential_form(
            f"observability_{provider_value}", f"{choice_name} credentials",
            [{"key": c["key"], "label": c["label"], "param": cred_param(layer_id, c),
              "secure": c.get("secure", True), "placeholder": c.get("placeholder", ""),
              "options": c.get("options")} for c in creds],
            help_html=dd_help,
            note=f"Saving also makes {choice_name} your trace backend.",
            validate=_site_ok, after_save=_make_active,
        )

        # Once all three Datadog credentials are saved, point the user at their
        # Datadog site to view Agent Observability. The web-app host is derived from
        # the saved "site" value, which is re-checked against the allowlist because
        # SSM can be written outside this UI.
        _dd_site = (get_param(cred_param(layer_id, {"key": "site"})) or "").strip()
        if provider_value == "datadog" and keys_saved and _dd_site in DATADOG_SITES:
            _dd_app = (f"https://app.{_dd_site}"
                       if _dd_site in ("datadoghq.com", "datadoghq.eu", "ddog-gov.com")
                       else f"https://{_dd_site}")
            st.markdown(
                f'<div class="ssm-note">✅ Datadog credentials saved. Open your Datadog '
                f'site to view Agent Observability and dashboards: '
                f'<a href="{_dd_app}/dashboard/lists" target="_blank" rel="noopener noreferrer">'
                f'{_dd_app}/dashboard/lists</a>.</div>',
                unsafe_allow_html=True,
            )
    # Paste snippet for Step 6 of 1_gateway_tools.ipynb. Datadog's and CloudZero's
    # stay locked until their credentials are saved, like the other partner pages.
    _obs_state = "open" if (not creds or keys_saved) else "locked"
    if _obs_state == "locked":
        _lock_hint()
    st.markdown('<div class="section-label">Code for your notebook cells</div>',
                unsafe_allow_html=True)
    for stage in OBSERVABILITY_STAGES:
        with st.container(key=f"snip-wrap-observability-{stage['id']}-{_obs_state}"):
            with st.expander(f"📋 {stage['label']}", expanded=False):
                st.caption(stage["help"])
                st.code(observability_snippet(choice, stage["id"]), language="python")

elif layer_id == "decision":
    # =====================================================================
    # Decision layer (L5): ONE notebook (l5-application/1_decision_models.ipynb).
    # Everything provider-neutral lives in the notebook; the provider-specific
    # "Connect" block is generated here as a paste snippet. Perplexity needs an
    # API key, so its snippet stays locked until the key is saved to SSM.
    # No "Save selection" button: the Connect cell publishes the provider to
    # /anycompany/agentcore/decision_provider when the participant runs it.
    # =====================================================================
    def _decision_status(choice, meta):
        if meta.get("key_param"):
            return None
        # Show where the pre-provisioned endpoint is, so participants know whether
        # the Connect cell will run straight away or wait.
        status = get_param(f"{SSM_PREFIX}/decider_status")
        if not status:
            txt = ("Not found - the workshop stack may have been deployed with "
                   "DeployDecider=false, or in another region.")
        elif status == "READY":
            txt = (f"Ready on {get_param(f'{SSM_PREFIX}/decider_instance_type', '?')} "
                   f"({get_param(f'{SSM_PREFIX}/decider_device', '?')}).")
        else:
            txt = status
        return f"<b>Endpoint status:</b> {txt}"

    _staged_provider_page(
        "decision", DECISION_PROVIDERS, DECISION_STAGES, decision_snippet,
        heading="How the decision step works",
        intro="A decision model answers typed questions with a probability instead of free "
              "text. Before each message reaches the orchestrator, the chatbot uses it to "
              "check scope, urgency, sentiment and whether to escalate to a person.",
        status_fn=_decision_status,
        diagram=(_decision_diagram(), "Decision models: pluggable triage step"),
    )

elif layer_id == "websearch":
    # =====================================================================
    # Orchestration > Tools: connected in l3-orchestration/1_gateway_tools.ipynb (Step 4)
    # and ONE Research Agent for all three web-search providers - each is a
    # target on the same AgentCore Gateway. Only the Connect cell differs.
    # The Connect cell publishes the choice to /anycompany/agentcore/websearch_provider.
    # =====================================================================
    def _websearch_status(choice, meta):
        if choice.startswith("Brave"):
            status = get_param(f"{SSM_PREFIX}/brave_mcp_build_status")
            if not status:
                txt = ("Not found - the workshop stack may predate the Brave image build. "
                       "Redeploy it, or pick another provider.")
            elif status == "READY":
                txt = "Ready - the Connect cell starts the server in about a minute."
            else:
                txt = f"{status} (the Connect cell waits for it)"
            return f"<b>Brave MCP image:</b> {txt}"
        if choice.startswith("AgentCore"):
            return (f"<b>Region:</b> {REGION}. No API key, no infrastructure - the "
                    "Gateway's IAM role calls the connector.")
        return None

    _staged_provider_page(
        "websearch", WEBSEARCH_PROVIDERS, WEBSEARCH_STAGES, websearch_snippet,
        heading="How the web-search tool works",
        intro="Web search lets the Research Agent answer questions about current products "
              "and cite its sources. Every provider is an MCP tool behind the AgentCore "
              "Gateway, so the agent code is the same whichever you pick.",
        status_fn=_websearch_status,
        diagram=(_websearch_diagram(), "Tools: pluggable web search"),
    )

else:
    st.markdown('<div class="section-label">Choose a provider</div>', unsafe_allow_html=True)

    # Provider cards (Option A): horizontal cards - logo on the left, then name,
    # tagline and an "AWS SERVICE" / "PARTNER SOLUTION" tag. Each card is a native st.button
    # (fast in-place rerun, no page reload). Streamlit adds a "st-key-<key>" class
    # to every keyed widget, so the card CSS hooks onto the "pcard-" key prefix;
    # "pcard-aws-" vs "pcard-key-" lets the tag pill be colored per type.
    PER_ROW = 2
    for start in range(0, len(options), PER_ROW):
        row = options[start:start + PER_ROW]
        cols = st.columns(PER_ROW)
        for col, opt in zip(cols, row):
            i = options.index(opt)
            meta = layer["providers"][opt]
            is_sel = opt == current
            # Honor an explicit "kind" field if the provider sets one (used by
            # partners that don't carry an API-key field). Fall back to the
            # credential check.
            kind = meta.get("kind") or ("key" if meta.get("credential") else "aws")
            tag = "PARTNER SOLUTION" if kind == "key" else "AWS SERVICE"
            tagline = meta.get("tagline") or meta.get("description", "").split(".")[0]
            name = opt.replace(" (AWS)", "")
            label = (
                f"![logo]({get_logo_data_uri(opt)})\n\n"
                f"**{name}**\n\n"
                f"{tagline}\n\n"
                f"{tag}"
            )
            with col:
                if st.button(
                    label,
                    key=f"pcard-{kind}-{layer_id}-{i}",
                    type="primary" if is_sel else "secondary",
                    use_container_width=True,
                ):
                    st.session_state.selections[layer_id] = opt
                    st.rerun()

    choice = _chosen(layer)
    provider = layer["providers"][choice]

    # --- API key, if needed ---
    cred = provider.get("credential")
    if cred:
        _credential_form(
            f"{layer_id}_{cred['key']}", f"{choice.replace(' (AWS)', '')} credentials",
            [{"key": cred["key"], "label": cred["label"], "param": cred_param(layer_id, cred),
              "placeholder": cred.get("placeholder", "")}],
        )

    # --- Code snippet ---
    # Only when Datadog is the observability provider does L3 auto-wire tracing
    # from the L0 SSM choice, so there is nothing to hand-copy — keep that
    # heading honest. CloudWatch and every other provider/layer are unchanged.
    _snip_heading = ("Reference only — no copy needed"
                     if (layer_id == "observability" and choice == "Datadog")
                     else "Copy this into your notebook")
    st.markdown(f'<div class="section-label section-label--plain">{_snip_heading}</div>',
                unsafe_allow_html=True)
    snippet = get_snippet(layer_id, choice)
    if snippet:
        with st.expander("📋 Code snippet", expanded=False):
            st.code(snippet, language="python")
    else:
        st.caption("No snippet for this option yet.")

    # --- Persist selection ---
    st.write("")
    if st.button(f"Set “{choice}” as the {layer['label']} provider",
                 key=f"savesel_{layer_id}", type="primary", use_container_width=True):
        try:
            put_param(f"{CONFIG_PREFIX}/{layer_id}/provider", choice, secure=False)
            # Toast survives the st.rerun() below.
            st.toast(f"✅ {choice} saved as {layer['label']} provider", icon="✅")
            st.rerun()
        except Exception as e:
            st.error(f"Failed to save: {e}")
