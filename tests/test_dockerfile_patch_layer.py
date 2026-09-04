"""Guard the Dockerfile's TEMPORARY OS-package patch layer and its tracking.

The layer backports OS packages the pinned base image has not yet rebuilt
(CVE-2026-53615 in util-linux, CVE-2026-14456 in openssl). Its removal
conditions live only in the Dockerfile comment, and the comment has already
drifted once: the header named only CVE-2026-53615 and four packages while the
layer patched seven. Nothing in the suite looked at the Dockerfile, so nothing
could catch it. These tests exist so the comment and the release-workflow check
cannot drift from the layer again (tracked in #254).
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text()
RELEASE_YAML = (REPO / ".github" / "workflows" / "release.yaml").read_text()

# The packages the layer currently pins, and the CVE each set belongs to.
UTIL_LINUX_PKGS = ["util-linux", "bsdutils", "mount", "login"]
OPENSSL_PKGS = ["openssl", "libssl3t64", "openssl-provider-legacy"]
ALL_PKGS = UTIL_LINUX_PKGS + OPENSSL_PKGS


def _pinned_packages() -> list[str]:
    """The packages the RUN block actually pins, in order.

    Anchor on the RUN line, not the substring "apt-get install": the TEMPORARY
    comment also mentions `apt-get install`, so a naive split would land in the
    comment and find no pins. Join the backslash-continued lines so the whole
    install command is on one line before matching.
    """
    lines = DOCKERFILE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("RUN "))
    block = lines[start]
    while block.rstrip().endswith("\\"):
        block = block.rstrip()[:-1] + " " + lines[start + 1]
        start += 1
    block = block.split("apt-get install", 1)[1]
    # The install flags carry no '='; only the pkg=version pins do.
    return re.findall(r"(\S+)=\S+", block)


def test_layer_pins_exactly_the_documented_packages():
    """The RUN block must pin exactly the seven documented packages — no more,
    no fewer. A new pin without a comment update (or a removed pin) fails here."""
    assert _pinned_packages() == ALL_PKGS


def test_comment_enumerates_every_pinned_package():
    """The TEMPORARY comment must name every package the layer patches, so a
    maintainer reading only the comment knows the full scope (the header once
    hid the three openssl packages)."""
    for pkg in ALL_PKGS:
        assert pkg in DOCKERFILE, f"TEMPORARY comment does not enumerate {pkg}"


def test_comment_names_both_cves():
    """The comment must not hide a multi-CVE patch under a single CVE header."""
    assert "CVE-2026-53615" in DOCKERFILE
    assert "CVE-2026-14456" in DOCKERFILE


def test_comment_links_the_tracking_issue():
    """The comment must link the tracking issue so the removal conditions have
    an owner and cannot be silently forgotten across releases."""
    assert "#254" in DOCKERFILE


def test_release_workflow_checks_the_layer_is_still_needed():
    """The release workflow must query the base image and fail loudly once it
    ships the fixes, so the layer cannot linger as dead weight."""
    assert "Check TEMPORARY OS-package patch layer is still needed" in RELEASE_YAML
    # It must actually query the base image's installed version.
    assert "dpkg-query -W -f='${Version}\\n'" in RELEASE_YAML
    # And compare it against the pinned version.
    assert "dpkg --compare-versions" in RELEASE_YAML


def test_release_workflow_pins_match_the_dockerfile():
    """The workflow's package|version pairs must match the Dockerfile pins, so
    the check tracks what the layer actually installs."""
    for pkg in ALL_PKGS:
        # The Dockerfile pins `pkg=<version>`; the workflow pins `pkg|<version>`.
        m = re.search(rf"^\s*{re.escape(pkg)}=(\S+)", DOCKERFILE, re.MULTILINE)
        assert m, f"Dockerfile does not pin {pkg}"
        version = m.group(1)
        assert f"{pkg}|{version}" in RELEASE_YAML, (
            f"release workflow does not check {pkg} at the Dockerfile pin {version}"
        )


def test_release_workflow_reads_the_base_from_the_dockerfile():
    """The check must read the base image from the Dockerfile's FROM line, not
    hardcode a copy that would drift from a base-image bump."""
    assert "grep -m1 '^FROM ' Dockerfile" in RELEASE_YAML
