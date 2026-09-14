"""Static fork publication controls; no package upload or Actions runtime emulation.

Only the small, pure condition grammar used by these jobs is evaluated. The
GitHub runner's dependency/success handling remains an independent runtime gate.
"""
import ast
import os
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(os.environ.get("NEXT1000_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
PRODUCTION = ROOT / ".github/workflows/CD-publish_to_pypi.yml"
TEST_RELEASE = ROOT / ".github/workflows/CD-test_publish_to_pypi.yml"
CANONICAL = "runpod/runpod-python"


class UniqueLoader(yaml.BaseLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError("Duplicate YAML key: " + key)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def read(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)


def condition_value(expression, context):
    """Evaluate an allowlisted AND/OR/equality condition, never Python eval()."""
    if expression is None:
        return True
    expression = expression.strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    token = re.compile(r"\s*(==|&&|\|\||\(|\)|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_.-]*)")
    parts = []
    position = 0
    while position < len(expression):
        match = token.match(expression, position)
        if match is None:
            raise ValueError("Unsupported condition grammar")
        value = match.group(1)
        position = match.end()
        if value in context:
            parts.append(repr(context[value]))
        elif value.startswith("'"):
            parts.append(repr(value[1:-1].replace("''", "'")))
        elif value in ("==", "(", ")"):
            parts.append(value)
        elif value == "&&":
            parts.append("and")
        elif value == "||":
            parts.append("or")
        else:
            raise ValueError("Unknown condition identifier: " + value)
    tree = ast.parse(" ".join(parts), mode="eval")

    def visit(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bool, type(None))):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [bool(visit(child)) for child in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(left, str) and isinstance(right, str):
                return left.casefold() == right.casefold()
            return left == right
        raise ValueError("Unsupported condition node")

    return bool(visit(tree.body))


def context(repository, release="false", event="push", forced="false"):
    return {"github.repository": repository,
            "needs.release-please.outputs.release_created": release,
            "github.event_name": event, "inputs.force_publish": forced}


def all_jobs():
    production = read(PRODUCTION)["jobs"]
    testing = read(TEST_RELEASE)["jobs"]
    assert set(production) == {"release-please", "pypi-publish", "notify-runpod-workers"}
    assert set(testing) == {"build-n-publish"}
    return dict(production, **testing)


@pytest.mark.parametrize("repository", [
    "rarephlamps-ship-it/runpod-python", "other/runpod-python", "runpod/runpod-python-extra",
])
@pytest.mark.parametrize("release,event,forced", [
    ("false", "push", "false"), ("true", "push", "false"),
    ("false", "workflow_dispatch", "true"), ("true", "workflow_dispatch", "true"),
])
def test_no_fork_can_publish_or_dispatch_workers(repository, release, event, forced):
    for name, job in all_jobs().items():
        assert not condition_value(job.get("if"), context(repository, release, event, forced)), name


@pytest.mark.parametrize("release,event,forced,allowed", [
    ("false", "push", "false", False), ("", "push", "false", False),
    ("true", "push", "false", True), ("false", "push", "true", False),
    ("false", "workflow_dispatch", "false", False),
    ("false", "workflow_dispatch", "true", True),
])
def test_upstream_publish_requires_release_or_explicit_dispatch(release, event, forced, allowed):
    jobs = all_jobs()
    for name in ("pypi-publish", "notify-runpod-workers"):
        assert condition_value(jobs[name].get("if"), context(CANONICAL, release, event, forced)) is allowed


def test_upstream_entry_jobs_remain_available():
    jobs = all_jobs()
    assert condition_value(jobs["release-please"].get("if"), context(CANONICAL))
    assert condition_value(jobs["build-n-publish"].get("if"), context(CANONICAL))


def test_release_dependencies_and_protected_environments_remain():
    jobs = all_jobs()
    assert jobs["pypi-publish"]["needs"] == "release-please"
    assert jobs["notify-runpod-workers"]["needs"] == ["release-please", "pypi-publish"]
    assert jobs["pypi-publish"]["environment"]["name"] == "pypi-production"
    assert jobs["build-n-publish"]["environment"] == "test-release"
    for job in jobs.values():
        assert "always(" not in job.get("if", "")
        assert job.get("continue-on-error", "false") == "false"


def test_condition_subset_has_github_string_false_semantics():
    assert condition_value("needs.release-please.outputs.release_created", context(CANONICAL, "false"))
    assert not condition_value("needs.release-please.outputs.release_created == 'true'", context(CANONICAL, "false"))
    with pytest.raises(ValueError):
        condition_value("unknown.call()", context(CANONICAL))
