"""
Mock data generator: creates 12 months of realistic sales data across ~20 SKUs.
Patterns include seasonality, new product launches, declining SKUs, and repeat customers.
Customers are spread across 12 months to produce meaningful cohort retention data.
"""
import random
import math
from datetime import datetime, timedelta
from db import (
    init_db, get_db, upsert_customer, upsert_order,
    upsert_order_item, upsert_sku, rebuild_daily_sales,
    upsert_media_spend,
)

random.seed(42)

# --- SKU Catalog ---
SKUS = [
    # (sku, name, category, launch_offset_days, base_daily_units, trend, price)
    # trend: "steady", "growing", "declining", "seasonal", "new"
    ("WB-CLASSIC-BLK", "Classic Water Bottle Black", "Water Bottles", 365, 8, "steady", 24.99),
    ("WB-CLASSIC-WHT", "Classic Water Bottle White", "Water Bottles", 365, 6, "steady", 24.99),
    ("WB-SPORT-BLU", "Sport Water Bottle Blue", "Water Bottles", 365, 10, "growing", 29.99),
    ("WB-SPORT-RED", "Sport Water Bottle Red", "Water Bottles", 365, 5, "declining", 29.99),
    ("WB-KIDS-PNK", "Kids Water Bottle Pink", "Water Bottles", 200, 4, "growing", 19.99),
    ("WB-KIDS-GRN", "Kids Water Bottle Green", "Water Bottles", 200, 3, "steady", 19.99),
    ("WB-PREMIUM-SS", "Premium Stainless Steel 32oz", "Water Bottles", 365, 3, "steady", 44.99),
    ("TM-CLASSIC-BLK", "Classic Travel Mug Black", "Travel Mugs", 365, 7, "steady", 27.99),
    ("TM-CLASSIC-WHT", "Classic Travel Mug White", "Travel Mugs", 365, 5, "declining", 27.99),
    ("TM-CERAMIC-GRY", "Ceramic Travel Mug Grey", "Travel Mugs", 300, 4, "growing", 34.99),
    ("LB-TOTE-NAV", "Lunch Tote Navy", "Lunch Bags", 365, 6, "seasonal", 22.99),
    ("LB-TOTE-BLK", "Lunch Tote Black", "Lunch Bags", 365, 5, "seasonal", 22.99),
    ("LB-INSULATED-L", "Insulated Lunch Bag Large", "Lunch Bags", 365, 4, "steady", 32.99),
    ("ST-METAL-10PK", "Metal Straws 10-Pack", "Accessories", 365, 12, "growing", 14.99),
    ("ST-SILICONE-6PK", "Silicone Straws 6-Pack", "Accessories", 250, 8, "steady", 11.99),
    ("LID-SPORT", "Sport Lid Replacement", "Accessories", 365, 15, "steady", 9.99),
    ("LID-STRAW", "Straw Lid Replacement", "Accessories", 300, 10, "growing", 12.99),
    ("CB-SLEEVE-S", "Bottle Sleeve Small", "Accessories", 180, 3, "new", 16.99),
    ("CB-SLEEVE-L", "Bottle Sleeve Large", "Accessories", 90, 2, "new", 18.99),
    ("GIFT-SET-PRO", "Pro Gift Set Bundle", "Bundles", 150, 2, "seasonal", 79.99),
]

# --- Customer pool ---
CUSTOMER_POOL_SIZE = 500
REPEAT_PURCHASE_RATE = 0.25  # 25% of customers will buy again

# Monthly new customer targets (tapering from high early growth)
# Total = 500 across 12 months
MONTHLY_NEW_CUSTOMERS = [80, 65, 55, 45, 40, 35, 35, 30, 30, 30, 28, 27]


def generate_customers():
    """Generate a pool of customer IDs and emails, assigned to monthly cohorts."""
    customers = []
    idx = 0
    for month_idx, count in enumerate(MONTHLY_NEW_CUSTOMERS):
        for _ in range(count):
            if idx >= CUSTOMER_POOL_SIZE:
                break
            source = random.choice(["amazon", "shopify"])
            cid = f"{source[:3]}-cust-{idx:04d}"
            email = f"customer{idx}@example.com" if source == "shopify" else None
            customers.append({
                "id": cid,
                "email": email,
                "source": source,
                "order_count": 0,
                "cohort_month": month_idx,  # 0-indexed month when they first purchase
            })
            idx += 1
    return customers


def daily_demand(sku_info, day_offset, total_days):
    """Calculate expected demand for a SKU on a given day."""
    _, _, _, launch_offset, base, trend, _ = sku_info

    if day_offset < (total_days - launch_offset):
        return 0  # SKU hasn't launched yet

    days_since_launch = day_offset - (total_days - launch_offset)
    demand = base

    # Apply trend
    if trend == "growing":
        demand *= 1 + (days_since_launch / total_days) * 0.8
    elif trend == "declining":
        demand *= max(0.3, 1 - (days_since_launch / total_days) * 0.6)
    elif trend == "new":
        # Ramp up then stabilize
        ramp = min(1.0, days_since_launch / 60)
        demand *= ramp * 1.5
    elif trend == "seasonal":
        # Back-to-school bump (Aug-Sep) and holiday bump (Nov-Dec)
        month = (datetime(2025, 2, 1) + timedelta(days=day_offset)).month
        if month in (8, 9):
            demand *= 2.0
        elif month in (11, 12):
            demand *= 2.5
        elif month in (6, 7):
            demand *= 1.3

    # Day-of-week effect (weekends slightly lower)
    dow = (datetime(2025, 2, 1) + timedelta(days=day_offset)).weekday()
    if dow >= 5:
        demand *= 0.7

    # Add noise
    demand = max(0, demand + random.gauss(0, demand * 0.3))

    return int(round(demand))


def generate_mock_media_spend(conn):
    """Generate 12 months of historical spend + 6 months of future planned spend."""
    start_date = datetime(2025, 2, 1)

    # Seasonal multipliers for spend (higher in Q4, back-to-school)
    seasonal = {
        2: 1.0, 3: 1.0, 4: 0.9, 5: 0.9, 6: 1.1, 7: 1.2,
        8: 1.4, 9: 1.3, 10: 1.1, 11: 1.5, 12: 1.8, 1: 0.8,
    }

    base_spend = 5000.0
    base_roas = 2.5

    for i in range(18):  # 12 historical + 6 future
        dt = start_date + timedelta(days=i * 30)
        month_str = dt.strftime("%Y-%m")
        month_num = dt.month
        mult = seasonal.get(month_num, 1.0)

        spend = round(base_spend * mult + random.gauss(0, 300), 2)
        roas = round(base_roas + random.gauss(0, 0.3), 2)
        roas = max(1.0, roas)

        upsert_media_spend(conn, month_str, spend, roas)


def generate_mock_data():
    """Generate 12 months of mock sales data and insert into DB."""
    init_db()

    # Clear existing data so cohorts don't collapse on re-run
    with get_db() as conn:
        for table in ["daily_sku_sales", "order_items", "orders", "customers", "sku_master", "media_spend", "sync_log"]:
            conn.execute(f"DELETE FROM {table}")

    start_date = datetime(2025, 2, 1)
    end_date = datetime(2026, 2, 1)
    total_days = (end_date - start_date).days

    customers = generate_customers()
    # Build a mapping: month_index -> list of new customers for that month
    monthly_cohorts = {}
    for c in customers:
        m = c["cohort_month"]
        monthly_cohorts.setdefault(m, []).append(c)

    # Track which customers are available (already acquired) and repeat pool
    available_customers = []
    repeat_pool = []
    order_counter = 0

    print(f"Generating {total_days} days of mock data for {len(SKUS)} SKUs...")

    with get_db() as conn:
        for day_offset in range(total_days):
            current_date = start_date + timedelta(days=day_offset)
            date_str = current_date.strftime("%Y-%m-%d")

            # Check if we're in a new month — release that month's cohort
            current_month_idx = (current_date.year - start_date.year) * 12 + (current_date.month - start_date.month)
            # On the first day of each month, add the new cohort to available pool
            if current_date.day <= 1 or day_offset == 0:
                new_cohort = monthly_cohorts.get(current_month_idx, [])
                available_customers.extend(new_cohort)

            # Track which new customers haven't ordered yet this month
            new_this_month = [c for c in available_customers if c["order_count"] == 0]

            for sku_info in SKUS:
                sku, name, category, launch_offset, base, trend, price = sku_info
                units = daily_demand(sku_info, day_offset, total_days)

                if units <= 0:
                    continue

                # Split units across individual orders (1-3 units per order typically)
                remaining = units
                while remaining > 0:
                    qty = min(remaining, random.choices([1, 2, 3], weights=[0.7, 0.2, 0.1])[0])
                    remaining -= qty

                    # Pick customer: repeat or new
                    if repeat_pool and random.random() < REPEAT_PURCHASE_RATE:
                        cust = random.choice(repeat_pool)
                    elif new_this_month:
                        cust = new_this_month.pop(random.randrange(len(new_this_month)))
                    elif repeat_pool:
                        # All new customers for this month used up; reuse repeat customers
                        cust = random.choice(repeat_pool)
                    elif available_customers:
                        cust = random.choice(available_customers)
                    else:
                        continue  # Skip order if no customers available yet

                    source = cust["source"]
                    order_counter += 1
                    order_id = f"{source[:3]}-ord-{order_counter:06d}"

                    # Insert customer
                    upsert_customer(conn, cust["id"], cust["email"], source, date_str)

                    # Insert order
                    total = round(qty * price, 2)
                    upsert_order(conn, order_id, source, order_id, cust["id"], date_str, total)

                    # Insert order item
                    upsert_order_item(conn, order_id, sku, name, qty, price)

                    # Insert/update SKU master
                    upsert_sku(conn, sku, name, category, date_str, source)

                    # Add to repeat pool
                    cust["order_count"] += 1
                    if cust not in repeat_pool:
                        repeat_pool.append(cust)

            if day_offset % 30 == 0:
                print(f"  Generated through {date_str}...")

        # Rebuild daily aggregates
        print("Rebuilding daily sales aggregates...")
        rebuild_daily_sales(conn)

        # Generate media spend data
        print("Generating media spend data...")
        generate_mock_media_spend(conn)

    print(f"Done! Generated {order_counter} orders across {total_days} days.")
    return order_counter


if __name__ == "__main__":
    count = generate_mock_data()
    print(f"\nMock data generation complete: {count} total orders")
