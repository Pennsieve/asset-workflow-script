#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Usage:
    python trigger_workflows.py package_ids.txt
    python trigger_workflows.py --package-id PKG1 --package-id PKG2
    python trigger_workflows.py package_ids.txt --workflow-id UUID --dataset-id UUID
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()


def prompt_for_token() -> str:
    """Wait for user to update .env with a new session token, then re-read it."""
    print("\n  SESSION EXPIRED — update PENNSIEVE_SESSION_TOKEN in your .env file, then press Enter.")
    input("  Press Enter to continue...")
    load_dotenv(override=True)
    token = os.environ.get("PENNSIEVE_SESSION_TOKEN", "")
    print(f"  Token refreshed from .env.\n")
    return token


def api_request(method: str, url: str, token_holder: list, **kwargs):
    """Make an API request, prompting for a new token on 403."""
    headers = {"Authorization": f"Bearer {token_holder[0]}"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 403:
        token_holder[0] = prompt_for_token()
        headers["Authorization"] = f"Bearer {token_holder[0]}"
        resp = requests.request(method, url, headers=headers, **kwargs)
    resp.raise_for_status()
    return resp


def get_run_status(api_host: str, token_holder: list, run_id: str, organization_id: str) -> dict | None:
    """Find a specific run from the workflow runs list and return it."""
    resp = api_request(
        "GET", f"{api_host}/compute/workflows/runs", token_holder,
        params={"organization_id": organization_id},
    )
    runs = resp.json().get("runs", [])
    for run in runs:
        if run.get("uuid") == run_id:
            return run
    return None


def wait_for_run_completion(
    api_host: str, token_holder: list, run_id: str, organization_id: str,
    poll_interval: float = 30, timeout: float = 3600,
) -> str:
    """Poll until a workflow run's completedAt is set. Returns the final status."""
    start = time.time()
    while True:
        run = get_run_status(api_host, token_holder, run_id, organization_id)
        if run and run.get("completedAt"):
            return run.get("status", "SUCCEEDED")
        elapsed = time.time() - start
        if elapsed > timeout:
            status = run.get("status", "UNKNOWN") if run else "NOT_FOUND"
            return f"TIMEOUT (last status: {status})"
        remaining = timeout - elapsed
        wait = min(poll_interval, remaining)
        status = run.get("status", "UNKNOWN") if run else "NOT_FOUND"
        print(f"  Status: {status} — waiting {int(wait)}s (elapsed {int(elapsed)}s)...")
        time.sleep(wait)


def trigger_workflow_run(api_host: str, token_holder: list, config: dict, package_id: str) -> dict:
    """Trigger a single workflow run for one package."""
    payload = {
        "workflowInstanceConfiguration": {
            "workflowId": config["workflow_id"],
            "computeNodeId": config["compute_node_id"],
            "processorConfigs": [
                {"nodeId": config["target_zarr_node_id"], "executionTarget": "standard"},
                {"nodeId": config["target_thumb_node_id"], "executionTarget": "standard"},
                {"nodeId": config["thumb_processor_node_id"], "executionTarget": "standard",
                 "version": "v1.1.0", "cpu": "4096", "memory": "30720"},
                {"nodeId": config["nifti_processor_node_id"], "executionTarget": "standard",
                 "version": "v1.1.1", "cpu": "4096", "memory": "30720"},
            ],
        },
        "datasetId": config["dataset_id"],
        "dataSources": {
            config["source_node_id"]: {
                "packageIds": [package_id],
            },
        },
        "dataTargets": {
            config["target_zarr_node_id"]: {
                "params": {
                    "ASSET_PROPERTIES_FILE": "asset_properties.json",
                    "ASSET_NAME": "assets",
                    "ASSET_TYPE": "ome-zarr",
                },
            },
            config["target_thumb_node_id"]: {
                "params": {
                    "ASSET_PROPERTIES_FILE": "asset_properties.json",
                    "ASSET_NAME": "assets",
                    "ASSET_TYPE": "thumb",
                },
            },
        },
    }
    resp = api_request("POST", f"{api_host}/compute/workflows/runs", token_holder, json=payload)
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
    parser.add_argument("--session-token", default=os.environ.get("PENNSIEVE_SESSION_TOKEN"),
                        help="Session token from the browser")
    parser.add_argument("--workflow-id", default=os.environ.get("WORKFLOW_ID"))
    parser.add_argument("--dataset-id", default=os.environ.get("DATASET_ID"))
    parser.add_argument("--organization-id", default=os.environ.get("ORGANIZATION_ID"),
                        help="Organization ID for status polling (e.g. N:organization:...)")
    parser.add_argument("--compute-node-id", default=os.environ.get("COMPUTE_NODE_ID", "599f80a6-ab76-495b-85d9-f363825a2413"))
    parser.add_argument("--source-node-id", default=os.environ.get("SOURCE_NODE_ID", "node_1782328302979_nn2lxy5ws"))
    parser.add_argument("--target-zarr-node-id", default=os.environ.get("TARGET_ZARR_NODE_ID", "node_1782328304796_6dgwnyyui"))
    parser.add_argument("--target-thumb-node-id", default=os.environ.get("TARGET_THUMB_NODE_ID", "node_1782328305861_n1cjh790i"))
    parser.add_argument("--nifti-processor-node-id", default=os.environ.get("NIFTI_PROCESSOR_NODE_ID", "node_1782328328813_qqj7dy17u"))
    parser.add_argument("--thumb-processor-node-id", default=os.environ.get("THUMB_PROCESSOR_NODE_ID", "node_1782328311019_yrfgve16l"))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("POLL_INTERVAL", "60")),
                        help="Seconds between status checks (default: 60)")
    parser.add_argument("--run-timeout", type=float, default=float(os.environ.get("RUN_TIMEOUT", "3600")),
                        help="Max seconds to wait for a run to complete (default: 3600)")
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
    if not args.session_token:
        missing.append("PENNSIEVE_SESSION_TOKEN (--session-token)")
    if not args.workflow_id:
        missing.append("WORKFLOW_ID (--workflow-id)")
    if not args.dataset_id:
        missing.append("DATASET_ID (--dataset-id)")
    if not args.compute_node_id:
        missing.append("COMPUTE_NODE_ID (--compute-node-id)")
    if not args.organization_id:
        missing.append("ORGANIZATION_ID (--organization-id)")
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    print(f"Workflow ID:       {args.workflow_id}")
    print(f"Dataset ID:        {args.dataset_id}")
    print(f"Compute Node:      {args.compute_node_id}")
    print(f"Packages:          {len(package_ids)}")
    print(f"API host:          {args.api_host}")
    print()

    if args.dry_run:
        for i, pkg_id in enumerate(package_ids, 1):
            print(f"[DRY RUN] {i}/{len(package_ids)} Would trigger for package: {pkg_id}")
        print(f"\nDry run complete. {len(package_ids)} runs would be triggered.")
        return

    # token_holder is a list so it can be updated by reference when refreshed
    token_holder = [args.session_token]
    print("Using provided session token.\n")

    # Trigger runs sequentially — wait for each to complete before starting the next
    succeeded = 0
    failed = 0
    workflow_config = {
        "workflow_id": args.workflow_id,
        "dataset_id": args.dataset_id,
        "compute_node_id": args.compute_node_id,
        "source_node_id": args.source_node_id,
        "target_zarr_node_id": args.target_zarr_node_id,
        "target_thumb_node_id": args.target_thumb_node_id,
        "nifti_processor_node_id": args.nifti_processor_node_id,
        "thumb_processor_node_id": args.thumb_processor_node_id,
    }

    for i, pkg_id in enumerate(package_ids, 1):
        try:
            print(f"[{i}/{len(package_ids)}] Triggering for package: {pkg_id}...", end=" ")
            result = trigger_workflow_run(args.api_host, token_holder, workflow_config, pkg_id)
            run_id = result.get("uuid", result.get("executionRunId", ""))
            print(f"OK (runId: {run_id})")

            if i < len(package_ids) and run_id:
                print(f"  Waiting for run {run_id} to complete...")
                final_status = wait_for_run_completion(
                    args.api_host, token_holder, run_id, args.organization_id,
                    poll_interval=args.poll_interval,
                    timeout=args.run_timeout,
                )
                print(f"  Run finished: {final_status}")
                if final_status != "SUCCEEDED":
                    print(f"  WARNING: Run did not succeed — continuing anyway.")

            succeeded += 1
        except requests.HTTPError as e:
            print(f"FAILED ({e.response.status_code}: {e.response.text})")
            failed += 1
        except Exception as e:
            print(f"FAILED ({e})")
            failed += 1

    print(f"\nDone. Succeeded: {succeeded}, Failed: {failed}")


if __name__ == "__main__":
    main()
