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
            status, message = queued.pop(0)
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

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1


def _workload(name: str, phase: str) -> dict:
    return {
        "metadata": {"name": name, "labels": {"created-by": "dispatch-bridge"}},
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
        assert [w["metadata"]["name"] for w in result] == ["a", "c", "d"]


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
    def test_groups_active_workloads(self) -> None:
        api = FakeAPI(
            responses={
                "list_namespaced_custom_object": [
                    {
                        "items": [
                            {
                                "metadata": {"name": "a"},
                                "spec": {"coderAgentRef": {"name": "coder-py"}},
                                "status": {"phase": "Running"},
                            },
                            {
                                "metadata": {"name": "b"},
                                "spec": {"coderAgentRef": {"name": "coder-py"}},
                                "status": {"phase": "Running"},
                            },
                            {
                                "metadata": {"name": "c"},
                                "spec": {"coderAgentRef": {"name": "coder-go"}},
                                "status": {"phase": "Running"},
                            },
                            {
                                "metadata": {"name": "d"},
                                "spec": {},
                                "status": {"phase": "Running"},
                            },
                        ]
                    }
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
                    {
                        "items": [
                            {
                                "metadata": {"name": "a"},
                                "spec": {"coderAgentRef": {"name": "coder-py"}},
                                "status": {"phase": "Succeeded"},
                            },
                        ]
                    }
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
                            {
                                "metadata": {"name": "a"},
                                "spec": {"coderAgentRef": {"name": "coder-py"}},
                                "status": {"phase": "Running"},
                            }
                        ]
                    }
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
