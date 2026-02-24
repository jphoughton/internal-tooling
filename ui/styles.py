"""Global CSS and Plotly theming for Hydrant dashboard."""
import streamlit as st
import plotly.graph_objects as go
import plotly.io as pio


# The full CSS string — copied from dashboard.py lines 210-627
GLOBAL_CSS = """
<style>
/* ── Typography — Visby CF (fallback to system) ── */
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"] {
    font-family: 'Visby CF', 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
}

/* ── App background — clean white ── */
.stApp {
    background: #f7f8fc !important;
}

/* ── Sidebar — Hydrant navy ── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0B2A45 0%, #0F3557 40%, #143D61 100%) !important;
    border-right: none;
    box-shadow: 4px 0 20px rgba(15,53,87,0.15);
}
section[data-testid="stSidebar"] * {
    color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2 {
    font-size: 1.3rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}

/* ── Headers — Hydrant navy ── */
h1 {
    font-weight: 700 !important;
    letter-spacing: -0.03em !important;
    font-size: 2rem !important;
    margin-bottom: 0.1rem !important;
    color: #0F3557 !important;
}
h2 {
    font-weight: 600 !important;
    letter-spacing: -0.02em !important;
    font-size: 1.3rem !important;
    margin-top: 1.8rem !important;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid #E8EDF3;
    color: #0F3557 !important;
}
h3 {
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    font-size: 1.05rem !important;
    color: #1a3a5c !important;
}

/* ── Body text — scoped to avoid overriding component internals ── */
.main [data-testid="stMarkdownContainer"] p,
.main [data-testid="stMarkdownContainer"] span,
.main [data-testid="stMarkdownContainer"] li {
    color: #2c3e50;
}
.main label {
    color: #2c3e50;
}

/* ── Metric cards — white cards with sky-blue accent ── */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #E8EDF3;
    border-radius: 14px;
    padding: 18px 22px !important;
    box-shadow: 0 2px 8px rgba(15,53,87,0.06);
    transition: all 0.2s ease;
    border-top: 3px solid #7ECCE5;
}
[data-testid="stMetric"]:hover {
    box-shadow: 0 4px 16px rgba(15,53,87,0.1);
    transform: translateY(-1px);
}
[data-testid="stMetricLabel"] {
    font-size: 0.72rem !important;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #6b7c93 !important;
    opacity: 1 !important;
    font-weight: 600 !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.55rem !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em;
    color: #0F3557 !important;
}
[data-testid="stMetricDelta"] {
    font-size: 0.78rem !important;
    font-weight: 600 !important;
}
[data-testid="stMetricDelta"][data-testid] svg { color: inherit !important; }

/* ── DataFrames & tables — force light/white everywhere ── */
[data-testid="stDataFrame"] {
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #E8EDF3;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
    background: #ffffff !important;
}
[data-testid="stDataFrame"] iframe {
    border-radius: 12px;
    background: #ffffff !important;
}
[data-testid="stDataFrame"] > div,
[data-testid="stDataFrame"] > div > div,
[data-testid="stDataFrame"] > div > div > div {
    background: #ffffff !important;
}
/* Glide Data Editor (canvas-based): force container backgrounds white */
[data-testid="stDataFrame"] [data-testid="glideDataEditor"],
[data-testid="stDataFrame"] canvas,
[data-testid="stDataFrame"] .dvn-scroller,
[data-testid="stDataFrame"] [class*="glide"],
[data-testid="stDataFrame"] [class*="data-grid"],
[data-testid="stDataFrame"] [role="grid"],
[data-testid="stDataFrame"] [role="gridcell"],
[data-testid="stDataFrame"] [role="columnheader"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
    color: #1e2d3d !important;
}
/* Target the Glide wrapper divs exhaustively */
[data-testid="stDataFrame"] div[style] {
    background: #ffffff !important;
    background-color: #ffffff !important;
}
/* Static table rendering (st.table) */
.stDataFrame table,
[data-testid="stTable"] table {
    background: #ffffff !important;
    color: #1e2d3d !important;
    border-radius: 12px;
    overflow: hidden;
}
.stDataFrame table th,
[data-testid="stTable"] table th {
    background: #F0F4F8 !important;
    color: #0F3557 !important;
    font-weight: 600 !important;
    font-size: 0.76rem !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border-bottom: 2px solid #D6DEE8 !important;
    padding: 11px 14px !important;
}
.stDataFrame table td,
[data-testid="stTable"] table td {
    background: #ffffff !important;
    color: #1e2d3d !important;
    border-bottom: 1px solid #F0F4F8 !important;
    padding: 9px 14px !important;
    font-size: 0.84rem !important;
}
.stDataFrame table tr:hover td,
[data-testid="stTable"] table tr:hover td {
    background: #F7FAFC !important;
}

/* ── Tabs — rounded pill style with Hydrant blue ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: #F0F4F8;
    border-radius: 12px;
    padding: 4px;
    border: 1px solid #E8EDF3;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 9px;
    font-weight: 600;
    font-size: 0.84rem;
    padding: 8px 22px;
    transition: all 0.15s ease;
    color: #5a6f83 !important;
    background: transparent !important;
}
.stTabs [data-baseweb="tab"] * {
    color: inherit !important;
}
.stTabs [data-baseweb="tab"]:hover {
    background: rgba(126,204,229,0.15) !important;
    color: #0F3557 !important;
}
.stTabs [data-baseweb="tab"]:hover * {
    color: #0F3557 !important;
}
.stTabs [aria-selected="true"] {
    background: #0F3557 !important;
    color: #ffffff !important;
    box-shadow: 0 2px 8px rgba(15,53,87,0.25);
}
.stTabs [aria-selected="true"] *,
.stTabs [aria-selected="true"] span,
.stTabs [aria-selected="true"] p,
.stTabs [aria-selected="true"] div {
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
/* Tab highlight bar — remove default underline, we use pill bg instead */
.stTabs [data-baseweb="tab-highlight"] {
    display: none !important;
}
.stTabs [data-baseweb="tab-border"] {
    display: none !important;
}

/* ── Buttons — rounded, Hydrant style ── */
.stButton button {
    border-radius: 24px !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    letter-spacing: 0.01em;
    transition: all 0.15s ease !important;
    border: 1px solid #D6DEE8 !important;
    color: #0F3557 !important;
    background: #ffffff !important;
}
.stButton button:hover {
    border-color: #7ECCE5 !important;
    box-shadow: 0 2px 8px rgba(126,204,229,0.2);
    color: #0F3557 !important;
}
.stButton button[kind="primary"],
.stButton button[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #7ECCE5, #5BB8D4) !important;
    border: none !important;
    color: #0F3557 !important;
    box-shadow: 0 2px 10px rgba(126,204,229,0.3) !important;
}
.stButton button[kind="primary"]:hover,
.stButton button[data-testid="stBaseButton-primary"]:hover {
    box-shadow: 0 4px 16px rgba(126,204,229,0.4) !important;
    transform: translateY(-1px);
}

/* ── Expanders — white card ── */
[data-testid="stExpander"] {
    background: #ffffff !important;
    border: 1px solid #E8EDF3 !important;
    border-radius: 12px !important;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(15,53,87,0.04);
}
[data-testid="stExpander"]:hover {
    border-color: #7ECCE5 !important;
}
[data-testid="stExpanderToggleIcon"] {
    opacity: 0.6;
}

/* ── Dividers ── */
hr {
    opacity: 0.3 !important;
    margin: 2.5rem 0 !important;
    border-color: #D6DEE8 !important;
}

/* ── Captions ── */
.stCaption, [data-testid="stCaptionContainer"] {
    font-size: 0.8rem !important;
    color: #7a8da0 !important;
    opacity: 1 !important;
}

/* ── Progress bars — sky blue ── */
[data-testid="stProgress"] > div > div {
    border-radius: 8px !important;
    background: #E8EDF3 !important;
}
[data-testid="stProgress"] > div > div > div {
    background: linear-gradient(90deg, #7ECCE5, #5BB8D4) !important;
    border-radius: 8px !important;
}

/* ── Alert boxes ── */
[data-testid="stAlert"] {
    border-radius: 12px !important;
    border: 1px solid #E8EDF3 !important;
}

/* ── Selectboxes & inputs ── */
[data-testid="stSelectbox"] > div > div,
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stDateInput > div > div > input {
    border-radius: 10px !important;
    border-color: #D6DEE8 !important;
    background: #ffffff !important;
    color: #0F3557 !important;
}
[data-testid="stSelectbox"] > div > div:focus-within,
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: #7ECCE5 !important;
    box-shadow: 0 0 0 3px rgba(126,204,229,0.15) !important;
}

/* ── Data editor — force white background ── */
[data-testid="stDataEditor"] {
    border: 1px solid #E8EDF3;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
    background: #ffffff !important;
}
[data-testid="stDataEditor"] > div,
[data-testid="stDataEditor"] > div > div,
[data-testid="stDataEditor"] > div > div > div,
[data-testid="stDataEditor"] div[style],
[data-testid="stDataEditor"] [data-testid="glideDataEditor"],
[data-testid="stDataEditor"] canvas,
[data-testid="stDataEditor"] [role="grid"],
[data-testid="stDataEditor"] [role="gridcell"],
[data-testid="stDataEditor"] [role="columnheader"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] > div {
    border-radius: 12px !important;
    border-color: #D6DEE8 !important;
    background: #F7FAFC !important;
}

/* ── Toggle switch ── */
[data-testid="stToggle"] label span {
    font-weight: 500 !important;
    color: #2c3e50 !important;
}

/* ── Radio buttons (horizontal) ── */
.stRadio > div[role="radiogroup"] > label {
    border-radius: 8px;
    padding: 4px 12px;
    transition: background 0.15s ease;
    color: #2c3e50 !important;
}
.stRadio > div[role="radiogroup"] > label:hover {
    background: rgba(126,204,229,0.1);
}

/* ── Main container — full width ── */
.block-container {
    padding-top: 1.5rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    max-width: 100% !important;
}

/* ── Sidebar navigation — styled radio buttons ── */
section[data-testid="stSidebar"] .stRadio [data-testid="stWidgetLabel"] { display: none; }
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] {
    gap: 1px !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label {
    padding: 10px 16px !important;
    margin: 1px 4px !important;
    border-radius: 10px !important;
    font-size: 0.88rem !important;
    font-weight: 500 !important;
    color: rgba(255,255,255,0.7) !important;
    transition: all 0.18s ease !important;
    cursor: pointer !important;
    background: transparent !important;
    border-left: 3px solid transparent !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label:hover {
    background: rgba(126,204,229,0.12) !important;
    color: #ffffff !important;
}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label[data-checked="true"],
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label:has(input:checked) {
    background: rgba(126,204,229,0.18) !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    border-left: 3px solid #7ECCE5 !important;
}
/* Hide the radio dot */
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] label > div:first-child {
    display: none !important;
}
/* Section label pseudo-element styling */
section[data-testid="stSidebar"] .nav-section {
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: rgba(255,255,255,0.35);
    padding: 14px 20px 4px;
    font-weight: 600;
}

/* ── Sidebar Business Variables expander ── */
section[data-testid="stSidebar"] [data-testid="stExpander"] {
    background: rgba(255,255,255,0.07) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    border-radius: 10px !important;
    box-shadow: none !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"]:hover {
    border-color: rgba(126,204,229,0.4) !important;
    background: rgba(255,255,255,0.10) !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    letter-spacing: 0.01em;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stNumberInput input {
    background: rgba(255,255,255,0.14) !important;
    border: 1px solid rgba(255,255,255,0.25) !important;
    border-radius: 6px !important;
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
    font-size: 0.82rem !important;
    padding: 4px 8px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stSelectbox > div > div {
    background: rgba(255,255,255,0.14) !important;
    border: 1px solid rgba(255,255,255,0.25) !important;
    border-radius: 6px !important;
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] label {
    font-size: 0.75rem !important;
    color: rgba(255,255,255,0.75) !important;
    -webkit-text-fill-color: rgba(255,255,255,0.75) !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stCaption,
section[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stCaptionContainer"] {
    color: rgba(255,255,255,0.5) !important;
    font-size: 0.7rem !important;
}
/* Sidebar edit buttons (non-primary) */
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton button:not([kind="primary"]):not([data-testid="stBaseButton-primary"]) {
    background: rgba(126,204,229,0.12) !important;
    border: 1px solid rgba(126,204,229,0.3) !important;
    color: #7ECCE5 !important;
    -webkit-text-fill-color: #7ECCE5 !important;
    font-weight: 600 !important;
    border-radius: 8px !important;
    font-size: 0.78rem !important;
    padding: 5px 14px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] .stButton button:not([kind="primary"]):not([data-testid="stBaseButton-primary"]):hover {
    background: rgba(126,204,229,0.22) !important;
    border-color: rgba(126,204,229,0.5) !important;
}
/* Sidebar primary buttons */
section[data-testid="stSidebar"] [data-testid="stExpander"] button[kind="primary"],
section[data-testid="stSidebar"] [data-testid="stExpander"] button[data-testid="stBaseButton-primary"] {
    background: #7ECCE5 !important;
    color: #0B2A45 !important;
    -webkit-text-fill-color: #0B2A45 !important;
    border: none !important;
    font-weight: 700 !important;
    border-radius: 8px !important;
    font-size: 0.8rem !important;
    padding: 6px 16px !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] button[kind="primary"]:hover,
section[data-testid="stSidebar"] [data-testid="stExpander"] button[data-testid="stBaseButton-primary"]:hover {
    background: #5dbdd8 !important;
}

/* ── Dialog form grids — compact number inputs ── */
[data-testid="stDialog"] * {
    color: #1e2d3d;
}
[data-testid="stDialog"] [data-testid="stCaptionContainer"] {
    color: #6b7c93 !important;
}
[data-testid="stDialog"] .stNumberInput {
    margin-bottom: -10px;
}
[data-testid="stDialog"] .stNumberInput input {
    padding: 5px 8px !important;
    font-size: 0.84rem !important;
    border-radius: 8px !important;
    border-color: #D6DEE8 !important;
    background: #ffffff !important;
    color: #0F3557 !important;
    text-align: right;
}
[data-testid="stDialog"] .stNumberInput input:focus {
    border-color: #7ECCE5 !important;
    box-shadow: 0 0 0 2px rgba(126,204,229,0.15) !important;
}
/* Tighten column gaps in dialog grids */
[data-testid="stDialog"] [data-testid="stHorizontalBlock"] {
    gap: 0.4rem !important;
}
/* Dialog tabs (for quarterly inbound) */
[data-testid="stDialog"] .stTabs [data-baseweb="tab-list"] {
    gap: 2px;
    background: #F0F4F8;
    border-radius: 10px;
    padding: 3px;
}
[data-testid="stDialog"] .stTabs [data-baseweb="tab"] {
    font-size: 0.78rem;
    padding: 6px 14px;
    border-radius: 8px;
    color: #5a6f83 !important;
}
[data-testid="stDialog"] .stTabs [aria-selected="true"] {
    background: #0F3557 !important;
    color: #ffffff !important;
}
[data-testid="stDialog"] .stTabs [aria-selected="true"] * {
    color: #ffffff !important;
    -webkit-text-fill-color: #ffffff !important;
}
[data-testid="stDialog"] .stTabs [data-baseweb="tab-highlight"],
[data-testid="stDialog"] .stTabs [data-baseweb="tab-border"] {
    display: none !important;
}
/* Dialog primary button */
[data-testid="stDialog"] button[kind="primary"],
[data-testid="stDialog"] button[data-testid="stBaseButton-primary"],
[data-testid="stDialog"] [data-testid="stFormSubmitButton"] button {
    background: linear-gradient(135deg, #7ECCE5, #5BB8D4) !important;
    border: none !important;
    color: #0F3557 !important;
    -webkit-text-fill-color: #0F3557 !important;
    font-weight: 700 !important;
    border-radius: 10px !important;
    font-size: 0.85rem !important;
    padding: 8px 20px !important;
}

/* ── Styled dataframe (Pandas Styler) ── */
.stTable {
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #E8EDF3;
    box-shadow: 0 2px 12px rgba(15,53,87,0.06);
}

/* ── Number input styling ── */
.stNumberInput label, .stTextInput label, .stSelectbox label, .stDateInput label {
    color: #0F3557 !important;
    font-weight: 500 !important;
}

/* ── Multiselect ── */
[data-testid="stMultiSelect"] span[data-baseweb="tag"] {
    background: #7ECCE5 !important;
    color: #0F3557 !important;
    border-radius: 6px !important;
}
</style>
"""


def setup_plotly_theme():
    """Register and activate the Hydrant Plotly template."""
    template = go.layout.Template()
    template.layout.plot_bgcolor = "#ffffff"
    template.layout.paper_bgcolor = "rgba(0,0,0,0)"
    template.layout.font = dict(
        family="Visby CF, DM Sans, -apple-system, sans-serif",
        size=12,
        color="#2c3e50",
    )
    template.layout.xaxis = dict(
        gridcolor="#E8EDF3", gridwidth=1,
        linecolor="#D6DEE8", linewidth=1,
        tickfont=dict(color="#6b7c93"),
    )
    template.layout.yaxis = dict(
        gridcolor="#E8EDF3", gridwidth=1,
        linecolor="#D6DEE8", linewidth=1,
        tickfont=dict(color="#6b7c93"),
    )
    template.layout.margin = dict(l=0, r=0, t=30, b=0)
    template.layout.colorway = [
        "#0F3557",  # Hydrant navy
        "#7ECCE5",  # Sky blue
        "#F58B3D",  # Orange
        "#C5E0A5",  # Soft green
        "#F4A3A0",  # Coral
        "#5BB8D4",  # Medium blue
        "#FFC857",  # Warm yellow
        "#A8D5BA",  # Mint
        "#E87461",  # Warm red
        "#B5C7D3",  # Steel blue
    ]
    pio.templates["hydrant"] = template
    pio.templates.default = "hydrant"


def get_nav_section_css(nav_groups):
    """Generate CSS for sidebar navigation section headers.

    Args:
        nav_groups: list of (group_name, [page_names]) tuples
    """
    style = '<style>\n/* Section dividers injected via nth-child */\n'
    idx = 0
    for grp_name, grp_pages in nav_groups:
        child_num = idx + 1
        style += f'''
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({child_num}) {{
    margin-top: {'20px' if idx > 0 else '4px'} !important;
    flex-wrap: wrap !important;
    padding-top: 28px !important;
    position: relative !important;
}}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({child_num})::before {{
    content: "{grp_name.upper()}";
    position: absolute;
    top: 2px;
    left: 16px;
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: rgba(255,255,255,0.35) !important;
    font-weight: 600;
    line-height: 1;
    -webkit-text-fill-color: rgba(255,255,255,0.35) !important;
}}
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({child_num}):has(input:checked),
section[data-testid="stSidebar"] .stRadio div[role="radiogroup"] > label:nth-child({child_num})[data-checked="true"] {{
    background: linear-gradient(to bottom, transparent 22px, rgba(126,204,229,0.18) 22px) !important;
}}
'''
        idx += len(grp_pages)
    style += '</style>'
    return style


def inject_global_styles():
    """Inject all global CSS into the Streamlit app."""
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
    setup_plotly_theme()
