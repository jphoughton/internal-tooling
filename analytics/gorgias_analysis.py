"""
Analysis of Gorgias support conversations.

Two layers, per request:
  1. Local rules (free, offline): volume/trends, keyword topic buckets,
     lexicon sentiment + escalation flags, product/SKU mention counts.
  2. Claude (optional, deep): theme clustering + an executive summary over a
     sample of conversation transcripts.

Designed to run on the JSON produced by scripts/gorgias_download.py.
"""
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime
from html import unescape

logger = logging.getLogger(__name__)

# --- Topic buckets: ordered; first match wins per conversation -------------
TOPIC_RULES = [
    ("Shipping & Delivery", r"\b(ship|shipping|deliver|delivery|tracking|carrier|usps|ups|fedex|lost package|never arrived|where('?s| is) my order|wismo|stuck)\b"),
    ("Subscription Mgmt", r"\b(subscription|subscribe|recharge|skip|pause|cancel(l)?ed?|cancel my|renew|reschedule|frequency|next order|manage my)\b"),
    ("Refund / Billing", r"\b(refund|charge|charged|billing|double charged|money back|dispute|chargeback|invoice|payment failed)\b"),
    ("Damaged / Quality", r"\b(damaged|broke|broken|leak|leaking|melted|clump|clumpy|expired|defect|wrong (item|product|flavor)|missing (item|stick|sticks))\b"),
    ("Product Questions", r"\b(ingredient|sugar|caffeine|sweetener|allergen|vegan|gluten|how (do|to) (i|you) (use|drink|mix)|dosage|how much|nutrition|keto)\b"),
    ("Order Changes", r"\b(change my order|update address|wrong address|edit order|add to order|cancel order before|modify)\b"),
    ("Account / Login", r"\b(log ?in|password|reset|account|sign ?in|can'?t access|locked out)\b"),
    ("Wholesale / B2B", r"\b(wholesale|bulk|retail|reseller|distributor|stock your|carry your)\b"),
    ("Promo / Discount", r"\b(promo|coupon|discount code|code didn'?t work|sale|deal|gift card)\b"),
]

# --- Lightweight sentiment lexicon -----------------------------------------
NEG_WORDS = {
    "angry", "furious", "frustrated", "frustrating", "disappointed", "disappointing",
    "terrible", "horrible", "awful", "worst", "hate", "unacceptable", "ridiculous",
    "scam", "fraud", "never again", "cancel", "refund", "complaint", "lawyer",
    "bbb", "dispute", "chargeback", "useless", "garbage", "waste", "rude", "ignored",
    "still waiting", "no response", "third time", "again and again",
}
POS_WORDS = {
    "thank", "thanks", "appreciate", "love", "great", "awesome", "amazing",
    "perfect", "excellent", "happy", "wonderful", "fantastic", "best", "helpful",
}
ESCALATION_PATTERNS = [
    r"\b(lawyer|attorney|legal action|sue|lawsuit)\b",
    r"\b(bbb|better business bureau|fraud|scam)\b",
    r"\b(chargeback|dispute the charge|disputing)\b",
    r"\b(cancel (my|all) (subscription|account)|never (buy|order) again)\b",
    r"\b(third time|3rd time|multiple times|still (no|waiting|haven'?t))\b",
    r"\b(unacceptable|ridiculous|outrageous)\b",
]

# Hydrant product/flavor lexicon -> normalized label
PRODUCT_TERMS = {
    "Lemon Lime": r"\blemon ?lime\b",
    "Blood Orange": r"\bblood orange\b",
    "Grapefruit": r"\bgrapefruit\b",
    "Iced Lemon Tea": r"\biced lemon\b",
    "Cherry": r"\bcherry\b",
    "Strawberry": r"\bstrawberr\w*\b",
    "Watermelon": r"\bwatermelon\b",
    "Pineapple": r"\bpineapple\b",
    "Energy": r"\benergy\b",
    "Immunity": r"\bimmunity\b",
    "Sleep": r"\bsleep\b",
    "Caffeine/Coffee": r"\b(caffeine|coffee|latte)\b",
    "Variety Pack": r"\bvariety pack\b",
}


# Tags / subjects that mark non-human, system-generated tickets.
SYSTEM_TAGS = {"Mail Delivery System"}
SYSTEM_SUBJECT_PATTERNS = [
    r"^mail (system error|delivery)",
    r"returned mail",
    r"undeliverable",
    r"delivery (status notification|has failed)",
    r"out of office",
    r"automatic reply",
    r"error charge has (just )?been successfully processed",  # Recharge/Shopify auto-notice
]


def is_system_ticket(ticket):
    """True if the ticket is an automated bounce / spam / auto-reply, not a human convo."""
    if ticket.get("spam"):
        return True
    tags = {(x.get("name") if isinstance(x, dict) else x) for x in (ticket.get("tags") or [])}
    if tags & SYSTEM_TAGS:
        return True
    subj = (ticket.get("subject") or "").lower()
    return any(re.search(p, subj) for p in SYSTEM_SUBJECT_PATTERNS)


def filter_real_conversations(tickets):
    """Drop system/bounce/spam tickets. Returns (real, dropped_count)."""
    real = [t for t in tickets if not is_system_ticket(t)]
    return real, len(tickets) - len(real)


def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"[ \t]+", " ", text)).strip()


def conversation_text(ticket, customer_only=True):
    """Concatenate message bodies. customer_only keeps inbound (from_agent=False).

    Falls back to subject + excerpt when full messages weren't fetched — this
    lets population-level analysis run on the list payload alone.
    """
    parts = []
    for m in ticket.get("messages", []):
        if customer_only and m.get("from_agent"):
            continue
        body = m.get("stripped_text") or m.get("body_text") or ""
        if not body:
            body = _strip_html(m.get("body_html") or m.get("stripped_html") or "")
        if body:
            parts.append(body)
    if not parts:
        if ticket.get("subject"):
            parts.append(ticket["subject"])
        if ticket.get("excerpt"):
            parts.append(_strip_html(ticket["excerpt"]))
    return "\n".join(parts)


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def classify_topic(text):
    low = text.lower()
    for label, pat in TOPIC_RULES:
        if re.search(pat, low):
            return label
    return "Other / Uncategorized"


def score_sentiment(text):
    low = text.lower()
    neg = sum(low.count(w) for w in NEG_WORDS)
    pos = sum(low.count(w) for w in POS_WORDS)
    if neg > pos and neg >= 2:
        return "negative"
    if pos > neg and pos >= 2:
        return "positive"
    return "neutral"


def is_escalation(text):
    low = text.lower()
    return [p for p in ESCALATION_PATTERNS if re.search(p, low)]


def product_mentions(text):
    low = text.lower()
    return [label for label, pat in PRODUCT_TERMS.items() if re.search(pat, low)]


def analyze_local(tickets):
    """Run the offline rule-based analysis. Returns a dict of structured results."""
    by_day = Counter()
    by_week = Counter()
    by_channel = Counter()
    topics = Counter()
    sentiments = Counter()
    products = Counter()
    topic_x_product = defaultdict(Counter)
    escalations = []
    resolution_hours = []
    status_counts = Counter()
    tag_counts = Counter()

    for t in tickets:
        created = _parse_dt(t.get("created_datetime"))
        closed = _parse_dt(t.get("closed_datetime"))
        if created:
            by_day[created.date().isoformat()] += 1
            iso = created.isocalendar()
            by_week[f"{iso[0]}-W{iso[1]:02d}"] += 1
        if created and closed and closed >= created:
            resolution_hours.append((closed - created).total_seconds() / 3600)
        status_counts[t.get("status") or "unknown"] += 1
        for tag in (t.get("tags") or []):
            name = tag.get("name") if isinstance(tag, dict) else tag
            if name:
                tag_counts[name] += 1
        by_channel[t.get("channel") or "unknown"] += 1

        text = conversation_text(t)
        topic = classify_topic(text)
        topics[topic] += 1
        sentiments[score_sentiment(text)] += 1

        for p in product_mentions(text):
            products[p] += 1
            topic_x_product[topic][p] += 1

        hits = is_escalation(text)
        if hits or score_sentiment(text) == "negative":
            escalations.append({
                "id": t.get("id"),
                "subject": t.get("subject"),
                "created": t.get("created_datetime"),
                "topic": topic,
                "escalation_signals": hits,
                "excerpt": text[:280].replace("\n", " "),
            })

    res = sorted(resolution_hours)
    res_stats = {}
    if res:
        res_stats = {
            "count_resolved": len(res),
            "median_hours": round(res[len(res) // 2], 1),
            "mean_hours": round(sum(res) / len(res), 1),
            "p90_hours": round(res[int(len(res) * 0.9) - 1], 1),
        }

    return {
        "total_conversations": len(tickets),
        "resolution_time": res_stats,
        "status": dict(status_counts.most_common()),
        "tags": dict(tag_counts.most_common(25)),
        "by_day": dict(sorted(by_day.items())),
        "by_week": dict(sorted(by_week.items())),
        "by_channel": dict(by_channel.most_common()),
        "topics": dict(topics.most_common()),
        "sentiment": dict(sentiments),
        "product_mentions": dict(products.most_common()),
        "topic_by_product": {k: dict(v.most_common()) for k, v in topic_x_product.items()},
        "escalations": sorted(escalations, key=lambda x: x["created"] or "", reverse=True),
    }


# --- Claude deep pass -------------------------------------------------------
def analyze_with_claude(tickets, local_results, api_key, model="claude-opus-4-8",
                        sample_size=120):
    """
    Send a representative sample of transcripts to Claude for theme clustering
    and an executive summary. Returns the model's markdown text.

    Requires the `anthropic` package and an API key. Sampling keeps token cost
    bounded; we stratify across topics so rare buckets aren't dropped.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("pip install anthropic to use the Claude analysis layer")

    sample = _stratified_sample(tickets, sample_size)
    transcripts = []
    for t in sample:
        text = conversation_text(t, customer_only=True)[:1500]
        if text.strip():
            transcripts.append(f"--- Ticket {t.get('id')} | {t.get('subject','(no subject)')}\n{text}")

    corpus = "\n\n".join(transcripts)
    stats = (
        f"Local stats over {local_results['total_conversations']} convos:\n"
        f"Topics: {local_results['topics']}\n"
        f"Sentiment: {local_results['sentiment']}\n"
        f"Product mentions: {local_results['product_mentions']}\n"
    )

    prompt = (
        "You are a CX analyst for Hydrant (a DTC hydration-mix brand). Below are "
        f"{len(transcripts)} customer support conversation transcripts (customer "
        "messages only) plus pre-computed local stats.\n\n"
        f"{stats}\n\n"
        "Produce a concise executive analysis in markdown with these sections:\n"
        "1. **Top themes** — cluster the conversations into 6–10 themes with an "
        "estimated share and a one-line description each. Go beyond the crude "
        "keyword buckets above where the transcripts justify it.\n"
        "2. **Emerging / notable issues** — anything spiking or unusual.\n"
        "3. **Product-specific signal** — which products drive which complaints.\n"
        "4. **Escalation & churn-risk patterns** — language indicating cancellation, "
        "disputes, or anger; rough volume.\n"
        "5. **Top 5 recommended actions** — concrete, prioritized.\n\n"
        "Be specific and quantitative. Transcripts follow:\n\n"
        f"{corpus}"
    )

    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def _stratified_sample(tickets, n):
    """Sample up to n tickets spread across local topic buckets."""
    buckets = defaultdict(list)
    for t in tickets:
        buckets[classify_topic(conversation_text(t))].append(t)
    if not buckets:
        return tickets[:n]
    per = max(1, n // len(buckets))
    out = []
    for items in buckets.values():
        out.extend(items[:per])
    return out[:n]
