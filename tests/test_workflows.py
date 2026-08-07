"""Guard the CI definitions themselves.

A workflow once shipped with two `python -c "..."` blocks whose bodies sat at
column 0 inside a YAML `run: |` scalar. Block-scalar content must be indented
deeper than its key, so the file was not valid YAML and every scheduled run
failed — unnoticed for weeks, because a workflow that cannot parse produces no
useful failure signal and nobody reads a schedule that is never green.

Nothing in the test suite looked at the workflows, so nothing could catch it.
These tests exist so the remaining workflows cannot fail the same way.
"""

import re
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
    with open(path) as f:
        doc = yaml.safe_load(f)
    jobs = doc.get("jobs")
    assert jobs, f"{path.name} declares no jobs"
    for name, job in jobs.items():
        if "uses" in job:  # reusable workflow call, no inline steps
            continue
        assert job.get("steps"), f"{path.name}:{name} declares no steps"


# Matches a repo-script path anywhere in a run block, including a leading ./ and
# surrounding quotes. Token-prefix matching missed all of those, which would have
# made this check quietly vacuous the first time someone wrote `./.github/...`.
SCRIPT_REF = re.compile(r"""(?:^|[\s"'(=])\.?/?(\.github/scripts/[\w./-]+)""")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_referenced_local_scripts_exist(path):
    """A `run:` pointing at a repo script that does not exist fails only at
    schedule time, which for a weekly cron means up to a week of silence."""
    repo = WORKFLOW_DIR.parents[1]
    with open(path) as f:
        doc = yaml.safe_load(f)
    for name, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            for ref in SCRIPT_REF.findall(step.get("run") or ""):
                assert (repo / ref).is_file(), (
                    f"{path.name}:{name} runs missing script {ref}"
                )


@pytest.mark.parametrize(
    "run_line",
    [
        "python .github/scripts/x.py",
        "python ./.github/scripts/x.py",
        'python "./.github/scripts/x.py"',
        "python '.github/scripts/x.py'",
        "cd repo && python .github/scripts/x.py --flag",
        "bash(.github/scripts/x.py)",
    ],
)
def test_script_reference_detection_covers_common_spellings(run_line):
    """The detector is only useful if it actually finds the reference; each of
    these spellings appears in real workflows."""
    assert SCRIPT_REF.findall(run_line) == [".github/scripts/x.py"]


def test_script_reference_detection_ignores_lookalikes():
    """Must not fire on a path that merely contains the fragment."""
    assert SCRIPT_REF.findall("echo vendor/.github/scripts-old/x.py") == []
