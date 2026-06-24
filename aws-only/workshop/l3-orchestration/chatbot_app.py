"""AnyCompany Customer Support Chatbot — Streamlit UI

Run with: streamlit run chatbot_app.py
"""

import boto3
import json
import os
import time
import uuid
import streamlit as st
import jwt
from jwt import PyJWKClient

# --- Configuration ---
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
SSM_PREFIX = "/anycompany/agentcore"

@st.cache_resource
def get_clients():
    from botocore.config import Config
    ssm = boto3.client("ssm", region_name=REGION)
    agentcore_runtime = boto3.client("bedrock-agentcore", region_name=REGION,
                                     config=Config(read_timeout=300))
    cognito = boto3.client("cognito-idp", region_name=REGION)
    return ssm, agentcore_runtime, cognito


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


def redact_sensitive(text: str) -> str:
    """Redact sensitive patterns (SSN, credit card, email) before streaming to the UI."""
    import re
    # SSN pattern: XXX-XX-XXXX
    text = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', 'XXX-XX-XXXX', text)
    # Credit card: 4 groups of 4 digits
    text = re.sub(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', 'XXXX-XXXX-XXXX-XXXX', text)
    # Email addresses
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[REDACTED-EMAIL]', text)
    return text


def stream_orchestrator_to_ui(message: str, session_id: str, actor_id: str, tier: str) -> str:
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

    # Prepend authenticated identity so the orchestrator always knows who it's talking to
    augmented_message = f"[Authenticated customer: {actor_id} | Tier: {tier}]\n\n{message}"

    start_time = time.time()
    response = agentcore_runtime.invoke_harness(
        harnessArn=config["harness_arn"],
        runtimeSessionId=session_id,
        actorId=actor_id,
        messages=[{"role": "user", "content": [{"text": augmented_message}]}],
    )

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

    with st.spinner("Thinking..."):
        for event in response["stream"]:
            # ---- tool use: orchestrator is calling something ----
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                tool_use = start.get("toolUse")
                if tool_use:
                    active_tool_name = tool_use.get("name", "tool")
                    if active_tool_name not in HIDDEN_TOOLS:
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
                    text = redact_sensitive(text)
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
    st.caption(f"⏱ {latency:.1f}s")
    return full_text


# --- Streamlit UI ---
st.set_page_config(page_title="AnyCompany Support", page_icon="🛍️")
st.title("🛍️ AnyCompany Customer Support")

# Initialize session state
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
    st.session_state.username = ""
    st.session_state.customer_id = ""
    st.session_state.tier = ""
    st.session_state.session_id = ""
    st.session_state.messages = []
    st.session_state.pending_prompt = None

# --- Login ---
if not st.session_state.authenticated:
    st.markdown("### 🔐 Login")
    st.markdown("Use your AnyCompany credentials to access support.")

    with st.form("login_form"):
        username = st.text_input("Username", placeholder="e.g. gold_customer")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")

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
                st.rerun()
            else:
                st.error(f"Login failed: {result['error']}")
        else:
            st.warning("Please enter username and password.")

    st.markdown("---")
    st.markdown("**Test accounts:** `gold_customer`, `silver_customer`, `bronze_customer` ")

# --- Chat ---
else:
    # Sidebar with user info
    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state.username}")
        st.markdown(f"**Customer ID:** {st.session_state.customer_id}")
        st.markdown(f"**Tier:** {st.session_state.tier.upper()}")
        st.markdown(f"**Session:** {st.session_state.session_id[:8]}...")
        if st.button("Logout"):
            st.session_state.authenticated = False
            st.session_state.messages = []
            st.rerun()

        st.markdown("---")
        st.markdown("**💡 Try asking:**")
        for label, prompt_text in [
            ("🪪 Show my Customer ID",                    "What is my customer ID?"),
            ("📦 List all my orders",                     "List all my orders"),
            ("↩️ Refund-eligible orders",                 "Which of my orders are eligible for a refund?"),
            ("🏠 Home Decor order details",               "Show me all details about my most recent Home Decor order including email and address"),
        ]:
            if st.button(label, key=label, use_container_width=True):
                st.session_state.pending_prompt = prompt_text
                st.rerun()

    # Display chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    typed = st.chat_input("How can I help you today?")
    prompt = st.session_state.pending_prompt or typed
    st.session_state.pending_prompt = None
    if prompt:
        # Add user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

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
            )

        # Add assistant message
        st.session_state.messages.append({"role": "assistant", "content": response})
