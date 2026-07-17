#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Usage:
    python trigger_workflows.py package_ids.txt
    python trigger_workflows.py --package-id PKG1 --package-id PKG2
    python trigger_workflows.py package_ids.txt --workflow-id UUID --dataset-id UUID
    python trigger_workflows.py --discover
    python trigger_workflows.py --discover --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()


def trigger_workflow_run(api_host: str, token: str, refresh_token: str, config: dict, package_id: str) -> dict:
    """Trigger a single workflow run for one package."""
    payload = {
        "workflowInstanceConfiguration": {
            "workflowId": config["workflow_id"],
            "computeNodeId": config["compute_node_id"],
            "processorConfigs": [
                {"nodeId": config["target_zarr_node_id"], "executionTarget": "standard"},
                {"nodeId": config["target_thumb_node_id"], "executionTarget": "standard"},
                {"nodeId": config["thumb_processor_node_id"], "executionTarget": "standard",
                 "version": "v1.2.0", "cpu": "4096", "memory": "30720"},
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
                    "ASSET_NAME": "preview",
                    "ASSET_TYPE": "ome-zarr",
                },
            },
            config["target_thumb_node_id"]: {
                "params": {
                    "ASSET_PROPERTIES_FILE": "asset_properties.json",
                    "ASSET_NAME": "thumbnail",
                    "ASSET_TYPE": "thumb",
                },
            },
        },
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    resp = requests.post(f"{api_host}/compute/workflows/runs", headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def fetch_mri_packages(api_host: str, token: str, refresh_token: str, dataset_id: str) -> list[str]:
    """Fetch all non-deleted MRI packages from a dataset via the Pennsieve API."""
    # The packages endpoint is on the v1 API (api.pennsieve.io), not api2
    packages_host = api_host.replace("api2.", "api.")
    headers = {"Authorization": f"Bearer {token}"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    package_ids = []
    cursor = None

    while True:
        params = {"types": "MRI", "pageSize": 500}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            f"{packages_host}/datasets/{dataset_id}/packages",
            headers=headers,
            params=params,
        )
        if resp.status_code == 401:
            print("\nError: Unauthorized. Your session token may have expired.", file=sys.stderr)
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()

        packages = data.get("packages", [])
        for pkg in packages:
            content = pkg.get("content", {})
            if content.get("state") != "DELETED":
                package_ids.append(content["nodeId"])

        print(f"  Fetched {len(package_ids)} MRI packages so far...")

        cursor = data.get("cursor")
        if not cursor:
            break

    return package_ids


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
    parser.add_argument("--refresh-token", default=os.environ.get("PENNSIEVE_REFRESH_TOKEN"),
                        help="Refresh token from the browser (sent as x-refresh-token header)")
    parser.add_argument("--workflow-id", default=os.environ.get("WORKFLOW_ID"))
    parser.add_argument("--dataset-id", default=os.environ.get("DATASET_ID"))
    parser.add_argument("--compute-node-id", default=os.environ.get("COMPUTE_NODE_ID", "599f80a6-ab76-495b-85d9-f363825a2413"))
    parser.add_argument("--source-node-id", default=os.environ.get("SOURCE_NODE_ID", "node_1782328302979_nn2lxy5ws"))
    parser.add_argument("--target-zarr-node-id", default=os.environ.get("TARGET_ZARR_NODE_ID", "node_1782328304796_6dgwnyyui"))
    parser.add_argument("--target-thumb-node-id", default=os.environ.get("TARGET_THUMB_NODE_ID", "node_1782328305861_n1cjh790i"))
    parser.add_argument("--nifti-processor-node-id", default=os.environ.get("NIFTI_PROCESSOR_NODE_ID", "node_1782328328813_qqj7dy17u"))
    parser.add_argument("--thumb-processor-node-id", default=os.environ.get("THUMB_PROCESSOR_NODE_ID", "node_1782328311019_yrfgve16l"))
    parser.add_argument("--delay", type=float, default=float(os.environ.get("DELAY_BETWEEN_RUNS", "30")),
                        help="Seconds to wait between triggering runs (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be triggered without actually doing it")
    parser.add_argument("--discover", action="store_true",
                        help="Auto-discover all MRI packages from the dataset instead of providing IDs")

    args = parser.parse_args()

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
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    # Collect package IDs
    package_ids = []
    if args.discover:
        print(f"Discovering MRI packages from dataset {args.dataset_id}...")
        package_ids = fetch_mri_packages(args.api_host, args.session_token, args.refresh_token, args.dataset_id)
        print(f"Found {len(package_ids)} MRI packages.\n")
    else:
        if args.package_file:
            package_ids.extend(load_package_ids(args.package_file))
        if args.package_ids:
            package_ids.extend(args.package_ids)

    if not package_ids:
        parser.error("No package IDs provided. Use a file, --package-id flags, or --discover.")

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

    token = args.session_token
    print("Using provided session token.\n")

    # Trigger runs with a fixed delay between each
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
            result = trigger_workflow_run(args.api_host, token, args.refresh_token, workflow_config, pkg_id)
            run_id = result.get("uuid", result.get("executionRunId", ""))
            print(f"OK (runId: {run_id})")
            succeeded += 1
        except requests.HTTPError as e:
            print(f"FAILED ({e.response.status_code}: {e.response.text})")
            failed += 1
        except Exception as e:
            print(f"FAILED ({e})")
            failed += 1

        if i < len(package_ids) and args.delay > 0:
            print(f"  Waiting {int(args.delay)}s before next run...")
            time.sleep(args.delay)

    print(f"\nDone. Succeeded: {succeeded}, Failed: {failed}")


if __name__ == "__main__":
    main()
