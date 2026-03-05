"""Parse Robo3PL invoice PDFs and extract structured data."""
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)


def parse_robo3pl_invoice(pdf_bytes, filename=''):
    """Extract invoice data from a Robo3PL PDF.

    Args:
        pdf_bytes: Raw PDF file bytes.
        filename: Original filename — used to extract week range
                  (e.g. "Robo3PL - Hydrant - Invoice 1310 - 2_16_2026-2_22_2026.pdf").

    Returns:
        dict with keys: invoice_number, invoice_date, week_start, week_end,
                        total_amount, line_items (list of dicts).
    """
    import pdfplumber

    from io import BytesIO
    pdf = pdfplumber.open(BytesIO(pdf_bytes))
    text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    pdf.close()

    result = {
        'invoice_number': None,
        'invoice_date': None,
        'week_start': None,
        'week_end': None,
        'total_amount': 0.0,
        'line_items': [],
    }

    # --- Invoice number ---
    m = re.search(r'INVOICE\s*#\s*(\d+)', text)
    if m:
        result['invoice_number'] = m.group(1)

    # --- Invoice date ---
    m = re.search(r'DATE\s+(\d{1,2}/\d{1,2}/\d{4})', text)
    if m:
        result['invoice_date'] = datetime.strptime(m.group(1), '%m/%d/%Y').strftime('%Y-%m-%d')

    # --- Week range from filename ---
    # Pattern: M_D_YYYY-M_D_YYYY
    m = re.search(r'(\d{1,2}_\d{1,2}_\d{4})\s*-\s*(\d{1,2}_\d{1,2}_\d{4})', filename)
    if m:
        result['week_start'] = datetime.strptime(m.group(1), '%m_%d_%Y').strftime('%Y-%m-%d')
        result['week_end'] = datetime.strptime(m.group(2), '%m_%d_%Y').strftime('%Y-%m-%d')

    # --- Line items ---
    # Match lines like: "Fulfillment Services:Order Fee 584 0.50 292.00"
    # or "Amazon Case Pack Fulfillment 41 3.50 143.50"
    line_pattern = re.compile(
        r'^(.+?)\s+([\d,]+)\s+([\d,.]+)\s+([\d,.]+)$', re.MULTILINE
    )
    for m in line_pattern.finditer(text):
        activity = m.group(1).strip()
        # Skip header row
        if activity.upper().startswith('ACTIVITY'):
            continue
        qty_str = m.group(2).replace(',', '')
        rate_str = m.group(3).replace(',', '')
        amount_str = m.group(4).replace(',', '')
        try:
            qty = int(qty_str)
            rate = float(rate_str)
            amount = float(amount_str)
        except ValueError:
            continue
        result['line_items'].append({
            'activity': activity,
            'qty': qty,
            'rate': rate,
            'amount': amount,
        })

    # --- Total from PAYMENT line ---
    m = re.search(r'PAYMENT\s+([\d,.]+)', text)
    if m:
        result['total_amount'] = float(m.group(1).replace(',', ''))
    elif result['line_items']:
        result['total_amount'] = sum(li['amount'] for li in result['line_items'])

    log.info('Parsed invoice #%s: %s to %s, total $%.2f, %d line items',
             result['invoice_number'], result['week_start'], result['week_end'],
             result['total_amount'], len(result['line_items']))

    return result
