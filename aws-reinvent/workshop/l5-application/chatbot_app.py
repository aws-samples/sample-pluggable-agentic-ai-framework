"""AnyCompany Customer Support Chatbot — Streamlit UI

Run with: streamlit run chatbot_app.py
"""

import boto3
import html
import json
import os
import time
import uuid
import streamlit as st
import jwt
from jwt import PyJWKClient

import decision_models as dm  # local module (ships beside this app); the L5 decision-models lab

# --- Configuration ---
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SSM_PREFIX = "/anycompany/agentcore"

# --- Decision-model support triage (optional) ---
# A decision model runs business checks on each customer message, in ONE call:
#   in scope? (yes/no) · escalate to a human? (yes/no) · urgency (rubric score)
#   · sentiment (choice)
# The results show as badges under the customer's message.
# The orchestrator is not changed by any of this; the only action taken is the
# scope gate: a confidently off-topic message gets a canned reply instead of a
# Harness turn. All thresholds live here, in our code, not in the model.
# The provider is whichever one the L5 decision-models notebook connected to: its
# Connect cell publishes 'perplexity' or 'strands' to /anycompany/agentcore/decision_provider.
# For Perplexity, the API key is the one the L0 Partner Selection UI saved to SSM.
# If nothing is configured, or the provider is unavailable, triage is skipped and
# the app behaves exactly as before; the decision step never blocks the chat.
OUT_OF_SCOPE_GATE = 0.80  # only hard-block when the decider is this sure it's off-topic
ESCALATE_GATE = 0.70      # flag for a human agent at or above this p(escalate)
NEGATIVE_GATE = 0.70      # never scope-block a message this confidently negative (upset customer)
OUT_OF_SCOPE_REPLY = (
    "I'm the AnyCompany customer support assistant, so I can only help with your "
    "orders, returns and refunds, account, or questions about products, such as "
    "reviews, specs and comparisons. Is there something along those lines I can help you with?"
)
PROVIDER_LABELS = {"perplexity": "Perplexity Decisions", "strands": "Strands Decider"}

# Labels for the technology chosen at each layer (values the notebooks write to SSM).
KB_LABELS = {"bedrock": "Amazon Bedrock Knowledge Bases", "couchbase": "Couchbase Capella"}
WEBSEARCH_LABELS = {"agentcore": "AgentCore Web Search", "brave": "Brave Search",
                    "perplexity": "Perplexity Search"}
OBSERVABILITY_LABELS = {"cloudwatch": "Amazon CloudWatch", "datadog": "Datadog"}
MODEL_PROVIDER_LABELS = {"bedrock": "Amazon Bedrock", "baseten": "Baseten"}

@st.cache_resource
def get_clients():
    from botocore.config import Config
    ssm = boto3.client("ssm", region_name=REGION)
    agentcore_runtime = boto3.client("bedrock-agentcore", region_name=REGION,
                                     config=Config(read_timeout=300))
    cognito = boto3.client("cognito-idp", region_name=REGION)
    return ssm, agentcore_runtime, cognito


@st.cache_resource(ttl=300)
def get_decider():
    """Connect to the decision provider chosen in the L5 decision-models notebook.

    Returns (decider or None, status text for the sidebar). Re-checked every 5
    minutes, so connecting a provider in the notebook turns triage on without
    restarting the app. Any problem disables triage instead of breaking the chat.
    """
    ssm, _, _ = get_clients()
    try:
        provider = ssm.get_parameter(Name=dm.PROVIDER_PARAM)["Parameter"]["Value"]
    except Exception:
        return None, "off — no provider connected (run the L5 decision-models notebook)"
    label = PROVIDER_LABELS.get(provider)
    if label is None:
        return None, f"off — unknown provider {provider!r}"
    try:
        if provider == "perplexity":
            decider = dm.perplexity_from_ssm(REGION)
        else:
            decider = dm.strands_from_ssm(REGION, wait_minutes=0)
    except Exception as e:
        print(f"[triage] {provider} unavailable — triage disabled: {e}")
        return None, f"off — {label} unavailable"
    print(f"[triage] active: {provider}")  # never log the API key
    return decider, label


@st.cache_data(ttl=300)
def get_layer_choices():
    """Return [(layer name, chosen technology)] for the sidebar, read from SSM.

    Re-read every 5 minutes, like the triage provider. A layer that has not
    been set up yet shows "not set up" instead of failing the page.
    """
    ssm, _, _ = get_clients()

    def param(name, default=None):
        try:
            return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}")["Parameter"]["Value"].strip()
        except Exception:
            return default

    def model(name):
        model_id = param(name)
        if not model_id:
            return "not set up"
        provider, _, rest = model_id.partition("/")
        if not rest:   # an older L2 run published a bare Bedrock id
            provider, rest = "bedrock", model_id
        return f"{MODEL_PROVIDER_LABELS.get(provider, provider)} · {rest}"

    kb = param("kb_provider")
    web = param("websearch_provider")
    obs = (param("config/observability/provider") or "cloudwatch").lower()
    return [
        ("L1 Data & Knowledge", KB_LABELS.get(kb, kb) if kb else "not set up"),
        ("L2 Sub-agent model", model("model_id")),
        ("L2 Orchestrator model", model("harness_model_id")),
        ("L3 Web search", WEBSEARCH_LABELS.get(web, web) if web else "not set up"),
        ("L4 Security", "Amazon Bedrock Guardrails" if param("guardrail_id") else "not set up"),
        ("L6 Observability", OBSERVABILITY_LABELS.get(obs, obs)),
    ]


@st.cache_data
def get_config():
    ssm, _, _ = get_clients()

    def _get(name, default=None):
        try:
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return default

    harness_arn = _get(f"{SSM_PREFIX}/harness_arn", "")
    if not harness_arn:
        control = boto3.client("bedrock-agentcore-control", region_name=REGION)
        for h in control.list_harnesses().get("harnesses", []):
            if h.get("harnessName") == "anycompany_orchestrator_v3":
                harness_arn = h["arn"]
                break

    return {
        "harness_arn": harness_arn,
        "cognito_pool_id": _get(f"{SSM_PREFIX}/cognito_user_pool_id", ""),
        "cognito_client_id": _get(f"{SSM_PREFIX}/cognito_client_id", ""),
    }


@st.cache_resource
def _jwks_client():
    """Cached PyJWKClient that fetches Cognito's JWKS (signing keys).

    PyJWKClient caches signing keys internally, so verifying many tokens
    only triggers one HTTPS call to the JWKS endpoint per process.
    """
    config = get_config()
    pool_id = config["cognito_pool_id"]
    if not pool_id:
        raise RuntimeError(
            "Missing cognito_user_pool_id in SSM. Run L3 pre-requisites first."
        )
    jwks_url = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    return PyJWKClient(jwks_url)


def authenticate(username: str, password: str) -> dict:
    _, _, cognito = get_clients()
    config = get_config()
    try:
        # WORKSHOP ONLY: USER_PASSWORD_AUTH with shared passwords is for demonstration.
        # Production: Use federated identity (SAML/OIDC). See https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html
        resp = cognito.initiate_auth(
            ClientId=config["cognito_client_id"],
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
        result = resp["AuthenticationResult"]
        # Verify the JWT signature against Cognito's JWKS, then read claims.
        # The token comes from a trusted Cognito API call here, but we verify
        # anyway so claim extraction is signature-checked end-to-end.
        signing_key = _jwks_client().get_signing_key_from_jwt(result["IdToken"]).key
        claims = jwt.decode(
            result["IdToken"],
            signing_key,
            algorithms=["RS256"],
            audience=config["cognito_client_id"],
            issuer=f"https://cognito-idp.{REGION}.amazonaws.com/{config['cognito_pool_id']}",
        )
        return {
            "success": True,
            "id_token": result["IdToken"],
            "customer_id": claims.get("custom:customer_id", "unknown"),
            "tier": claims.get("custom:tier", "unknown"),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def is_out_of_scope(triage: dict | None) -> bool:
    """True when triage is confident the message is off-topic.

    Never blocked, even if it scores as off-topic: a message flagged for a human
    agent ("let me talk to your manager"), or a confidently negative one ("I am
    disappointed") - both name no order or product, but an upset customer must
    always reach the orchestrator (which asks what went wrong), never a canned reply.
    """
    if triage is None:
        return False
    if triage["escalate"] >= ESCALATE_GATE:
        return False
    if triage["sentiment"] == "negative" and triage["sentiment_conf"] >= NEGATIVE_GATE:
        return False
    return (1.0 - triage["in_scope"]) >= OUT_OF_SCOPE_GATE


def run_triage(message: str) -> dict | None:
    """Support-triage checks on one customer message, or None if no decision
    provider is connected or the call fails (fail-open: never blocks the chat)."""
    decider, _ = get_decider()
    if decider is None:
        return None
    try:
        return dm.triage_message(decider, message)
    except Exception as e:
        print(f"[triage] skipped ({type(e).__name__}): {e}")
        return None


def _badge(text: str, fg: str, bg: str) -> str:
    return (f'<span style="display:inline-block;margin:2px 6px 0 0;padding:1px 9px;'
            f'border-radius:999px;font-size:.78rem;font-weight:600;'
            f'color:{fg};background:{bg};">{text}</span>')


_SENTIMENT_BADGE = {
    "positive": ("🙂 Positive", "#14532d", "#dcfce7"),
    "neutral":  ("😐 Neutral",  "#334155", "#e2e8f0"),
    "negative": ("🙁 Negative", "#7c2d12", "#ffedd5"),
}
_URGENCY_BADGE = {
    "Routine":  ("#334155", "#e2e8f0"),
    "Elevated": ("#713f12", "#fef3c7"),
    "Urgent":   ("#7f1d1d", "#fee2e2"),
}


def render_triage(triage: dict | None) -> None:
    """Badges under the customer's message. Text labels carry the meaning, so
    they read correctly without color."""
    if not triage:
        return
    if is_out_of_scope(triage):
        badges = [_badge(f"Out of scope ({1 - triage['in_scope']:.2f})", "#334155", "#e2e8f0")]
    else:
        label, fg, bg = _SENTIMENT_BADGE.get(triage["sentiment"], _SENTIMENT_BADGE["neutral"])
        badges = [_badge(f"Sentiment: {label} ({triage['sentiment_conf']:.2f})", fg, bg)]
        ufg, ubg = _URGENCY_BADGE.get(triage["urgency_label"], _URGENCY_BADGE["Routine"])
        badges.append(_badge(f"Urgency: {triage['urgency_label']} ({triage['urgency']:.2f})", ufg, ubg))
        if triage["escalate"] >= ESCALATE_GATE:
            badges.append(_badge(f"🚩 Flag for human agent ({triage['escalate']:.2f})",
                                 "#ffffff", "#b91c1c"))
    st.markdown("".join(badges), unsafe_allow_html=True)


def stream_orchestrator_to_ui(message: str, session_id: str, actor_id: str, tier: str,
                              triage: dict | None = None) -> str:
    """Stream the harness response into the live Streamlit chat bubble.

    Shows a spinner for the entire duration of the request. Named agent
    tool calls render as status widgets; shell/hidden tools are invisible.
    Text deltas stream token-by-token into a placeholder.

    actor_id scopes long-term memory (preferences, facts) per user. Pass
    the authenticated Cognito customer_id so each user's extracted memory
    stays isolated under namespaces like `preferences/{actorId}`.

    Returns the full assistant text (without the status decorations) so the
    caller can save it to chat history.
    """
    _, agentcore_runtime, _ = get_clients()
    config = get_config()

    # --- Scope gate from the triage result ----------------------------------
    # A confidently off-topic message gets a canned reply instead of a Harness
    # turn. Everything else goes to the orchestrator unchanged.
    if is_out_of_scope(triage):
        st.markdown(OUT_OF_SCOPE_REPLY)
        st.caption("Handled without the orchestrator (out of scope)")
        return OUT_OF_SCOPE_REPLY

    # Prepend authenticated identity so the orchestrator always knows who it's
    # talking to. The Session id is included so the orchestrator can echo it as
    # the session_id argument on agent-invoker calls, grouping the specialists'
    # telemetry into one session (Datadog observability; harmless otherwise).
    augmented_message = f"[Authenticated customer: {actor_id} | Tier: {tier} | Session: {session_id}]\n\n{message}"

    start_time = time.time()

    full_text = ""
    text_placeholder = None
    active_status = None
    active_tool_name = None
    # Tool names that are internal scaffolding the customer doesn't need to see.
    HIDDEN_TOOLS = {
        "shell",
        "code_interpreter",
        "agentcore_code_interpreter",
        "browser",
        "agentcore_browser",
    }
    # Every tool on the Harness gateway (find and call agents) is scaffolding too.
    # "registry-discovery" and "agent-invoker" are its targets in the L3 stack;
    # "registry-tools" is the Harness's name for that gateway.
    HIDDEN_TOOL_PREFIXES = ("registry-discovery___", "agent-invoker___", "registry-tools___")

    # The spinner starts before invoke_harness: the call itself takes a few
    # seconds while the Harness accepts the request and opens the stream.
    with st.spinner("Thinking..."):
        response = agentcore_runtime.invoke_harness(
            harnessArn=config["harness_arn"],
            runtimeSessionId=session_id,
            actorId=actor_id,
            messages=[{"role": "user", "content": [{"text": augmented_message}]}],
        )
        for event in response["stream"]:
            # ---- tool use: orchestrator is calling something ----
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                tool_use = start.get("toolUse")
                if tool_use:
                    active_tool_name = tool_use.get("name", "tool")
                    hidden = active_tool_name in HIDDEN_TOOLS or active_tool_name.startswith(HIDDEN_TOOL_PREFIXES)
                    if not hidden:
                        active_status = st.status(
                            f"Calling {active_tool_name}...",
                            expanded=False,
                            state="running",
                        )
                    else:
                        active_status = None  # hidden tool — no widget

            elif "contentBlockStop" in event:
                if active_status is not None:
                    active_status.update(
                        label=f"✓ {active_tool_name}",
                        state="complete",
                        expanded=False,
                    )
                active_status = None
                active_tool_name = None

            # ---- text delta: stream into the chat bubble ----
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text")
                if text:
                    if text_placeholder is None:
                        text_placeholder = st.empty()
                    full_text += text
                    text_placeholder.markdown(full_text + "▌")  # caret while streaming

            elif "runtimeClientError" in event:
                err = f"\n\n⚠️ Error: {event['runtimeClientError']['message']}"
                full_text += err
                if text_placeholder is None:
                    text_placeholder = st.empty()
                text_placeholder.markdown(full_text)
                if active_status is not None:
                    active_status.update(state="error", expanded=True)
                return full_text

    # Final render without the streaming caret.
    if text_placeholder is not None:
        text_placeholder.markdown(full_text)

    latency = time.time() - start_time
    st.markdown(f'<div class="ac-meta">Answered in {latency:.1f}s</div>', unsafe_allow_html=True)
    return full_text


# --- Streamlit UI ---
st.set_page_config(page_title="AnyCompany Support", page_icon="🛍️", layout="centered")

# Presentation only: navy accent, card-style chat bubbles, tidy sidebar.
st.markdown("""
<style>
  :root { --navy:#1e3a8a; --navy-soft:#eef2ff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; }
  [data-testid="stHeader"] { background: transparent; }
  .block-container { padding-top: 1.6rem; max-width: 820px; }

  /* Page header */
  .ac-header { display:flex; align-items:center; gap:14px; padding:4px 0 6px; }
  /* Header bar stays pinned at the top while the chat scrolls or streams.
     Streamlit wraps each container in a same-size stLayoutWrapper, so the
     wrapper is the element that has to stick. */
  [data-testid="stLayoutWrapper"]:has(> .st-key-ac-topbar),
  .st-key-ac-topbar { position:sticky; top:0; z-index:100; background:#fff; }
  .st-key-ac-topbar { border-bottom:1px solid var(--line); padding:6px 0 18px; margin-bottom:12px; }
  /* Sign out (header, top right): quiet outline button */
  .st-key-signout button { border:1px solid var(--line); background:#fff; color:var(--ink);
                           font-size:.85rem; white-space:nowrap; }
  .st-key-signout button:hover { border-color:var(--navy); color:var(--navy); background:var(--navy-soft); }
  .ac-logo { width:46px; height:46px; border-radius:12px; background:var(--navy-soft);
             border:1px solid #c7d2fe; display:flex; align-items:center; justify-content:center;
             font-size:1.6rem; line-height:1; }
  .ac-title { font-size:1.35rem; font-weight:700; color:var(--ink); line-height:1.2; }
  .ac-sub { font-size:.86rem; color:var(--muted); }

  /* Login card */
  .ac-card-title { font-size:1.15rem; font-weight:700; color:var(--ink); margin-bottom:2px; }
  .ac-card-sub { font-size:.88rem; color:var(--muted); margin-bottom:6px; }
  [data-testid="stForm"] { border:1px solid var(--line); border-radius:12px;
                           padding:22px 22px 10px; background:#fff;
                           box-shadow:0 1px 3px rgba(15,23,42,.06); }
  .ac-hint { font-size:.82rem; color:var(--muted); margin-top:10px; }
  .ac-hint code { background:var(--navy-soft); color:var(--navy); padding:1px 6px;
                  border-radius:6px; font-size:.8rem; }

  /* Chat messages keep Streamlit's default look (no boxes). */
  [data-testid="stChatInput"] textarea { font-size:.95rem; }
  .ac-meta { font-size:.75rem; color:var(--muted); margin-top:4px; }

  /* Sidebar */
  section[data-testid="stSidebar"] { background:#f8fafc; border-right:1px solid var(--line); }
  section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding-top:.5rem; }
  .ac-brand { display:flex; align-items:center; gap:10px; padding:2px 2px 14px;
              margin-bottom:12px; border-bottom:1px solid var(--line); }
  .ac-brand img { width:40px; height:auto; flex-shrink:0; }
  .ac-brand .name { font-size:1.02rem; font-weight:800; letter-spacing:-.01em;
                    color:var(--ink); line-height:1.25; }
  .ac-brand .name span { background:linear-gradient(90deg,#6d5cff,#0891b2);
                         -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .ac-account { background:#fff; border:1px solid var(--line); border-radius:12px;
                padding:14px 16px; margin-bottom:6px; }
  .ac-account .who { font-weight:700; color:var(--ink); font-size:1rem; }
  .ac-account .row { font-size:.82rem; color:var(--muted); margin-top:4px; }
  .ac-account .row b { color:var(--ink); font-weight:600; }
  .ac-tier { display:inline-block; padding:1px 9px; border-radius:999px; font-size:.72rem;
             font-weight:700; letter-spacing:.04em; margin-left:6px; vertical-align:2px; }
  .ac-section { font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
                color:var(--muted); margin:6px 0 2px; }
  section[data-testid="stSidebar"] .stButton > button {
      justify-content:flex-start; text-align:left; border:1px solid var(--line);
      background:#fff; color:var(--ink); font-size:.86rem; padding:6px 12px; }
  section[data-testid="stSidebar"] .stButton > button:hover {
      border-color:var(--navy); color:var(--navy); background:var(--navy-soft); }
  section[data-testid="stSidebar"] [data-testid="stExpander"] details {
      background:#fff; border:1px solid var(--line); border-radius:12px; }
</style>
""", unsafe_allow_html=True)

# Framework logo, shared with the L0 Partner Selection UI. Missing file -> text only.
def _framework_logo_uri() -> str:
    import base64
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                        "l0-partner-selection", "assets", "icons", "paf-logo.svg")
    try:
        with open(path, "rb") as f:
            return "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""


_PAF_LOGO_URI = _framework_logo_uri()

def session_id_copy(session_id: str) -> None:
    """'Session 3F2A9C1E…' with a copy icon that copies the full session id.

    Custom HTML in st.markdown can't run JavaScript, so this is a tiny
    component (its own iframe), which can use the clipboard.
    """
    import streamlit.components.v1 as components
    full = json.dumps(session_id)                      # safe JS string literal
    short = html.escape(session_id[:8]) + "…"
    components.html(f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;
            color:#64748b;display:flex;align-items:center;gap:6px;">
  Session <b style="color:#0f172a;font-weight:600;font-family:ui-monospace,Menlo,monospace;">{short}</b>
  <button id="cp" title="Copy full session ID" aria-label="Copy full session ID"
          style="border:1px solid #e2e8f0;background:#fff;border-radius:6px;cursor:pointer;
                 padding:2px 5px;display:flex;align-items:center;color:#1e3a8a;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="9" y="9" width="13" height="13" rx="2"></rect>
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
  </button>
  <span id="ok" style="color:#059669;font-size:12px;"></span>
</div>
<script>
  const id = {full};
  document.getElementById("cp").onclick = async () => {{
    try {{ await navigator.clipboard.writeText(id); }}
    catch (e) {{                                   // fallback for older browsers
      const t = document.createElement("textarea"); t.value = id;
      document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove();
    }}
    const ok = document.getElementById("ok"); ok.textContent = "Copied";
    setTimeout(() => ok.textContent = "", 1500);
  }};
</script>""", height=30)


_TIER_STYLE = {"gold": ("#854d0e", "#fef3c7"), "silver": ("#334155", "#e2e8f0"),
               "bronze": ("#9a3412", "#ffedd5")}

# Initialize session state
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
    st.session_state.username = ""
    st.session_state.customer_id = ""
    st.session_state.tier = ""
    st.session_state.session_id = ""
    st.session_state.messages = []
    st.session_state.pending_prompt = None

# --- Header: brand on the left, Sign out on the right once signed in ---
# In a keyed container so CSS can pin it to the top while the chat scrolls.
with st.container(key="ac-topbar"):
    _brand_col, _signout_col = st.columns([5, 1], vertical_alignment="center")
    with _brand_col:
        st.markdown(
            '<div class="ac-header"><div class="ac-logo">🛍️</div><div>'
            '<div class="ac-title">AnyCompany Customer Support</div>'
            '<div class="ac-sub">Orders, returns and refunds, and product questions</div>'
            '</div></div>',
            unsafe_allow_html=True,
        )
    if st.session_state.authenticated:
        with _signout_col:
            if st.button("Sign out", key="signout", icon=":material/logout:", use_container_width=True):
                st.session_state.authenticated = False
                st.session_state.messages = []
                st.rerun()

# --- Login ---
if not st.session_state.authenticated:
    # Everything on the sign-in screen lives in one placeholder, so it can be
    # cleared the moment sign-in succeeds. Otherwise Streamlit keeps showing it,
    # faded, until the chat page has finished loading.
    login_area = st.empty()
    with login_area.container():
        st.markdown('<div class="ac-card-title">Sign in</div>'
                    '<div class="ac-card-sub">Use your AnyCompany credentials to access support.</div>',
                    unsafe_allow_html=True)

        with st.form("login_form"):
            username = st.text_input("Username", placeholder="e.g. gold_customer")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

        if submitted:
            if username and password:
                with st.spinner("Authenticating..."):
                    result = authenticate(username, password)
                if result["success"]:
                    st.session_state.authenticated = True
                    st.session_state.username = username
                    st.session_state.customer_id = result["customer_id"]
                    st.session_state.tier = result["tier"]
                    st.session_state.session_id = str(uuid.uuid4()).upper()
                    login_area.empty()
                    st.rerun()
                else:
                    st.error(f"Login failed: {result['error']}")
            else:
                st.warning("Please enter username and password.")

        st.markdown('<div class="ac-hint">Test accounts: <code>gold_customer</code> '
                    '<code>silver_customer</code> <code>bronze_customer</code></div>',
                    unsafe_allow_html=True)

# --- Chat ---
else:
    # Sidebar with user info
    with st.sidebar:
        _logo = (f'<img src="{_PAF_LOGO_URI}" alt="Pluggable Agentic AI Framework logo"/>'
                 if _PAF_LOGO_URI else "")
        st.markdown(
            f'<div class="ac-brand">{_logo}'
            '<div class="name">Pluggable Agentic <span>AI Framework</span></div></div>',
            unsafe_allow_html=True,
        )
        tier = (st.session_state.tier or "").lower()
        tfg, tbg = _TIER_STYLE.get(tier, ("#334155", "#e2e8f0"))
        st.markdown(
            '<div class="ac-account">'
            f'<div class="who">{html.escape(st.session_state.username)}'
            f'<span class="ac-tier" style="color:{tfg};background:{tbg};">{html.escape(tier.upper())}</span></div>'
            f'<div class="row">Customer ID <b>{html.escape(st.session_state.customer_id)}</b></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        session_id_copy(st.session_state.session_id)

        st.markdown('<hr style="margin:4px 0;">', unsafe_allow_html=True)   # tight divider
        choices = get_layer_choices()
        # L5 is the support-triage decision model, shown between L4 and L6.
        choices.insert(5, ("L5 Decision Models", get_decider()[1]))
        with st.expander("Expand to see your Stack", expanded=False):
            st.markdown(
                "".join(
                    '<div style="font-size:0.85rem; line-height:1.45; margin-bottom:6px;">'
                    f'<span style="color:#1e3a8a; font-weight:600;">{html.escape(layer)}</span><br>'
                    f'<span style="color:#475569;">{html.escape(tech)}</span></div>'
                    for layer, tech in choices
                ),
                unsafe_allow_html=True,
            )

        st.markdown('<hr style="margin:4px 0;">', unsafe_allow_html=True)   # tight divider
        st.markdown('<div class="ac-section">Try asking</div>', unsafe_allow_html=True)
        for icon, label, prompt_text in [
            (":material/badge:",          "Show my customer ID",           "What is my customer ID?"),
            (":material/package_2:",      "List all my orders",            "List all my orders"),
            (":material/assignment_return:", "Which orders can I return?", "Which of my orders are eligible for a refund?"),
            (":material/chair:",          "Home Decor order details",      "Show me all details about my most recent Home Decor order including email and address"),
            (":material/travel_explore:", "Product research (web)",        "What do recent reviews say about the Google Pixel 9 battery life? Cite your sources."),
            # Triage demos (need a decision provider): scope gate and escalation flag.
            (":material/block:",          "Out-of-scope question",         "Write me a poem about the ocean."),
            (":material/support_agent:",  "Frustrated customer, escalate", "This is the third time my refund has failed and nobody is helping. I'm really frustrated. Please escalate my case to your manager."),
        ]:
            if st.button(label, key=label, icon=icon, use_container_width=True):
                st.session_state.pending_prompt = prompt_text
                st.rerun()

    # Display chat history (with each customer message's triage badges)
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "user":
                render_triage(msg.get("triage"))

    # Chat input
    typed = st.chat_input("How can I help you today?")
    prompt = st.session_state.pending_prompt or typed
    st.session_state.pending_prompt = None
    if prompt:
        # Add user message, then triage it (one decision-model call) and show
        # the badges under it.
        user_msg = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg)
        with st.chat_message("user"):
            st.markdown(prompt)
            with st.spinner("Checking your message..."):
                triage = run_triage(prompt)
            user_msg["triage"] = triage
            render_triage(triage)

        # Stream the response into the chat bubble. Tool-use events
        # become status widgets so the user sees what the orchestrator is
        # doing during the silent deliberation gap; text deltas stream
        # token-by-token into a placeholder.
        with st.chat_message("assistant"):
            response = stream_orchestrator_to_ui(
                prompt,
                st.session_state.session_id,
                st.session_state.customer_id,
                st.session_state.tier,
                triage=triage,
            )

        # Add assistant message
        st.session_state.messages.append({"role": "assistant", "content": response})
