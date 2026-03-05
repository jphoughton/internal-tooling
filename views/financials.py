"""Financials page — placeholder after bank data removal."""
import streamlit as st


def render(ctx):
    """Render the Financials page."""
    st.title('Financials')
    st.info(
        'This page previously displayed bank transaction data. '
        'Use the **Cash Flow** page for cash flow projections '
        'and the **Variables** page for P&L inputs.'
    )
