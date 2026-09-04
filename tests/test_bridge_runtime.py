"""Unit tests for :class:`bridge.main.BridgeRuntime`.

The runtime is the testable seam extracted from the inline closures that
previously lived inside ``_real_main`` and were excluded from coverage.
The fixtures here use lightweight stand-in objects for the Kubernetes
``CustomObjectsApi`` so each method is exercised in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest
from kubernetes.client.rest import ApiException

import bridge.main as main
from bridge.prune import terminal_since_key
from bridge.main import (
    BridgeRuntime,
    _count_active_workloads,
    _delete_workload,
    _list_bridge_workloads,
    _list_failed_workloads,
    _list_terminal_candidates,
    _list_workload_tasks,
    _load_by_coder_agent,
    _active_workloads,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Call:
    name: str
    kwargs: Dict[str, Any]


@dataclass
class FakeAPI:
    """Records calls and returns scripted responses.

    ``responses`` maps a method name (e.g. ``"list_namespaced_custom_object"``)
    to a list of values; each call pops the head. ``errors`` maps a method
    name to a list of ``(status, message)`` exceptions to raise before the
    next response is returned.
    """

    responses: Dict[str, List[Any]] = field(default_factory=dict)
    errors: Dict[str, List] = field(default_factory=dict)
    delete_status: int = 200
    calls: List[_Call] = field(default_factory=list)
    sleep_calls: int = 0

    def _consume_error(self, name: str) -> None:
        queued = self.errors.get(name)
        if queued:
            entry = queued.pop(0)
            if entry is None:
                return
            status, message = entry
            raise ApiException(status=status, reason=message)

    def delete_namespaced_custom_object(self, **kwargs: Any) -> Any:
        self.calls.append(_Call("delete_namespaced_custom_object", kwargs))
        self._consume_error("delete_namespaced_custom_object")
        return {"status": "Success", "details": {"status": self.delete_status}}

    def get_namespaced_custom_object(self, **kwargs: Any) -> Any:
        self.calls.append(_Call("get_namespaced_custom_object", kwargs))
        self._consume_error("get_namespaced_custom_object")
        responses = self.responses.get("get_namespaced_custom_object", [])
        return responses.pop(0) if responses else {}

    def list_namespaced_custom_object(self, **kwargs: Any) -> Any:
        self.calls.append(_Call("list_namespaced_custom_object", kwargs))
        self._consume_error("list_namespaced_custom_object")
        responses = self.responses.get("list_namespaced_custom_object", [])
        return responses.pop(0) if responses else {"items": []}

    def patch_namespaced_custom_object(self, **kwargs: Any) -> Any:
        self.calls.append(_Call("patch_namespaced_custom_object", kwargs))
        self._consume_error("patch_namespaced_custom_object")
        responses = self.responses.get("patch_namespaced_custom_object", [])
        return responses.pop(0) if responses else {}

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1


def _workload(name: str, phase: str, *, coder: str | None = None) -> dict:
    spec: Dict[str, Any] = {}
    if coder:
        spec = {"coderAgentRef": {"name": coder}}
    return {
        "metadata": {"name": name, "labels": {"created-by": "dispatch-bridge"}},
        "spec": spec,
        "status": {"phase": phase},
    }


def _task(name: str, workload: str, *, kind: str = "issue-fix", phase: str) -> dict:
    return {
        "metadata": {
            "name": name,
            "labels": {"foreman.llmkube.dev/workload": workload},
        },
        "spec": {"kind": kind},
        "status": {"phase": phase},
    }


def _prfix_workload(name: str, phase: str) -> dict:
    return {
        "metadata": {"name": name, "labels": {"created-by": "dispatch-bridge-prfix"}},
        "status": {"phase": phase},
    }


# ---------------------------------------------------------------------------
# _list_bridge_workloads / _list_failed_workloads / _active_workloads
# ---------------------------------------------------------------------------


class TestListBridgeWorkloads:
    def test_returns_items(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running"), _workload("b", "Failed")]}
                ]
            }
        )
        result = _list_bridge_workloads(api, "ns")
        assert [w["metadata"]["name"] for w in result] == ["a", "b"]

    def test_uses_dispatch_bridge_label(self) -> None:
        api = FakeAPI(
            responses={"list_namespaced_custom_object": [{"items": []}]}
        )
        _list_bridge_workloads(api, "ns")
        assert api.calls[0].kwargs["label_selector"] == "created-by=dispatch-bridge"


class TestListFailedWorkloads:
    def test_only_includes_failed_phases(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("a", "Failed"),
                            _workload("b", "Running"),
                            _workload("c", "Timeout"),
                            _workload("d", "Cancelled"),
                            _workload("e", "Succeeded"),
                        ]
                    }
                ]
            }
        )
        result = _list_failed_workloads(api, "ns")
        assert [w["metadata"]["name"] for w in result] == ["a"]


class TestActiveWorkloads:
    def test_excludes_terminal_phases(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("running", "Running"),
                            _workload("pending", "Pending"),
                            _workload("done", "Succeeded"),
                            _workload("failed", "Failed"),
                        ]
                    }
                ]
            }
        )
        result = _active_workloads(api, "ns")
        assert [w["metadata"]["name"] for w in result] == ["running", "pending"]

    def test_excludes_completed_phase(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("running", "Running"),
                            _workload("completed", "Completed"),
                        ]
                    }
                ]
            }
        )
        result = _active_workloads(api, "ns")
        assert [w["metadata"]["name"] for w in result] == ["running"]


# ---------------------------------------------------------------------------
# _count_active_workloads
# ---------------------------------------------------------------------------


class TestCountActiveWorkloads:
    def test_excludes_terminal_phases(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("w-running", "Running"),
                            _workload("w-pending", "Pending"),
                            _workload("w-done", "Succeeded"),
                            _workload("w-failed", "Failed"),
                            _workload("w-cancelled", "Cancelled"),
                        ]
                    }
                ]
            }
        )
        assert _count_active_workloads(api, "ns") == 2

    def test_excludes_completed_phase(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("w-running", "Running"),
                            _workload("w-completed", "Completed"),
                        ]
                    }
                ]
            }
        )
        assert _count_active_workloads(api, "ns") == 1

    def test_includes_prfix_when_requested(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("w1", "Running"),
                        ]
                    },
                    {
                        "items": [
                            _prfix_workload("w2", "Running"),
                        ]
                    },
                ]
            }
        )
        assert (
            _count_active_workloads(
                api,
                "ns",
                include_prfix=True,
                prfix_created_by="dispatch-bridge-prfix",
            )
            == 2
        )

    def test_excludes_prfix_by_default(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("w1", "Running"),
                        ]
                    }
                ]
            }
        )
        assert _count_active_workloads(api, "ns") == 1


# ---------------------------------------------------------------------------
# _load_by_coder_agent
# ---------------------------------------------------------------------------


class TestLoadByCoderAgent:
    def test_counts_running_issue_fix(self) -> None:
        # An active Workload with a non-terminal issue-fix task is busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {"items": [_task("t1", "a", phase="Running")]},
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_terminal_issue_fix_with_running_review_frees_slot(self) -> None:
        # A terminal issue-fix task plus a running review does NOT hold the
        # coder busy: only issue-fix tasks count, and only while non-terminal.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {
                        "items": [
                            _task("t1", "a", kind="issue-fix", phase="Succeeded"),
                            _task("t2", "a", kind="review", phase="Running"),
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {}

    def test_no_tasks_counts_busy(self) -> None:
        # A Workload with no matching task fails closed and stays busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {"items": []},
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_api_error_counts_busy(self) -> None:
        # A task-list exception fails closed: every active ref is busy.
        api = FakeAPI(
            errors={"list_namespaced_custom_object": [None, (500, "boom")]},
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]}
                ]
            },
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_missing_task_label_counts_busy(self) -> None:
        # A task missing the workload label fails closed: every active ref busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {
                        "items": [
                            {
                                "metadata": {"name": "t1"},
                                "spec": {"kind": "issue-fix"},
                                "status": {"phase": "Succeeded"},
                            }
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_unknown_issue_fix_phase_counts_busy(self) -> None:
        # An issue-fix task in an unknown phase fails closed and stays busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {"items": [_task("t1", "a", phase="Unknown")]},
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_missing_issue_fix_phase_counts_busy(self) -> None:
        # A missing phase fails closed and keeps the coder busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {
                        "items": [
                            {
                                "metadata": {
                                    "labels": {"foreman.llmkube.dev/workload": "a"}
                                },
                                "spec": {"kind": "issue-fix"},
                                "status": {},
                            }
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_review_only_task_counts_busy(self) -> None:
        # A review-only workload has no resolvable issue-fix task, so it is busy.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {"items": [_task("t1", "a", kind="review", phase="Running")]},
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_mixed_terminality_counts_busy(self) -> None:
        # One terminal issue-fix task still leaves the coder busy when another
        # issue-fix task is still running (all must be terminal).
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Running", coder="coder-py")]},
                    {
                        "items": [
                            _task("t1", "a", phase="Succeeded"),
                            _task("t2", "a", phase="Running"),
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {"coder-py": 1}

    def test_uses_workload_label_selector(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": []},
                    {"items": []},
                ]
            }
        )
        _load_by_coder_agent(api, "ns")
        task_calls = [
            c for c in api.calls if c.kwargs.get("plural") == "agentictasks"
        ]
        assert len(task_calls) == 1
        assert task_calls[0].kwargs["label_selector"] == "foreman.llmkube.dev/workload"

    def test_single_task_list_call(self) -> None:
        # Both workloads' tasks come from the one AgenticTask list call.
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("a", "Running", coder="coder-py"),
                            _workload("b", "Running", coder="coder-go"),
                        ]
                    },
                    {
                        "items": [
                            _task("t1", "a", phase="Running"),
                            _task("t2", "b", phase="Running"),
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {
            "coder-py": 1,
            "coder-go": 1,
        }
        task_calls = [
            c for c in api.calls if c.kwargs.get("plural") == "agentictasks"
        ]
        assert len(task_calls) == 1

    def test_groups_active_workloads(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("a", "Running", coder="coder-py"),
                            _workload("b", "Running", coder="coder-py"),
                            _workload("c", "Running", coder="coder-go"),
                            _workload("d", "Running"),
                        ]
                    },
                    {
                        "items": [
                            _task("t1", "a", phase="Running"),
                            _task("t2", "b", phase="Running"),
                            _task("t3", "c", phase="Running"),
                        ]
                    },
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {
            "coder-py": 2,
            "coder-go": 1,
        }

    def test_skips_terminal_workloads(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Succeeded", coder="coder-py")]},
                    {"items": [_task("t1", "a", phase="Running")]},
                ]
            }
        )
        assert _load_by_coder_agent(api, "ns") == {}


# ---------------------------------------------------------------------------
# _list_terminal_candidates
# ---------------------------------------------------------------------------


class TestListTerminalCandidates:
    def test_concatenates_bridge_and_prfix(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Succeeded")]},
                    {"items": [_prfix_workload("b", "Failed")]},
                ]
            }
        )
        result = _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        assert [w["metadata"]["name"] for w in result] == ["a", "b"]

    def test_stamp_failure_is_logged_without_raising(self) -> None:
        """The handler must survive a failing PATCH. "name" is a reserved
        LogRecord attribute, so passing it via extra raises KeyError inside
        logging and takes the whole tick down with it — the failure path has to
        actually run in a test, not carry a no-cover pragma."""
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Failed")]},
                    {"items": []},
                ]
            },
            errors={"patch_namespaced_custom_object": [(404, "not found")]},
        )
        result = _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        # The candidate list is still returned so prune proceeds on the fallback.
        assert [w["metadata"]["name"] for w in result] == ["a"]

    def test_failed_stamp_does_not_leave_the_annotation_behind(self) -> None:
        """stamp_terminal_since mutates the manifest in place, so a failed PATCH
        must drop the annotation again. Retaining it makes terminal_since read
        "now" for every terminal Workload on every tick, and prune can never
        reach a TTL — prune silently stopped in 0.6.29 for exactly this reason."""
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Completed")]},
                    {"items": []},
                ]
            },
            errors={"patch_namespaced_custom_object": [(403, "forbidden")]},
        )
        result = _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        annotations = (result[0].get("metadata") or {}).get("annotations") or {}
        assert not [k for k in annotations if "terminal-since" in k], annotations

    def test_successful_stamp_keeps_the_annotation(self) -> None:
        """On success the cluster holds the stamp, so the returned manifest must
        match it — prune should measure age from the observation, not re-stamp."""
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Completed")]},
                    {"items": []},
                ]
            }
        )
        result = _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        annotations = (result[0].get("metadata") or {}).get("annotations") or {}
        assert terminal_since_key("Completed") in annotations

    def test_stamps_terminal_since_on_the_workloads_plural(self) -> None:
        """The stamp is persisted with a PATCH; the plural must be the real CRD
        name. "agenticworkloads" does not exist, so a wrong plural 404s, gets
        swallowed by the handler, and the annotation silently never persists —
        leaving terminal_since to fall back to creationTimestamp."""
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Failed")]},
                    {"items": []},
                ]
            }
        )
        _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        patches = [c for c in api.calls if c.name == "patch_namespaced_custom_object"]
        assert len(patches) == 1, api.calls
        assert patches[0].kwargs["plural"] == "workloads"
        assert patches[0].kwargs["group"] == "foreman.llmkube.dev"
        assert patches[0].kwargs["name"] == "a"
        annotations = patches[0].kwargs["body"]["metadata"]["annotations"]
        assert terminal_since_key("Failed") in annotations

    def test_uses_dedicated_label_selectors(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": []},
                    {"items": []},
                ]
            }
        )
        _list_terminal_candidates(api, "ns", "dispatch-bridge-prfix")
        selectors = [
            c.kwargs.get("label_selector")
            for c in api.calls
            if c.name == "list_namespaced_custom_object"
        ]
        assert selectors == [
            "created-by=dispatch-bridge",
            "created-by=dispatch-bridge-prfix",
        ]


# ---------------------------------------------------------------------------
# _list_workload_tasks
# ---------------------------------------------------------------------------


class TestListWorkloadTasks:
    def test_uses_workload_label_selector(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [{"metadata": {"name": "t1"}}]}
                ]
            }
        )
        result = _list_workload_tasks(api, "ns", "wl-1")
        assert len(result) == 1
        call = api.calls[0]
        assert call.kwargs["label_selector"] == "foreman.llmkube.dev/workload=wl-1"
        assert call.kwargs["plural"] == "agentictasks"
        assert call.kwargs["namespace"] == "ns"

    def test_returns_empty_when_no_tasks(self) -> None:
        api = FakeAPI(
            responses={"list_namespaced_custom_object": [{"items": []}]}
        )
        assert _list_workload_tasks(api, "ns", "wl-1") == []

    def test_503_retries_and_continues(self, monkeypatch) -> None:
        monkeypatch.setattr(main.time, "sleep", lambda _delay: None)
        api = FakeAPI(
            responses={"list_namespaced_custom_object": [
                {"items": [{"metadata": {"name": "t1"}}]},
            ]},
            errors={"list_namespaced_custom_object": [
                (503, "Service Unavailable"),
            ]},
        )

        result = _list_workload_tasks(api, "ns", "wl-1")

        assert result == [{"metadata": {"name": "t1"}}]
        assert len(api.calls) == 2


# ---------------------------------------------------------------------------
# _delete_workload
# ---------------------------------------------------------------------------


class TestDeleteWorkload:
    def test_foreground_poll_succeeds_after_404(self) -> None:
        # delete ok, then the first poll get returns 404 -> success.
        api = FakeAPI(
            errors={"get_namespaced_custom_object": [(404, "missing")]},
        )
        _delete_workload(api, "ns", "w1", timeout=2)
        names = [c.name for c in api.calls]
        assert names[0] == "delete_namespaced_custom_object"
        assert "get_namespaced_custom_object" in names

    def test_tolerates_404_on_initial_delete(self) -> None:
        api = FakeAPI(
            errors={"delete_namespaced_custom_object": [(404, "missing")]},
        )
        _delete_workload(api, "ns", "w1", timeout=2)
        names = [c.name for c in api.calls]
        assert names == ["delete_namespaced_custom_object"]

    def test_propagation_policy_is_foreground(self) -> None:
        api = FakeAPI(errors={"get_namespaced_custom_object": [(404, "missing")]})
        _delete_workload(api, "ns", "w1", timeout=2)
        delete_call = api.calls[0]
        body = delete_call.kwargs["body"]
        assert body.propagation_policy == "Foreground"

    def test_raises_timeout_when_workload_persists(self) -> None:
        # delete ok, then every poll get returns the workload still present.
        api = FakeAPI(
            responses={
                "get_namespaced_custom_object": [
                    {
                        "metadata": {"name": "w1"},
                        "status": {"phase": "Terminating"},
                    }
                ]
            }
        )
        with pytest.raises(TimeoutError):
            _delete_workload(api, "ns", "w1", timeout=0)
        assert api.sleep_calls == 0


# ---------------------------------------------------------------------------
# BridgeRuntime
# ---------------------------------------------------------------------------


def _runtime(api: FakeAPI) -> BridgeRuntime:
    return BridgeRuntime(
        api=api, namespace="ns", prfix_created_by="dispatch-bridge-prfix"
    )


class TestBridgeRuntime:
    def test_count_active_workloads_delegates(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("a", "Running"),
                            _workload("b", "Succeeded"),
                        ]
                    }
                ]
            }
        )
        assert _runtime(api).count_active_workloads() == 1

    def test_delete_workload_delegates_with_timeout(self) -> None:
        api = FakeAPI(errors={"get_namespaced_custom_object": [(404, "missing")]})
        _runtime(api).delete_workload("w1")
        assert any(
            c.name == "delete_namespaced_custom_object" for c in api.calls
        )

    def test_list_workload_tasks_delegates(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [{"metadata": {"name": "t1"}}]}
                ]
            }
        )
        result = _runtime(api).list_workload_tasks("wl-1")
        assert result and result[0]["metadata"]["name"] == "t1"

    def test_load_by_coder_agent_delegates(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            _workload("a", "Running", coder="coder-py"),
                        ]
                    },
                    {
                        "items": [
                            _task("t1", "a", phase="Running"),
                        ]
                    },
                ]
            }
        )
        assert _runtime(api).load_by_coder_agent() == {"coder-py": 1}

    def test_list_terminal_candidates_uses_prfix_label(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {"items": [_workload("a", "Succeeded")]},
                    {"items": [_prfix_workload("b", "Failed")]},
                ]
            }
        )
        result = _runtime(api).list_terminal_candidates()
        assert [w["metadata"]["name"] for w in result] == ["a", "b"]
