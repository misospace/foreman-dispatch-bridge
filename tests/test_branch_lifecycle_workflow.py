"""Guard the branch-lifecycle-check workflow's issue-filing step.

The scheduled job red-ed twice in a row on the default branch (#301, #303)
because `gh issue create --label branch-lifecycle` hard-fails with
"could not add label: 'branch-lifecycle' not found" when the label does not
exist in the repo. The workflow never created the label, so every run that
found a stale branch died at the filing step.

These tests extract the real `run:` script of the "File or update tracking
issue" step and execute it against a fake `gh` on PATH, so a regression that
re-introduces an unconditional `--label` fails here instead of on the next
daily schedule.
"""

import os
import stat
import subprocess
from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "branch-lifecycle-check.yaml"
)

FAKE_GH = """\
#!/usr/bin/env bash
log() { printf '%s\\n' "$*" >> "$GH_LOG"; }
case "$1" in
  label)
    log "label $2"
    if [ "$2" = "list" ]; then
      if [ -n "${GH_LABEL_EXISTS:-}" ]; then
        printf 'branch-lifecycle\\n'
      fi
    elif [ "$2" = "create" ]; then
      if [ -n "${GH_LABEL_CREATE_OK:-}" ]; then
        exit 0
      fi
      echo "could not create label: permission denied" >&2
      exit 1
    fi
    ;;
  api)
    log "api $2"
    printf ''
    ;;
  issue)
    log "issue $2"
    if [ "$2" = "create" ]; then
      shift 2
      log "create-args $*"
      echo "https://github.com/misospace/foreman-dispatch-bridge/issues/301"
    fi
    ;;
esac
exit 0
"""


def _file_step_script() -> str:
    with open(WORKFLOW) as f:
        doc = yaml.safe_load(f)
    steps = doc["jobs"]["stale-branches"]["steps"]
    for step in steps:
        if step.get("name") == "File or update tracking issue":
            return step["run"]
    raise AssertionError("workflow has no 'File or update tracking issue' step")


def _run_step(tmp_path: Path, env_extra: dict) -> list[str]:
    """Execute the real step script with a fake `gh`; return the call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    work = tmp_path / "work"
    work.mkdir()
    (work / "stale_branches.tsv").write_text(
        "foreman/wl-x/issue-1\t" + "a" * 40 + "\t21\n"
    )

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_TOKEN": "fake",
        "REPO": "misospace/foreman-dispatch-bridge",
        "RUN_URL": "https://github.com/misospace/foreman-dispatch-bridge/actions/runs/1",
        "STALE_TSV": "stale_branches.tsv",
        "GH_LOG": str(tmp_path / "gh.log"),
    }
    env.update(env_extra)

    result = subprocess.run(
        ["bash", "-e"],
        input=_file_step_script(),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"step script failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    log_path = tmp_path / "gh.log"
    assert log_path.is_file(), "fake gh was never invoked"
    return log_path.read_text().splitlines()


def test_missing_label_is_created_before_filing(tmp_path):
    """The label does not exist and creation is permitted: the step must
    create it and file the issue with it, without failing."""
    log = _run_step(tmp_path, {"GH_LABEL_EXISTS": "", "GH_LABEL_CREATE_OK": "1"})
    assert "label create" in log
    create_args = next(line for line in log if line.startswith("create-args"))
    assert "--label" in create_args and "branch-lifecycle" in create_args, create_args


def test_existing_label_is_not_recreated(tmp_path):
    """The label already exists: no create call, issue filed with the label."""
    log = _run_step(tmp_path, {"GH_LABEL_EXISTS": "1", "GH_LABEL_CREATE_OK": ""})
    assert "label create" not in log
    create_args = next(line for line in log if line.startswith("create-args"))
    assert "--label" in create_args and "branch-lifecycle" in create_args, create_args


def test_label_create_failure_still_files_the_issue(tmp_path):
    """Creation is not permitted (token lacks the right): the step must still
    file the issue — without the label — instead of failing the run. This is
    the exact failure mode of #301/#303: the finding was delivered only after
    the job had already red-ed."""
    log = _run_step(tmp_path, {"GH_LABEL_EXISTS": "", "GH_LABEL_CREATE_OK": ""})
    assert "label create" in log
    create_args = next(line for line in log if line.startswith("create-args"))
    assert "--label" not in create_args, create_args


# --- Close tracking issue when no stale branches remain ---------------------
#
# The workflow filed a tracking issue when stale branches existed but never
# closed one when they were all merged or deleted, so tracking issues (e.g.
# #317) lingered long after the branches they flagged were gone. These tests
# guard the new "Close tracking issue when no stale branches remain" step.

FAKE_GH_CLOSE = """\
#!/usr/bin/env bash
log() { printf '%s\\n' "$*" >> "$GH_LOG"; }
case "$1" in
  api)
    log "api $2"
    # Emit the open tracking-issue numbers the step is expected to close.
    if [ -n "${GH_OPEN_ISSUES:-}" ]; then
      printf '%s\\n' "${GH_OPEN_ISSUES}"
    fi
    ;;
  issue)
    log "issue $2"
    if [ "$2" = "close" ]; then
      shift 2
      log "close-args $*"
    fi
    ;;
esac
exit 0
"""


def _close_step_script() -> str:
    with open(WORKFLOW) as f:
        doc = yaml.safe_load(f)
    steps = doc["jobs"]["stale-branches"]["steps"]
    for step in steps:
        if step.get("name") == "Close tracking issue when no stale branches remain":
            return step["run"]
    raise AssertionError("workflow has no 'Close tracking issue when no stale branches remain' step")


def _run_close_step(tmp_path: Path, open_issues: str) -> list[str]:
    """Execute the real close step with a fake `gh`; return the call log.

    `open_issues` is a newline-separated list of issue numbers the fake `gh`
    reports as open (empty string means none).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH_CLOSE)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    work = tmp_path / "work"
    work.mkdir()

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_TOKEN": "fake",
        "REPO": "misospace/foreman-dispatch-bridge",
        "GH_LOG": str(tmp_path / "gh.log"),
        "GH_OPEN_ISSUES": open_issues,
    }

    result = subprocess.run(
        ["bash", "-e"],
        input=_close_step_script(),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"close step failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    log_path = tmp_path / "gh.log"
    assert log_path.is_file(), "fake gh was never invoked"
    return log_path.read_text().splitlines()


def test_close_step_closes_each_open_tracking_issue(tmp_path):
    """With no stale branches, every open tracking issue is closed."""
    log = _run_close_step(tmp_path, "317\n318")
    closed = [line for line in log if line.startswith("close-args")]
    assert len(closed) == 2, closed
    assert any("317" in line for line in closed), closed
    assert any("318" in line for line in closed), closed


def test_close_step_noop_when_no_open_issues(tmp_path):
    """No open tracking issue: the step exits cleanly without calling close."""
    log = _run_close_step(tmp_path, "")
    assert not any(line.startswith("close-args") for line in log), log
