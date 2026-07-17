# Pennsieve Workflow Scripts

Trigger Pennsieve workflow runs in bulk for a list of package IDs. Supports two workflows:

- **NIfTI-to-Zarr** — converts `.nii` / `.nii.gz` files
- **OME-TIFF-to-Zarr** — converts `.ome.tiff` / `.ome.tif` files

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials and workflow config
```

## Usage

You must specify at least one workflow type: `--nifti` and/or `--tiff`.

### Auto-discover packages from a dataset

Use `--dataset` to automatically fetch all MRI packages from the dataset, classify them by file extension, and run the selected workflow(s). Deleted packages are excluded.

```bash
# Discover and run both workflows
python trigger_workflows.py --dataset --nifti --tiff

# Only NIfTI packages
python trigger_workflows.py --dataset --nifti

# Only OME-TIFF packages, dry run
python trigger_workflows.py --dataset --tiff --dry-run
```

### Provide package IDs manually

In file/CLI mode, exactly one workflow type must be specified.

```bash
# From a file (one package ID per line)
python trigger_workflows.py package_ids.txt --nifti

# From CLI args
python trigger_workflows.py --package-id PKG1 --package-id PKG2 --tiff

# Both file and CLI args
python trigger_workflows.py package_ids.txt --package-id PKG3 --nifti
```

### Other options

```bash
# Override config at runtime
python trigger_workflows.py package_ids.txt --nifti --workflow-id UUID --dataset-id UUID

# Preview without triggering
python trigger_workflows.py package_ids.txt --nifti --dry-run
```

All config can be set via `.env`, environment variables, or CLI flags. CLI flags take precedence.
