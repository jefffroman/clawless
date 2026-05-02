#!/usr/bin/env python3
"""
Usage: ./scripts/check-costs.py [N_DAYS]
Shows AWS costs for the past N days (default: 7), broken down by service.
"""
import sys
import boto3
from datetime import date, timedelta
from collections import defaultdict


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    end = date.today()
    start = end - timedelta(days=n)

    ce = boto3.client("ce", region_name="us-east-1")

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": str(start), "End": str(end)},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals = defaultdict(float)
    daily = []

    for result in resp["ResultsByTime"]:
        day = result["TimePeriod"]["Start"]
        day_total = 0.0
        for group in result["Groups"]:
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            totals[service] += cost
            day_total += cost
        daily.append((day, day_total))

    label = f"{n} day" + ("s" if n != 1 else "")
    print(f"AWS costs: {start} → {end}  ({label})\n")

    print("Daily:")
    for day, total in daily:
        bar = "█" * int(total * 20)  # rough visual scale
        print(f"  {day}  ${total:7.4f}  {bar}")

    grand_total = sum(totals.values())
    print(f"\nBy service (total ${grand_total:.4f}):")
    for service, cost in sorted(totals.items(), key=lambda x: -x[1]):
        if cost >= 0.0001:
            print(f"  {service:<45}  ${cost:.4f}")

    print(f"\n  {'TOTAL':<45}  ${grand_total:.4f}")


if __name__ == "__main__":
    main()
