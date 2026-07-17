#!/usr/bin/env python3
"""Trigger Pennsieve workflow runs for a list of package IDs.

Supports two workflows: NIfTI-to-Zarr and OME-TIFF-to-Zarr.

Usage:
    # Dataset mode: auto-discover packages, run both workflows
    python trigger_workflows.py --dataset --nifti --tiff

    # Dataset mode: only nifti
    python trigger_workflows.py --dataset --nifti

    # File mode: run package IDs through nifti workflow
    python trigger_workflows.py package_ids.txt --nifti

    # File mode: run package IDs through tiff workflow
    python trigger_workflows.py package_ids.txt --tiff
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

NIFTI_EXTENSIONS = (".nii.gz", ".nii")
TIFF_EXTENSIONS = (".ome.tiff", ".ome.tif")


def classify_package(name: str) -> str | None:
    """Classify a package name as 'nifti', 'tiff', or None based on file extension."""
    lower = name.lower()
    for ext in NIFTI_EXTENSIONS:
        if lower.endswith(ext):
            return "nifti"
    for ext in TIFF_EXTENSIONS:
        if lower.endswith(ext):
            return "tiff"
    return None


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
                 "version": config["thumb_processor_version"], "cpu": "4096", "memory": "30720"},
                {"nodeId": config["converter_processor_node_id"], "executionTarget": "standard",
                 "version": config["converter_processor_version"], "cpu": "4096", "memory": "30720"},
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


def fetch_dataset_packages(api_host: str, token: str, refresh_token: str, dataset_id: str) -> list[dict]:
    """Fetch all non-deleted MRI packages from a dataset via the Pennsieve API.

    Returns list of dicts with 'node_id' and 'name' keys.
    """
    # The packages endpoint is on the v1 API (api.pennsieve.io), not api2
    packages_host = api_host.replace("api2.", "api.")
    headers = {"Authorization": f"Bearer {token}"}
    if refresh_token:
        headers["x-refresh-token"] = refresh_token
    packages = []
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

        for pkg in data.get("packages", []):
            content = pkg.get("content", {})
            if content.get("state") != "DELETED":
                packages.append({
                    "node_id": content["nodeId"],
                    "name": content.get("name", ""),
                })

        print(f"  Fetched {len(packages)} MRI packages so far...")

        cursor = data.get("cursor")
        if not cursor:
            break

    return packages


def load_package_ids(filepath: str) -> list[str]:
    """Load package IDs from a text file (one per line)."""
    with open(filepath) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def run_workflow(args, workflow_config: dict, package_ids: list[str], label: str):
    """Run a workflow for a list of package IDs. Returns (succeeded, failed) counts."""
    print(f"\n--- {label} workflow ---")
    print(f"Workflow ID:       {workflow_config['workflow_id']}")
    print(f"Packages:          {len(package_ids)}")
    print()

    if args.dry_run:
        for i, pkg_id in enumerate(package_ids, 1):
            print(f"[DRY RUN] {i}/{len(package_ids)} Would trigger for package: {pkg_id}")
        print(f"\nDry run complete. {len(package_ids)} runs would be triggered.")
        return len(package_ids), 0

    token = args.session_token
    succeeded = 0
    failed = 0

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

    return succeeded, failed


def build_nifti_config(args) -> dict:
    """Build workflow config dict for NIfTI workflow."""
    return {
        "workflow_id": args.workflow_id,
        "dataset_id": args.dataset_id,
        "compute_node_id": args.compute_node_id,
        "source_node_id": args.source_node_id,
        "target_zarr_node_id": args.target_zarr_node_id,
        "target_thumb_node_id": args.target_thumb_node_id,
        "converter_processor_node_id": args.nifti_processor_node_id,
        "thumb_processor_node_id": args.thumb_processor_node_id,
        "converter_processor_version": args.nifti_processor_version,
        "thumb_processor_version": args.thumb_processor_version,
    }


def build_tiff_config(args) -> dict:
    """Build workflow config dict for OME-TIFF workflow."""
    return {
        "workflow_id": args.tiff_workflow_id,
        "dataset_id": args.dataset_id,
        "compute_node_id": args.tiff_compute_node_id,
        "source_node_id": args.tiff_source_node_id,
        "target_zarr_node_id": args.tiff_target_zarr_node_id,
        "target_thumb_node_id": args.tiff_target_thumb_node_id,
        "converter_processor_node_id": args.tiff_processor_node_id,
        "thumb_processor_node_id": args.tiff_thumb_processor_node_id,
        "converter_processor_version": args.tiff_processor_version,
        "thumb_processor_version": args.tiff_thumb_processor_version,
    }


def validate_config(args) -> list[str]:
    """Validate required config. Returns list of missing items."""
    missing = []
    if not args.session_token:
        missing.append("PENNSIEVE_SESSION_TOKEN (--session-token)")
    if not args.dataset_id:
        missing.append("DATASET_ID (--dataset-id)")

    if args.nifti:
        if not args.workflow_id:
            missing.append("WORKFLOW_ID (--workflow-id)")
        if not args.compute_node_id:
            missing.append("COMPUTE_NODE_ID (--compute-node-id)")

    if args.tiff:
        if not args.tiff_workflow_id:
            missing.append("TIFF_WORKFLOW_ID (--tiff-workflow-id)")
        if not args.tiff_compute_node_id:
            missing.append("TIFF_COMPUTE_NODE_ID (--tiff-compute-node-id)")

    return missing


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

    # Workflow type selection
    parser.add_argument("--nifti", action="store_true",
                        help="Run the NIfTI-to-Zarr workflow")
    parser.add_argument("--tiff", action="store_true",
                        help="Run the OME-TIFF-to-Zarr workflow")

    # General config
    parser.add_argument("--api-host", default=os.environ.get("PENNSIEVE_API_HOST", "https://api2.pennsieve.io"))
    parser.add_argument("--session-token", default=os.environ.get("PENNSIEVE_SESSION_TOKEN"),
                        help="Session token from the browser")
    parser.add_argument("--refresh-token", default=os.environ.get("PENNSIEVE_REFRESH_TOKEN"),
                        help="Refresh token from the browser (sent as x-refresh-token header)")
    parser.add_argument("--dataset-id", default=os.environ.get("DATASET_ID"))
    parser.add_argument("--delay", type=float, default=float(os.environ.get("DELAY_BETWEEN_RUNS", "30")),
                        help="Seconds to wait between triggering runs (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be triggered without actually doing it")
    parser.add_argument("--dataset", action="store_true",
                        help="Auto-discover MRI packages from the dataset and classify by file type")

    # NIfTI workflow config
    parser.add_argument("--workflow-id", default=os.environ.get("WORKFLOW_ID"))
    parser.add_argument("--compute-node-id", default=os.environ.get("COMPUTE_NODE_ID"))
    parser.add_argument("--source-node-id", default=os.environ.get("SOURCE_NODE_ID"))
    parser.add_argument("--target-zarr-node-id", default=os.environ.get("TARGET_ZARR_NODE_ID"))
    parser.add_argument("--target-thumb-node-id", default=os.environ.get("TARGET_THUMB_NODE_ID"))
    parser.add_argument("--nifti-processor-node-id", default=os.environ.get("NIFTI_PROCESSOR_NODE_ID"))
    parser.add_argument("--thumb-processor-node-id", default=os.environ.get("THUMB_PROCESSOR_NODE_ID"))
    parser.add_argument("--nifti-processor-version", default=os.environ.get("NIFTI_PROCESSOR_VERSION", "v1.1.1"))
    parser.add_argument("--thumb-processor-version", default=os.environ.get("THUMB_PROCESSOR_VERSION", "v1.2.0"))

    # TIFF workflow config
    parser.add_argument("--tiff-workflow-id", default=os.environ.get("TIFF_WORKFLOW_ID"))
    parser.add_argument("--tiff-compute-node-id", default=os.environ.get("TIFF_COMPUTE_NODE_ID"))
    parser.add_argument("--tiff-source-node-id", default=os.environ.get("TIFF_SOURCE_NODE_ID"))
    parser.add_argument("--tiff-target-zarr-node-id", default=os.environ.get("TIFF_TARGET_ZARR_NODE_ID"))
    parser.add_argument("--tiff-target-thumb-node-id", default=os.environ.get("TIFF_TARGET_THUMB_NODE_ID"))
    parser.add_argument("--tiff-processor-node-id", default=os.environ.get("TIFF_PROCESSOR_NODE_ID"))
    parser.add_argument("--tiff-thumb-processor-node-id", default=os.environ.get("TIFF_THUMB_PROCESSOR_NODE_ID"))
    parser.add_argument("--tiff-processor-version", default=os.environ.get("TIFF_PROCESSOR_VERSION", "v1.0.0"))
    parser.add_argument("--tiff-thumb-processor-version", default=os.environ.get("TIFF_THUMB_PROCESSOR_VERSION", "v1.2.0"))

    args = parser.parse_args()

    # Validate workflow type selection
    if not args.nifti and not args.tiff:
        parser.error("At least one workflow type must be specified: --nifti and/or --tiff")

    if not args.dataset and args.nifti and args.tiff:
        parser.error("Cannot use both --nifti and --tiff without --dataset. "
                      "In file/CLI mode, specify exactly one workflow type.")

    # Validate required config
    missing = validate_config(args)
    if missing:
        print(f"Error: Missing required config:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("\nSet these in .env or pass as CLI args.", file=sys.stderr)
        sys.exit(1)

    # Collect package IDs
    nifti_package_ids = []
    tiff_package_ids = []

    if args.dataset:
        print(f"Discovering MRI packages from dataset {args.dataset_id}...")
        packages = fetch_dataset_packages(args.api_host, args.session_token, args.refresh_token, args.dataset_id)

        skipped = []
        for pkg in packages:
            file_type = classify_package(pkg["name"])
            if file_type == "nifti" and args.nifti:
                nifti_package_ids.append(pkg["node_id"])
            elif file_type == "tiff" and args.tiff:
                tiff_package_ids.append(pkg["node_id"])
            else:
                skipped.append(pkg)

        print(f"\nFound {len(packages)} MRI packages total:")
        if args.nifti:
            print(f"  NIfTI:      {len(nifti_package_ids)}")
        if args.tiff:
            print(f"  OME-TIFF:   {len(tiff_package_ids)}")
        if skipped:
            print(f"  Skipped:    {len(skipped)}")
        print()

        if not nifti_package_ids and not tiff_package_ids:
            print("No matching packages found for the selected workflow type(s).")
            return
    else:
        # File/CLI mode — all IDs go to the single selected workflow
        package_ids = []
        if args.package_file:
            package_ids.extend(load_package_ids(args.package_file))
        if args.package_ids:
            package_ids.extend(args.package_ids)

        if not package_ids:
            parser.error("No package IDs provided. Use a file, --package-id flags, or --dataset.")

        if args.nifti:
            nifti_package_ids = package_ids
        else:
            tiff_package_ids = package_ids

    print(f"Dataset ID:        {args.dataset_id}")
    print(f"API host:          {args.api_host}")

    if not args.dry_run:
        print("\nUsing provided session token.")

    total_succeeded = 0
    total_failed = 0

    if nifti_package_ids:
        nifti_config = build_nifti_config(args)
        s, f = run_workflow(args, nifti_config, nifti_package_ids, "NIfTI")
        total_succeeded += s
        total_failed += f

    if tiff_package_ids:
        tiff_config = build_tiff_config(args)
        s, f = run_workflow(args, tiff_config, tiff_package_ids, "OME-TIFF")
        total_succeeded += s
        total_failed += f

    if not args.dry_run:
        print(f"\nDone. Succeeded: {total_succeeded}, Failed: {total_failed}")


if __name__ == "__main__":
    main()
