"""Guard the CI definitions themselves.

A workflow once shipped with two `python -c "..."` blocks whose bodies sat at
column 0 inside a YAML `run: |` scalar. Block-scalar content must be indented
deeper than its key, so the file was not valid YAML and every scheduled run
failed — unnoticed for weeks, because a workflow that cannot parse produces no
useful failure signal and nobody reads a schedule that is never green.

Nothing in the test suite looked at the workflows, so nothing could catch it.
These tests exist so the remaining workflows cannot fail the same way.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.y*ml"))


def test_workflow_directory_is_not_empty():
    """A glob that silently matches nothing would make every test below vacuous."""
    assert WORKFLOWS, f"no workflows found under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path):
    with open(path) as f:
        doc = yaml.safe_load(f)
    assert isinstance(doc, dict), f"{path.name} did not parse to a mapping"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_has_jobs_with_steps(path):
    """Parsing is necessary but not sufficient — a truncated block scalar can still
    yield a mapping while silently dropping the steps that were meant to run."""
    doc = yaml.safe_load(open(path))
    jobs = doc.get("jobs")
    assert jobs, f"{path.name} declares no jobs"
    for name, job in jobs.items():
        if "uses" in job:  # reusable workflow call, no inline steps
            continue
        assert job.get("steps"), f"{path.name}:{name} declares no steps"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_referenced_local_scripts_exist(path):
    """A `run:` pointing at a repo script that does not exist fails only at
    schedule time, which for a weekly cron means up to a week of silence."""
    repo = WORKFLOW_DIR.parents[1]
    doc = yaml.safe_load(open(path))
    for name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            for token in run.split():
                if token.startswith(".github/scripts/"):
                    assert (repo / token).is_file(), (
                        f"{path.name}:{name} runs missing script {token}"
                    )
