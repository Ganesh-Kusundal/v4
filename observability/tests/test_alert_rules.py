"""Lane 5b — the alert rules and the Alertmanager route are real, not decorative.

Three properties, each one a way the alerting could be quietly broken:

  1. **Both files parse.** A rule file that YAML cannot load is an alert that
     can never fire, and nothing else in the repo would notice. This project
     has no ``pyyaml`` dependency and this lane does not add one, so the check
     uses a small stdlib-only parser for the block-mapping/block-sequence
     subset these two files use (see :func:`_parse_yaml`).
  2. **Every rule has an explicit numeric threshold.** The metrics catalog
     describes its alert column in prose — "p99 > SLO", "spike", "sustained
     rate" — and prose is not something Prometheus can evaluate. A rule whose
     expression is a bare metric name has no threshold and is rejected here.
  3. **Every referenced metric name is one the code actually emits.** This is
     the anti-vacuity check. An alert on a metric the source never publishes
     stays green forever, which is worse than no alert at all because it reads
     as coverage. The emitted names are extracted from the ``*/src`` trees, and
     because some call sites encode a dimension into the name
     (``risk.py``/``engine.py`` do this) the extractor also recovers those
     *families* so a regex selector over them is still checked.

The metric-name cross-check is the reason :func:`_emitted_metric_names` reads
the source rather than a hardcoded list: a list would only prove the list
agrees with itself, which is the same defect the check exists to prevent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "deploy" / "prometheus-rules.yml"
ALERTMANAGER_PATH = REPO_ROOT / "deploy" / "alertmanager.yml"

#: The package source trees that call ``.counter()/.gauge()/.histogram()``.
SRC_DIRS = (
    "domain/src",
    "brokers/src",
    "config/src",
    "execution/src",
    "replay/src",
    "runtime/src",
    "strategy/src",
    "analytics/src",
    "market_data/src",
    "interfaces/src",
    "research/src",
    "reactive/src",
    "application/src",
    "observability/src",
    "operations/src",
    "persistence/src",
    "services/duckdb-analytics/src",
)

#: The production-readiness programme. A rule may reference a metric that this
#: spec mandates but that a *not-yet-merged* lane has not emitted yet, provided
#: the rule marks itself ``metric_pending`` with the owning acceptance
#: criterion. The name must literally appear in the spec, so the allowance can
#: never authorise a made-up metric — it only recognises a spec-mandated one.
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "0002-production-readiness-programme" / "index.md"

#: PromQL functions and keywords that appear in an expression but are not
#: metric names. Anything matched as a bare identifier that is in here is
#: skipped, so ``sum by (le)`` does not read as a metric called "sum".
_PROMQL_NON_METRICS = frozenset(
    {
        "abs", "absent", "absent_over_time", "and", "avg", "avg_over_time",
        "bool", "by", "ceil", "changes", "clamp_max", "clamp_min", "count",
        "count_over_time", "day_of_month", "day_of_week", "day_of_year",
        "delta", "deriv", "exp", "floor", "histogram_count",
        "histogram_quantile", "histogram_sum", "hour", "idelta",
        "increase", "irate", "label_replace", "label_join", "last_over_time",
        "ln", "log10", "log2", "max", "max_over_time", "min",
        "min_over_time", "minute", "month", "or", "predict_linear", "quantile_over_time",
        "rate", "resets", "round", "scalar", "sort", "sort_desc", "sqrt",
        "stddev_over_time", "stdvar_over_time", "sum", "sum_over_time",
        "time", "timestamp", "vector", "without", "year",
    }
)

#: A metric-name token in PromQL: ``[a-zA-Z_:][a-zA-Z0-9_:]*``.
_METRIC_TOKEN = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
#: A histogram quantile reads ``<name>_bucket``; the family name is the stem.
_BUCKET_SUFFIX = re.compile(r"_(?:bucket|sum|count)$")
#: A comparison against a literal number, which is what makes a threshold real.
_NUMERIC_COMPARISON = re.compile(r"(?:[<>]=?|[=!]=)\s*-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Minimal YAML (stdlib only — pyyaml is not a dependency of this project)
# ---------------------------------------------------------------------------


def _yaml_scalar(token: str):
    """Coerce one scalar token to bool/int/float/str/None."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _is_mapping_key(text: str) -> bool:
    return bool(re.match(r'^([A-Za-z0-9_.\-]+):(\s|$)', text))


def _parse_yaml(text: str):
    """Parse the block-mapping/block-sequence subset used by ``deploy/*.yml``.

    Deliberately not a general YAML implementation: it handles exactly what
    the two deployment files contain (nested mappings, sequences of mappings,
    sequences of scalars, and quoted or bare scalars) and raises on anything
    outside that, so an unsupported construct fails the test loudly rather
    than being silently misread as a different structure.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))

    # A single-element list so every nested reader shares one cursor; a
    # plain int would be a fresh local in each closure and never advance.
    cursor = [0]

    def parse_block(indent: int):
        if cursor[0] >= len(lines):
            return None
        first = lines[cursor[0]][1]
        return parse_seq(indent) if first == "-" or first.startswith("- ") else parse_map(indent)

    def parse_seq(indent: int):
        out = []
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind < indent or not (content == "-" or content.startswith("- ")):
                break
            if ind > indent:
                raise ValueError(
                    f"unexpected indent in sequence: {ind} (want {indent}): {content!r}"
                )
            item = content[1:].strip()
            cursor[0] += 1
            if item == "":
                out.append(parse_block(lines[cursor[0]][0]) if cursor[0] < len(lines) else None)
            elif _is_mapping_key(item):
                out.append(_parse_item_mapping(item, indent))
            else:
                out.append(_yaml_scalar(item))
        return out

    def _parse_item_mapping(first_item: str, seq_indent: int) -> dict:
        """A ``- key: value`` entry, plus any deeper keys that belong to it."""
        entry: dict = {}
        key, value = first_item.split(":", 1)
        key, value = key.strip().strip("\"'"), value.strip()
        if value == "":
            entry[key] = _nested_or_none(seq_indent)
        else:
            entry[key] = _yaml_scalar(value)
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind <= seq_indent or content == "-" or content.startswith("- "):
                break
            sub_key, sub_value = content.split(":", 1)
            sub_key, sub_value = sub_key.strip().strip("\"'"), sub_value.strip()
            cursor[0] += 1
            entry[sub_key] = (
                _nested_or_none(ind) if sub_value == "" else _yaml_scalar(sub_value)
            )
        return entry

    def _nested_or_none(parent_indent: int):
        if cursor[0] < len(lines) and lines[cursor[0]][0] > parent_indent:
            return parse_block(lines[cursor[0]][0])
        return None

    def parse_map(indent: int):
        out = {}
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(
                    f"unexpected indent in mapping: {ind} (want {indent}): {content!r}"
                )
            if content == "-" or content.startswith("- "):
                break
            key, value = content.split(":", 1)
            key, value = key.strip().strip("\"'"), value.strip()
            cursor[0] += 1
            out[key] = _nested_or_none(ind) if value == "" else _yaml_scalar(value)
        return out

    return parse_block(lines[0][0] if lines else 0)


# ---------------------------------------------------------------------------
# What the source actually emits
# ---------------------------------------------------------------------------


def _sanitize(name: str) -> str:
    """Mirror ``MetricsRegistry._sanitized`` so names match the wire form.

    The registry replaces every character outside ``[a-zA-Z0-9_:]`` with an
    underscore, which is why ``kill_switch.tripped`` is exposed as
    ``kill_switch_tripped``. Alerting on the dotted logical name matches
    nothing, so the check has to use the exposed name.
    """
    return re.sub(r"[^a-zA-Z0-9_:]", "_", name)


def _emitted_metric_names(repo_root: Path = REPO_ROOT) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(exact_names, family_prefixes)`` the source tree emits.

    ``exact_names`` are the fully-determined wire names. ``family_prefixes``
    cover the call sites that build a name with an f-string
    (``f"orders.rejection_reason.{reason}"``), where the code emits every
    ``orders_rejection_reason_*`` it is given — the suffix is data, not a name
    the source can be held to.
    """
    files = [p for d in SRC_DIRS for p in (repo_root / d).rglob("*.py") if p.is_file()]
    exact: set[str] = set()
    families: set[str] = set()

    for path in files:
        text = path.read_text(encoding="utf-8")
        constants = dict(
            re.findall(r"^([A-Z_][A-Z0-9_]*)\s*=\s*\"([^\"]*)\"", text, re.M)
        )
        for match in re.finditer(r"\.(?:counter|gauge|histogram)\(\s*([^,)\n]+)", text):
            argument = match.group(1).strip()
            if argument.startswith('"') and argument.endswith('"'):
                exact.add(_sanitize(argument[1:-1]))
            elif argument.startswith('f"'):
                prefix = argument[2:-1].split("{")[0].rstrip(".")
                if prefix:
                    families.add(_sanitize(prefix))
            elif argument in constants:
                exact.add(_sanitize(constants[argument]))
        # ``_KIND_METRICS`` and friends: a table of names handed to
        # ``counter(name)``, so the literals are emission sites too.
        for table in re.finditer(r"_[A-Z_]*KIND_METRICS[^=]*=\s*\{(.*?)\n\}", text, re.S):
            for literal in re.finditer(r'"([a-zA-Z0-9_]+)"', table.group(1)):
                exact.add(_sanitize(literal.group(1)))

    # A URL constant is not a metric; neither is anything with a scheme.
    exact = {n for n in exact if not n.startswith(("http", "wss"))}
    return frozenset(exact), frozenset(families)


def _referenced_metric_names(expr: str) -> tuple[set[str], set[str]]:
    """Return ``(exact_names, regex_patterns)`` an expression refers to.

    ``{__name__=~"..."}`` selectors yield a pattern, because the code emits a
    whole family under that prefix and enumerating the members in the rule
    file is exactly the brittleness the family form exists to avoid.
    """
    exact: set[str] = set()
    patterns: set[str] = set()

    for selector in re.findall(r'__name__\s*=~\s*"([^"]+)"', expr):
        patterns.add(selector)

    # Reduce the expression to just its metric references: drop label
    # selectors ({...}), range selectors ([5m]) and aggregation clauses
    # ("by (le)" / "without (x)"), none of which name a metric.
    scrubbed = re.sub(r"\{[^}]*\}", " ", expr)          # label matchers
    scrubbed = re.sub(r"\[[^\]]*\]", " ", scrubbed)     # range selectors: [5m]
    scrubbed = re.sub(r"\b(?:by|without|on|ignoring)\s*\([^)]*\)", " ", scrubbed)
    for token in _METRIC_TOKEN.findall(scrubbed):
        if token in _PROMQL_NON_METRICS or token.startswith("__"):
            continue
        exact.add(_BUCKET_SUFFIX.sub("", token))

    return exact, patterns


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rules_doc() -> dict:
    assert RULES_PATH.is_file(), f"missing {RULES_PATH}"
    return _parse_yaml(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def alertmanager_doc() -> dict:
    assert ALERTMANAGER_PATH.is_file(), f"missing {ALERTMANAGER_PATH}"
    return _parse_yaml(ALERTMANAGER_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def all_rules(rules_doc: dict) -> list[dict]:
    rules = [r for group in rules_doc.get("groups") or [] for r in group.get("rules") or []]
    assert rules, "prometheus-rules.yml declared no rules"
    return rules


@pytest.fixture(scope="module")
def emitted() -> tuple[frozenset[str], frozenset[str]]:
    return _emitted_metric_names()


# ---------------------------------------------------------------------------
# 1. Both files parse
# ---------------------------------------------------------------------------


def test_rule_file_parses_into_groups(rules_doc: dict) -> None:
    groups = rules_doc.get("groups")
    assert isinstance(groups, list) and groups, "rules file has no 'groups' list"
    for group in groups:
        assert group.get("name"), "every group needs a name"
        assert group.get("rules"), f"group {group.get('name')!r} has no rules"


def test_alertmanager_parses(rules_doc: dict, alertmanager_doc: dict) -> None:
    # Parsing happened in the fixture; assert it produced the real structure
    # rather than a silently-empty result.
    assert rules_doc.get("groups")
    assert alertmanager_doc.get("route") is not None or alertmanager_doc.get("receivers")


# ---------------------------------------------------------------------------
# 2. Every rule has an explicit numeric threshold
# ---------------------------------------------------------------------------


def test_every_rule_has_a_numeric_threshold(all_rules: list[dict]) -> None:
    """No rule may be a bare metric name: prose is not a threshold.

    The catalog's alert column says things like "p99 > SLO" and "sustained
    rate". Translated literally into PromQL those become a metric name with no
    comparison, which Prometheus accepts and which then can never fire. This is
    the failure the lane exists to prevent, so it is asserted directly.
    """
    offenders: list[str] = []
    for rule in all_rules:
        expr = rule.get("expr")
        assert expr, f"rule {rule.get('alert')!r} has no expr"
        if not _NUMERIC_COMPARISON.search(expr):
            offenders.append(f"{rule.get('alert')}: {expr!r} has no numeric threshold")
    assert not offenders, "rules without an explicit numeric threshold:\n" + "\n".join(offenders)


def test_every_rule_declares_for_severity_and_annotations(all_rules: list[dict]) -> None:
    """``for``/severity/summary/description on every rule, as the lane requires."""
    for rule in all_rules:
        name = rule.get("alert")
        assert name, "a rule has no 'alert' name"
        assert "for" in rule, f"{name}: no 'for' — a rule with no duration never fires"
        labels = rule.get("labels") or {}
        assert labels.get("severity") in ("warning", "critical"), (
            f"{name}: severity must be warning or critical, got {labels.get('severity')!r}"
        )
        annotations = rule.get("annotations") or {}
        assert annotations.get("summary"), f"{name}: no annotations.summary"
        assert annotations.get("description"), f"{name}: no annotations.description"


def test_the_catalogue_latency_thresholds_are_present(all_rules: list[dict]) -> None:
    """p50 50 ms / p95 250 ms / p99 750 ms on both latency histograms.

    Seconds, because that is the unit the metric is published in: 0.05, 0.25,
    0.75. Asserted on the parsed numbers rather than the text so a rule cannot
    satisfy this by mentioning 750 in its description.
    """
    expected = {
        "orders_submit_latency_seconds": {0.5: 0.05, 0.95: 0.25, 0.99: 0.75},
        "orders_process_latency_seconds": {0.5: 0.05, 0.95: 0.25, 0.99: 0.75},
    }
    for metric, quantiles in expected.items():
        for quantile, threshold in quantiles.items():
            matching = [
                r for r in all_rules
                if metric in r.get("expr", "")
                and f"histogram_quantile({quantile}," in r["expr"]
            ]
            assert matching, f"no p{int(quantile * 100)} rule for {metric}"
            for rule in matching:
                assert f"> {threshold}" in rule["expr"], (
                    f"{rule['alert']}: p{int(quantile * 100)} of {metric} must fire above "
                    f"{threshold}s, got {rule['expr']!r}"
                )


# ---------------------------------------------------------------------------
# 3. Every referenced metric is one the code emits
# ---------------------------------------------------------------------------


def test_every_referenced_metric_is_actually_emitted(
    all_rules: list[dict], emitted: tuple[frozenset[str], frozenset[str]]
) -> None:
    """The anti-vacuity check.

    An alert on a metric nothing publishes stays green forever while looking
    like coverage, so every name an expression touches has to be found in the
    source tree. Names the source builds with an f-string are checked against
    the family prefix, and ``{__name__=~...}`` selectors are checked to sit
    under a family the code emits.
    """
    exact_emitted, families = emitted
    known: set[str] = set(exact_emitted) | set(families)
    spec_text = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""

    unknown: list[str] = []
    unknown_patterns: list[str] = []
    for rule in all_rules:
        names, patterns = _referenced_metric_names(rule.get("expr", ""))
        pending = (rule.get("labels") or {}).get("metric_pending")
        for name in sorted(names):
            if name in known:
                continue
            # A spec-mandated, not-yet-emitted metric is allowed only when the
            # rule marks it pending against a named AC and the spec quotes it.
            if pending and name in spec_text:
                continue
            unknown.append(f"{rule['alert']}: metric {name!r} is never emitted")
        for pattern in sorted(patterns):
            # A pattern must match at least one emitted name, or at least be
            # anchored under a family the code emits.
            if not (
                any(re.fullmatch(pattern, n) for n in exact_emitted)
                or any(re.match(re.escape(f) + r"[a-z0-9_]*", pattern) or pattern.startswith(f)
                       for f in families)
            ):
                unknown_patterns.append(
                    f"{rule['alert']}: selector {pattern!r} matches no emitted metric family"
                )

    assert not unknown, "alerts on metrics the code never emits:\n" + "\n".join(unknown)
    assert not unknown_patterns, "selectors over nothing emitted:\n" + "\n".join(unknown_patterns)


def test_the_sanitizer_is_why_names_are_underscored(
    emitted: tuple[frozenset[str], frozenset[str]],
) -> None:
    """Pin the assumption the rule file is built on.

    If the registry ever stops rewriting dots to underscores, the dotted names
    come back and every expression in the rule file goes dead at once. This
    fails on that change instead.
    """
    exact_emitted, _ = emitted
    assert "kill_switch_tripped" in exact_emitted
    assert "kill_switch.tripped" not in exact_emitted
    assert "orders_submit_latency_seconds" in exact_emitted


# ---------------------------------------------------------------------------
# 4. Alertmanager has a delivery route
# ---------------------------------------------------------------------------


def test_alertmanager_has_receivers_and_a_route(alertmanager_doc: dict) -> None:
    receivers = alertmanager_doc.get("receivers")
    assert isinstance(receivers, list) and receivers, "alertmanager declares no receivers"
    for receiver in receivers:
        assert receiver.get("name"), "every receiver needs a name"
    route = alertmanager_doc.get("route")
    assert isinstance(route, dict), "alertmanager declares no route"
    assert route.get("receiver") in {r["name"] for r in receivers}, (
        f"root route points at {route.get('receiver')!r}, which is not a declared receiver"
    )


def test_every_route_points_at_a_declared_receiver(alertmanager_doc: dict) -> None:
    names = {r["name"] for r in alertmanager_doc.get("receivers") or []}
    route = alertmanager_doc.get("route") or {}
    for child in route.get("routes") or []:
        assert child.get("receiver") in names, (
            f"sub-route points at {child.get('receiver')!r}, not a declared receiver"
        )


def test_alertmanager_routes_on_severity(alertmanager_doc: dict, all_rules: list[dict]) -> None:
    """The severities the rules emit must be the ones the routes match on.

    A rule labelled ``critical`` with no matching route is an alert that
    evaluates to firing and is then delivered nowhere.
    """
    route = alertmanager_doc.get("route") or {}
    routed: set[str] = set()
    for child in route.get("routes") or []:
        for matcher in child.get("matchers") or []:
            match = re.search(r'severity\s*=\s*"([^"]*)"', matcher)
            if match:
                routed.add(match.group(1))
    emitted_severity = {r["labels"]["severity"] for r in all_rules}
    assert emitted_severity <= routed, (
        f"rules emit severities {sorted(emitted_severity - routed)} that no route matches"
    )


def test_receivers_have_a_real_integration_shape(alertmanager_doc: dict) -> None:
    """A receiver must name an integration, not just exist."""
    integrations = (
        "webhook_configs",
        "pagerduty_configs",
        "slack_configs",
        "opsgenie_configs",
        "email_configs",
        "victorops_configs",
    )
    for receiver in alertmanager_doc.get("receivers") or []:
        assert any(receiver.get(i) for i in integrations), (
            f"receiver {receiver.get('name')!r} declares no integration"
        )


def test_pending_metrics_are_spec_mandated(all_rules: list[dict]) -> None:
    """A ``metric_pending`` rule must name a metric the programme actually asks for.

    This is what keeps the pending allowance from becoming a loophole: a rule
    can only defer a metric that (a) the source does not emit yet, (b) is
    marked pending against a named acceptance criterion, and (c) appears
    verbatim in the production-readiness spec. Invent a name and this fails
    even though the anti-vacuity test would otherwise wave it through.
    """
    spec_text = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""
    assert spec_text, f"could not read the programme spec at {SPEC_PATH}"

    checked = 0
    for rule in all_rules:
        pending = (rule.get("labels") or {}).get("metric_pending")
        if not pending:
            continue
        checked += 1
        names, _ = _referenced_metric_names(rule.get("expr", ""))
        # every referenced name must either already be emitted (so the
        # pending marker is stale) or be quoted in the spec
        for name in sorted(names):
            assert name in spec_text, (
                f"{rule['alert']} defers to {name!r} but the programme spec never "
                f"mentions it — remove the metric_pending label or fix the name"
            )
    assert checked, "no rule uses metric_pending (expected the AC-15 quarantine rule)"
