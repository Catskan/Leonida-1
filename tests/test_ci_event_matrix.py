"""Which CI jobs run for which event, worked out from the live workflow files.

    python -I tests/test_ci_event_matrix.py

``.github/workflows/ci.yml`` and ``runtime.yml`` choose their jobs with ``on:`` filters, job ``if:`` predicates,
``needs:`` and an event-dependent matrix. A wrong predicate turns nothing red: the job is just skipped (#125 moved
macOS to manual dispatch and pushes to main during recovery). This test evaluates the predicates the way GitHub does
for five contexts -- a pull request with a code change, a docs-only pull request, a push to integration/ci-recovery,
a push to main and a manual dispatch -- plus a cancelled run of each and a run where every job that another job
needs fails after setting its outputs -- and compares the jobs that run, and the operating systems of their legs,
with the tables below (CI-21, #99).

The workflow reader and the expression evaluator know only what these files use. The evaluator handles string
literals, ``==``, ``!=``, ``&&``, ``||``, ``!``, parentheses, the four status functions, ``fromJSON``,
``github.event_name``, ``github.ref``, ``runner.os``, ``matrix.<key>`` and ``needs.<job>.result`` /
``.outputs.<name>``. Anything else raises ``Unsupported``, so a new construct fails the test instead of reading as
a pass. The value of a needed job's output is modelled, so the output must come straight from one unconditional
step of that job. An ``if:`` without a status function gets GitHub's implicit ``success()``, which is false once a
needed job was skipped or the run was cancelled. That is why the ci.yml jobs need ``!cancelled()`` to run on
pushes, where ``change-scope`` is skipped.

Left to other tests and not repeated here:
* which test files each job selects, and that the semantic job installs SPIRV-Tools: tests/test_ci_registration.py;
* what unset, "0" and "1" mean to the helpers: tests/semantic/test_spirv_tools_env_contract.py;
* the runtime path list: tests/test_runtime_paths.py;
* the CTest inventory: #132 (CI-10).

Only the two workflow files are read. No game files, no network, standard library only.
"""
import collections
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
RUNTIME = os.path.join(ROOT, ".github", "workflows", "runtime.yml")

L, W, M = "linux", "windows", "macos"
PUSH_BRANCHES = ["integration/ci-recovery", "main"]
CODE_CHANGE = ["runtime/shims/acphal.cpp"]
DOCS_CHANGE = ["docs/testing.md"]

# failing: every job another job needs fails, after its outputs are set (e.g. runtime tests after the upload).
Context = collections.namedtuple("Context", "event ref files outputs cancelled failing", defaults=(False,))


def outputs(docs_only):
    # The values the two jobs with outputs give when they run; a reference to any other output raises.
    return {("change-scope", "docs-only"): docs_only, ("windows", "wine-artifact"): "success"}


CONTEXTS = {
    "pull request, code": Context("pull_request", "refs/pull/1/merge", CODE_CHANGE, outputs("false"), False),
    "pull request, docs only": Context("pull_request", "refs/pull/1/merge", DOCS_CHANGE, outputs("true"), False),
    "push, integration/ci-recovery": Context("push", "refs/heads/integration/ci-recovery", CODE_CHANGE,
                                             outputs("false"), False),
    "push, main": Context("push", "refs/heads/main", CODE_CHANGE, outputs("false"), False),
    "workflow_dispatch": Context("workflow_dispatch", "refs/heads/integration/ci-recovery", [], outputs("false"),
                                 False),
}

# job -> operating system of each of its legs (sorted), for the jobs that run;
# None when the event does not start the workflow.
CI_PUSH = {"check": [W], "translator-tests": [L], "semantic-tests": [L, W], "viewer-tests": [L],
           "stdlib-tests-linux": [L]}
CI_EXPECTED = {
    "pull request, code": {"change-scope": [L], "check": [W], "translator-tests": [L], "semantic-tests": [L],
                           "viewer-tests": [L], "stdlib-tests-linux": [L]},
    "pull request, docs only": {"change-scope": [L]},
    "push, integration/ci-recovery": CI_PUSH,
    "push, main": dict(CI_PUSH, **{"macos-python": [M]}),
    "workflow_dispatch": dict(CI_PUSH, **{"macos-python": [M]}),
}
# runtime.yml has path filters: a docs-only change does not start it, a runtime change does, a dispatch always does.
RUNTIME_PUSH = {"windows": [W], "wine": [L, L], "linux-gpu-tools": [L]}
RUNTIME_EXPECTED = {
    "pull request, code": {"windows": [W], "linux-gpu-tools": [L]},
    "pull request, docs only": None,
    "push, integration/ci-recovery": RUNTIME_PUSH,
    "push, main": dict(RUNTIME_PUSH, macos=[M]),
    "workflow_dispatch": dict(RUNTIME_PUSH, macos=[M]),
}
# The semantic leg that installs SPIRV-Tools must require them, exactly "1"; the other legs say "0".
SPIRV_SWITCH = "LEONIDA_REQUIRE_SPIRV_TOOLS"
SPIRV_SWITCH_BY_OS = {L: "1", W: "0"}
RUNNER_OS = {L: "Linux", W: "Windows", M: "macOS"}


class Unsupported(Exception):
    """A workflow construct this model does not know: the test fails rather than guess."""


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# --- the YAML subset the workflows use: block mappings and sequences, block scalars, one-line flow lists ---

KEY = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$")


def strip_comment(value):
    if "\\" in value and '"' in value:
        raise Unsupported("backslash near a double quote, escapes are not modelled: " + value)
    for m in re.finditer(r"\s#", value):
        prefix = value[:m.start()]
        if prefix.count("'") % 2 == 0 and prefix.count('"') % 2 == 0:
            return prefix.rstrip()
    return value


def scalar(value):
    value = strip_comment(value).strip()
    if value.startswith("["):
        if not value.endswith("]"):
            raise Unsupported("multi-line flow sequence: " + value)
        inner = value[1:-1].strip()
        return [scalar(item) for item in inner.split(",")] if inner else []
    if value == "{}":
        return {}
    if value.startswith(("{", "&", "*", "!")):
        raise Unsupported("YAML flow mapping, anchor, alias or tag: " + value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].replace("''", "'") if value[0] == "'" else value[1:-1]
    if value.startswith(("'", '"')):
        raise Unsupported("multi-line quoted scalar: " + value)
    return value


def parse_yaml(text):
    lines = []
    for raw in text.splitlines():
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise Unsupported("tab indentation: " + raw)
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    if not lines:
        raise Unsupported("empty workflow")
    node, pos = _node(lines, 0, lines[0][0])
    if pos != len(lines):
        raise Unsupported("unread line: " + lines[pos][1])
    return node


def _is_item(text):
    return text == "-" or text.startswith("- ")


def _node(lines, pos, indent):
    return (_seq if _is_item(lines[pos][1]) else _map)(lines, pos, indent)


def _map(lines, pos, indent):
    out = {}
    while pos < len(lines) and lines[pos][0] == indent:
        text = lines[pos][1]
        m = KEY.match(text)
        if not m or _is_item(text):
            raise Unsupported("not a mapping entry: " + text)
        key, value = m.group(1), strip_comment(m.group(2) or "").strip()
        if key in out:
            raise Unsupported("duplicate key: " + key)
        pos += 1
        if value in ("|", "|-", "|+", ">", ">-", ">+"):
            start = pos
            while pos < len(lines) and lines[pos][0] > indent:
                pos += 1
            out[key] = "\n".join(line for _, line in lines[start:pos])
        elif value:
            out[key] = scalar(value)
        elif pos < len(lines) and lines[pos][0] > indent:
            out[key], pos = _node(lines, pos, lines[pos][0])
        elif pos < len(lines) and lines[pos][0] == indent and _is_item(lines[pos][1]):
            out[key], pos = _seq(lines, pos, indent)
        else:
            out[key] = None
    if pos < len(lines) and lines[pos][0] > indent:
        raise Unsupported("unexpected indentation: " + lines[pos][1])
    return out, pos


def _seq(lines, pos, indent):
    out = []
    while pos < len(lines) and lines[pos][0] == indent and _is_item(lines[pos][1]):
        rest = lines[pos][1][1:].strip()
        if not rest:
            raise Unsupported("sequence item on its own line")
        if _is_item(rest):
            raise Unsupported("nested sequence item: " + rest)
        if KEY.match(rest):
            lines[pos] = (indent + 2, rest)  # the item is a mapping whose keys sit two columns in
            item, pos = _map(lines, pos, indent + 2)
        else:
            item, pos = scalar(rest), pos + 1
        out.append(item)
    return out, pos


# --- GitHub expressions ---

TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(==|!=|&&|\|\||!|\(|\)|,)"
                   r"|([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))")
REF = re.compile(r"github\.(?:event_name|ref)|runner\.os|matrix\.[A-Za-z0-9_-]+"
                 r"|needs\.[A-Za-z0-9_-]+\.(?:result|outputs\.[A-Za-z0-9_-]+)")
STATUS = ("success", "failure", "cancelled", "always")
JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def tokenize(expr):
    tokens, pos, expr = [], 0, expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise Unsupported("cannot read %r in %r" % (expr[pos:], expr))
        pos = m.end()
        text, op, name = m.groups()
        if text is not None:
            tokens.append(("str", text[1:-1].replace("''", "'")))
        else:
            tokens.append(("op", op) if op else ("name", name))
    return tokens


def parse_expression(expr):
    tokens, pos = tokenize(expr), [0]

    def peek(op=None):
        tok = tokens[pos[0]] if pos[0] < len(tokens) else ("end", None)
        return tok if op is None else tok == ("op", op)

    def take():
        tok = peek()
        if tok[0] == "end":
            raise Unsupported("%r ends early" % expr)
        pos[0] += 1
        return tok

    def expect(op):
        if take() != ("op", op):
            raise Unsupported("expected %r in %r" % (op, expr))

    def primary():
        kind, value = take()
        if kind == "str":
            return ("lit", value)
        if (kind, value) == ("op", "("):
            node = disjunction()
            expect(")")
            return node
        if kind != "name":
            raise Unsupported("unexpected %r in %r" % (value, expr))
        if peek("("):
            take()
            args = []
            if not peek(")"):
                args.append(disjunction())
                while peek(","):
                    take()
                    args.append(disjunction())
            expect(")")
            name = value.casefold()
            if (name in STATUS and not args) or (name == "fromjson" and len(args) == 1):
                return ("call", name, args)
            raise Unsupported("function %s() with %d argument(s) in %r" % (value, len(args), expr))
        if value in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[value])
        if REF.fullmatch(value):
            return ("ref", value)
        raise Unsupported("context %r in %r" % (value, expr))

    def unary():
        if peek("!"):
            take()
            return ("not", unary())
        return primary()

    def comparison():
        node = unary()
        while peek()[0] == "op" and peek()[1] in ("==", "!="):
            node = ("cmp", take()[1], node, unary())
        return node

    def chain(kind, op, operand):
        def parse():
            node = operand()
            while peek(op):
                take()
                node = (kind, node, operand())
            return node
        return parse

    conjunction = chain("and", "&&", comparison)
    disjunction = chain("or", "||", conjunction)
    tree = disjunction()
    if peek()[0] != "end":
        raise Unsupported("trailing %r in %r" % (peek()[1], expr))
    return tree


def has_status(node):
    if node[0] == "call":
        return node[1] in STATUS or any(has_status(arg) for arg in node[2])
    return any(isinstance(child, tuple) and has_status(child) for child in node[1:])


def truthy(value):
    if isinstance(value, float):
        return value == value and value != 0
    return value not in (None, False, "")


def as_number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        return 0.0 if text == "" else float(text) if JSON_NUMBER.fullmatch(text) else float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    return float("nan")


def loose_equal(a, b):
    """GitHub's ==: strings ignore case, mismatched types compare as numbers, arrays never equal."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if type(a) is type(b) and (a is None or isinstance(a, bool)):
        return a == b
    return as_number(a) == as_number(b)


def evaluate(node, lookup, status):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ref":
        return lookup(node[1])
    if kind == "not":
        return not truthy(evaluate(node[1], lookup, status))
    if kind == "cmp":
        same = loose_equal(evaluate(node[2], lookup, status), evaluate(node[3], lookup, status))
        return same if node[1] == "==" else not same
    if kind in ("and", "or"):
        # Both sides are evaluated so that every reference is checked; && and || return an operand.
        left, right = evaluate(node[1], lookup, status), evaluate(node[2], lookup, status)
        if kind == "and":
            return right if truthy(left) else left
        return left if truthy(left) else right
    if node[1] == "fromjson":
        text = evaluate(node[2][0], lookup, status)
        if not isinstance(text, str):
            raise Unsupported("fromJSON of a non-string")
        return json.loads(text)
    if status is None:
        raise Unsupported(node[1] + "() outside an if:")
    return status(node[1])


def whole_expression(text):
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text.strip(), re.S)
    if not m or "${{" in m.group(1):
        raise Unsupported("text around ${{ }}: " + text)
    return m.group(1)


def value_of(text, lookup):
    """A value that may be a ${{ }} expression (runs-on, a matrix row, an env entry)."""
    if not isinstance(text, str):
        raise Unsupported("%r is not a string" % (text,))
    if "${{" not in text:
        return text
    tree = parse_expression(whole_expression(text))
    if has_status(tree):
        raise Unsupported("status function outside an if: " + text)
    return evaluate(tree, lookup, None)


def condition(text, lookup, status):
    """An if: as GitHub decides it, with the implicit success() when no status function is named."""
    if text is None:
        return status("success")
    if not isinstance(text, str):
        raise Unsupported("if: %r is not an expression" % (text,))
    tree = parse_expression(whole_expression(text) if "${{" in text else text)
    value = truthy(evaluate(tree, lookup, status))
    return value if has_status(tree) else status("success") and value


# --- running a workflow for one context ---

EVENT_FILTERS = {"push": {"branches", "paths"}, "pull_request": {"paths"}, "workflow_dispatch": set()}
JOB_KEYS = {"name", "needs", "if", "runs-on", "strategy", "env", "steps", "timeout-minutes", "outputs",
            "permissions", "defaults"}
STRATEGY_KEYS = {"matrix", "fail-fast", "max-parallel"}


def as_list(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise Unsupported("expected a string or a list of strings: %r" % (value,))


def glob_match(pattern, path):
    if pattern.startswith("!") or any(c in pattern for c in "?[]{}+"):
        raise Unsupported("filter pattern: " + pattern)
    parts = re.split(r"(\*\*|\*)", pattern)
    regex = "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    return re.fullmatch(regex, path) is not None


def triggers(workflow):
    on = workflow.get("on")
    if not isinstance(on, dict):
        raise Unsupported("on: is not a mapping of events")
    for event, filters in on.items():
        if event not in EVENT_FILTERS:
            raise Unsupported("trigger %r is not modelled" % event)
        if filters is not None and not isinstance(filters, dict):
            raise Unsupported("%s: filters are not a mapping" % event)
        unknown = set(filters or {}) - EVENT_FILTERS[event]
        if unknown:
            raise Unsupported("%s filter(s) not modelled: %s" % (event, ", ".join(sorted(unknown))))
    return on


def triggered(workflow, ctx):
    on = triggers(workflow)
    if ctx.event not in on:
        return False
    filters = on[ctx.event] or {}
    if "branches" in filters:
        branch = ctx.ref[len("refs/heads/"):] if ctx.ref.startswith("refs/heads/") else None
        if branch is None or not any(glob_match(p, branch) for p in as_list(filters["branches"])):
            return False
    if "paths" in filters:
        patterns = as_list(filters["paths"])
        if not any(glob_match(p, f) for p in patterns for f in ctx.files):
            return False
    return True


SOURCE = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.(outcome|outputs\.([A-Za-z0-9_-]+))\s*\}\}")


def check_output_source(jname, job, output):
    """The model gives a needed job's output a value, so that output must come from one step that always runs."""
    text = (job.get("outputs") or {}).get(output)
    m = SOURCE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not m:
        raise Unsupported("%s output %s = %r is not one step's outcome or output" % (jname, output, text))
    steps = [s for s in job.get("steps") or [] if s.get("id") == m.group(1)]
    if len(steps) != 1 or "if" in steps[0]:
        raise Unsupported("%s output %s: no single unconditional step with id %s" % (jname, output, m.group(1)))
    run = str(steps[0].get("run", ""))
    if m.group(3) and not ("GITHUB_OUTPUT" in run and m.group(3) + "=" in run):
        raise Unsupported("%s output %s: step %s does not write %s" % (jname, output, m.group(1), m.group(3)))


def lookup_for(ctx, jobs=None, needs=(), results=None, runner_os=None, matrix=None):
    def lookup(name):
        parts = name.split(".")
        if name == "github.event_name":
            return ctx.event
        if name == "github.ref":
            return ctx.ref
        if parts[0] == "runner":
            if runner_os is None:
                raise Unsupported("runner.os where no runner exists yet")
            return runner_os
        if parts[0] == "matrix":
            if matrix is None or parts[1] not in matrix:
                raise Unsupported(name + " is not a matrix key here")
            return matrix[parts[1]]
        job = parts[1]
        if job not in needs:
            raise Unsupported("%s: %s is not in this job's needs" % (name, job))
        if parts[2] == "result":
            return results[job]
        if parts[3] not in (jobs[job].get("outputs") or {}):
            raise Unsupported("%s: %s declares no output %s" % (name, job, parts[3]))
        check_output_source(job, jobs[job], parts[3])
        if results[job] == "skipped":
            return None
        if (job, parts[3]) not in ctx.outputs:
            raise Unsupported("%s has no value in this model" % name)
        return ctx.outputs[(job, parts[3])]
    return lookup


def needs_of(jobs, name):
    needs = as_list(jobs[name].get("needs", []))
    missing = [n for n in needs if n not in jobs]
    if missing:
        raise Unsupported("%s needs unknown job(s) %s" % (name, ", ".join(missing)))
    return needs


def matrix_legs(job, ctx):
    strategy = job.get("strategy") or {}
    if set(strategy) - STRATEGY_KEYS:
        raise Unsupported("strategy key(s) not modelled: " + ", ".join(sorted(set(strategy) - STRATEGY_KEYS)))
    matrix = strategy.get("matrix")
    if matrix is None:
        return [{}]
    if not isinstance(matrix, dict):
        raise Unsupported("a whole-matrix expression is not modelled")
    legs = [{}]
    for key, values in matrix.items():
        if key in ("include", "exclude"):
            raise Unsupported("matrix " + key + " is not modelled")
        if isinstance(values, str):
            values = value_of(values, lookup_for(ctx))
        if not isinstance(values, list) or not values:
            raise Unsupported("matrix.%s is not a non-empty list" % key)
        legs = [dict(leg, **{key: v}) for leg in legs for v in values]
    return legs


def os_family(label):
    if not isinstance(label, str):
        raise Unsupported("runs-on %r is not one label" % (label,))
    for prefix, family in (("ubuntu-", L), ("windows-", W), ("macos-", M)):
        if label.startswith(prefix):
            return family
    raise Unsupported("runner label not modelled: " + label)


def leg_systems(job, ctx):
    return [(os_family(value_of(job.get("runs-on"), lookup_for(ctx, matrix=leg))), leg)
            for leg in matrix_legs(job, ctx)]


def job_table(workflow, ctx):
    """{job: sorted operating system of each leg} for the jobs that start, or None if the workflow does not."""
    if not triggered(workflow, ctx):
        return None
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise Unsupported("no jobs")
    results, table, pending = {}, {}, list(jobs)
    while pending:
        ready = [name for name in pending if all(n in results for n in needs_of(jobs, name))]
        if not ready:
            raise Unsupported("needs cycle among " + ", ".join(pending))
        for name in ready:
            pending.remove(name)
            job = jobs[name]
            if set(job) - JOB_KEYS:
                raise Unsupported("%s: job key(s) not modelled: %s" % (name, ", ".join(sorted(set(job) - JOB_KEYS))))
            needs = needs_of(jobs, name)
            if any(needs_of(jobs, n) for n in needs):
                # GitHub's implicit success() also looks at the needs of needs; only one level is modelled.
                raise Unsupported("%s needs a job that has needs of its own" % name)

            def status(fn, needs=needs):
                if fn == "always":
                    return True
                if fn == "cancelled":
                    return ctx.cancelled
                if fn == "failure":
                    return any(results[n] == "failure" for n in needs)
                return not ctx.cancelled and all(results[n] == "success" for n in needs)

            if condition(job.get("if"), lookup_for(ctx, jobs, needs, results), status):
                needed = any(name in needs_of(jobs, other) for other in jobs)
                results[name] = "failure" if ctx.failing and needed else "success"
                table[name] = sorted(system for system, _ in leg_systems(job, ctx))
            else:
                results[name] = "skipped"
    return table


# --- the contract ---

def describe(legs):
    return " + ".join(legs) if legs else "nothing"


def table_problems(wname, cname, got, want):
    if got is None or want is None:
        if (got is None) != (want is None):
            return ["%s, %s: %s, expected %s" % (wname, cname, "not started" if got is None else "started",
                                                 "not started" if want is None else "started")]
        return []
    return ["%s, %s: %s runs on %s, expected %s" % (wname, cname, job, describe(got.get(job)), describe(want.get(job)))
            for job in sorted(set(got) | set(want)) if got.get(job) != want.get(job)]


def spirv_switch_problems(workflow):
    """Where LEONIDA_REQUIRE_SPIRV_TOOLS is set, and its value on each semantic leg that runs."""
    jobs = workflow["jobs"]
    sites = [("the workflow", None)] if SPIRV_SWITCH in (workflow.get("env") or {}) else []
    for jname, job in jobs.items():
        if SPIRV_SWITCH in (job.get("env") or {}):
            sites.append((jname + " (job env)", None))
        for step in job.get("steps") or []:
            if SPIRV_SWITCH in (step.get("env") or {}):
                sites.append((jname, step))
    if len(sites) != 1 or sites[0][0] != "semantic-tests":
        return ["%s is set in %s; expected only on the semantic-tests test step"
                % (SPIRV_SWITCH, ", ".join(where for where, _ in sites) or "no job")]
    job, step = jobs["semantic-tests"], sites[0][1]
    expr = step["env"][SPIRV_SWITCH]
    # Which steps install SPIRV-Tools is test_ci_registration.py's check; here only where they run counts.
    installs = [s for s in job["steps"] if "spirv-tools" in str(s.get("run", ""))]
    problems = ["semantic-tests: the test step sets %s:, but it must run, and fail, on every leg" % key
                for key in ("if", "continue-on-error") if key in step]
    for cname, ctx in CONTEXTS.items():
        if "semantic-tests" not in (job_table(workflow, ctx) or {}):
            continue
        for system, leg in leg_systems(job, ctx):
            lookup = lookup_for(ctx, runner_os=RUNNER_OS[system], matrix=leg)
            value = value_of(expr, lookup)
            want = SPIRV_SWITCH_BY_OS.get(system)
            installed = any(condition(s.get("if"), lookup, lambda fn: fn in ("success", "always")) for s in installs)
            if value != want:
                problems.append("semantic-tests, %s, %s leg: %s=%r, expected %r" % (cname, system, SPIRV_SWITCH,
                                                                                     value, want))
            if installed != (value == "1"):
                problems.append("semantic-tests, %s, %s leg: SPIRV-Tools %s but %s=%r"
                                % (cname, system, "installed" if installed else "not installed", SPIRV_SWITCH, value))
    return problems


def contract_problems(ci_text, runtime_text):
    problems = []
    ci = parse_yaml(ci_text)
    for wname, workflow, expected in (("ci.yml", ci, CI_EXPECTED), ("runtime.yml", parse_yaml(runtime_text),
                                                                      RUNTIME_EXPECTED)):
        on = triggers(workflow)
        branches = sorted(as_list(((on.get("push") or {}).get("branches")) or []))
        if branches != PUSH_BRANCHES:
            problems.append("%s: push branches %s, expected %s" % (wname, branches, PUSH_BRANCHES))
        problems += ["%s: no %s trigger" % (wname, e) for e in ("pull_request", "workflow_dispatch") if e not in on]
        for cname, ctx in CONTEXTS.items():
            problems += table_problems(wname, cname, job_table(workflow, ctx), expected[cname])
            # A cancelled run starts nothing: every job waits on success() or !cancelled().
            want = None if expected[cname] is None else {}
            problems += table_problems(wname, cname + " (cancelled)", job_table(workflow, ctx._replace(cancelled=True)),
                                       want)
            # The jobs that need another one guard with !cancelled(), so its failure changes nothing downstream.
            problems += table_problems(wname, cname + " (a needed job fails)",
                                       job_table(workflow, ctx._replace(failing=True)), expected[cname])
    return problems + spirv_switch_problems(ci)


class WorkflowEventMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci, cls.runtime = read(CI), read(RUNTIME)

    def mutated(self, text, old, new):
        self.assertIn(old, text, "the mutation no longer applies; update the control")
        return text.replace(old, new, 1)

    def test_the_workflows_follow_the_tables(self):
        self.assertEqual(contract_problems(self.ci, self.runtime), [])

    def test_the_evaluator_follows_github(self):
        def value(expr, **kw):
            return value_of("${{ %s }}" % expr, lookup_for(CONTEXTS["push, main"], **kw))
        self.assertEqual(value("runner.os == 'Linux' && '1' || '0'", runner_os="Linux"), "1")
        self.assertEqual(value("runner.os == 'Linux' && '1' || '0'", runner_os="Windows"), "0")
        self.assertIs(value("github.event_name == 'PUSH'"), True)
        self.assertIs(value("null == ''"), True)
        self.assertIs(value("null != 'true'"), True)
        self.assertIs(value("'1' == '01'"), False)
        self.assertEqual(value("fromJSON('[\"a\", \"b\"]')"), ["a", "b"])
        self.assertIs(value("fromJSON('1') == '1'"), True)
        self.assertIs(value("fromJSON('1') == fromJSON('1.0')"), True)
        lookup = lookup_for(CONTEXTS["push, main"])
        self.assertFalse(condition("github.event_name == 'push'", lookup, lambda fn: fn == "always"))
        self.assertTrue(condition("${{ !cancelled() && github.event_name == 'push' }}", lookup, lambda fn: False))

    def test_a_skipped_need_skips_a_job_without_a_status_function(self):
        ci = self.mutated(self.ci, "if: ${{ !cancelled() && needs.change-scope.outputs.docs-only != 'true' }}",
                          "if: ${{ needs.change-scope.outputs.docs-only != 'true' }}")
        problems = contract_problems(ci, self.runtime)
        for cname in ("push, integration/ci-recovery", "push, main", "workflow_dispatch"):
            self.assertIn("ci.yml, %s: check runs on nothing, expected windows" % cname, problems)

    def test_always_would_start_a_job_in_a_cancelled_run(self):
        ci = self.mutated(self.ci, "if: ${{ !cancelled() && needs.change-scope.outputs.docs-only != 'true' }}",
                          "if: ${{ always() && needs.change-scope.outputs.docs-only != 'true' }}")
        self.assertIn("ci.yml, push, main (cancelled): check runs on windows, expected nothing",
                      contract_problems(ci, self.runtime))

    def test_macos_on_pull_requests_is_reported(self):
        ci = self.mutated(self.ci, "(github.event_name == 'workflow_dispatch' ||",
                          "(github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' ||")
        self.assertIn("ci.yml, pull request, code: macos-python runs on macos, expected nothing",
                      contract_problems(ci, self.runtime))
        runtime = self.mutated(self.runtime, "(github.event_name == 'workflow_dispatch' ||",
                               "(github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch' ||")
        self.assertIn("runtime.yml, pull request, code: macos runs on macos, expected nothing",
                      contract_problems(self.ci, runtime))

    def test_dropping_the_recovery_branch_is_reported(self):
        ci = self.mutated(self.ci, "branches: [main, integration/ci-recovery]", "branches: [main]")
        problems = contract_problems(ci, self.runtime)
        self.assertIn("ci.yml, push, integration/ci-recovery: not started, expected started", problems)
        self.assertIn("ci.yml: push branches ['main'], expected %s" % PUSH_BRANCHES, problems)

    def test_swapping_the_semantic_matrix_is_reported(self):
        ci = self.mutated(self.ci, """'["ubuntu-latest"]' || '["windows-latest", "ubuntu-latest"]'""",
                          """'["windows-latest", "ubuntu-latest"]' || '["ubuntu-latest"]'""")
        problems = contract_problems(ci, self.runtime)
        self.assertIn("ci.yml, pull request, code: semantic-tests runs on linux + windows, expected linux", problems)
        self.assertIn("ci.yml, push, main: semantic-tests runs on linux, expected linux + windows", problems)

    def test_requiring_spirv_tools_on_windows_is_reported(self):
        ci = self.mutated(self.ci, "runner.os == 'Linux' && '1' || '0'", "runner.os == 'Linux' && '1' || '1'")
        problems = contract_problems(ci, self.runtime)
        self.assertIn("semantic-tests, push, main, windows leg: %s='1', expected '0'" % SPIRV_SWITCH, problems)
        self.assertIn("semantic-tests, push, main, windows leg: SPIRV-Tools not installed but %s='1'" % SPIRV_SWITCH,
                      problems)

    def test_wine_after_failed_native_tests_is_checked(self):
        runtime = self.mutated(self.runtime, "github.event_name != 'pull_request' && !cancelled() && ",
                               "github.event_name != 'pull_request' && ")
        self.assertIn("runtime.yml, push, main (a needed job fails): wine runs on nothing, expected linux + linux",
                      contract_problems(self.ci, runtime))

    def test_dropping_a_leg_is_reported(self):
        runtime = self.mutated(self.runtime, "wine: [ubuntu, winehq-stable]", "wine: [ubuntu]")
        self.assertIn("runtime.yml, push, main: wine runs on linux, expected linux + linux",
                      contract_problems(self.ci, runtime))

    def test_a_conditional_semantic_test_step_is_reported(self):
        ci = self.mutated(self.ci, "      - name: Translator semantic tests\n",
                          "      - name: Translator semantic tests\n        if: runner.os == 'Windows'\n")
        self.assertIn("semantic-tests: the test step sets if:, but it must run, and fail, on every leg",
                      contract_problems(ci, self.runtime))

    def test_unknown_constructs_fail_closed(self):
        old = "(github.event_name == 'workflow_dispatch' ||"
        schedule = "  workflow_dispatch:\n  schedule:\n    - cron: '0 3 * * *'\n"
        upload = "id: upload-wine-artifact\n"
        cases = [  # (ci.yml, runtime.yml)
            (self.mutated(self.ci, old, "(contains(github.ref, 'main') ||"), self.runtime),
            (self.mutated(self.ci, old, "(github.base_ref == 'main' ||"), self.runtime),
            (self.mutated(self.ci, "outputs.docs-only != ", "outputs.docs_only != "), self.runtime),
            (self.mutated(self.ci, "  workflow_dispatch:\n", schedule), self.runtime),
            (self.mutated(self.ci, "runs-on: ubuntu-24.04", "runs-on: self-hosted"), self.runtime),
            (self.ci, self.mutated(self.runtime, "    timeout-minutes: 20\n", "    continue-on-error: true\n")),
            (self.ci, self.mutated(self.runtime, "artifact.outcome }}", "artifact.outcomes }}")),
            (self.ci, self.mutated(self.runtime, "id: upload-wine-artifact\n", "id: upload-wine\n")),
            (self.ci, self.mutated(self.runtime, "id: upload-wine-artifact\n", upload + "        if: false\n")),
            (self.mutated(self.ci, "steps.paths.outputs.docs-only", "steps.path.outputs.docs-only"), self.runtime),
            (self.mutated(self.ci, "docs-only=$(", "docs_only=$("), self.runtime),
            (self.mutated(self.ci, "  change-scope:\n", '  change-scope:\n    name: "a \\" # b"\n'), self.runtime),
            (self.ci, self.mutated(self.runtime, "wine: [ubuntu, winehq-stable]", "wine:\n          - - ubuntu")),
        ]
        for i, (ci, runtime) in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(Unsupported):
                contract_problems(ci, runtime)


if __name__ == "__main__":
    unittest.main()
