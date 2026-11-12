# Workspace Telemetry

A local-first Python telemetry and work-session service with explicit evidence, retention and mocked integration tests.

**Status:** Development service. Configure private destinations locally; review optional integration and retention settings before use. No personal activity history is included.

[Project portfolio](https://zacharysturman.com/projects/workspace-telemetry)

## Quick start

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
```

## Usage

```sh
./run.sh --help
```

## How it works

A Python package separates normalized event ingestion, work-session state, evidence exports and optional Notion writes. Tests use temporary directories and mocked remote calls.

## Testing

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_work_session.py -q
```

See [validation and next steps](docs/NEXT_STEPS.md) for the checks performed on this snapshot. Automated fixture tests do not establish physical-device compatibility.

## Configuration and privacy

This is a curated source snapshot, published November 12, 2026. Configure your own endpoints, credentials and signing identities. Private development history, local environment files, personal recordings, runtime databases, signing materials and deployment automation are excluded. Deployment and device installation are explicit operator actions.

## License and attribution

Existing licenses and third-party notices apply to the files they cover. Publication of this snapshot does not grant a new license to original material; no additional license is granted where none is supplied.
