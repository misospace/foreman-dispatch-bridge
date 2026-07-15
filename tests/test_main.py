from unittest.mock import Mock

from bridge.models import ClaimedItem
import bridge.main as main_module
from bridge.main import run_once

LANES = ["local", "cloud", "frontier"]


def _claim_stub(mapping):
    # mapping: lane -> ClaimedItem | None. Each lane's item is served once, then
    # None, so run_once's per-lane drain loop terminates.
    served = set()

    def claim_one(agent_name, lane):
        if lane in served:
            return None
        served.add(lane)
        return mapping.get(lane)
    return claim_one


def test_creates_one_workload_per_claimed_lane():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
                   created.append, namespace="llm")
    assert res == ["local:created:wl-a-b-3", "cloud:empty", "frontier:empty"]
    assert len(created) == 1
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"
    assert created[0]["metadata"]["labels"]["lane"] == "local"


def test_all_empty_creates_nothing():
    created = []
    res = run_once(LANES, "foreman/coder", _claim_stub({}), created.append, namespace="llm")
    assert res == ["local:empty", "cloud:empty", "frontier:empty"]
    assert created == []


def test_stamps_matching_gate_profile_on_workload():
    created = []
    item = ClaimedItem(repo="misospace/dispatch", issue_number=7, intent="fix", lane="local")
    profiles = {"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
             created.append, namespace="llm", gate_profiles=profiles)
    assert created[0]["spec"]["gateProfile"] == {"language": "node"}


def test_unmatched_repo_falls_back_to_wildcard_gate_profile():
    created = []
    item = ClaimedItem(repo="misospace/windowstead", issue_number=1, intent="fix", lane="cloud")
    profiles = {"misospace/dispatch": {"language": "node"}, "*": {"language": "generic"}}
    run_once(LANES, "foreman/coder", _claim_stub({"cloud": item}),
             created.append, namespace="llm", gate_profiles=profiles)
    assert created[0]["spec"]["gateProfile"] == {"language": "generic"}


def test_no_gate_profiles_leaves_workload_without_one():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append, namespace="llm")
    assert "gateProfile" not in created[0]["spec"]


def test_passes_agent_name_and_lane_through():
    seen = []
    def claim_one(agent_name, lane):
        seen.append((agent_name, lane))
        return None
    run_once(LANES, "foreman/coder", claim_one, lambda m: None, namespace="llm")
    assert seen == [("foreman/coder", "local"), ("foreman/coder", "cloud"), ("foreman/coder", "frontier")]


def test_cap_stops_claiming_at_max_in_progress():
    # Already at the cap: no lane should claim, each reports capped.
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item, "cloud": item, "frontier": item}),
                   created.append, namespace="llm", in_progress=10, max_in_progress=10)
    assert created == []
    assert all(r.endswith("capped:10/10") for r in res)


def test_cap_allows_claims_up_to_remaining_headroom():
    # 9 in progress, cap 10: exactly one more claim, then capped.
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    res = run_once(LANES, "foreman/coder", _claim_stub({"local": item, "cloud": item, "frontier": item}),
                   created.append, namespace="llm", in_progress=9, max_in_progress=10)
    assert len(created) == 1
    assert res[0] == "local:created:wl-a-b-3"
    assert res[1].endswith("capped:10/10") and res[2].endswith("capped:10/10")


def test_drains_lane_up_to_headroom():
    # A lane with 3 ready items and headroom for 2 claims both, then stops at cap.
    created = []
    items = [ClaimedItem(repo="a/b", issue_number=n, intent="x", lane="local")
             for n in (1, 2, 3)]

    def claim_one(agent_name, lane):
        return items.pop(0) if lane == "local" and items else None

    res = run_once(["local"], "foreman/coder", claim_one, created.append,
                   namespace="llm", in_progress=8, max_in_progress=10)
    assert len(created) == 2  # only the 2 slots of headroom
    assert res == ["local:created:wl-a-b-1", "local:created:wl-a-b-2"]


def test_drains_whole_lane_when_uncapped():
    # No cap: a lane with multiple ready items is fully drained in one tick.
    created = []
    items = [ClaimedItem(repo="a/b", issue_number=n, intent="x", lane="local")
             for n in (1, 2, 3)]

    def claim_one(agent_name, lane):
        return items.pop(0) if lane == "local" and items else None

    res = run_once(["local"], "foreman/coder", claim_one, created.append,
                   namespace="llm")
    assert len(created) == 3
    assert res == ["local:created:wl-a-b-1", "local:created:wl-a-b-2", "local:created:wl-a-b-3"]


def test_cap_zero_is_uncapped():
    created = []
    item = ClaimedItem(repo="a/b", issue_number=3, intent="fix", lane="local")
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}),
             created.append, namespace="llm", in_progress=999, max_in_progress=0)
    assert len(created) == 1  # cap 0 => no gating even with high in_progress


def test_base_lane_routes_to_python_coder_by_repo_language():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=1, intent="fix", lane="local")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    base_coder_agents = {"python": "coder-python", "node": "coder-node", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-python"


def test_base_lane_routes_to_node_coder_by_repo_language():
    created = []
    item = ClaimedItem(repo="misospace/dispatch", issue_number=2, intent="fix", lane="local")
    gate_profiles = {"misospace/dispatch": {"language": "node"}}
    base_coder_agents = {"python": "coder-python", "node": "coder-node", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-node"


def test_base_lane_no_gate_profile_falls_back_to_wildcard_coder():
    created = []
    item = ClaimedItem(repo="misospace/unmapped", issue_number=3, intent="fix", lane="local")
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_base_lane_generic_language_falls_back_to_wildcard_coder():
    created = []
    item = ClaimedItem(repo="misospace/generic-repo", issue_number=4, intent="fix", lane="local")
    gate_profiles = {"misospace/generic-repo": {"language": "generic"}}
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


def test_frontier_lane_wins_over_language_routing():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=5, intent="fix", lane="frontier")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    lane_coder_agents = {"frontier": "coder-frontier"}
    base_coder_agents = {"python": "coder-python", "*": "coder"}
    run_once(LANES, "foreman/coder", _claim_stub({"frontier": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles,
             lane_coder_agents=lane_coder_agents, base_coder_agents=base_coder_agents)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder-frontier"


def test_empty_base_coder_agents_is_legacy_behavior():
    created = []
    item = ClaimedItem(repo="misospace/miso-py", issue_number=6, intent="fix", lane="local")
    gate_profiles = {"misospace/miso-py": {"language": "python"}}
    run_once(LANES, "foreman/coder", _claim_stub({"local": item}), created.append,
             namespace="llm", gate_profiles=gate_profiles)
    assert created[0]["spec"]["coderAgentRef"]["name"] == "coder"


class FakeApiException(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def test_count_active_workloads_filters_terminal_phases():
    api = Mock()
    api.list_namespaced_custom_object.return_value = {
        "items": [
            {"status": {"phase": "Running"}},
            {"status": {"phase": "Completed"}},
            {"status": {"phase": "Failed"}},
            {"status": None},
        ]
    }

    assert main_module.count_active_workloads(api, "llm") == 2
    api.list_namespaced_custom_object.assert_called_once_with(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace="llm",
        plural="workloads",
        label_selector="created-by=dispatch-bridge",
    )


def test_list_terminal_candidates_deduplicates_selectors():
    bridge = {"metadata": {"name": "issue-workload"}}
    shared = {"metadata": {"name": "shared-workload"}}
    prfix = {"metadata": {"name": "prfix-workload"}}
    api = Mock()

    def list_objects(**kwargs):
        if kwargs["label_selector"] == "created-by=dispatch-bridge":
            return {"items": [bridge, shared]}
        return {"items": [shared, prfix]}

    api.list_namespaced_custom_object.side_effect = list_objects

    assert main_module.list_terminal_candidates(api, "llm") == [bridge, shared, prfix]
    assert [
        call.kwargs["label_selector"] for call in api.list_namespaced_custom_object.call_args_list
    ] == ["created-by=dispatch-bridge", "created-by=dispatch-bridge-prfix"]


def test_delete_workload_uses_foreground_and_polls_until_404():
    api = Mock()
    api.get_namespaced_custom_object.side_effect = [
        {"metadata": {"name": "wl"}},
        FakeApiException(404),
    ]
    delete_options = Mock(return_value={"foreground": True})
    sleeps = []

    main_module.delete_workload(
        api,
        "llm",
        "wl",
        api_exception=FakeApiException,
        delete_options_factory=delete_options,
        sleep=sleeps.append,
    )

    delete_options.assert_called_once_with(propagation_policy="Foreground")
    api.delete_namespaced_custom_object.assert_called_once_with(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace="llm",
        plural="workloads",
        name="wl",
        body={"foreground": True},
    )
    assert api.get_namespaced_custom_object.call_count == 2
    assert sleeps == [1]


def test_list_workload_tasks_uses_workload_label_selector():
    tasks = [{"metadata": {"name": "review"}}]
    api = Mock()
    api.list_namespaced_custom_object.return_value = {"items": tasks}

    assert main_module.list_workload_tasks(api, "llm", "wl") == tasks
    api.list_namespaced_custom_object.assert_called_once_with(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace="llm",
        plural="agentictasks",
        label_selector="foreman.llmkube.dev/workload=wl",
    )


def test_http_and_workload_helpers_use_injected_transports():
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True}
    request_get = Mock(return_value=response)
    request_post = Mock(return_value=response)

    assert main_module.http_get(request_get, "https://dispatch/items", {"x": "y"}) == {"ok": True}
    assert main_module.http_post(request_post, "https://dispatch/claim", {}, {"lane": "local"}) == {"ok": True}
    request_get.assert_called_once_with("https://dispatch/items", headers={"x": "y"}, timeout=20)
    request_post.assert_called_once_with(
        "https://dispatch/claim", headers={}, json={"lane": "local"}, timeout=30
    )

    conflict = Mock(status_code=409)
    assert main_module.http_post(Mock(return_value=conflict), "url", {}, {}) is None
    conflict.raise_for_status.assert_not_called()

    api = Mock()
    manifest = {"metadata": {"name": "wl"}}
    main_module.create_workload(api, "llm", manifest, FakeApiException)
    api.create_namespaced_custom_object.assert_called_once_with(
        group="foreman.llmkube.dev",
        version="v1alpha1",
        namespace="llm",
        plural="workloads",
        body=manifest,
    )


def test_retry_and_dispatch_helpers_are_testable_with_fakes():
    api = Mock()
    api.list_namespaced_custom_object.return_value = {
        "items": [
            {"metadata": {"name": "failed"}, "status": {"phase": "Failed"}},
            {"metadata": {"name": "running"}, "status": {"phase": "Running"}},
        ]
    }
    assert [workload["metadata"]["name"] for workload in main_module.list_failed_workloads(api, "llm")] == [
        "failed"
    ]

    review_task = {
        "spec": {"kind": "review"},
        "status": {
            "verdict": "NO-GO",
            "result": {"extra": {"modelSummary": "add a regression test"}},
        },
    }
    api.list_namespaced_custom_object.return_value = {"items": [review_task]}
    assert "add a regression test" in main_module.feedback_for(api, "llm", "failed")

    dispatch = Mock()
    dispatch.find_issue_id.return_value = "issue-id"
    dispatch.escalate.return_value = True
    dispatch.mark_pr_fix.return_value = True
    dispatch.list_pr_fix_queued.return_value = [{"repo": "org/repo"}]
    item = ClaimedItem("org/repo", 7, "fix", "local", "issue-id")

    assert main_module.lookup_issue_id(dispatch, "agent", ["local"], item) == "issue-id"
    assert main_module.escalate_workload(dispatch, "agent", "frontier", 3, item) is True
    assert "3 failed attempts" in dispatch.escalate.call_args.args[2]
    assert main_module.mark_pr_fix(dispatch, "org/repo", 8, "FIXED", "done") is True
    assert main_module.list_queued_pr_fixes(dispatch) == [{"repo": "org/repo"}]


def test_pr_is_mergeable_uses_injected_http_transport():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.side_effect = [
        {"mergeable_state": "dirty"},
        {"mergeable_state": "clean"},
    ]
    request_get = Mock(return_value=response)

    assert not main_module.pr_is_mergeable(request_get, "token", "org/repo", 1)
    assert main_module.pr_is_mergeable(request_get, "token", "org/repo", 1)
    assert request_get.call_args.kwargs["headers"]["Authorization"] == "Bearer token"


def test_runtime_helpers_isolate_transport_failures():
    api = Mock()
    api.create_namespaced_custom_object.side_effect = FakeApiException(409)
    main_module.create_workload(api, "llm", {}, FakeApiException)

    api.create_namespaced_custom_object.side_effect = FakeApiException(500)
    try:
        main_module.create_workload(api, "llm", {}, FakeApiException)
    except FakeApiException as exc:
        assert exc.status == 500
    else:
        assert False, "non-conflict create error was swallowed"

    api.delete_namespaced_custom_object.side_effect = FakeApiException(404)
    main_module.delete_workload(
        api,
        "llm",
        "gone",
        api_exception=FakeApiException,
        delete_options_factory=Mock(),
    )
    api.delete_namespaced_custom_object.side_effect = FakeApiException(500)
    try:
        main_module.delete_workload(
            api,
            "llm",
            "broken",
            api_exception=FakeApiException,
            delete_options_factory=Mock(),
        )
    except FakeApiException as exc:
        assert exc.status == 500
    else:
        assert False, "delete error was swallowed"

    failing_dispatch = Mock()
    failing_dispatch.find_issue_id.side_effect = RuntimeError("dispatch unavailable")
    failing_dispatch.mark_pr_fix.side_effect = RuntimeError("dispatch unavailable")
    item = ClaimedItem("org/repo", 7, "fix", "local")
    assert main_module.lookup_issue_id(failing_dispatch, "agent", ["local"], item) == ""
    assert not main_module.mark_pr_fix(failing_dispatch, "org/repo", 8, "FIXED")

    api.list_namespaced_custom_object.side_effect = RuntimeError("k8s unavailable")
    assert main_module.feedback_for(api, "llm", "failed") == ""
    assert not main_module.pr_is_mergeable(
        Mock(side_effect=RuntimeError("github unavailable")), "", "org/repo", 1
    )


def test_real_main_wires_runtime_phases_in_order(monkeypatch, capsys):
    import bridge.claim as claim_module
    from kubernetes import client, config

    events = []
    api = Mock()
    api.list_namespaced_custom_object.return_value = {"items": []}
    dispatch = Mock()

    monkeypatch.setattr(config, "load_incluster_config", lambda: events.append("config"))
    monkeypatch.setattr(client, "CustomObjectsApi", lambda: api)
    monkeypatch.setattr(claim_module, "DispatchClient", lambda *args: dispatch)

    def retry(*args, **kwargs):
        events.append("retry")
        assert args[1].func is main_module.list_failed_workloads
        assert kwargs["feedback_for"].func is main_module.feedback_for
        return ["retry-output"]

    def claim(*args, **kwargs):
        events.append("claim")
        assert args[3].func is main_module.create_workload
        assert kwargs["max_in_progress"] == 2
        return ["claim-output"]

    def prfix_reconcile(*args, **kwargs):
        events.append("prfix-reconcile")
        assert args[0].func is main_module.list_prfix_workloads
        return ["prfix-output"]

    def prfix_drain(*args, **kwargs):
        events.append("prfix-drain")
        assert args[0].func is main_module.list_queued_pr_fixes
        return ["drain-output"]

    def prune(*args, **kwargs):
        events.append("prune")
        assert args[0].func is main_module.list_terminal_candidates
        return ["prune-output"]

    monkeypatch.setattr(main_module, "reconcile_failures", retry)
    monkeypatch.setattr(main_module, "run_once", claim)
    monkeypatch.setattr(main_module, "reconcile_pr_fixes", prfix_reconcile)
    monkeypatch.setattr(main_module, "drain_pr_fixes", prfix_drain)
    monkeypatch.setattr(main_module, "prune_workloads", prune)

    env = {
        "DISPATCH_AGENT_TOKEN": "token",
        "DISPATCH_LANES": "local,frontier",
        "GATEPROFILE_MAP": "{}",
        "LANE_CODER_AGENTS": "{}",
        "REVISION_CODER_AGENTS": "{}",
        "BASE_CODER_AGENTS": "{}",
        "ESCALATION_LANE": "large",
        "PR_FIX_ENABLED": "true",
        "PR_FIX_LANE_AGENTS": "{}",
        "MAX_IN_PROGRESS": "2",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    main_module._real_main()

    assert events == ["config", "retry", "claim", "prfix-reconcile", "prfix-drain", "prune"]
    assert capsys.readouterr().out.splitlines() == [
        "retry-output",
        "claim-output",
        "prfix-output",
        "drain-output",
        "prune-output",
    ]
