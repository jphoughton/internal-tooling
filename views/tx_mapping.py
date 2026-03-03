"""
Transaction Category Mapping screen.

Allows the user to map unique bank transaction patterns to spending categories.
The system normalizes transaction summaries (strips IDs, amounts, dates) to create
stable patterns, then looks up the assigned category for each pattern.

CFO perspective: This is the "teach the system once" screen. Map each vendor/payee
to a category, and all future imports auto-classify. Think of it like setting up
rules in QuickBooks -- but smarter because it normalizes the messy bank descriptions.
"""
import logging

import streamlit as st
import pandas as pd

from db import get_db, read_sql, upsert_category_mapping
from analytics.cashflow import (
    normalize_summary, suggest_category, get_mapping_stats,
    reclassify_all_transactions,
)
from utils.constants import CASHFLOW_CATEGORIES

log = logging.getLogger(__name__)

# Category options for dropdowns (sorted by group then label)
_CATEGORY_OPTIONS = ['unmapped'] + sorted(
    [k for k in CASHFLOW_CATEGORIES if k != 'unmapped'],
    key=lambda k: (CASHFLOW_CATEGORIES[k]['group'], CASHFLOW_CATEGORIES[k]['label']),
)

_CATEGORY_LABELS = {k: CASHFLOW_CATEGORIES[k]['label'] for k in CASHFLOW_CATEGORIES}
_CATEGORY_LABELS['unmapped'] = 'Unmapped'


def render(ctx):
    """Render the transaction mapping page."""
    st.markdown('## Transaction Category Mapping')
    st.caption(
        'Map bank transaction patterns to categories. '
        'Each unique pattern only needs to be mapped once -- '
        'all future imports will auto-classify.'
    )

    # Stats bar
    try:
        with get_db() as conn:
            stats = get_mapping_stats(conn)

        cols = st.columns(4)
        cols[0].metric('Total Transactions', f"{stats['total_tx_count']:,}")
        cols[1].metric('Unique Patterns', f"{stats['unique_patterns']:,}")
        cols[2].metric('Mapped Patterns', f"{stats['mapped_patterns']:,}")
        cols[3].metric(
            'Unmapped Transactions',
            f"{stats['unmapped_tx_count']:,}",
            delta=(
                f"-{stats['total_tx_count'] - stats['unmapped_tx_count']:,} classified"
                if stats['total_tx_count'] > 0 else None
            ),
            delta_color='normal',
        )
    except Exception as e:
        log.warning('Could not load mapping stats: %s', e)
        st.warning(f'Could not load mapping stats: {e}')
        stats = {}

    st.markdown('---')

    # Filter controls
    filter_col1, filter_col2 = st.columns([1, 1])
    with filter_col1:
        show_filter = st.segmented_control(
            'Show',
            ['Unmapped Only', 'All Patterns'],
            default='Unmapped Only',
            key='txmap_filter',
        )
    with filter_col2:
        direction_filter = st.segmented_control(
            'Direction',
            ['All', 'Credits', 'Debits'],
            default='All',
            key='txmap_direction',
        )

    # Load patterns
    try:
        with get_db() as conn:
            if show_filter == 'Unmapped Only':
                patterns_df = read_sql("""
                    SELECT
                        LOWER(COALESCE(summary, '')) as raw_pattern,
                        MIN(summary) as example,
                        COUNT(*) as tx_count,
                        direction,
                        SUM(amount) as total_amount,
                        MIN(tx_date) as first_seen,
                        MAX(tx_date) as last_seen,
                        MAX(category) as current_category
                    FROM cashflow_transactions
                    WHERE (category = 'unmapped' OR category IS NULL)
                    GROUP BY LOWER(COALESCE(summary, '')), direction
                    ORDER BY tx_count DESC
                """, conn)
            else:
                patterns_df = read_sql("""
                    SELECT
                        LOWER(COALESCE(summary, '')) as raw_pattern,
                        MIN(summary) as example,
                        COUNT(*) as tx_count,
                        direction,
                        SUM(amount) as total_amount,
                        MIN(tx_date) as first_seen,
                        MAX(tx_date) as last_seen,
                        MAX(category) as current_category
                    FROM cashflow_transactions
                    GROUP BY LOWER(COALESCE(summary, '')), direction
                    ORDER BY tx_count DESC
                """, conn)

            # Also load existing mappings
            existing_mappings = {}
            mapping_rows = conn.execute(
                'SELECT match_pattern, category, subcategory, is_transfer, is_duplicate '
                'FROM category_mappings'
            ).fetchall()
            for r in mapping_rows:
                existing_mappings[r['match_pattern']] = {
                    'category': r['category'],
                    'subcategory': r['subcategory'],
                    'is_transfer': r['is_transfer'],
                    'is_duplicate': r['is_duplicate'],
                }
    except Exception as e:
        log.warning('Could not load transaction patterns: %s', e)
        st.error(f'Could not load transaction patterns: {e}')
        st.info('Upload bank transactions first from the Cash Flow page.')
        return

    if patterns_df.empty:
        st.info(
            'No transaction patterns found. '
            'Upload bank transactions from the Cash Flow page first.'
        )
        return

    # Apply direction filter
    if direction_filter == 'Credits':
        patterns_df = patterns_df[patterns_df['direction'] == 'credit']
    elif direction_filter == 'Debits':
        patterns_df = patterns_df[patterns_df['direction'] == 'debit']

    # Normalize patterns and add suggestions
    patterns_df['normalized'] = patterns_df['example'].apply(
        lambda x: normalize_summary(x) if pd.notna(x) else ''
    )

    st.markdown(f'**{len(patterns_df)} patterns** to review')

    # Mapping form
    with st.form('mapping_form'):
        mapping_updates = []

        for idx, row in patterns_df.iterrows():
            normalized = row['normalized']
            example = row.get('example', '') or ''
            tx_count = int(row.get('tx_count', 0))
            total = float(row.get('total_amount', 0) or 0)
            direction = row.get('direction', '') or ''
            current = row.get('current_category', 'unmapped') or 'unmapped'

            # Check existing mapping
            existing = existing_mappings.get(normalized, {})
            if existing:
                default_cat = existing.get('category', current)
            else:
                # Use suggestion
                suggested, _suggested_sub = suggest_category(normalized)
                default_cat = suggested

            # Ensure default is in options
            if default_cat not in _CATEGORY_OPTIONS:
                default_cat = 'unmapped'
            default_idx = _CATEGORY_OPTIONS.index(default_cat)

            # Row display
            row_cols = st.columns([3, 1, 1, 2])

            # Pattern info
            dir_icon = '>' if direction == 'credit' else '<'
            row_cols[0].markdown(
                f'<div style="font-size:0.8rem;">'
                f'<b>{dir_icon} {example[:80]}</b><br/>'
                f'<span style="color:#94a3b8;">Pattern: {normalized[:60]}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Count + total
            row_cols[1].markdown(
                f'<div style="font-size:0.8rem;text-align:center;">'
                f'<b>{tx_count}</b> txns<br/>'
                f'${total:,.0f}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Date range
            first = str(row.get('first_seen', '') or '')[:10]
            last = str(row.get('last_seen', '') or '')[:10]
            row_cols[2].markdown(
                f'<div style="font-size:0.75rem;color:#94a3b8;">'
                f'{first}<br/>to {last}'
                f'</div>',
                unsafe_allow_html=True,
            )

            # Category dropdown
            selected = row_cols[3].selectbox(
                'Category',
                _CATEGORY_OPTIONS,
                index=default_idx,
                format_func=lambda x: _CATEGORY_LABELS.get(x, x),
                key=f'txmap_cat_{idx}',
                label_visibility='collapsed',
            )

            mapping_updates.append({
                'normalized': normalized,
                'example': example,
                'category': selected,
            })

        # Submit
        save_clicked = st.form_submit_button(
            'Save All Mappings', type='primary', use_container_width=True,
        )

    # Handle save outside form
    if save_clicked and mapping_updates:
        saved = 0
        try:
            with get_db() as conn:
                for update in mapping_updates:
                    if update['category'] != 'unmapped' and update['normalized']:
                        is_transfer = 1 if update['category'] == 'internal_transfer' else 0
                        is_dup = 1 if update['category'] == 'duplicate' else 0
                        upsert_category_mapping(
                            conn,
                            match_pattern=update['normalized'],
                            category=update['category'],
                            raw_example=update['example'],
                            is_transfer=is_transfer,
                            is_duplicate=is_dup,
                        )
                        saved += 1
        except Exception as e:
            log.error('Failed to save mappings: %s', e)
            st.error(f'Failed to save mappings: {e}')

        if saved > 0:
            st.success(f'Saved {saved} mappings!')
            # Reclassify transactions
            try:
                with st.spinner('Re-classifying transactions...'):
                    with get_db() as conn:
                        count = reclassify_all_transactions(conn)
                st.success(f'Updated {count} transactions with new categories.')
            except Exception as e:
                log.error('Reclassification failed: %s', e)
                st.warning(f'Mappings saved but reclassification failed: {e}')
            st.rerun()
        else:
            st.info('No new mappings to save (all still set to "Unmapped").')

    # Reclassify button (outside form)
    if st.button('Re-classify All Transactions', key='txmap_reclassify'):
        try:
            with st.spinner('Re-classifying all transactions...'):
                with get_db() as conn:
                    count = reclassify_all_transactions(conn)
            st.success(f'Updated {count} transactions.')
        except Exception as e:
            log.error('Reclassification failed: %s', e)
            st.error(f'Re-classification failed: {e}')
        st.rerun()
