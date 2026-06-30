#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Usage:
    python trigger_workflows.py package_ids.txt
    python trigger_workflows.py --package-id PKG1 --package-id PKG2
    python trigger_workflows.py package_ids.txt --workflow-id UUID --dataset-id UUID
"""

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()


def get_session_token(api_host: str, api_key: str, api_secret: str) -> str:
    """Exchange API key/secret for a session token."""
    resp = requests.post(
        f"{api_host}/account/api/session",
        json={"tokenId": api_key, "secret": api_secret},
    )
    resp.raise_for_status()
    return resp.json()["session_token"]


def trigger_workflow_run(
    api_host: str,
    token: str,
    workflow_id: str,
    dataset_id: str,
    package_id: str,
) -> dict:
    """Trigger a single workflow run for one package."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "workflowId": workflow_id,
        "datasetId": dataset_id,
        "packageIds": [package_id],
    }
    resp = requests.post(
        f"{api_host}/compute/workflows/runs",
        headers=headers,
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def load_package_ids(filepath: str) -> list[str]:
    """Load package IDs from a text file (one per line)."""
    with open(filepath) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def main():
    parser = argparse.ArgumentParser(
        description="Trigger Pennsieve workflow runs for multiple packages"
    )
    parser.add_argument(
        "package_file",
        nargs="?",
        help="Text file with one package ID per line",
    )
    parser.add_argument(
        "--package-id",
        action="append",
        dest="package_ids",
        help="Package ID (can be specified multiple times)",
    )
    parser.add_argument("--api-host", default=os.environ.get("PENNSIEVE_API_HOST", "https://api2.pennsieve.io"))
    parser.add_argument("--api-key", default=os.environ.get("PENNSIEVE_API_KEY"))
    parser.add_argument("--api-secret", default=os.environ.get("PENNSIEVE_API_SECRET"))
    parser.add_argument("--workflow-id", default=os.environ.get("WORKFLOW_ID"))
    parser.add_argument("--dataset-id", default=os.environ.get("DATASET_ID"))
    parser.add_argument("--delay", type=float, default=float(os.environ.get("DELAY_BETWEEN_RUNS", "1")))
    parser.add_argument("--dry-run", action="store_true", help="Print what would be triggered without actually doing it")

    args = parser.parse_args()

    # Collect package IDs from file and/or CLI args
    package_ids = []
    if args.package_file:
        package_ids.extend(load_package_ids(args.package_file))
    if args.package_ids:
        package_ids.extend(args.package_ids)

    if not package_ids:
        parser.error("No package IDs provided. Use a file or --package-id flags.")

    # Validate required config
    missing = []
    if not args.api_key:
        missing.append("PENNSIEVE_API_KEY (--api-key)")
    if not args.api_secret:
        missing.append("PENNSIEVE_API_SECRET (--api-secret)")
    if not args.workflow_id:
        missing.append("WORKFLOW_ID (--workflow-id)")
    if not args.dataset_id:
        missing.append("DATASET_ID (--dataset-id)")
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    print(f"Workflow ID: {args.workflow_id}")
    print(f"Dataset ID:  {args.dataset_id}")
    print(f"Packages:    {len(package_ids)}")
    print(f"API host:    {args.api_host}")
    print()

    if args.dry_run:
        for i, pkg_id in enumerate(package_ids, 1):
            print(f"[DRY RUN] {i}/{len(package_ids)} Would trigger for package: {pkg_id}")
        print(f"\nDry run complete. {len(package_ids)} runs would be triggered.")
        return

    # Authenticate
    print("Authenticating...")
    token = get_session_token(args.api_host, args.api_key, args.api_secret)
    print("Authenticated.\n")

    # Trigger runs
    succeeded = 0
    failed = 0
    for i, pkg_id in enumerate(package_ids, 1):
        try:
            print(f"[{i}/{len(package_ids)}] Triggering for package: {pkg_id}...", end=" ")
            result = trigger_workflow_run(args.api_host, token, args.workflow_id, args.dataset_id, pkg_id)
            print(f"OK (run: {result.get('id', 'unknown')})")
            succeeded += 1
        except requests.HTTPError as e:
            print(f"FAILED ({e.response.status_code}: {e.response.text})")
            failed += 1
        except Exception as e:
            print(f"FAILED ({e})")
            failed += 1

        if i < len(package_ids) and args.delay > 0:
            time.sleep(args.delay)

    print(f"\nDone. Succeeded: {succeeded}, Failed: {failed}")


if __name__ == "__main__":
    main()
