"""Generate the Operational Qualification (OQ) report for this release.

Runs the full automated test suite, checks every requirement in
tools/traceability.py against the results, and writes
validation/OQ-<version>-<commit>.md with the environment, the outcome of each
requirement, and the evidence (test names). Exit status is non-zero if any
requirement is not met, so it can gate a release.

    python tools/oq_report.py
"""

from __future__ import annotations

import datetime as dt
import importlib.metadata as metadata
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from traceability import REQUIREMENTS  # noqa: E402

_RANK = {"passed": 0, "skipped": 1, "failed": 2}
PACKAGES = ("pymupdf", "pymupdf-fonts", "pikepdf", "pypdf", "pandas", "openpyxl", "google-genai", "anthropic")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _run_tests() -> dict[str, str]:
    """test function name -> passed / failed / skipped (any failure wins)."""
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"], cwd=ROOT,
                       capture_output=True, text=True)
        tree = ET.parse(junit)
    outcome: dict[str, str] = {}
    for case in tree.iter("testcase"):
        name = case.get("name", "").split("[", 1)[0]
        status = ("failed" if case.find("failure") is not None or case.find("error") is not None
                  else "skipped" if case.find("skipped") is not None else "passed")
        # a parametrised test is one requirement's evidence: its worst case counts
        if _RANK[status] >= _RANK.get(outcome.get(name, ""), -1):
            outcome[name] = status
    return outcome


def main() -> int:
    version = metadata.version("crf-annotation") if _installed("crf-annotation") else "dev"
    commit = _git("rev-parse", "--short", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    results = _run_tests()

    rows, met = [], 0
    for req_id, text, risk, tests in REQUIREMENTS:
        evidence = [(t, results.get(t, "missing")) for t in tests]
        ok = bool(evidence) and all(status == "passed" for _, status in evidence)
        met += ok
        rows.append((req_id, text, risk, ok, evidence))

    total_tests = len(results)
    passed = sum(s == "passed" for s in results.values())
    failed = [n for n, s in results.items() if s == "failed"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    qualified = met == len(REQUIREMENTS) and not failed and not dirty

    lines = [
        f"# Operational Qualification report - aCRF Gen {version}",
        "",
        f"- **Release:** commit `{commit}`{' (uncommitted changes present)' if dirty else ''}",
        f"- **Executed:** {stamp}",
        f"- **Environment:** Python {platform.python_version()} on {platform.system()} {platform.machine()}",
        f"- **Result:** {'QUALIFIED' if qualified else 'NOT QUALIFIED'} - {met}/{len(REQUIREMENTS)} requirements met; "
        f"{passed}/{total_tests} automated tests passed",
        "",
        "## Component versions",
        "",
        "| Component | Version |",
        "|---|---|",
        *[f"| {p} | {metadata.version(p) if _installed(p) else 'not installed'} |" for p in PACKAGES],
        "",
        "## Requirements and evidence",
        "",
        "| ID | Requirement | Risk | Result | Evidence (automated tests) |",
        "|---|---|---|---|---|",
    ]
    for req_id, text, risk, ok, evidence in rows:
        tests = "<br>".join(f"{'✅' if s == 'passed' else '❌'} `{t}`" + ("" if s == "passed" else f" ({s})")
                            for t, s in evidence)
        lines.append(f"| {req_id} | {text} | {risk} | {'Met' if ok else '**Not met**'} | {tests} |")
    if failed:
        lines += ["", "## Failed tests", "", *[f"- `{name}`" for name in sorted(failed)]]
    lines += [
        "",
        "## Scope",
        "",
        "This report covers operational qualification of the software release against its user requirements. "
        "Installation qualification of a deployment and performance qualification on the customer's own CRFs are "
        "described in docs/governance/validation-plan.md. aCRF Gen output is a draft for qualified review.",
        "",
    ]

    out = ROOT / "validation"
    out.mkdir(exist_ok=True)
    report = out / f"OQ-{version}-{commit}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"{'QUALIFIED' if qualified else 'NOT QUALIFIED'}: {met}/{len(REQUIREMENTS)} requirements, "
          f"{passed}/{total_tests} tests -> {report.relative_to(ROOT)}")
    return 0 if qualified else 1


def _installed(name: str) -> bool:
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
