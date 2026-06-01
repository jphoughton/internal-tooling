#!/usr/bin/env python3
"""
Download Gorgias conversations and run analysis.

Usage:
    python scripts/gorgias_export.py --days 90                 # download + local analysis
    python scripts/gorgias_export.py --days 90 --claude        # + Claude deep pass
    python scripts/gorgias_export.py --analyze-only data.json  # re-analyze existing dump

Credentials come from .env: GORGIAS_DOMAIN, GORGIAS_EMAIL, GORGIAS_API_KEY
(ANTHROPIC_API_KEY for --claude).

Outputs land in scripts/gorgias_out/:
    conversations_<stamp>.json   raw tickets + messages
    analysis_<stamp>.json        structured local results
    report_<stamp>.md            human-readable report (+ Claude section)
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from etl import gorgias_client as gc
from analytics import gorgias_analysis as ga

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gorgias_export")

OUT_DIR = Path(__file__).resolve().parent / "gorgias_out"


def _creds():
    load_dotenv(override=True)
    domain = os.getenv("GORGIAS_DOMAIN", "")
    email = os.getenv("GORGIAS_EMAIL", "")
    key = os.getenv("GORGIAS_API_KEY", "")
    missing = [n for n, v in [("GORGIAS_DOMAIN", domain), ("GORGIAS_EMAIL", email),
                              ("GORGIAS_API_KEY", key)] if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")
    return domain, email, key


def download(days, sample_messages):
    domain, email, key = _creds()
    ok, msg = gc.test_connection(domain, email, key)
    log.info("Connection: %s", msg)
    if not ok:
        sys.exit(1)

    since = datetime.utcnow() - timedelta(days=days)
    log.info("Downloading ticket list since %s ...", since.date())
    tickets = gc.fetch_tickets(domain, email, key, since=since)
    log.info("Downloaded %d conversations (list payload)", len(tickets))

    # Enrich a stratified sample with full message bodies for the deep read.
    if sample_messages and sample_messages < len(tickets):
        sample = ga._stratified_sample(tickets, sample_messages)
        log.info("Fetching full messages for %d sampled tickets ...", len(sample))
        gc.fetch_messages_for(domain, email, key, sample)
    elif sample_messages:
        log.info("Fetching full messages for all %d tickets ...", len(tickets))
        gc.fetch_messages_for(domain, email, key, tickets)

    return tickets


def write_report(local, claude_md, stamp):
    lines = [
        f"# Gorgias Conversation Analysis — {stamp}",
        "",
        f"**Total conversations:** {local['total_conversations']}",
        "",
        (f"**Resolution time:** median {local['resolution_time'].get('median_hours','n/a')}h, "
         f"mean {local['resolution_time'].get('mean_hours','n/a')}h, "
         f"p90 {local['resolution_time'].get('p90_hours','n/a')}h "
         f"(n={local['resolution_time'].get('count_resolved',0)})"),
        "",
        "## Volume by week",
        "",
        "| Week | Conversations |",
        "|------|--------------:|",
    ]
    for wk, n in local["by_week"].items():
        lines.append(f"| {wk} | {n} |")

    lines += ["", "## Channels", "", "| Channel | Count |", "|---------|------:|"]
    for ch, n in local["by_channel"].items():
        lines.append(f"| {ch} | {n} |")

    lines += ["", "## Status", "", "| Status | Count |", "|--------|------:|"]
    for s, n in local["status"].items():
        lines.append(f"| {s} | {n} |")

    if local.get("tags"):
        lines += ["", "## Top tags", "", "| Tag | Count |", "|-----|------:|"]
        for tg, n in local["tags"].items():
            lines.append(f"| {tg} | {n} |")

    lines += ["", "## Topics (keyword buckets)", "", "| Topic | Count | Share |",
              "|-------|------:|------:|"]
    total = max(local["total_conversations"], 1)
    for tp, n in local["topics"].items():
        lines.append(f"| {tp} | {n} | {n/total:.0%} |")

    lines += ["", "## Sentiment", "", "| Sentiment | Count |", "|-----------|------:|"]
    for s, n in local["sentiment"].items():
        lines.append(f"| {s} | {n} |")

    lines += ["", "## Product mentions", "", "| Product | Mentions |",
              "|---------|---------:|"]
    for p, n in local["product_mentions"].items():
        lines.append(f"| {p} | {n} |")

    esc = local["escalations"]
    lines += ["", f"## Escalations & negative conversations ({len(esc)})", ""]
    for e in esc[:50]:
        sig = ", ".join(s.strip("\\b()") for s in e["escalation_signals"]) or "negative tone"
        lines.append(f"- **#{e['id']}** ({e['topic']}, {sig}) — {e['excerpt']}")

    if claude_md:
        lines += ["", "---", "", "# Claude Deep Analysis", "", claude_md]

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--sample-messages", type=int, default=400,
                    help="fetch full message bodies for N stratified tickets "
                         "(0 = list only; large N = slow). Default 400.")
    ap.add_argument("--claude", action="store_true", help="run Claude deep pass")
    ap.add_argument("--claude-sample", type=int, default=120)
    ap.add_argument("--analyze-only", metavar="JSON", help="skip download, analyze this dump")
    ap.add_argument("--include-system", action="store_true",
                    help="keep bounce/spam/auto-reply tickets (default: filtered out)")
    args = ap.parse_args()

    load_dotenv(override=True)  # ensure .env is loaded even in --analyze-only mode
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if args.analyze_only:
        tickets = json.loads(Path(args.analyze_only).read_text())
        log.info("Loaded %d conversations from %s", len(tickets), args.analyze_only)
    else:
        tickets = download(args.days, args.sample_messages)
        raw_path = OUT_DIR / f"conversations_{stamp}.json"
        raw_path.write_text(json.dumps(tickets, indent=2, default=str))
        log.info("Wrote %s", raw_path)

    if not args.include_system:
        tickets, dropped = ga.filter_real_conversations(tickets)
        log.info("Filtered out %d system/bounce/spam tickets; %d real conversations remain",
                 dropped, len(tickets))

    local = ga.analyze_local(tickets)
    (OUT_DIR / f"analysis_{stamp}.json").write_text(json.dumps(local, indent=2))

    claude_md = None
    if args.claude:
        ak = os.getenv("ANTHROPIC_API_KEY", "")
        if not ak:
            log.warning("ANTHROPIC_API_KEY not set — skipping Claude pass")
        else:
            log.info("Running Claude deep pass on a sample of %d ...", args.claude_sample)
            claude_md = ga.analyze_with_claude(tickets, local, ak,
                                               sample_size=args.claude_sample)

    report = write_report(local, claude_md, stamp)
    report_path = OUT_DIR / f"report_{stamp}.md"
    report_path.write_text(report)
    log.info("Wrote %s", report_path)

    print("\n" + "=" * 60)
    print(f"Conversations: {local['total_conversations']}")
    print(f"Top topics:    {dict(list(local['topics'].items())[:5])}")
    print(f"Sentiment:     {local['sentiment']}")
    print(f"Escalations:   {len(local['escalations'])}")
    print(f"Report:        {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
