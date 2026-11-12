"""
Timely Filing Alerter
---------------------
Warns billing teams about healthcare claims that are approaching (or have
passed) a payer's timely filing deadline.

How it works
1. Each payer has a filing limit (days from date of service) and a
   corrected-claim limit (days from the payer's rejection/denial date).
2. For every open claim we compute the deadline and the days remaining.
3. We assign an alert level and a priority score (urgency x dollars at stake).
4. Claims are ranked and printed, and exported to CSV for the work queue.

NOTE: The limits below are ILLUSTRATIVE. Always load real limits from your
payer contracts / provider manuals.

Run:  python timely_filing_alerter.py
"""

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

# ----------------------------------------------------------------------------
# 1. Payer rules (days). Replace with data from your contract database.
# ----------------------------------------------------------------------------
PAYER_RULES = {
    # payer:            (initial filing days, corrected-claim days)
    "MEDICARE":         (365, 365),
    "MEDICAID":         (365, 180),
    "AETNA":            (180, 180),
    "CIGNA":            (90, 90),
    "UNITED":           (90, 90),
    "BLUE CROSS":       (180, 180),
    "DEFAULT":          (90, 90),
}

# Alert thresholds (days remaining)
CRITICAL_DAYS = 7
HIGH_DAYS = 15
MEDIUM_DAYS = 30


# ----------------------------------------------------------------------------
# 2. Data model
# ----------------------------------------------------------------------------
@dataclass
class Claim:
    claim_id: str
    payer: str
    patient_id: str
    date_of_service: date
    amount: float
    status: str                       # NOT_SUBMITTED | SUBMITTED | REJECTED | PAID
    submitted_date: Optional[date] = None
    rejected_date: Optional[date] = None   # payer rejection / denial date
    has_proof_of_timely_filing: bool = False  # e.g. 277CA / clearinghouse receipt


@dataclass
class Alert:
    claim: Claim
    deadline: date
    days_left: int
    level: str
    priority: float
    action: str


# ----------------------------------------------------------------------------
# 3. Core logic
# ----------------------------------------------------------------------------
def get_rules(payer: str):
    return PAYER_RULES.get(payer.upper(), PAYER_RULES["DEFAULT"])


def compute_deadline(claim: Claim) -> Optional[date]:
    """Return the date by which the claim must be (re)filed, or None if no
    action is needed."""
    initial_days, corrected_days = get_rules(claim.payer)

    if claim.status == "PAID":
        return None
    if claim.status == "NOT_SUBMITTED":
        return claim.date_of_service + timedelta(days=initial_days)
    if claim.status == "REJECTED" and claim.rejected_date:
        # Corrected claim window starts at rejection, but can never extend
        # beyond the original filing limit unless proof of timely filing exists.
        corrected_deadline = claim.rejected_date + timedelta(days=corrected_days)
        original_deadline = claim.date_of_service + timedelta(days=initial_days)
        if claim.has_proof_of_timely_filing:
            return corrected_deadline
        return min(corrected_deadline, original_deadline)
    return None  # SUBMITTED and awaiting payer response: nothing due


def classify(days_left: int) -> str:
    if days_left < 0:
        return "EXPIRED"
    if days_left <= CRITICAL_DAYS:
        return "CRITICAL"
    if days_left <= HIGH_DAYS:
        return "HIGH"
    if days_left <= MEDIUM_DAYS:
        return "MEDIUM"
    return "LOW"


def priority_score(days_left: int, amount: float) -> float:
    """Higher = work first. Urgency grows as the deadline nears; dollars
    at stake scale it (log-damped so one huge claim doesn't dominate)."""
    import math
    urgency = 1.0 / (1 + max(days_left, 0))
    return round(urgency * math.log10(amount + 10) * 100, 2)


def recommended_action(level: str, claim: Claim) -> str:
    if level == "EXPIRED":
        if claim.has_proof_of_timely_filing:
            return "Appeal with proof of timely filing"
        return "Deadline missed: review for write-off / appeal"
    if claim.status == "NOT_SUBMITTED":
        return "Scrub and submit immediately" if level in ("CRITICAL", "HIGH") \
            else "Schedule submission"
    if claim.status == "REJECTED":
        return "Fix rejection and resubmit corrected claim"
    return "Review"


def generate_alerts(claims: List[Claim], today: Optional[date] = None) -> List[Alert]:
    today = today or date.today()
    alerts = []
    for c in claims:
        deadline = compute_deadline(c)
        if deadline is None:
            continue
        days_left = (deadline - today).days
        level = classify(days_left)
        alerts.append(Alert(
            claim=c,
            deadline=deadline,
            days_left=days_left,
            level=level,
            priority=priority_score(days_left, c.amount),
            action=recommended_action(level, c),
        ))
    # Expired first (most days overdue on top), then by priority
    alerts.sort(key=lambda a: (a.level != "EXPIRED", -a.priority))
    return alerts


# ----------------------------------------------------------------------------
# 4. Output helpers
# ----------------------------------------------------------------------------
def print_report(alerts: List[Alert], include_low: bool = False) -> None:
    header = f"{'Claim':<9}{'Payer':<12}{'Amount':>10}  {'Deadline':<11}{'Left':>5}  {'Level':<9}Action"
    print(header)
    print("-" * len(header) + "-" * 30)
    for a in alerts:
        if a.level == "LOW" and not include_low:
            continue
        c = a.claim
        print(f"{c.claim_id:<9}{c.payer:<12}{c.amount:>10,.2f}  "
              f"{a.deadline.isoformat():<11}{a.days_left:>5}  {a.level:<9}{a.action}")


def export_csv(alerts: List[Alert], path: str = "timely_filing_alerts.csv") -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["claim_id", "payer", "patient_id", "amount", "status",
                    "deadline", "days_left", "level", "priority", "action"])
        for a in alerts:
            c = a.claim
            w.writerow([c.claim_id, c.payer, c.patient_id, c.amount, c.status,
                        a.deadline, a.days_left, a.level, a.priority, a.action])
    print(f"\nExported {len(alerts)} alerts to {path}")


def notify(alerts: List[Alert]) -> None:
    """Stub for email / Teams / Slack. Plug in your own sender here."""
    urgent = [a for a in alerts if a.level in ("EXPIRED", "CRITICAL")]
    if urgent:
        print(f"\n[NOTIFY] {len(urgent)} urgent claim(s) need attention today.")


# ----------------------------------------------------------------------------
# 5. Demo
# ----------------------------------------------------------------------------
def sample_claims(today: date) -> List[Claim]:
    d = lambda n: today - timedelta(days=n)   # n days ago
    return [
        Claim("CLM1001", "Cigna",      "P001", d(85),  1250.00, "NOT_SUBMITTED"),
        Claim("CLM1002", "Aetna",      "P002", d(160), 4800.50, "NOT_SUBMITTED"),
        Claim("CLM1003", "Medicare",   "P003", d(120), 320.00,  "NOT_SUBMITTED"),
        Claim("CLM1004", "United",     "P004", d(100), 2100.00, "NOT_SUBMITTED"),
        Claim("CLM1005", "Cigna",      "P005", d(60),  900.00,  "REJECTED",
              submitted_date=d(55), rejected_date=d(50)),
        Claim("CLM1006", "Blue Cross", "P006", d(170), 7600.00, "REJECTED",
              submitted_date=d(150), rejected_date=d(140), has_proof_of_timely_filing=True),
        Claim("CLM1007", "Medicaid",   "P007", d(30),  540.00,  "SUBMITTED",
              submitted_date=d(28)),
        Claim("CLM1008", "United",     "P008", d(20),  3100.00, "PAID",
              submitted_date=d(18)),
    ]


if __name__ == "__main__":
    today = date.today()
    claims = sample_claims(today)
    alerts = generate_alerts(claims, today)
    print(f"Timely Filing Alert Report  ({today.isoformat()})\n")
    print_report(alerts, include_low=True)
    notify(alerts)
    export_csv(alerts)
