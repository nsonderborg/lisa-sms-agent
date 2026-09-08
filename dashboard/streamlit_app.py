import streamlit as st

from dashboard.queries import (
    get_conversations_df,
    get_escalation_queue_df,
    get_leads_df,
    get_message_stats_df,
    get_intent_distribution_df,
    get_summary_metrics,
)

st.set_page_config(page_title="Lisa Dashboard", layout="wide")
st.title("Lisa -- Oversigt")

# --- Top-level summary metrics ---
metrics = get_summary_metrics()
col1, col2, col3, col4 = st.columns(4)
col1.metric("Samtaler", metrics["total_conversations"])
col2.metric("Aktive i dag", metrics["active_today"])
col3.metric("Eskaleringer", metrics["unresolved_escalations"])
col4.metric("Leads", metrics["total_leads"])

# --- Tabs ---
tab1, tab2, tab3, tab4 = st.tabs(["Samtaler", "Eskaleringer", "Leads", "Analyse"])

with tab1:
    st.subheader("Samtaler")
    df = get_conversations_df()
    if df.empty:
        st.info("Ingen samtaler endnu.")
    else:
        st.dataframe(df, use_container_width=True)

with tab2:
    st.subheader("Eskaleringskoe")
    df = get_escalation_queue_df()
    if df.empty:
        st.success("Ingen uloeste eskaleringer!")
    else:
        st.dataframe(df, use_container_width=True)

with tab3:
    st.subheader("Lead Pipeline")
    df = get_leads_df()
    if df.empty:
        st.info("Ingen leads endnu.")
    else:
        for status in ["new", "contacted", "converted"]:
            status_df = df[df["status"] == status]
            if not status_df.empty:
                st.markdown(f"**{status.capitalize()}** ({len(status_df)})")
                st.dataframe(status_df, use_container_width=True)

with tab4:
    st.subheader("Besked Analyse")
    col_left, col_right = st.columns(2)
    with col_left:
        st.markdown("**Beskeder per dag (sidste 30 dage)**")
        stats_df = get_message_stats_df()
        if not stats_df.empty:
            st.bar_chart(stats_df.set_index("date"))
        else:
            st.info("Ingen data endnu.")
    with col_right:
        st.markdown("**Intent fordeling**")
        intent_df = get_intent_distribution_df()
        if not intent_df.empty:
            st.bar_chart(intent_df.set_index("intent"))
        else:
            st.info("Ingen data endnu.")

st.caption("Data opdateres hvert 30. sekund. Tryk Ctrl+R for at genindlaese.")
