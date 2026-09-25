"""Streamlit Web Interface for hands - Computer-Use Automation System.

Launch via:
    streamlit run app.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import streamlit as st

# Setup import path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hands import scripts
from hands.envfile import load_env
from hands.harness import MockServer
from hands.pipeline import discover, replay
from hands.policy import load_policy
from hands.recorder import load_capability
from hands.tenancy import TenantOverride, load_override

# Page Config
st.set_page_config(
    page_title="hands — Computer-Use Automation",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Load environment variables (.env)
load_env()

POLICY_PATH = ROOT / "policies" / "msc.policy.json"
PROFILE_PATH = ROOT / "profiles" / "msc.json"
CAPS_DIR = ROOT / "capabilities"
EVIDENCE_DIR = ROOT / "evidence"

# Custom CSS for polished styling
st.markdown("""
<style>
    .main-title {
        font-size: 2.2rem;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-title {
        color: #6c757d;
        font-size: 1.05rem;
        margin-bottom: 1.5rem;
    }
    .metric-box {
        background-color: #f8f9fa;
        border-radius: 8px;
        padding: 12px;
        border-left: 4px solid #0d6efd;
    }
    .stAlert {
        border-radius: 8px;
    }
</style>
""", unsafe_allow_html=True)


def get_available_capabilities() -> dict[str, Path]:
    """Finds all capability JSON files in capabilities/ and evidence/."""
    caps: dict[str, Path] = {}
    if CAPS_DIR.exists():
        for p in sorted(CAPS_DIR.glob("*.json")):
            caps[f"{p.stem} (capabilities/)"] = p
    alt_dir = EVIDENCE_DIR / "_dryrun-scripted" / "capabilities"
    if alt_dir.exists():
        for p in sorted(alt_dir.glob("*.json")):
            name = f"{p.stem} (evidence/_dryrun/)"
            if name not in caps:
                caps[name] = p
    return caps


# Sidebar
with st.sidebar:
    st.markdown("## ⚙️ System Settings")
    st.markdown("**Target:** Mock Banking Console (MSC)")
    
    tenant_choice = st.selectbox(
        "Bank Tenant",
        ["prairie", "lakeside"],
        help="Prairie Federal Credit Union vs Lakeside Community Bank"
    )
    
    headed_mode = st.checkbox("Show Browser Window (Headed)", value=False,
                              help="Uncheck for fast headless execution; check to watch Chromium in action.")
    
    screenshot_mode = st.selectbox(
        "Screenshot Capture",
        ["steps", "failure"],
        index=0,
        help="'steps' takes a screenshot after every step; 'failure' captures only upon error."
    )
    
    st.divider()
    st.markdown("### 🔑 API Key Status")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key and len(openai_key.strip()) > 10:
        st.success(f"OpenAI Key: Active (...{openai_key.strip()[-4:]})")
    else:
        st.warning("OpenAI Key: Not Set (Scripted dry-run available)")

    st.markdown("---")
    st.caption("Built for **interface.ai** Computer-Use System Assessment")


# Header
st.markdown('<div class="main-title">🤖 hands — Computer-Use Automation</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-title">Deterministic Replay & LLM Discovery for Legacy Back-Office Applications</div>',
    unsafe_allow_html=True
)

tab_replay, tab_discover, tab_console, tab_evidence = st.tabs([
    "▶️ Deterministic Replay (No LLM)",
    "🔍 LLM Discovery Agent",
    "🏦 Mock Bank Console",
    "📁 Audit Evidence Viewer",
])

# -----------------------------------------------------------------------------------------
# TAB 1: DETERMINISTIC REPLAY
# -----------------------------------------------------------------------------------------
with tab_replay:
    st.markdown("### ⚡ Deterministic Capability Replay")
    st.info("💡 **Zero LLM tokens are consumed here.** The compiled capability executes deterministically in sub-seconds.")

    avail_caps = get_available_capabilities()
    if not avail_caps:
        st.error("No capability files found in `capabilities/`. Run Discovery first!")
    else:
        col_cap, col_fault = st.columns([2, 1])
        with col_cap:
            selected_cap_name = st.selectbox("Select Capability Artifact", list(avail_caps.keys()))
            cap_file = avail_caps[selected_cap_name]
            cap_obj = load_capability(cap_file)
            st.caption(f"**Description:** {cap_obj.description}")

        with col_fault:
            fault_type = st.selectbox(
                "Runtime Fault Injection",
                [
                    "none",
                    "notice (Compliance Interstitial)",
                    "slow_ms=2000 (Network Latency)",
                    "flaky_member=2 (Transient HTTP 503)",
                    "app_error_acct (Database HTTP 500 Crash)",
                    "expire_after=2 (Session Timeout)",
                ],
                help="Test the engine's ability to recover from unexpected runtime states."
            )
            fault_param = fault_type.split(" ")[0] if " " in fault_type else fault_type
            if fault_param == "none":
                fault_param = ""

        # Dynamic Input Parameters
        st.markdown("#### Input Parameters")
        input_values: dict[str, str] = {}
        input_cols = st.columns(min(len(cap_obj.inputs) or 1, 3))

        # Quick preset buttons for savings balance
        if "member_savings_balance" in cap_obj.id or "lookup" in cap_obj.id:
            st.caption("Quick Test Presets:")
            pcol1, pcol2, pcol3, pcol4 = st.columns(4)
            preset_clicked = None
            if pcol1.button("12345 (Normal Account)", use_container_width=True):
                preset_clicked = "12345"
            if pcol2.button("99999 (Not Found)", use_container_width=True):
                preset_clicked = "99999"
            if pcol3.button("77777 (Restricted Staff)", use_container_width=True):
                preset_clicked = "77777"
            if pcol4.button("40551 (No Savings Acct)", use_container_width=True):
                preset_clicked = "40551"
            if preset_clicked:
                st.session_state["member_id_val"] = preset_clicked

        for i, param in enumerate(cap_obj.inputs):
            col = input_cols[i % len(input_cols)]
            default_val = param.example or ("12345" if param.name == "member_id" else "")
            if param.name == "member_id" and "member_id_val" in st.session_state:
                default_val = st.session_state["member_id_val"]

            if param.enum:
                input_values[param.name] = col.selectbox(
                    f"{param.name} ({param.type.value})",
                    param.enum,
                    help=param.description
                )
            elif param.type.value == "boolean":
                input_values[param.name] = str(col.checkbox(param.name, value=True, help=param.description)).lower()
            else:
                input_values[param.name] = col.text_input(
                    f"{param.name} ({param.type.value})",
                    value=default_val,
                    help=param.description
                )

        # Multi-tenant override toggle
        override_obj = None
        if tenant_choice == "lakeside":
            ov_file = ROOT / "overrides" / f"lakeside.{cap_obj.id}.json"
            if ov_file.exists():
                st.success(f"Tenant Override loaded for Lakeside: `{ov_file.name}`")
                override_obj = load_override(ov_file)
            else:
                st.warning("No override file found for Lakeside; running base artifact directly.")

        st.markdown("---")
        if st.button("🚀 Run Deterministic Replay", type="primary", use_container_width=True):
            with st.spinner("Replaying capability through Playwright..."):
                t_start = time.time()
                # Run mock bank in-process
                with MockServer(tenant=tenant_choice, port=8765, faults=fault_param) as srv:
                    out_run_dir = EVIDENCE_DIR / "_ui_runs"
                    policy = load_policy(POLICY_PATH)
                    res = replay(
                        cap_obj,
                        input_values,
                        target=srv.url,
                        policy=policy,
                        out_dir=out_run_dir,
                        headless=not headed_mode,
                        shots=screenshot_mode,
                        override=override_obj,
                    )
                elapsed = time.time() - t_start

            # Display Results
            st.markdown("### 📊 Replay Results")
            r_col1, r_col2, r_col3, r_col4 = st.columns(4)

            status_color = {
                "success": "🟢",
                "business_outcome": "🔵",
                "failed": "🔴",
                "blocked": "🟠",
                "escalated": "🟡",
            }.get(res.status, "⚪")

            r_col1.metric("Status", f"{status_color} {res.status.upper()}")
            r_col2.metric("Duration", f"{elapsed:.2f}s")
            r_col3.metric("Steps Completed", f"{res.steps_completed} / {len(cap_obj.steps)}")
            r_col4.metric("Recoveries / Drift", f"{len(res.recoveries)} / {len(res.drift)}")

            if res.outputs:
                st.success(f"**Extracted Outputs:** `{json.dumps(res.outputs, indent=2)}`")

            if res.outcome:
                st.info(f"**Business Outcome:** `{res.outcome}` — Normal business answer (not a crash).")

            if res.failure:
                st.error(f"**Failure Category:** `{res.failure.category.value}`\n\n**Message:** {res.failure.message}")

            if res.recoveries:
                st.warning(f"**Recoveries Handled:** {', '.join(r.code for r in res.recoveries)}")

            if res.drift:
                st.info(f"**Drift Detected:** {', '.join(f'{d.step_id}:{d.kind}' for d in res.drift)}")

            # Show Screenshots
            run_dir = out_run_dir / res.run_id / "shots"
            if run_dir.exists():
                shot_files = sorted(run_dir.glob("*.png"))
                if shot_files:
                    st.markdown("#### 📸 Captured Screen Evidence")
                    shot_cols = st.columns(min(len(shot_files), 3))
                    for idx, shot in enumerate(shot_files):
                        with shot_cols[idx % len(shot_cols)]:
                            st.image(str(shot), caption=shot.stem, use_container_width=True)


# -----------------------------------------------------------------------------------------
# TAB 2: LLM DISCOVERY AGENT
# -----------------------------------------------------------------------------------------
with tab_discover:
    st.markdown("### 🔍 Goal-Driven Discovery Agent")
    st.markdown("The LLM explores the unfamiliar legacy UI, figures out the flow, and compiles a reusable Capability.")

    goal_input = st.text_area(
        "Natural Language Goal",
        value="Look up member 12345 in the member servicing console and read their savings account's available balance.",
        height=70
    )

    d_col1, d_col2, d_col3 = st.columns(3)
    with d_col1:
        provider = st.selectbox(
            "Model Provider",
            ["scripted-savings (Key-Free)", "openai", "anthropic", "scripted-open"],
            index=0,
            help="Select 'scripted-savings' to test immediately without any API key."
        )

    with d_col2:
        default_model = "gpt-4o" if "openai" in provider else "claude-sonnet-5"
        selected_model = st.text_input("Model ID", value=default_model)

    with d_col3:
        max_steps = st.slider("Max Steps", min_value=5, max_value=25, value=15)

    if "openai" in provider and not os.environ.get("OPENAI_API_KEY"):
        user_key = st.text_input("Enter OpenAI API Key", type="password", help="Or save it in your .env file")
        if user_key:
            os.environ["OPENAI_API_KEY"] = user_key.strip()

    if st.button("🚀 Start LLM Discovery Run", type="primary", use_container_width=True):
        from hands.cli import _model

        # Dummy args structure for model factory
        class Args:
            pass
        a = Args()
        a.provider = "scripted-savings" if "scripted-savings" in provider else ("scripted-open" if "scripted-open" in provider else provider)
        a.model = selected_model

        try:
            model_client = _model(a)
            st.info(f"Initialized model adapter: `{model_client.name}`")

            with st.spinner("Agent running: observing UI, deciding actions, and acting..."):
                with MockServer(tenant="prairie", port=8765) as srv:
                    out_dir = EVIDENCE_DIR / "_ui_discovery"
                    policy = load_policy(POLICY_PATH)
                    cap, d_res, v_res = discover(
                        target=srv.url,
                        goal=goal_input,
                        model=model_client,
                        policy=policy,
                        profile_path=PROFILE_PATH,
                        out_dir=out_dir,
                        cap_dir=CAPS_DIR,
                        headless=not headed_mode,
                        tenant="prairie",
                        max_steps=max_steps,
                        say=lambda msg: None
                    )

            if cap:
                st.success(f"🎉 **Discovery Succeeded!** Compiled capability: `{cap.id}` (Status: `{cap.status}`)")
                col_m1, col_m2 = st.columns(2)
                col_m1.metric("Steps Used", d_res.steps_used)
                col_m2.metric("Stop Reason", d_res.stop_reason)

                with st.expander("📄 View Compiled Capability Artifact (JSON)", expanded=False):
                    st.json(cap.model_dump(mode="json", exclude_none=True))

                if v_res:
                    st.info(f"**Verification Replay Result:** `{v_res.status}` ({v_res.message})")
            else:
                st.error(f"Discovery stopped without compiling an artifact: {d_res.stop_reason} ({d_res.summary})")

        except Exception as exc:
            st.error(f"Error during discovery: {exc}")


# -----------------------------------------------------------------------------------------
# TAB 3: MOCK BANK CONSOLE
# -----------------------------------------------------------------------------------------
with tab_console:
    st.markdown("### 🏦 Mock Legacy Core-Banking Console")
    st.markdown("""
    This project includes a fully functional, self-hosted mock core-banking application ("MSC").
    It features HTML `<frameset>` tags, unassociated table layouts, and injectable fault conditions.
    """)

    c_col1, c_col2 = st.columns(2)
    with c_col1:
        st.markdown("""
        #### 🔐 Login Credentials
        * **URL:** [http://127.0.0.1:8765](http://127.0.0.1:8765)
        * **Username:** `teller01`
        * **Password:** `demo-not-a-real-password`
        """)

    with c_col2:
        st.markdown("""
        #### 👥 Synthetic Members
        * **12345** — Dana Whitfield (Savings: $4,721.37)
        * **20817** — Marcus Oyelaran (Checking & Certificate)
        * **33190** — Priya Raman (High Balance)
        * **40551** — Member without savings account
        * **77777** — Restricted employee record (Teller denied)
        * **99999** — Non-existent member record
        """)

    st.info("To launch the banking web console manually in your terminal: `python -m mockbank --port 8765`")


# -----------------------------------------------------------------------------------------
# TAB 4: AUDIT EVIDENCE VIEWER
# -----------------------------------------------------------------------------------------
with tab_evidence:
    st.markdown("### 📁 Audit Evidence & Run Logs")
    st.markdown("Every run produces an auditable, redacted event trail (`events.jsonl`), masked screenshots, and DOM snapshots.")

    summary_file = EVIDENCE_DIR / "_dryrun-scripted" / "summary.json"
    if summary_file.exists():
        st.markdown("#### 19-Scenario Verification Matrix")
        summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
        st.dataframe(summary_data, use_container_width=True)
    else:
        st.info("Run `python scripts/make_evidence.py --provider scripted` to generate the full 19-scenario evidence suite.")
