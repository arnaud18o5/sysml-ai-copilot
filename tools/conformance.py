"""Conformance harness against the official OMG SysML v2 release.

Measures, for every `.sysml` file of the pinned SysML-v2-Release tag
(examples, training, validation and the standard library):

  parse      the file is accepted by our grammar
  roundtrip  parse(serialize(parse(text))) is the same model (nothing lost)
             and serializing again gives the same text
  resolve    the file (with the standard library loaded) has no
             unresolved / ambiguous reference errors

and compares the result to `tools/conformance_baseline.json`. The check is a
ratchet: a file that passed a level in the baseline must keep passing it, so
coverage can only go up. Run with --update-baseline after an improvement.

    venv/bin/python tools/conformance.py fetch            # clone the pinned release
    venv/bin/python tools/conformance.py report           # summary + top blockers
    venv/bin/python tools/conformance.py check            # ratchet (CI)
    venv/bin/python tools/conformance.py check --update-baseline
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sysml_copilot.syntax import SysmlSyntaxError, parse_document, serialize_document, tree_signature  # noqa: E402
from sysml_copilot.workspace import Workspace  # noqa: E402

RELEASE_REPO = "https://github.com/Systems-Modeling/SysML-v2-Release.git"
RELEASE_TAG = "2026-09.1"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CORPUS = os.path.join(ROOT, ".cache", "SysML-v2-Release")
BASELINE = os.path.join(HERE, "conformance_baseline.json")
LEVELS = ("parse", "roundtrip", "resolve")


def fetch(corpus=DEFAULT_CORPUS, tag=RELEASE_TAG):
    if os.path.isdir(os.path.join(corpus, ".git")):
        print(f"corpus already present in {corpus}")
        return
    os.makedirs(os.path.dirname(corpus), exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", tag, "--filter=blob:none",
                    "--sparse", RELEASE_REPO, corpus], check=True)
    subprocess.run(["git", "-C", corpus, "sparse-checkout", "set", "sysml/src", "sysml.library"], check=True)
    print(f"fetched {RELEASE_REPO}@{tag} into {corpus}")


def corpus_files(corpus):
    out = []
    for d, dirs, files in os.walk(corpus):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.endswith(".sysml"):
                out.append(os.path.join(d, f))
    return out


def _category(rel):
    parts = rel.split(os.sep)
    if parts[0] == "sysml.library":
        return "library/" + parts[1]
    return parts[2] if len(parts) > 2 else parts[0]


def measure(corpus):
    files = corpus_files(corpus)
    if not files:
        raise SystemExit(f"no .sysml files under {corpus}; run `tools/conformance.py fetch` first")
    results = {}
    parsed = {}
    blockers = collections.Counter()
    for path in files:
        rel = os.path.relpath(path, corpus)
        entry = {"parse": False, "roundtrip": False, "resolve": False}
        results[rel] = entry
        try:
            text = open(path, encoding="utf-8").read()
            doc = parse_document(text, rel)
        except SysmlSyntaxError as exc:
            entry["error"] = str(exc)
            blockers[str(exc).split(" at line")[0].split(" (expected")[0]] += 1
            continue
        except Exception as exc:  # grammar/builder bug: count it, don't crash the report
            entry["error"] = f"{type(exc).__name__}: {exc}"
            blockers[f"crash: {type(exc).__name__}"] += 1
            continue
        entry["parse"] = True
        parsed[rel] = doc
        try:
            out = serialize_document(doc)
            again = parse_document(out, rel)
            same_tree = tree_signature(again) == tree_signature(doc)
            entry["roundtrip"] = same_tree and serialize_document(again) == out
            if not same_tree:
                entry["error"] = "roundtrip: the serialized text does not parse back to the same model"
        except Exception as exc:
            entry["error"] = f"roundtrip: {exc}"
    # Resolution: load every parsed library file plus the file itself. Only
    # the target file's own references are checked (resolving them may
    # still walk into the library) - a full diagnostics() pass would also
    # re-validate every library file's own references on every iteration,
    # which is pure waste since that part of the result never changes.
    library = [doc for rel, doc in parsed.items() if rel.startswith("sysml.library")]
    for rel, doc in parsed.items():
        ws = Workspace()
        ws.documents.extend([doc] + [d for d in library if d is not doc])
        ws.reindex()
        errors = [(node, role, ref) for node in doc.walk() for role, ref in node.refs()
                  if not ws.resolve_node_ref(node, ref).ok]
        results[rel]["resolve"] = not errors
        if errors:
            results[rel]["unresolved"] = len(errors)
    return results, blockers


def summarize(results):
    by_cat = collections.defaultdict(lambda: collections.Counter())
    for rel, r in results.items():
        cat = _category(rel)
        by_cat[cat]["files"] += 1
        for lvl in LEVELS:
            by_cat[cat][lvl] += r[lvl]
    total = collections.Counter()
    for c in by_cat.values():
        total.update(c)
    return total, by_cat


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def report(results, blockers, top=20):
    total, by_cat = summarize(results)
    n = total["files"]
    print(f"OMG SysML v2 release {RELEASE_TAG}: {n} files")
    for lvl in LEVELS:
        print(f"  {lvl:<10} {total[lvl]:4d}/{n}  {pct(total[lvl], n)}")
    print("\nBy category (parse / roundtrip / resolve):")
    for cat in sorted(by_cat):
        c = by_cat[cat]
        print(f"  {c['parse']:3d} {c['roundtrip']:3d} {c['resolve']:3d} / {c['files']:3d}  {cat}")
    print(f"\nTop {top} blockers (first syntax error per failing file):")
    for msg, count in blockers.most_common(top):
        print(f"  {count:4d}  {msg}")


def check(results, update=False):
    current = {rel: [lvl for lvl in LEVELS if r[lvl]] for rel, r in results.items()}
    if update:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump({"release": RELEASE_TAG, "files": dict(sorted(current.items()))}, f, indent=1)
            f.write("\n")
        print(f"baseline updated: {BASELINE}")
        return 0
    try:
        with open(BASELINE, encoding="utf-8") as f:
            baseline = json.load(f)["files"]
    except FileNotFoundError:
        print("no baseline yet; run with --update-baseline")
        return 1
    regressions = []
    for rel, levels in baseline.items():
        now = set(current.get(rel, []))
        lost = [lvl for lvl in levels if lvl not in now]
        if lost:
            regressions.append((rel, lost, results.get(rel, {}).get("error")))
    gains = sum(len(set(current[r]) - set(baseline.get(r, []))) for r in current)
    if regressions:
        print(f"{len(regressions)} conformance regression(s):")
        for rel, lost, err in regressions:
            print(f"  {rel}: lost {', '.join(lost)}" + (f" ({err})" if err else ""))
        return 1
    print(f"no regression ({gains} new level(s) passing"
          + ("; run with --update-baseline to lock them in)" if gains else ")"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "report", "check", "json"])
    ap.add_argument("--corpus", default=os.environ.get("SYSML_CORPUS", DEFAULT_CORPUS))
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args(argv)
    if args.command == "fetch":
        fetch(args.corpus)
        return 0
    results, blockers = measure(args.corpus)
    if args.command == "json":
        print(json.dumps(results, indent=1))
        return 0
    if args.command == "report":
        report(results, blockers, args.top)
        return 0
    report(results, blockers, top=5)
    print()
    return check(results, args.update_baseline)


if __name__ == "__main__":
    sys.exit(main())
