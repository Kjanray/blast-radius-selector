"""Blast Radius selector.

For each patch:
  1. apply it to a pristine sqlglot checkout (in a symlink overlay under /tmp),
  2. find the code units whose AST actually changed,
  3. map those units to candidate tests with a coverage map built at the pinned commit,
  4. run the candidates against the patched tree and keep only the ones that fail,
  5. split the failing tests over workers to minimise busiest-worker + 2 s/worker.
"""

import ast
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
APP = Path(__file__).resolve().parent
MAP = APP / "map.json.gz"
TMP = Path(os.environ.get("BR_TMP", "/tmp"))
PARALLEL = int(os.environ.get("BR_PAR", "2"))
DEADLINE = float(os.environ.get("BR_DEADLINE", "150"))  # seconds of wall time we allow ourselves
START = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- patch parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Return list of dicts: old, new (paths or None), hunks [(ostart, olen, nstart, nlen, lines)]."""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            files.append(cur)
        elif ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur["hunks"]:
                cur = {"old": None, "new": None, "hunks": [], "binary": False}
                files.append(cur)
            o = ln[4:].split("\t")[0]
            n = lines[i + 1][4:].split("\t")[0]
            cur["old"] = None if o == "/dev/null" else re.sub(r"^a/", "", o)
            cur["new"] = None if n == "/dev/null" else re.sub(r"^b/", "", n)
            i += 2
            continue
        elif ln.startswith("new file mode") and cur is not None:
            cur["created"] = True
        elif ln.startswith("deleted file mode") and cur is not None:
            cur["deleted"] = True
        elif ln.startswith("rename from ") and cur is not None:
            cur["old"] = ln[len("rename from "):]
        elif ln.startswith("rename to ") and cur is not None:
            cur["new"] = ln[len("rename to "):]
        elif ln.startswith("Binary files") or ln.startswith("GIT binary patch"):
            if cur is not None:
                cur["binary"] = True
        elif ln.startswith("@@") and cur is not None:
            m = HUNK.match(ln)
            if m:
                os_, ol, ns, nl = m.groups()
                os_, ns = int(os_), int(ns)
                ol = 1 if ol is None else int(ol)
                nl = 1 if nl is None else int(nl)
                body = []
                i += 1
                oc = nc = 0
                while i < len(lines) and (oc < ol or nc < nl):
                    b = lines[i]
                    if b.startswith("\\"):
                        i += 1
                        continue
                    tag = b[:1] if b else " "
                    body.append((tag, b[1:]))
                    if tag in " -":
                        oc += 1
                    if tag in " +":
                        nc += 1
                    i += 1
                # swallow "\ No newline at end of file"
                while i < len(lines) and lines[i].startswith("\\"):
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], "nonl")
                    i += 1
                cur["hunks"].append((os_, ol, ns, nl, body))
                continue
        i += 1
    for f in files:
        if f.get("created"):
            f["old"] = None
        if f.get("deleted"):
            f["new"] = None
    return files


def apply_hunks(old_text, hunks):
    """Apply hunks to old_text. Returns new text or raises ValueError."""
    old = old_text.split("\n") if old_text else []
    # keep track of trailing newline
    trailing_nl = old_text.endswith("\n") if old_text else True
    if old_text and trailing_nl:
        old = old[:-1]
    out = []
    pos = 0  # index into old (0-based)
    new_trailing = trailing_nl
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol > 0 else os_  # for ol == 0, os_ is the line after which to insert
        expected = [b[1] for b in body if b[0] in " -"]
        # locate (allow small fuzz in offset)
        found = None
        for delta in [0] + [d for k in range(1, 200) for d in (k, -k)]:
            s = start + delta
            if s < pos or s < 0 or s + len(expected) > len(old):
                continue
            if old[s:s + len(expected)] == expected:
                found = s
                break
        if found is None:
            raise ValueError("hunk does not apply")
        out.extend(old[pos:found])
        for b in body:
            if b[0] in " +":
                out.append(b[1])
        pos = found + len(expected)
        for b in body:
            if len(b) > 2 and b[0] in " +":
                new_trailing = False
            elif len(b) > 2 and b[0] == "-":
                new_trailing = True
    out.extend(old[pos:])
    text = "\n".join(out)
    if out and new_trailing:
        text += "\n"
    return text


# --------------------------------------------------------------------------- AST units

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def units(text):
    """qualname -> (kind, dump, (start, end)).

    kind 'func' for functions, 'cls' for a class's own statements, 'mod' for module statements.
    A class/module "own statements" dump excludes function bodies (they are their own units) but
    keeps the list of method names so added/removed methods show up as class-level changes.
    """
    tree = ast.parse(text)
    res = {}

    def visit_body(body, prefix, kind, span):
        own = []
        for st in body:
            if isinstance(st, FUNC):
                q = prefix + st.name
                res[q] = ("func", _dump(st), (st.lineno, st.end_lineno))
                # decorators matter at definition time; include them in owner dump too
                own.append("def:" + st.name + ":" + "".join(_dump(d) for d in st.decorator_list))
                visit_nested(st, q + ".")
            elif isinstance(st, ast.ClassDef):
                q = prefix + st.name
                own.append("class:" + st.name)
                visit_body(st.body, q + ".", "cls", (st.lineno, st.end_lineno))
                hdr = _dump(ast.ClassDef(name=st.name, bases=st.bases, keywords=st.keywords, body=[], decorator_list=st.decorator_list, type_params=getattr(st, "type_params", [])))
                res[q + ".<hdr>"] = ("cls", hdr, (st.lineno, st.end_lineno))
            else:
                own.append(_dump(st))
        res[prefix + "<own>"] = (kind, "\n".join(own), span)

    def visit_nested(fn, prefix):
        pass  # nested defs are part of the function's dump

    visit_body(tree.body, "", "mod", (1, len(text.split("\n")) + 1))
    return res


def changed_units(old_text, new_text):
    """Return list of (kind, qualname, old_span or None). None result means unparsable."""
    try:
        new_u = units(new_text)
    except SyntaxError:
        return None
    try:
        old_u = units(old_text)
    except SyntaxError:
        return None
    out = []
    for q in set(old_u) | set(new_u):
        o = old_u.get(q)
        n = new_u.get(q)
        if o is not None and n is not None and o[1] == n[1]:
            continue
        if o is None:
            # added unit: treat as change to its owner
            owner = q.rsplit(".", 1)[0] + ".<own>" if "." in q else "<own>"
            ok = old_u.get(owner)
            if ok is not None:
                out.append((ok[0], owner, ok[2]))
            else:
                out.append(("mod", "<own>", old_u["<own>"][2]))
            continue
        out.append((o[0], q, o[2]))
    return out


# --------------------------------------------------------------------------- map

class Map:
    def __init__(self, path, tests):
        with gzip.open(path, "rt") as fh:
            m = json.load(fh)
        self.sets = [set(s) for s in m["sets"]]
        ids = m["tests"]
        num = {t["id"]: t["number"] for t in tests}
        self.conv = [num.get(t) for t in ids]  # map idx -> /data number
        self.files = {f: {int(k): v for k, v in d.items()} for f, d in m["files"].items()}
        self.import_lines = {f: set(v) for f, v in m["import_lines"].items()}
        self.opens = m["opens"]
        self.classes = m.get("classes", {})
        self.cpu = {}
        for i, c in enumerate(m.get("cpu", [])):
            if self.conv[i] is not None:
                self.cpu[self.conv[i]] = c
        self.setup = m.get("setup", {})

    def _num(self, idxs):
        return {self.conv[i] for i in idxs if i >= 0 and self.conv[i] is not None}

    def span_tests(self, path, start, end):
        fl = self.files.get(path)
        if not fl:
            return set()
        acc = set()
        seen = set()
        for ln in range(start, end + 1):
            s = fl.get(ln)
            if s is not None and s not in seen:
                seen.add(s)
                acc |= self.sets[s]
        return self._num(acc)

    def file_tests(self, path):
        fl = self.files.get(path)
        if not fl:
            return set()
        return self.span_tests(path, 1, max(fl))

    def class_tests(self, key):
        s = self.classes.get(key)
        return set() if s is None else self._num(self.sets[s])

    def open_tests(self, path):
        s = self.opens.get(path)
        if s is None:
            return set(), False
        st = self.sets[s]
        return self._num(st), (-1 in st)


# --------------------------------------------------------------------------- selection

def tests_in_file(tests, path):
    return {t["number"] for t in tests if t["file"] == path}


def candidates(files, mp, tests, all_nums):
    """Return (candidate test numbers, needs_run, reason)."""
    cand = set()
    any_effect = False
    for f in files:
        old, new = f["old"], f["new"]
        path = new or old
        if f.get("binary"):
            return set(all_nums), True, "binary"
        old_text = (SRC / old).read_text() if old and (SRC / old).exists() else ""
        if old and not (SRC / old).exists():
            # patch against a file we do not have: be conservative
            return set(all_nums), True, "missing-file"
        new_text = f.get("new_text")
        if new_text is None:
            new_text = ""
        if old_text == new_text and old == new:
            continue
        if path.endswith(".py") and old and new and old == new:
            cu = changed_units(old_text, new_text)
            if cu is None:
                # syntax error: whatever imports this file fails; run everything that touches it
                any_effect = True
                cand |= mp.file_tests(old)
                if old.startswith("tests/"):
                    cand |= tests_in_file(tests, old)
                if old.startswith("sqlglot/"):
                    cand |= set(all_nums)
                continue
            if not cu:
                continue  # comment/formatting only
            any_effect = True
            removed, inserts = hunk_lines(f["hunks"])
            old_tree = ast.parse(old_text)
            for kind, q, (s, e) in cu:
                if kind == "func":
                    cand |= precise_func_tests(mp, old, old_tree, s, e, removed, inserts)
                elif kind == "cls":
                    cand |= mp.span_tests(old, s, e)
                    cand |= class_users(mp, old, q, old_text)
                else:
                    cand |= mp.file_tests(old)
                if old.startswith("tests/"):
                    # test code: tests defined in the changed span
                    if kind == "func":
                        meth = q.split(".")[-1]
                        cls = q.split(".")[-2] if q.count(".") else None
                        for t in tests:
                            if t["file"] == old:
                                parts = t["id"].split("::")
                                if parts[-1] == meth and (cls is None or cls in parts):
                                    cand.add(t["number"])
                    else:
                        cand |= tests_in_file(tests, old)
            continue
        # non-python or created/deleted/renamed file
        any_effect = True
        ot, at_import = mp.open_tests(old or new)
        cand |= ot
        if at_import:
            # read during collection/import: every test in the reading module(s)
            cand |= set(all_nums)
        if path.startswith("tests/") and path.endswith(".py"):
            cand |= tests_in_file(tests, path)
        if old is None or new is None or old != new:
            for pth in {old, new} - {None}:
                d = pth.rsplit("/", 1)[0] if "/" in pth else ""
                dt, _ = mp.open_tests("DIR:" + d)
                cand |= dt
        if path.endswith(".py") and (old is None or new is None or old != new):
            # module added/removed/renamed: import-sensitive tests
            cand |= dir_tests(mp, tests, path)
        if path.endswith(".py") and old and old == new:
            cand |= mp.file_tests(old)
    return cand, any_effect, ""


def _trivial(line):
    s = line.strip()
    return not s or s.startswith("#")


def hunk_lines(hunks):
    """Old-file line numbers removed/modified, and insertion points (insert before old line P)."""
    removed, inserts = set(), set()
    for os_, ol, ns, nl, body in hunks:
        cur = os_ if ol > 0 else os_ + 1
        for b in body:
            tag = b[0]
            if tag == " ":
                cur += 1
            elif tag == "-":
                if not _trivial(b[1]):
                    removed.add(cur)
                cur += 1
            elif tag == "+":
                if not _trivial(b[1]):
                    inserts.add(cur)
    return removed, inserts


def _nodes_in(tree, s, e):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.stmt, ast.ExceptHandler, getattr(ast, "match_case", ast.ExceptHandler))):
            ln = getattr(n, "lineno", None)
            en = getattr(n, "end_lineno", None)
            if ln is None and hasattr(n, "pattern"):
                ln, en = n.pattern.lineno, n.body[-1].end_lineno
            if ln is not None and s <= ln and en <= e:
                start = ln
                decos = getattr(n, "decorator_list", None)
                if decos:
                    start = min([start] + [d.lineno for d in decos])
                out.append((start, en, n))
    return out


def _innermost(nodes, line):
    best = None
    for st, en, n in nodes:
        if st <= line <= en and (best is None or (en - st) < (best[1] - best[0])):
            best = (st, en, n)
    return best


def precise_func_tests(mp, path, tree, s, e, removed, inserts):
    """Tests that execute the statements a hunk touches inside function span [s, e]."""
    nodes = _nodes_in(tree, s, e)
    fn_node = None
    for st, en, n in nodes:
        if isinstance(n, FUNC) and n.lineno <= s + 50 and en == e:
            fn_node = (st, en, n)
    spans = set()
    whole = False
    for L in removed:
        if not (s <= L <= e):
            continue
        b = _innermost(nodes, L)
        if b is None:
            whole = True
            continue
        st, en, n = b
        if isinstance(n, FUNC) and n.body and L < n.body[0].lineno:
            # signature / decorator change: affects every caller
            spans.add((st, en))
        else:
            spans.add((st, en))
    for P in inserts:
        if not (s <= P <= e + 1):
            continue
        for L in (P - 1, P):
            if s <= L <= e:
                b = _innermost(nodes, L)
                if b is not None:
                    spans.add((b[0], b[1]))
    if whole or not spans:
        return mp.span_tests(path, s, e)
    res = set()
    for st, en in spans:
        res |= mp.span_tests(path, st, en)
    return res


def dir_tests(mp, tests, path):
    d = path.rsplit("/", 1)[0] if "/" in path else ""
    res = set()
    for t in tests:
        if "import" in t["file"] or "entry_point" in t["file"] or "build" in t["file"] or "docs" in t["file"]:
            res.add(t["number"])
    # anything that covers code in the same package __init__
    res |= mp.file_tests(d + "/__init__.py")
    return res


def class_users(mp, path, q, old_text):
    """Tests that use a class: cover any line of it (methods/lambdas), or of same-named subclasses in the file."""
    cls_q = q.rsplit(".", 1)[0]
    try:
        tree = ast.parse(old_text)
    except SyntaxError:
        return mp.file_tests(path)
    res = set()
    target = None

    def find(body, prefix):
        nonlocal target
        for st in body:
            if isinstance(st, ast.ClassDef):
                qq = prefix + st.name
                if qq == cls_q:
                    target = st
                    return
                find(st.body, qq + ".")

    find(tree.body, "")
    if target is None:
        return mp.file_tests(path)
    res |= mp.span_tests(path, target.lineno, target.end_lineno)
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    res |= mp.class_tests(mod + ":" + cls_q)
    # if the class has no executed lines at all (pure data class), fall back to the whole file
    fl = mp.files.get(path, {})
    has = any(target.lineno <= ln <= target.end_lineno for ln in fl)
    if not has:
        res |= mp.file_tests(path)
    return res


# --------------------------------------------------------------------------- overlay + run

def build_overlay(root, changes):
    """changes: rel path -> new text (None = delete)."""
    root.mkdir(parents=True, exist_ok=True)
    materialised = {root}

    def ensure_dir(rel_dir):
        # make rel_dir a real directory inside root, its siblings symlinked
        parts = [p for p in rel_dir.split("/") if p]
        cur_rel = ""
        cur = root
        for p in [""] + parts:
            if p:
                cur_rel = f"{cur_rel}/{p}" if cur_rel else p
                cur = root / cur_rel
            if cur in materialised and p:
                continue
            if p:
                if cur.is_symlink():
                    cur.unlink()
                cur.mkdir(exist_ok=True)
                materialised.add(cur)
            srcdir = SRC / cur_rel if cur_rel else SRC
            if srcdir.is_dir():
                for ent in os.listdir(srcdir):
                    dst = cur / ent
                    if not dst.exists() and not dst.is_symlink():
                        os.symlink(srcdir / ent, dst)

    # root level
    for ent in os.listdir(SRC):
        dst = root / ent
        if not dst.exists() and not dst.is_symlink():
            os.symlink(SRC / ent, dst)
    for rel, text in changes.items():
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        ensure_dir(d)
        dst = root / rel
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        if text is not None:
            dst.write_text(text)


RUNNER = APP / "runner_plugin.py"


WARM = ["pytest", "_pytest.python", "_pytest.unittest", "_pytest.runner", "_pytest.main", "_pytest.terminal",
        "_pytest.fixtures", "_pytest.mark", "_pytest.skipping", "_pytest.capture", "_pytest.logging",
        "_pytest.warnings", "_pytest.subtests", "pytest_timeout", "xdist", "xdist.plugin", "numpy", "pandas",
        "pandas.testing", "duckdb", "csv", "gzip", "tracemalloc", "unittest", "unittest.mock", "decimal",
        "datetime", "zoneinfo", "multiprocessing", "concurrent.futures", "concurrent.futures.process",
        "dateutil", "dateutil.parser", "pytz", "runner_plugin", "typing_extensions", "inspect", "dataclasses",
        "textwrap", "difflib", "pickle", "uuid", "statistics", "math", "functools", "itertools", "logging"]


def warm_imports():
    for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, "1")
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    import importlib
    for m in WARM:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            log("warm import failed", m, e)
    bad = [m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.") or m == "tests" or m.startswith("tests.")]
    for m in bad:
        del sys.modules[m]
    if bad:
        log("dropped pre-imported", len(bad), "sqlglot/tests modules")


def fork_pytest(root, args, envupd):
    """Fork a child that runs pytest in the overlay. Returns pid."""
    pid = os.fork()
    if pid == 0:
        try:
            fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            os.chdir(root)
            os.environ.update(envupd)
            sys.path[:0] = [str(root)]
            import pytest
            pytest.main(args)
        finally:
            os._exit(0)
    return pid


def prepare_run(cid, changes, nums, tests_by_num):
    root = TMP / f"ov_{cid}"
    build_overlay(root, changes)
    ids = sorted(tests_by_num[n]["id"] for n in nums)
    res_file = TMP / f"res_{cid}.json"
    args_file = TMP / f"args_{cid}.txt"
    args_file.write_text("\n".join(ids))
    to_file = TMP / f"to_{cid}.json"
    to_file.write_text(json.dumps({tests_by_num[n]["id"]: max(10.0, 8 * tests_by_num[n]["seconds"]) for n in nums}))
    envupd = dict(PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", BR_RES=str(res_file),
                  BR_WANTED=str(args_file), BR_TIMEOUTS=str(to_file), HOME=str(TMP),
                  PYTHONPATH=f"{root}:{APP}")
    test_files = sorted({tests_by_num[n]["file"] for n in nums})
    test_files = [f for f in test_files if (root / f).exists()]
    args = ["-q", "-p", "no:cacheprovider", "-p", "runner_plugin", "--rootdir", str(root), "-o", "addopts=",
            "--no-header", "-rN", "--tb=no", "--assert=plain", "-W", "ignore"] + test_files
    budget = max(30.0, sum(tests_by_num[n]["seconds"] for n in nums) * 8 + 30)
    return root, args, envupd, budget, res_file, [args_file, to_file, res_file]


def collect_run(cid, root, res_file, cleanup):
    r = None
    try:
        r = json.loads(res_file.read_text())
    except Exception as e:  # noqa: BLE001
        log(cid, "runner produced no result", e)
    shutil.rmtree(root, ignore_errors=True)
    for f in cleanup:
        try:
            f.unlink()
        except OSError:
            pass
    return r


def run_tests(cid, changes, nums, tests_by_num):
    root = TMP / f"ov_{cid}"
    build_overlay(root, changes)
    ids = sorted(tests_by_num[n]["id"] for n in nums)
    res_file = TMP / f"res_{cid}.json"
    args_file = TMP / f"args_{cid}.txt"
    args_file.write_text("\n".join(ids))
    timeouts = {tests_by_num[n]["id"]: max(10.0, 8 * tests_by_num[n]["seconds"]) for n in nums}
    (TMP / f"to_{cid}.json").write_text(json.dumps(timeouts))
    env = dict(os.environ)
    env.update(
        PYTHONPATH=f"{root}:{APP}",
        PYTHONDONTWRITEBYTECODE="1",
        SKIP_INTEGRATION="0",
        BR_RES=str(res_file),
        BR_WANTED=str(args_file),
        BR_TIMEOUTS=str(TMP / f"to_{cid}.json"),
        HOME=str(TMP),
    )
    test_files = sorted({tests_by_num[n]["file"] for n in nums})
    test_files = [f for f in test_files if (root / f).exists()]
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "runner_plugin",
           "--rootdir", str(root), "-o", "addopts=", "--no-header", "-rN",
           "--tb=no", "--assert=plain", "-W", "ignore"] + test_files
    budget = max(30.0, sum(tests_by_num[n]["seconds"] for n in nums) * 6 + 30)
    try:
        p = subprocess.run(cmd, cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=budget)
        err = p.stderr.decode(errors="replace")[-2000:]
    except subprocess.TimeoutExpired:
        err = "timeout"
    failed = None
    try:
        r = json.loads(res_file.read_text())
        failed = r
    except Exception as e:  # runner crashed
        log(cid, "runner produced no result", e, err)
    shutil.rmtree(root, ignore_errors=True)
    for f in (res_file, args_file, TMP / f"to_{cid}.json"):
        try:
            f.unlink()
        except OSError:
            pass
    return failed


def is_blind(t):
    """Tests that cost more CPU to verify than to just include in the output."""
    f, s = t["file"], t["seconds"]
    if f == "tests/test_executor.py":
        return True  # class setup alone loads all TPC data (~5 s CPU)
    if f == "tests/test_optimizer.py" and s >= 0.3:
        return True  # process-pool fixture tests: CPU far above recorded wall time
    return s >= 1.0


CPU_K = float(os.environ.get("BR_K", "0.07"))  # profile CPU (map) -> scorer CPU seconds


def choose_blind(allc, tests_by_num, mp):
    """Per test class: include unverified when that is cheaper than running it."""
    if not mp.cpu:
        return {n for n in allc if is_blind(tests_by_num[n])}
    groups = {}
    for n in allc:
        g = "::".join(tests_by_num[n]["id"].split("::")[:-1])
        groups.setdefault(g, set()).add(n)
    blind = set()
    for g, ns in groups.items():
        all_blind = sum(tests_by_num[n]["seconds"] for n in ns)
        per = {n: (tests_by_num[n]["seconds"], CPU_K * mp.cpu.get(n, 1.0)) for n in ns}
        mixed = CPU_K * mp.setup.get(g, 0.0) + sum(min(a, b) for a, b in per.values())
        if all_blind <= mixed:
            blind |= ns
        else:
            blind |= {n for n, (a, b) in per.items() if a < b}
    return blind


# --------------------------------------------------------------------------- split

def split(nums, tests_by_num):
    ms = {n: round(tests_by_num[n]["seconds"] * 1000) for n in nums}
    order = sorted(nums, key=lambda n: -ms[n])
    best = None
    for w in range(1, min(8, len(order)) + 1):
        workers = [[] for _ in range(w)]
        loads = [0] * w
        for n in order:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += ms[n]
        cost = max(loads) + 2000 * w
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best[1] if best else []


# --------------------------------------------------------------------------- main

def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    tests_by_num = {t["number"]: t for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    all_nums = set(tests_by_num)
    mp = Map(MAP, tests)
    patches = sorted((DATA / "commits").glob("*.patch"))
    jobs = []
    answers = {}
    for p in patches:
        cid = p.stem
        try:
            files = parse_patch(p.read_text(errors="surrogateescape"))
            changes = {}
            for f in files:
                if f.get("binary"):
                    continue
                old_text = (SRC / f["old"]).read_text() if f["old"] and (SRC / f["old"]).exists() else ""
                new_text = apply_hunks(old_text, f["hunks"]) if f["new"] else None
                f["new_text"] = new_text
                if f["old"] and f["old"] != f["new"]:
                    changes[f["old"]] = None
                if f["new"]:
                    changes[f["new"]] = new_text
            cand, effect, why = candidates(files, mp, tests, all_nums)
        except Exception as e:
            log(cid, "analysis failed", repr(e))
            answers[cid] = sorted(all_nums)  # unknown: run everything (unverified)
            continue
        if not effect or not cand:
            answers[cid] = []
            log(cid, "no effect" if not effect else "no candidates")
            continue
        jobs.append((cid, changes, cand))
    est = sum(sum(tests_by_num[n]["seconds"] for n in c) for _, _, c in jobs)
    log(f"{len(jobs)} commits to run, est {est:.1f}s of tests")

    def interpret(cid, allc, blind, cand, r):
        if r is None:
            return sorted(allc), "runner-failed"
        failed = set()
        for nid in r.get("failed", []):
            if nid in by_id:
                failed.add(by_id[nid])
        for fpath in r.get("collect_failed", []):
            # module failed to import: every selected test in it fails
            fp = fpath.rstrip("/")
            for n in cand:
                if tests_by_num[n]["id"].startswith(fp + "::") or tests_by_num[n]["file"].startswith(fp):
                    failed.add(n)
        # selected tests never reported (not collected) count as failing to be safe
        seen = set(r.get("seen", []))
        for n in cand:
            if tests_by_num[n]["id"] not in seen:
                failed.add(n)
        return sorted((failed & cand) | blind), f"ran {len(cand)} blind {len(blind)}"

    jobs.sort(key=lambda j: -sum(tests_by_num[n]["seconds"] for n in j[2]))
    queue = []
    for cid, changes, allc in jobs:
        blind = choose_blind(allc, tests_by_num, mp)
        cand = allc - blind
        if not cand:
            answers[cid] = sorted(blind)
            log(cid, "blind", len(blind))
        else:
            queue.append((cid, changes, allc, blind, cand))

    use_fork = os.environ.get("BR_FORK", "1") == "1" and hasattr(os, "fork")
    if use_fork:
        warm_imports()
        running = {}  # pid -> (job, started, budget, root, res_file, cleanup)
        while queue or running:
            while queue and len(running) < PARALLEL:
                job = queue.pop(0)
                cid, changes, allc, blind, cand = job
                if time.time() - START > DEADLINE:
                    answers[cid] = sorted(allc)
                    log(cid, "deadline")
                    continue
                root, args, envupd, budget, res_file, cleanup = prepare_run(cid, changes, cand, tests_by_num)
                pid = fork_pytest(root, args, envupd)
                running[pid] = (job, time.time(), budget, root, res_file, cleanup)
            if not running:
                break
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                now = time.time()
                for rp, (job, started, budget, *_rest) in list(running.items()):
                    if now - started > budget:
                        log(job[0], "killing after", round(now - started, 1))
                        try:
                            os.kill(rp, 9)
                        except OSError:
                            pass
                time.sleep(0.01)
                continue
            if pid not in running:
                continue
            job, started, budget, root, res_file, cleanup = running.pop(pid)
            cid, changes, allc, blind, cand = job
            r = collect_run(cid, root, res_file, cleanup)
            answers[cid], why = interpret(cid, allc, blind, cand, r)
            log(cid, why, "->", len(answers[cid]), f"failing ({time.time() - started:.1f}s)")
    else:
        def work(job):
            cid, changes, allc, blind, cand = job
            if time.time() - START > DEADLINE:
                return cid, sorted(allc), "deadline"
            r = run_tests(cid, changes, cand, tests_by_num)
            res, why = interpret(cid, allc, blind, cand, r)
            return cid, res, why

        with ThreadPoolExecutor(PARALLEL) as ex:
            for cid, res, why in ex.map(work, queue):
                answers[cid] = res
                log(cid, why, "->", len(res), "failing")
    for p in patches:
        cid = p.stem
        nums = answers.get(cid, sorted(all_nums))
        print(json.dumps({"commit": cid, "workers": split(nums, tests_by_num)}), flush=True)
    import resource
    a = resource.getrusage(resource.RUSAGE_SELF)
    b = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"done in {time.time() - START:.1f}s cpu self {a.ru_utime + a.ru_stime:.1f}s children {b.ru_utime + b.ru_stime:.1f}s")


if __name__ == "__main__":
    main()
