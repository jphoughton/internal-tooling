"""
Reorder timing engine.

Combines the waterfall SKU demand forecast with live Packiyo 3PL inventory
to produce an actionable reorder schedule.

Key assumptions (configurable):
- Lead time: 12 weeks (84 days) for all SKUs
- MOQ: 5,000 units per SKU per order
- Safety stock: 2 weeks of demand buffer beyond lead time

The engine answers: "When do I need to place each order so I don't stock out?"
"""
import math
import pandas as pd
from datetime import datetime, timedelta
from analytics.sku_flavors import get_flavor

# --- Defaults ---
LEAD_TIME_WEEKS = 12
LEAD_TIME_DAYS = LEAD_TIME_WEEKS * 7  # 84 days
MOQ_UNITS = 5_000
SAFETY_STOCK_WEEKS = 2  # extra buffer


def _daily_demand_from_monthly(sku_monthly: dict) -> list:
    """
    Convert {month_str: units} into a day-by-day demand list starting today.

    Each month's units are spread evenly across the days in that month.
    Returns list of (date, daily_units) tuples sorted chronologically.
    """
    today = datetime.utcnow().date()
    daily = []

    for month_str, units in sorted(sku_monthly.items()):
        dt = datetime.strptime(month_str, "%Y-%m")
        # Days in this month
        if dt.month == 12:
            next_month = dt.replace(year=dt.year + 1, month=1)
        else:
            next_month = dt.replace(month=dt.month + 1)
        days_in_month = (next_month - dt).days

        daily_rate = units / days_in_month if days_in_month > 0 else 0

        for d in range(days_in_month):
            day = (dt + timedelta(days=d)).date()
            if day >= today:
                daily.append((day, daily_rate))

    return daily


def _planned_arrivals_by_date(planned_inbound_sku):
    """
    Convert {month_str: units} into a dict {date: units} with arrivals
    landing on the first day of each month.
    """
    arrivals = {}
    for month_str, units in (planned_inbound_sku or {}).items():
        if units <= 0:
            continue
        try:
            dt = datetime.strptime(month_str, "%Y-%m").date()
        except ValueError:
            continue
        arrivals[dt] = arrivals.get(dt, 0) + units
    return arrivals


def _compute_stockout_date(current_stock, daily_demand, planned_arrivals=None):
    """
    Walk forward day-by-day, decrementing stock by daily demand and adding
    planned arrivals (inbound orders the user has already placed).
    Returns the date stock hits zero, or None if it never does within the forecast.
    """
    remaining = float(current_stock)
    arrivals = planned_arrivals or {}
    for day, demand in daily_demand:
        remaining += arrivals.get(day, 0)
        remaining -= demand
        if remaining <= 0:
            return day
    return None  # stock lasts beyond the forecast horizon


def build_reorder_plan(sku_forecast_table, inventory_data, lead_time_weeks=None,
                       moq=None, safety_weeks=None, forecast_skus=None,
                       planned_inbound=None):
    """
    Build a complete reorder plan for all SKUs.

    Args:
        sku_forecast_table: DataFrame from build_sku_forecast_table
            columns: SKU, Flavor, month1, month2, ...
        inventory_data: list of dicts from Packiyo get_inventory()
            keys: sku, name, quantity_available, quantity_on_hand, quantity_inbound, ...
        lead_time_weeks: weeks from order to arrival (default 12)
        moq: minimum order quantity per SKU (default 5000)
        safety_weeks: extra buffer weeks (default 2)
        forecast_skus: set of SKU strings to include (or None for all)
        planned_inbound: dict {sku: {month: units}} — orders the user has
            already placed. These are added to inventory at the start of
            each month, pushing out the stockout date.

    Returns:
        DataFrame with one row per SKU:
            SKU, Flavor, Current Stock, Inbound, Monthly Demand,
            Stockout Date, Reorder By, Days Until Reorder, Order Qty, Urgency
        list of reorder event dicts (for the timeline chart)
    """
    lt_weeks = lead_time_weeks or LEAD_TIME_WEEKS
    lt_days = lt_weeks * 7
    moq_val = moq or MOQ_UNITS
    safety_wk = safety_weeks or SAFETY_STOCK_WEEKS

    if sku_forecast_table is None or sku_forecast_table.empty:
        return pd.DataFrame(), []

    # Build inventory lookup (supports combined 3PL + FBA data)
    inv_lookup = {}
    for item in (inventory_data or []):
        inv_lookup[item["sku"]] = {
            "available": item.get("quantity_available", 0) or 0,
            "on_hand": item.get("quantity_on_hand", 0) or 0,
            "inbound": item.get("quantity_inbound", 0) or 0,
            "name": item.get("name", ""),
            "3pl_available": item.get("3pl_available", item.get("quantity_available", 0) or 0),
            "fba_fulfillable": item.get("fba_fulfillable", 0) or 0,
        }

    today = datetime.utcnow().date()
    rows = []
    all_events = []

    # Month columns in the forecast table
    meta_cols = {"SKU", "Flavor", "Variant"}
    month_cols = [c for c in sku_forecast_table.columns if c not in meta_cols]

    for _, fc_row in sku_forecast_table.iterrows():
        sku = fc_row["SKU"]

        if forecast_skus and sku not in forecast_skus:
            continue

        # Monthly demand dict
        monthly_demand = {}
        for m in month_cols:
            val = fc_row[m]
            if pd.notna(val) and val > 0:
                monthly_demand[m] = float(val)

        if not monthly_demand:
            continue

        # Current inventory
        inv = inv_lookup.get(sku, {})
        current_stock = inv.get("available", 0)
        inbound = inv.get("inbound", 0)
        effective_stock = current_stock + inbound

        # Average monthly demand
        avg_monthly = sum(monthly_demand.values()) / len(monthly_demand) if monthly_demand else 0

        # Daily demand projection
        daily = _daily_demand_from_monthly(monthly_demand)
        if not daily:
            continue

        # Planned inbound arrivals for this SKU
        sku_planned = (planned_inbound or {}).get(sku, {})
        arrivals = _planned_arrivals_by_date(sku_planned)
        planned_total = sum(sku_planned.values()) if sku_planned else 0

        # Stockout date (including planned arrivals)
        stockout_date = _compute_stockout_date(effective_stock, daily, arrivals)

        # Reorder-by date: stockout minus lead time
        if stockout_date:
            reorder_by = stockout_date - timedelta(days=lt_days)
            days_until_reorder = (reorder_by - today).days
        else:
            reorder_by = None
            days_until_reorder = 999  # won't stock out in forecast

        # Order quantity calculation
        # Cover lead_time + safety period demand, rounded up to MOQ
        cover_days = lt_days + (safety_wk * 7)
        cover_demand = sum(d for _, d in daily[:cover_days])
        if cover_demand > 0:
            order_qty = max(moq_val, math.ceil(cover_demand / moq_val) * moq_val)
        else:
            order_qty = moq_val

        # How many months will the order last?
        months_of_cover = order_qty / avg_monthly if avg_monthly > 0 else 0

        # Urgency classification
        # If planned inbound covers the gap, mark as EN ROUTE
        if planned_total > 0 and (stockout_date is None or days_until_reorder > 0):
            urgency = "EN ROUTE"
        elif planned_total > 0 and days_until_reorder <= 0:
            # Planned inbound exists but still stocking out before it arrives
            urgency = "EN ROUTE ⚠️"
        elif days_until_reorder < 0:
            urgency = "OVERDUE"
        elif days_until_reorder <= 7:
            urgency = "ORDER NOW"
        elif days_until_reorder <= 21:
            urgency = "ORDER SOON"
        elif days_until_reorder <= 42:
            urgency = "UPCOMING"
        else:
            urgency = "OK"

        flavor = get_flavor(sku, inv.get("name", ""))

        rows.append({
            "SKU": sku,
            "Flavor": flavor,
            "3PL Stock": inv.get("3pl_available", current_stock),
            "FBA Stock": inv.get("fba_fulfillable", 0),
            "Total Stock": current_stock,
            "Inbound": inbound,
            "Planned Inbound": planned_total,
            "Monthly Demand": round(avg_monthly),
            "Stockout Date": stockout_date.strftime("%Y-%m-%d") if stockout_date else "Beyond Forecast",
            "Reorder By": reorder_by.strftime("%Y-%m-%d") if reorder_by else "—",
            "Days Until Reorder": days_until_reorder if days_until_reorder < 999 else None,
            "Order Qty": order_qty,
            "Months of Cover": round(months_of_cover, 1),
            "Urgency": urgency,
        })

        # Build reorder events for timeline
        if stockout_date and reorder_by:
            all_events.append({
                "sku": sku,
                "flavor": flavor,
                "reorder_by": reorder_by,
                "arrival_date": reorder_by + timedelta(days=lt_days),
                "stockout_date": stockout_date,
                "order_qty": order_qty,
                "urgency": urgency,
                "current_stock": current_stock,
                "3pl_stock": inv.get("3pl_available", current_stock),
                "fba_stock": inv.get("fba_fulfillable", 0),
                "monthly_demand": round(avg_monthly),
            })

    result_df = pd.DataFrame(rows)
    if not result_df.empty:
        # Sort: most urgent first, then by best-seller rank within same urgency
        from analytics.sku_flavors import get_sku_sales_rank
        _rank = get_sku_sales_rank()
        _max_rank = len(_rank)
        urgency_order = {"OVERDUE": 0, "EN ROUTE ⚠️": 1, "ORDER NOW": 2, "ORDER SOON": 3, "EN ROUTE": 4, "UPCOMING": 5, "OK": 6}
        result_df["_urgency_sort"] = result_df["Urgency"].map(urgency_order).fillna(5)
        result_df["_sales_rank"] = result_df["SKU"].map(lambda s: _rank.get(s, _max_rank))
        result_df = result_df.sort_values(["_urgency_sort", "_sales_rank"]).drop(
            columns=["_urgency_sort", "_sales_rank"]
        ).reset_index(drop=True)

    return result_df, all_events


def build_inventory_runway_chart(sku, monthly_demand, current_stock, inbound,
                                  lead_time_weeks=None, moq=None, safety_weeks=None,
                                  planned_inbound_sku=None):
    """
    Build day-by-day inventory projection data for a single SKU.
    Shows current trajectory and what happens with a reorder.

    Args:
        planned_inbound_sku: dict {month_str: units} — already-ordered
            inbound that the user has recorded. Factored into both
            trajectories.

    Returns:
        DataFrame with columns: date, inventory_no_order, inventory_with_order
        dict with reorder info (order_date, arrival_date, qty)
    """
    lt_weeks = lead_time_weeks or LEAD_TIME_WEEKS
    lt_days = lt_weeks * 7
    moq_val = moq or MOQ_UNITS
    safety_wk = safety_weeks or SAFETY_STOCK_WEEKS

    effective_stock = (current_stock or 0) + (inbound or 0)
    daily = _daily_demand_from_monthly(monthly_demand)
    arrivals = _planned_arrivals_by_date(planned_inbound_sku)

    if not daily:
        return pd.DataFrame(), {}

    today = datetime.utcnow().date()

    # No-order trajectory (includes planned inbound from user)
    no_order = []
    inv = float(effective_stock)
    for day, demand in daily:
        inv += arrivals.get(day, 0)
        inv -= demand
        no_order.append({"date": day, "inventory_no_order": max(round(inv), 0)})

    # With-order trajectory — place order today (also includes planned inbound)
    cover_days = lt_days + (safety_wk * 7)
    cover_demand = sum(d for _, d in daily[:cover_days])
    order_qty = max(moq_val, math.ceil(cover_demand / moq_val) * moq_val)
    arrival_date = today + timedelta(days=lt_days)

    with_order = []
    inv = float(effective_stock)
    for day, demand in daily:
        inv += arrivals.get(day, 0)
        if day == arrival_date:
            inv += order_qty
        inv -= demand
        with_order.append({"date": day, "inventory_with_order": max(round(inv), 0)})

    # Merge
    df_no = pd.DataFrame(no_order)
    df_with = pd.DataFrame(with_order)
    if df_no.empty:
        return pd.DataFrame(), {}

    result = df_no.merge(df_with, on="date", how="outer").sort_values("date").reset_index(drop=True)

    reorder_info = {
        "order_date": today,
        "arrival_date": arrival_date,
        "order_qty": order_qty,
    }

    return result, reorder_info


def build_reorder_recommendations(proj_df, demand_df, month_cols,
                                   current_month, lead_time_weeks=12,
                                   moq_units=5000, safety_buffer_weeks=2):
    """Build a month-by-month reorder calendar from projected inventory.

    Walks each SKU's EOM inventory (from *proj_df*, which already includes
    planned inbound), detects months where inventory breaches the safety
    floor, works backwards by lead time to determine the order month, sizes
    the order to cover through the next reorder cycle, and repeats.

    Args:
        proj_df: DataFrame with columns SKU, Flavor, Total Stock, and one
            column per month (month_cols) containing EOM projected inventory.
        demand_df: Rollup demand DataFrame (from build_master_dtc_forecast)
            with columns SKU and the same month_cols containing monthly demand.
        month_cols: List of month strings like '2026-03', '2026-04', …
        current_month: Current month string ('2026-03').
        lead_time_weeks: Weeks from placing order to arrival (default 12).
        moq_units: Minimum order quantity (default 5000).
        safety_buffer_weeks: Extra buffer above zero (default 2).

    Returns:
        (rec_df, events_list) where rec_df has columns:
            SKU, Flavor, Status, *month_cols, Total
        and events_list is a list of order event dicts.
    """
    if proj_df is None or proj_df.empty or not month_cols:
        return pd.DataFrame(), []

    lead_time_months = math.ceil(lead_time_weeks * 7 / 30.44)

    from analytics.sku_flavors import get_sku_sales_rank
    _rank = get_sku_sales_rank()
    _max_rank = len(_rank)

    rows = []
    events = []

    for _, p_row in proj_df.iterrows():
        sku = p_row['SKU']
        flavor = p_row.get('Flavor', '')

        # Get monthly demand for this SKU
        d_row = demand_df[demand_df['SKU'] == sku]
        monthly_demand = {}
        for m in month_cols:
            if not d_row.empty and m in d_row.columns:
                monthly_demand[m] = float(d_row.iloc[0][m] or 0)
            else:
                monthly_demand[m] = 0

        # Build mutable EOM inventory array (from proj_df, already has planned inbound)
        eom = {m: float(p_row[m]) for m in month_cols}

        # Track orders placed per month
        orders = {m: 0 for m in month_cols}

        # Iterative breach detection
        # scan_from advances past unfixable breaches to prevent infinite loops
        scan_from = 0
        for _iteration in range(len(month_cols)):
            # Find first breach at or after scan_from: EOM < safety floor
            breach_idx = None
            for i in range(scan_from, len(month_cols)):
                m = month_cols[i]
                demand_m = monthly_demand.get(m, 0)
                safety_floor = demand_m * (safety_buffer_weeks / 4.33)
                if eom[m] < safety_floor:
                    breach_idx = i
                    break

            if breach_idx is None:
                break  # No more breaches

            breach_month = month_cols[breach_idx]

            # Determine order month (breach - lead_time_months, clamped to index 0)
            order_idx = max(0, breach_idx - lead_time_months)
            order_month = month_cols[order_idx]

            # Arrival month = order month + lead_time_months (clamped to last month)
            arrival_idx = min(order_idx + lead_time_months, len(month_cols) - 1)
            arrival_month = month_cols[arrival_idx]

            # If arrival is after breach, this breach is unfixable (lead time too
            # long).  Still place one order for post-arrival coverage, but advance
            # scan_from past the breach so we don't loop on it forever.
            if arrival_idx > breach_idx:
                scan_from = arrival_idx  # skip to when the order actually lands

            # Size the order: cover demand from arrival through lead_time_months + 1
            # additional months, minus inventory at arrival, plus safety target.
            coverage_end_idx = min(arrival_idx + lead_time_months + 1, len(month_cols))
            coverage_demand = sum(monthly_demand.get(month_cols[j], 0)
                                  for j in range(arrival_idx, coverage_end_idx))

            # Inventory just before arrival (EOM of month before arrival, or Total Stock if arrival is first month)
            if arrival_idx > 0:
                inv_at_arrival = eom[month_cols[arrival_idx - 1]]
            else:
                inv_at_arrival = float(p_row.get('Total Stock', 0))

            # Safety target for the coverage period
            avg_monthly_demand = coverage_demand / max(coverage_end_idx - arrival_idx, 1)
            safety_target = avg_monthly_demand * (safety_buffer_weeks / 4.33)

            needed = coverage_demand + safety_target - max(inv_at_arrival, 0)
            needed = max(needed, 0)

            if needed <= 0:
                # Breach exists but no additional stock needed (e.g. inv_at_arrival
                # is enough to cover) — advance past it and keep scanning.
                scan_from = max(scan_from, breach_idx + 1)
                continue

            # Round up to MOQ
            order_qty = math.ceil(needed / moq_units) * moq_units

            # Record order
            orders[order_month] += order_qty

            # Update virtual EOM from arrival month onward
            for j in range(arrival_idx, len(month_cols)):
                eom[month_cols[j]] += order_qty

            events.append({
                'sku': sku,
                'flavor': flavor,
                'order_month': order_month,
                'arrival_month': arrival_month,
                'breach_month': breach_month,
                'order_qty': order_qty,
            })

        # Determine status based on first order month
        first_order_months = [m for m in month_cols if orders[m] > 0]
        total_ordered = sum(orders.values())

        if not first_order_months:
            status = 'OK'
        else:
            # How many months from current_month to first order?
            try:
                current_idx = month_cols.index(current_month)
            except ValueError:
                current_idx = 0
            try:
                first_idx = month_cols.index(first_order_months[0])
            except ValueError:
                first_idx = 0
            months_away = first_idx - current_idx

            if months_away <= 0:
                status = 'ORDER NOW'
            elif months_away == 1:
                status = 'ORDER SOON'
            elif months_away <= 3:
                status = 'UPCOMING'
            else:
                status = 'PLANNED'

        row = {'SKU': sku, 'Flavor': flavor, 'Status': status}
        for m in month_cols:
            row[m] = orders[m]
        row['Total'] = total_ordered
        rows.append(row)

    rec_df = pd.DataFrame(rows)
    if not rec_df.empty:
        # Sort: most urgent first, then by best-seller rank
        urgency_order = {'ORDER NOW': 0, 'ORDER SOON': 1, 'UPCOMING': 2, 'PLANNED': 3, 'OK': 4}
        rec_df['_urgency_sort'] = rec_df['Status'].map(urgency_order).fillna(4)
        rec_df['_sales_rank'] = rec_df['SKU'].map(lambda s: _rank.get(s, _max_rank))
        rec_df = rec_df.sort_values(['_urgency_sort', '_sales_rank']).drop(
            columns=['_urgency_sort', '_sales_rank']
        ).reset_index(drop=True)

    return rec_df, events
