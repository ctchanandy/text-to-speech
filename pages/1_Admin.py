import streamlit as st

from app import (
    ENABLE_AUTH,
    ensure_auth_ready_for_page,
    hide_sidebar_ui,
    initialize_session_state,
    render_admin_metrics_panel,
    render_admin_user_panel,
    render_auth_toolbar,
)


st.set_page_config(page_title="Admin", page_icon="🛠️")
hide_sidebar_ui()
st.title("🛠️ Admin")
st.caption("Manage users and review usage metrics.")
if st.button("🔊 Back to Text-to-Speech"):
    st.switch_page("app.py")

initialize_session_state()

if not ENABLE_AUTH:
    st.warning("Authentication is disabled in config. Admin page is unavailable.")
    st.stop()

if not ensure_auth_ready_for_page():
    st.stop()

render_auth_toolbar()

if st.session_state.get("auth_role") != "admin":
    st.error("Access denied. Admin role is required.")
    st.stop()

render_admin_user_panel()
render_admin_metrics_panel()
