# Pennsieve Workflow Scripts

Trigger Pennsieve workflow runs in bulk for a list of package IDs.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials and workflow config
```

## Usage

### Auto-discover packages from a dataset

Use `--discover` to automatically fetch all MRI (`.nii.gz`) packages from the dataset configured in your `.env` (or via `--dataset-id`). Deleted packages are excluded.

```bash
# Discover and trigger
python trigger_workflows.py --discover

# Preview what would be triggered
python trigger_workflows.py --discover --dry-run
```

### Provide package IDs manually

```bash
# From a file (one package ID per line)
python trigger_workflows.py package_ids.txt

# From CLI args
python trigger_workflows.py --package-id PKG1 --package-id PKG2

# Both at once
python trigger_workflows.py package_ids.txt --package-id PKG3
```

### Other options

```bash
# Override config at runtime
python trigger_workflows.py package_ids.txt --workflow-id UUID --dataset-id UUID

# Preview without triggering
python trigger_workflows.py package_ids.txt --dry-run
```

All config can be set via `.env`, environment variables, or CLI flags. CLI flags take precedence.
