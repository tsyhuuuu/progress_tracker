#!/usr/bin/env python3
"""
scan_status.py

Read-only progress scanner for the multi-machine embodied_perceptron experiment tracker. Scans
every experiment directory listed in registry.yaml against THIS machine's local
embodied_perceptron checkout, and writes what it finds to status/<machine>.json.

progress_tracker is a separate repo from embodied_perceptron (split out of what used to be
embodied_perceptron/supervision/), so it does not live inside the repo it watches - each machine
must point it at its own embodied_perceptron checkout via config.yaml's repo_root (copy
config.yaml.example to config.yaml and edit it; gitignored since the path differs per machine).
--repo-root overrides config.yaml for a one-off run.

For each experiment dir (same shape as train_auto.sh's configs[] entries - a directory with a
conditions/ subfolder), and for each condition yaml under conditions/, this mirrors
tools/resume_or_train.py's own progress-reading approach (read_eval_progress(): the LAST row
of results/<condition_stem>/<timestamp>/plots/eval.csv, env_steps not step - see that script's
docstring for why) against EVERY existing results/<condition_stem>/<timestamp> run, not just
the latest one - round-robin REPEATS across several seeds (train_auto.sh's own pattern) can
leave several timestamp dirs alive under one condition at once, one per seed.

This performs no training/eval side effects and never touches results/outputs - read-only.
Safe to run anytime, on any of the 3 machines, independent of what train_auto.sh happens to be
doing right now.

Usage:
    uv run python scan_status.py
    uv run python scan_status.py --machine desktop1-win   # override hostname
    uv run python scan_status.py --report                 # also print a quick table
    uv run python scan_status.py --repo-root /path/to/embodied_perceptron  # skip config.yaml

Not yet wired into train_auto.sh (deliberately, for now - see README.md): run this by hand, or
add your own cron/loop, until the auto-commit+push step is added.
"""
import argparse
import json
import socket
import sys
import time
from pathlib import Path

import yaml

_TRACKER_DIR = Path(__file__).resolve().parent
_REGISTRY_PATH = _TRACKER_DIR / "registry.yaml"
_STATUS_DIR = _TRACKER_DIR / "status"
_CONFIG_PATH = _TRACKER_DIR / "config.yaml"


def resolve_repo_root(explicit: str | None, config_path: Path) -> Path:
    """embodied_perceptron's root on THIS machine: --repo-root > config.yaml's repo_root >
    a clear error telling the user how to set it up. A relative repo_root (config.yaml's
    default: "../embodied_perceptron") is resolved relative to config_path's own directory, not
    the current working directory, so this works the same regardless of where it's invoked from."""
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
    else:
        if not config_path.exists():
            sys.exit(
                f"error: {config_path} not found.\n"
                f"progress_tracker lives outside embodied_perceptron now, so it needs to be told "
                f"where THIS machine's embodied_perceptron checkout is.\n"
                f"Fix: cp {config_path.with_name('config.yaml.example')} {config_path}, then edit "
                f"repo_root - or pass --repo-root /path/to/embodied_perceptron for a one-off run."
            )
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        repo_root = cfg.get("repo_root")
        if not repo_root:
            sys.exit(f"error: {config_path} has no repo_root: entry.")
        root = Path(repo_root).expanduser()
        if not root.is_absolute():
            root = config_path.parent / root

    root = root.resolve()
    if not (root / "automation").exists():
        sys.exit(
            f"error: {root} doesn't look like an embodied_perceptron checkout "
            f"(no automation/ subfolder there). Check repo_root in {config_path}."
        )
    return root

# A run's eval.csv mtime newer than this counts as "active" (something is actively writing to
# it right now); older than this but still below max_env_steps counts as "stalled" (crashed,
# or simply not this machine's turn in a round-robin repeats loop - scan_status.py can't tell
# the difference from the filesystem alone, so "stalled" means "not currently making progress
# on THIS machine", not necessarily "broken").
_ACTIVE_THRESHOLD_SEC = 20 * 60


def read_eval_progress(run_dir: Path):
    """Return (step, env_steps) from run_dir/plots/eval.csv's LAST row, or None if
    missing/empty/unreadable. Same approach as tools/resume_or_train.py's own
    read_eval_progress() - env_steps (not step) is what max_env_steps is actually budgeted
    against."""
    eval_csv = run_dir / "plots" / "eval.csv"
    if not eval_csv.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_csv(eval_csv)
        if len(df) == 0:
            return None
        last = df.iloc[-1]
        return int(last["step"]), int(last["env_steps"])
    except Exception:
        return None


def read_seed(exp_dir: Path, config_stem: str, run_name: str):
    """The seed a run was actually trained with, from its outputs/.../.hydra/config.yaml
    snapshot (same source tools/resume_or_train.py's read_original_seed() uses) - not the
    condition yaml's current default, which only applies to a brand-new run."""
    snapshot = exp_dir / "outputs" / config_stem / run_name / ".hydra" / "config.yaml"
    if not snapshot.exists():
        return None
    try:
        with open(snapshot, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("seed")
    except Exception:
        return None


def scan_condition(exp_dir: Path, cond_yaml: Path, now: float):
    stem = cond_yaml.stem
    try:
        with open(cond_yaml, encoding="utf-8") as f:
            cond_cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        return {"stem": stem, "error": f"unreadable condition yaml: {exc}"}

    max_env_steps = cond_cfg.get("max_env_steps")
    results_dir = exp_dir / "results" / stem
    runs = []

    if results_dir.exists():
        for run_dir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
            progress = read_eval_progress(run_dir)
            eval_csv = run_dir / "plots" / "eval.csv"
            mtime = eval_csv.stat().st_mtime if eval_csv.exists() else run_dir.stat().st_mtime
            seed = read_seed(exp_dir, stem, run_dir.name)

            # models/final.pt is written by utils/logger.py's Logger.finish(), which
            # trainer/td3_online_trainer.py's train() calls exactly once, right as its while loop
            # exits for real (max_env_steps budget exhausted, cfg.steps reached, or plateau
            # convergence) - never on a crash/kill. That while loop is checked every single env
            # step, not just at eval_freq boundaries, so it can (and typically does) run for up
            # to one eval_freq's worth of extra steps past the LAST row eval.csv ever gets - no
            # further eval fires once the budget trips mid-cycle, so that final stretch (often
            # ~1% of max_env_steps) never lands in eval.csv at all. That's the "finished at 99%"
            # case: env_steps/max_env_steps genuinely undercounts real completion, and no amount
            # of rounding fixes it since the gap can exceed a rounding tolerance. final.pt
            # existing is a direct "the training loop actually finished" signal instead of an
            # inference from the (structurally lagging) progress ratio, so it takes priority.
            finished = (run_dir / "models" / "final.pt").exists()

            if progress is None:
                step, env_steps = None, None
                status = "done" if finished else "no_eval_yet"
            else:
                step, env_steps = progress
                # The max_env_steps/rounding check below is a fallback for runs from before this
                # repo tracked final.pt, or any other trainer that doesn't write it - normally
                # `finished` alone already covers the overshoot case. round()'d to the nearest
                # WHOLE percent (2 decimals of the fraction) - e.g. 2,990,000/3,000,000 = 99.67%
                # rounds to 100% and counts as done. Whichever branch fires, the dashboard/report
                # always display such a run as a flat 100.0%, not its raw percentage - see
                # build_dashboard.py's render_repeat_cell-adjacent run-pill code and this file's
                # print_report - so "done" and "shows 100%" can never disagree.
                if finished or (
                    max_env_steps and (
                        env_steps >= max_env_steps or round(env_steps / max_env_steps, 2) >= 1.0
                    )
                ):
                    status = "done"
                elif (now - mtime) < _ACTIVE_THRESHOLD_SEC:
                    status = "active"
                else:
                    status = "stalled"

            runs.append(
                {
                    "run": run_dir.name,
                    "seed": seed,
                    "status": status,
                    "step": step,
                    "env_steps": env_steps,
                    "max_env_steps": max_env_steps,
                    "progress": (
                        round(env_steps / max_env_steps, 4)
                        if env_steps is not None and max_env_steps
                        else None
                    ),
                    "last_update": int(mtime),
                }
            )

    return {
        "stem": stem,
        "max_env_steps": max_env_steps,
        # Actual repeat count observed on THIS machine alone - one run_dir per seed (see
        # train_auto.sh's resume-aware pattern: a repeat doesn't get a new dir, it resumes the
        # existing one for that seed), so this can run well past registry.yaml's methods:
        # planned_repeats: since round-robin repeats are split across all 3 machines,
        # build_dashboard.py sums this across machines (deduped by seed) for the real total.
        "run_count": len(runs),
        "runs": runs,
    }


def scan_experiment(repo_root: Path, exp_path: str, now: float):
    exp_dir = repo_root / exp_path
    if not exp_dir.exists():
        return {"available": False, "note": "directory not present on this machine"}

    conditions_dir = exp_dir / "conditions"
    if not conditions_dir.exists():
        return {"available": False, "note": "no conditions/ subfolder"}

    cond_yamls = sorted(conditions_dir.glob("*.yaml"))
    if not cond_yamls:
        return {"available": True, "conditions": [], "note": "conditions/ is empty"}

    return {
        "available": True,
        "conditions": [scan_condition(exp_dir, c, now) for c in cond_yamls],
    }


def build_status(repo_root: Path, registry: dict, machine: str):
    now = time.time()
    return {
        "machine": machine,
        "generated_at": int(now),
        "repo_root": str(repo_root),
        "experiments": {
            exp_path: scan_experiment(repo_root, exp_path, now) for exp_path in registry
        },
    }


def print_report(status: dict):
    print(f"machine: {status['machine']}  (scanned {time.ctime(status['generated_at'])})")
    print("-" * 72)
    for exp_path, exp in status["experiments"].items():
        if not exp.get("available"):
            print(f"{exp_path}: unavailable ({exp.get('note')})")
            continue
        for cond in exp["conditions"]:
            if "error" in cond:
                print(f"  {exp_path}/{cond['stem']}: {cond['error']}")
                continue
            if not cond["runs"]:
                print(f"  {exp_path}/{cond['stem']}: no runs yet")
                continue
            for run in cond["runs"]:
                # done always displays as a flat 100.0%, never its raw percentage (which can be
                # anywhere from ~99% - the round-to-nearest-whole-percent tolerance above - to
                # 133%+ on genuine overshoot) - status and displayed number must never disagree.
                # The stored progress field in status/*.json is left as the true raw value either
                # way.
                if run["status"] == "done":
                    pct = "100.0%"
                elif run["progress"] is not None:
                    pct = f"{run['progress'] * 100:5.1f}%"
                else:
                    pct = "  n/a"
                seed = run["seed"] if run["seed"] is not None else "?"
                print(
                    f"  [{run['status']:9s}] {exp_path}/{cond['stem']}  "
                    f"seed={seed}  {pct}  ({run['env_steps']}/{run['max_env_steps']})"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", default=None, help="override this machine's name (default: hostname)")
    parser.add_argument("--report", action="store_true", help="also print a human-readable table")
    parser.add_argument("--registry", default=str(_REGISTRY_PATH), help="path to registry.yaml")
    parser.add_argument("--out-dir", default=str(_STATUS_DIR), help="directory to write <machine>.json into")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="path to this machine's embodied_perceptron checkout (default: repo_root from config.yaml)",
    )
    parser.add_argument("--config", default=str(_CONFIG_PATH), help="path to config.yaml")
    args = parser.parse_args()

    machine = args.machine or socket.gethostname().replace(".local", "")
    repo_root = resolve_repo_root(args.repo_root, Path(args.config))

    with open(args.registry, encoding="utf-8") as f:
        registry = yaml.safe_load(f) or {}
    methods = registry.get("methods", {})

    status = build_status(repo_root, methods, machine)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{machine}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
    print(f"wrote {out_path}")

    if args.report:
        print()
        print_report(status)


if __name__ == "__main__":
    main()
