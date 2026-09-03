#!/usr/bin/env python3
"""
build_dashboard.py

Combine registry.yaml (the hand-written "why does this experiment exist" catalog) with every
status/<machine>.json (scan_status.py's read-only progress snapshots, one per machine, synced
into this repo via git) into one static HTML page: dashboard.html.

Two-tier layout, matching registry.yaml's own experiments:/methods: split:
  - top-level cards = EXPERIMENTS (automation/nested_learning/<name>, e.g.
    neural_memory_evaluation) - always-visible headline: purpose + one rolled-up progress meter
    covering every method/machine/condition/seed underneath it.
  - click a card's "method breakdown" to open a <details> disclosure onto its METHODS (the
    train_auto.sh configs[]-shaped dirs, e.g. neural_memory_evaluation/velocity_masked/cmaes) -
    each with its own per-machine meter, and its own <details> down to the condition x seed
    table (scan_status.py's finest grain).
A method's experiment is derived from its path (first 3 "/"-separated segments), not a
separate field - see registry.yaml's own header comment.

Unlike scan_status.py, this script never touches embodied_perceptron - it only reads files
local to this repo (registry.yaml, status/*.json), so it needs no repo_root/config.yaml.

This writes a page BODY fragment (a <title>, a <style> block, then markup) - no <!doctype>,
<html>, <head> or <body> tags - matching the shape the Artifact tool wraps at publish time.
Publishing it is a separate, deliberate step (not done by this script): run this, then publish
dashboard.html via the Artifact tool so it's reachable from a phone.

Usage:
    uv run python build_dashboard.py
    uv run python build_dashboard.py --out dashboard.html
"""
import argparse
import html
import json
import time
from pathlib import Path

import yaml

_TRACKER_DIR = Path(__file__).resolve().parent
_REGISTRY_PATH = _TRACKER_DIR / "registry.yaml"
_STATUS_DIR = _TRACKER_DIR / "status"
_OUT_PATH = _TRACKER_DIR / "dashboard.html"

# Mirrors scan_status.py's own _ACTIVE_THRESHOLD_SEC - also used here to grey out a whole
# machine's summary chip when its last scan is stale (the sync itself may have stopped, not
# just an individual run).
_STALE_MACHINE_SEC = 6 * 60 * 60

_STATUS_KEYS = ("active", "done", "stalled", "no_eval_yet")


def group_key(method_path: str) -> str:
    """automation/nested_learning/neural_memory_evaluation/velocity_masked/cmaes ->
    automation/nested_learning/neural_memory_evaluation - the experiment folder a method
    belongs to, regardless of how deeply the method itself is nested under it. Always the
    first 3 "/"-separated segments (automation/<category>/<experiment name>) by this repo's
    own directory convention (see registry.yaml's header comment)."""
    parts = method_path.split("/")
    return "/".join(parts[:3]) if len(parts) >= 3 else method_path


def load_registry(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_statuses(status_dir: Path) -> dict:
    statuses = {}
    for p in sorted(status_dir.glob("*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                statuses[p.stem] = json.load(f)
        except Exception:
            continue
    return statuses


# ---------------------------------------------------------------------------
# view-model
# ---------------------------------------------------------------------------

def empty_counts():
    return {k: 0 for k in _STATUS_KEYS}


def add_counts(a: dict, b: dict):
    for k in _STATUS_KEYS:
        a[k] = a.get(k, 0) + b.get(k, 0)


def method_touched_anywhere(method_path: str, cond_stem: str, statuses: dict) -> bool:
    """Whether any machine has at least one run under this (method, condition) - used for the
    global 'never touched on any machine' count."""
    for st in statuses.values():
        exp = st.get("experiments", {}).get(method_path)
        if not exp or not exp.get("available"):
            continue
        for cond in exp.get("conditions", []):
            if cond.get("stem") == cond_stem and cond.get("runs"):
                return True
    return False


def machine_summary_for_method(exp: dict) -> dict:
    """One machine's view of one method dir: run-status counts, mean progress, condition
    coverage. exp is a scan_status.py experiment entry (already known 'available')."""
    counts = empty_counts()
    progresses = []
    conditions_touched = 0
    for cond in exp.get("conditions", []):
        if cond.get("runs"):
            conditions_touched += 1
        for run in cond.get("runs", []):
            counts[run["status"]] = counts.get(run["status"], 0) + 1
            if run.get("progress") is not None:
                progresses.append(run["progress"])
    mean_progress = sum(progresses) / len(progresses) if progresses else None
    return {
        "counts": counts,
        "progresses": progresses,
        "mean_progress": mean_progress,
        "conditions_touched": conditions_touched,
        "conditions_total": len(exp.get("conditions", [])),
    }


def build_method_view(method_path: str, meta: dict, machines: list, statuses: dict, tally: dict):
    """tally: mutable dict accumulating global_counts / conditions_seen / conditions_never_touched
    as a side effect, shared across every method (avoids a second full pass)."""
    machine_views = []
    all_cond_stems = set()
    for m in machines:
        exp = statuses[m].get("experiments", {}).get(method_path)
        if exp is None:
            machine_views.append({"machine": m, "state": "no_data"})
            continue
        if not exp.get("available"):
            machine_views.append(
                {"machine": m, "state": "unavailable", "note": exp.get("note", "")}
            )
            continue
        for cond in exp.get("conditions", []):
            all_cond_stems.add(cond["stem"])
            for run in cond.get("runs", []):
                tally["global_counts"][run["status"]] = tally["global_counts"].get(run["status"], 0) + 1
        summary = machine_summary_for_method(exp)
        summary["machine"] = m
        summary["state"] = "ok"
        machine_views.append(summary)

    for stem in all_cond_stems:
        tally["conditions_seen"] += 1
        if not method_touched_anywhere(method_path, stem, statuses):
            tally["conditions_never_touched"] += 1

    # per-condition x per-machine breakdown, for the innermost <details> drill-down
    cond_rows = []
    for stem in sorted(all_cond_stems):
        row = {"stem": stem, "by_machine": {}}
        for m in machines:
            exp = statuses[m].get("experiments", {}).get(method_path)
            runs = []
            if exp and exp.get("available"):
                for cond in exp.get("conditions", []):
                    if cond["stem"] == stem:
                        runs = cond.get("runs", [])
            row["by_machine"][m] = runs
        cond_rows.append(row)

    return {
        "path": method_path,
        "meta": meta,
        "machine_views": machine_views,
        "condition_rows": cond_rows,
        "n_conditions": len(all_cond_stems),
    }


def aggregate_group(methods: list) -> dict:
    """Roll every method's per-machine summaries up into ONE headline number for the
    experiment card - across all methods, all machines, every run found."""
    counts = empty_counts()
    progresses = []
    conditions_total = 0
    conditions_touched = 0
    for method in methods:
        for mv in method["machine_views"]:
            if mv["state"] != "ok":
                continue
            add_counts(counts, mv["counts"])
            progresses.extend(mv["progresses"])
            conditions_touched += mv["conditions_touched"]
        conditions_total += method["n_conditions"]
    mean_progress = sum(progresses) / len(progresses) if progresses else None
    reg_status_counts = {}
    for method in methods:
        s = method["meta"].get("status", "unknown")
        reg_status_counts[s] = reg_status_counts.get(s, 0) + 1

    n_runs = sum(counts.values())
    if counts["active"] > 0:
        state = "active"
    elif n_runs > 0 and counts["done"] == n_runs:
        state = "done"
    else:
        state = "idle"
    if state == "idle":
        idle_label = "not started" if n_runs == 0 else ("stalled" if counts["stalled"] > 0 else "idle")
    else:
        idle_label = None

    return {
        "counts": counts,
        "mean_progress": mean_progress,
        "n_runs": n_runs,
        "conditions_total": conditions_total,
        "conditions_touched": conditions_touched,
        "reg_status_counts": reg_status_counts,
        "state": state,
        "idle_label": idle_label,
    }


def build_view_model(registry: dict, statuses: dict):
    machines = sorted(statuses.keys())
    experiments_meta = registry.get("experiments", {})
    methods_registry = registry.get("methods", {})

    tally = {
        "global_counts": empty_counts(),
        "conditions_seen": 0,
        "conditions_never_touched": 0,
    }

    methods_by_group = {}
    for method_path, meta in methods_registry.items():
        mv = build_method_view(method_path, meta, machines, statuses, tally)
        methods_by_group.setdefault(group_key(method_path), []).append(mv)

    # Preserve registry.yaml's experiments: order; append any group that only shows up under
    # methods: (registry.yaml omitted the header) so nothing silently disappears.
    group_order = list(experiments_meta.keys())
    for gk in methods_by_group:
        if gk not in group_order:
            group_order.append(gk)

    groups = []
    for gk in group_order:
        methods = methods_by_group.get(gk, [])
        if not methods:
            continue
        groups.append(
            {
                "path": gk,
                "meta": experiments_meta.get(gk, {"label": gk.rsplit("/", 1)[-1]}),
                "methods": methods,
                "rollup": aggregate_group(methods),
            }
        )

    # Active experiments first (need attention now), then idle/stalled, done last (nothing left
    # to watch) - stable sort keeps registry.yaml's own order as the tiebreaker within each.
    _STATE_ORDER = {"active": 0, "idle": 1, "done": 2}
    groups.sort(key=lambda g: _STATE_ORDER[g["rollup"]["state"]])

    machine_freshness = []
    now = time.time()
    for m in machines:
        gen_at = statuses[m].get("generated_at", 0)
        age = now - gen_at
        machine_freshness.append(
            {
                "machine": m,
                "generated_at": gen_at,
                "age_sec": age,
                "stale": age > _STALE_MACHINE_SEC,
            }
        )

    return {
        "machines": machines,
        "machine_freshness": machine_freshness,
        "groups": groups,
        "global_counts": tally["global_counts"],
        "conditions_seen": tally["conditions_seen"],
        "conditions_never_touched": tally["conditions_never_touched"],
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# rendering helpers
# ---------------------------------------------------------------------------

def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_pct(fraction: float) -> str:
    """Display-only percent string, 1 decimal place, rounded (not floored) and capped at 100% -
    env_steps can run past max_env_steps (a resumed run's last eval tick doesn't land exactly on
    the budget), and the raw value is worth keeping in status/*.json, but display never shows
    more than 100.0%. Rounding here is deliberately the same 1-decimal-percent precision
    scan_status.py's own done-vs-stalled check now rounds to (see its scan_condition() comment)
    - so a run that rounds up to "100.0%" here is always already classified done/green there
    too, never an orange pill showing "100.0%"."""
    return f"{min(fraction, 1.0) * 100:.1f}%"


def fmt_age(age_sec: float) -> str:
    if age_sec < 90:
        return "just now"
    m = age_sec / 60
    if m < 60:
        return f"{m:.0f}m ago"
    h = m / 60
    if h < 48:
        return f"{h:.1f}h ago"
    return f"{h / 24:.1f}d ago"


def fmt_env_steps(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


_STATUS_LABEL = {"done": "done", "active": "active", "stalled": "stalled", "no_eval_yet": "queued"}
_STATUS_CLASS = {
    "done": "st-done",
    "active": "st-active",
    "stalled": "st-stalled",
    "no_eval_yet": "st-queued",
}
_REG_STATUS_LABEL = {
    "active": "Active",
    "paused": "Paused",
    "done": "Done",
    "abandoned": "Abandoned",
}


def render_meter(fraction, size="normal") -> str:
    if fraction is None:
        pct_text = "—"
        width = 0
    else:
        pct_text = fmt_pct(fraction)
        width = max(0.0, min(1.0, fraction)) * 100
    cls = f"meter meter-{size}"
    return (
        f'<div class="{cls}"><div class="meter-track">'
        f'<div class="meter-fill" style="width:{width:.1f}%"></div></div>'
        f'<span class="meter-pct mono">{pct_text}</span></div>'
    )


def render_status_counts(counts: dict) -> str:
    parts = []
    for key in _STATUS_KEYS:
        n = counts.get(key, 0)
        if n == 0:
            continue
        parts.append(f'<span class="chip {_STATUS_CLASS[key]}">{n} {_STATUS_LABEL[key]}</span>')
    return "".join(parts) if parts else '<span class="chip st-muted">no runs</span>'


def render_machine_row(mv: dict) -> str:
    machine = esc(mv["machine"])
    if mv["state"] == "no_data":
        return (
            f'<div class="machine-row muted-row">'
            f'<span class="machine-name">{machine}</span>'
            f'<span class="muted-note">not synced yet (no status from this machine)</span></div>'
        )
    if mv["state"] == "unavailable":
        note = esc(mv.get("note", ""))
        return (
            f'<div class="machine-row muted-row">'
            f'<span class="machine-name">{machine}</span>'
            f'<span class="muted-note">not on this machine ({note})</span></div>'
        )
    meter = render_meter(mv["mean_progress"])
    counts_html = render_status_counts(mv["counts"])
    coverage = f'{mv["conditions_touched"]}/{mv["conditions_total"]} conditions started'
    return (
        f'<div class="machine-row">'
        f'<span class="machine-name">{machine}</span>'
        f"{meter}"
        f'<span class="coverage">{coverage}</span>'
        f'<span class="chips">{counts_html}</span>'
        f"</div>"
    )


def combined_repeat_count(row: dict) -> int:
    """Actual repeat count for this condition, combined across every machine (round-robin
    repeats split the seed range across all 3, so no single machine's status/*.json run_count
    is the real total - see registry.yaml's planned_repeats comment). Distinct seeds are
    deduped (two machines both landing on the same seed - overlapping SEED_START ranges -
    should count once, not twice); a run whose seed couldn't be read (e.g. a missing .hydra
    config snapshot) is counted individually since it can't be matched against anything."""
    seeds = set()
    unseeded = 0
    for runs in row["by_machine"].values():
        for run in runs:
            if run.get("seed") is not None:
                seeds.add(run["seed"])
            else:
                unseeded += 1
    return len(seeds) + unseeded


def repeats_summary_html(condition_rows: list, planned_repeats) -> str:
    """' · 6/8 conditions at target (10 repeats)' meta-line suffix - a one-glance rollup so you
    don't have to open the condition breakdown to see whether this method's repeats have caught
    up to registry.yaml's planned_repeats target. Empty string when no target is set yet."""
    if not planned_repeats or not condition_rows:
        return ""
    at_target = sum(1 for r in condition_rows if combined_repeat_count(r) >= planned_repeats)
    return f' &middot; {at_target}/{len(condition_rows)} conditions at target ({planned_repeats} repeats)'


def render_repeat_cell(row: dict, planned_repeats) -> str:
    actual = combined_repeat_count(row)
    if not planned_repeats:
        return f'<td class="repeat-cell mono">{actual}</td>'
    cls = "repeat-met" if actual >= planned_repeats else "repeat-short"
    return f'<td class="repeat-cell mono {cls}">{actual}/{planned_repeats}</td>'


def render_condition_detail_row(row: dict, machines: list, planned_repeats) -> str:
    stem = esc(row["stem"])
    cells = [render_repeat_cell(row, planned_repeats)]
    for m in machines:
        runs = row["by_machine"].get(m, [])
        if not runs:
            cells.append('<td class="cell-empty">–</td>')
            continue
        run_bits = []
        for run in sorted(runs, key=lambda r: (r.get("seed") is None, r.get("seed"))):
            seed = run["seed"] if run["seed"] is not None else "?"
            # done always shows a flat 100.0% rather than its raw percentage (scan_status.py
            # rounds env_steps/max_env_steps to the nearest whole percent for the done check, so
            # a done run's raw value can be ~99%, not just >=100%) - the pill's color (done =
            # green) and its number must never disagree.
            if run["status"] == "done":
                pct = "100.0%"
            elif run.get("progress") is not None:
                pct = fmt_pct(run["progress"])
            else:
                pct = "n/a"
            cls = _STATUS_CLASS.get(run["status"], "st-muted")
            run_bits.append(
                f'<span class="run-pill {cls}" title="{esc(fmt_env_steps(run.get("env_steps")))}'
                f'/{esc(fmt_env_steps(run.get("max_env_steps")))} env_steps">'
                f"seed{esc(seed)} {pct}</span>"
            )
        cells.append(f'<td>{"".join(run_bits)}</td>')
    return f'<tr><td class="stem-cell">{stem}</td>{"".join(cells)}</tr>'


def render_method_card(method: dict, machines: list) -> str:
    meta = method["meta"]
    label = esc(meta.get("label", method["path"]))
    purpose = esc(meta.get("purpose", "").strip())
    reg_status = meta.get("status", "unknown")
    note = esc(meta.get("note", "") or "")
    started = esc(meta.get("started") or "—")
    reg_status_label = _REG_STATUS_LABEL.get(reg_status, reg_status)

    planned_repeats = meta.get("planned_repeats")

    machine_rows = "".join(render_machine_row(mv) for mv in method["machine_views"])
    detail_rows = "".join(
        render_condition_detail_row(r, machines, planned_repeats) for r in method["condition_rows"]
    )
    header_cells = "".join(f"<th>{esc(m)}</th>" for m in machines)
    repeats_header = f"repeats /{planned_repeats}" if planned_repeats else "repeats"
    detail_table = (
        f'<table class="cond-table"><thead><tr><th>condition</th><th>{repeats_header}</th>'
        f"{header_cells}</tr></thead><tbody>{detail_rows}</tbody></table>"
        if detail_rows
        else '<p class="muted-note">no conditions/ found</p>'
    )

    return f"""
        <div class="method-card">
          <div class="method-head">
            <h3>{label}</h3>
            <span class="reg-status reg-{esc(reg_status)}">{reg_status_label}</span>
          </div>
          <p class="method-purpose">{purpose}</p>
          <div class="meta-line">Started: <span class="mono">{started}</span>{repeats_summary_html(method["condition_rows"], planned_repeats)}{f' &middot; {note}' if note else ''}</div>
          <div class="machine-rows">{machine_rows}</div>
          <details class="cond-details">
            <summary>Condition breakdown ({method["n_conditions"]} conditions)</summary>
            <div class="table-scroll">{detail_table}</div>
          </details>
        </div>
    """


def render_group_card(group: dict, machines: list) -> str:
    meta = group["meta"]
    label = esc(meta.get("label", group["path"]))
    purpose = esc((meta.get("purpose") or "").strip())
    plan_ref = esc(meta.get("plan_ref", ""))
    rollup = group["rollup"]

    reg_badges = "".join(
        f'<span class="chip reg-chip">{_REG_STATUS_LABEL.get(s, s)} &times;{n}</span>'
        for s, n in sorted(rollup["reg_status_counts"].items())
    )
    counts_html = render_status_counts(rollup["counts"])
    coverage = f'{rollup["conditions_touched"]}/{rollup["conditions_total"]} conditions started (all machines combined)'

    method_cards = "".join(render_method_card(m, machines) for m in group["methods"])

    state = rollup["state"]
    state_text = {"active": "In progress", "done": "Done"}.get(state, rollup["idle_label"])

    return f"""
    <section class="group-card">
      <div class="group-head">
        <div class="group-titles">
          <div class="eyebrow">{plan_ref}</div>
          <h2>{label}</h2>
          <div class="state-line state-{state}">
            <span class="status-dot dot-{state}"></span>{state_text}
          </div>
          <p class="purpose">{purpose}</p>
        </div>
      </div>
      <div class="group-rollup">
        {render_meter(rollup["mean_progress"], size="big")}
        <div class="rollup-meta">
          <span class="coverage">{coverage}</span>
          <div class="chips">{counts_html}{reg_badges}</div>
        </div>
      </div>
      <details class="methods-details">
        <summary>View method breakdown ({len(group["methods"])} methods)</summary>
        <div class="methods-list">{method_cards}</div>
      </details>
    </section>
    """


def render_kpi_tile(value: str, label: str, cls: str = "") -> str:
    return f'<div class="kpi {cls}"><div class="kpi-value mono">{value}</div><div class="kpi-label">{label}</div></div>'


def render_machine_chip(mf: dict) -> str:
    cls = "chip-stale" if mf["stale"] else "chip-fresh"
    age = fmt_age(mf["age_sec"])
    return f'<span class="machine-chip {cls}">{esc(mf["machine"])} <span class="mono">&middot; {age}</span></span>'


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

_STYLE = """
<meta charset="utf-8">
<title>Embodied Perceptron Runs</title>
<style>
:root {
  --surface-1:      #fcfcfb;
  --page-plane:     #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --gridline:       #e1e0d9;
  --border:         rgba(11,11,11,0.10);
  --accent:         #2a78d6;
  --accent-track:   #dfeaf9;
  --good:           #076b07;
  --good-dot:       #0ca30c;
  --good-bg:        #e4f5e2;
  --active-c:       #a3660d;
  --active-bg:      #fdf0d6;
  --active-dot:     #fab219;
  --stalled:        #b84a24;
  --stalled-bg:     #fbe6dd;
  --stalled-dot:    #ec835a;
  --muted-bg:       #eeede8;
  --card-bg:        #ffffff;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --surface-1:      #1a1a19;
    --page-plane:     #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --gridline:       #2c2c2a;
    --border:         rgba(255,255,255,0.10);
    --accent:         #3987e5;
    --accent-track:   #1d2c3f;
    --good:           #0ca30c;
    --good-dot:       #0ca30c;
    --good-bg:        #123415;
    --active-c:       #fab219;
    --active-bg:      #3a2d0e;
    --active-dot:     #fab219;
    --stalled:        #ec835a;
    --stalled-bg:     #3a2013;
    --stalled-dot:    #ec835a;
    --muted-bg:       #242422;
    --card-bg:        #1f1f1e;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --surface-1:      #1a1a19;
  --page-plane:     #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --gridline:       #2c2c2a;
  --border:         rgba(255,255,255,0.10);
  --accent:         #3987e5;
  --accent-track:   #1d2c3f;
  --good:           #0ca30c;
  --good-bg:        #123415;
  --active-c:       #fab219;
  --active-bg:      #3a2d0e;
  --active-dot:     #fab219;
  --stalled:        #ec835a;
  --stalled-bg:     #3a2013;
  --stalled-dot:    #ec835a;
  --muted-bg:       #242422;
  --card-bg:        #1f1f1e;
  color-scheme: dark;
}

* { box-sizing: border-box; }
body {
  background: var(--page-plane);
  color: var(--text-primary);
  font-family: "Source Sans 3", "Hiragino Sans", "Noto Sans JP", system-ui, sans-serif;
  margin: 0;
  padding: 0 16px 64px;
}
.mono { font-family: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace; font-variant-numeric: tabular-nums; }

.wrap { max-width: 880px; margin: 0 auto; }

header.page-head { padding: 28px 0 20px; }
header.page-head h1 {
  font-size: 1.6rem;
  margin: 0 0 6px;
  letter-spacing: -0.01em;
  text-wrap: balance;
}
header.page-head .sub { color: var(--text-secondary); font-size: 0.92rem; margin: 0 0 14px; }
.machine-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.machine-chip {
  font-size: 0.78rem;
  padding: 4px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  background: var(--card-bg);
}
.machine-chip.chip-stale { color: var(--stalled); border-color: var(--stalled); }

.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 10px;
  margin: 18px 0 30px;
}
.kpi {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 14px 12px;
}
.kpi-value { font-size: 1.5rem; font-weight: 600; }
.kpi-label { font-size: 0.76rem; color: var(--text-secondary); margin-top: 2px; }
.kpi.k-active .kpi-value { color: var(--active-c); }
.kpi.k-done .kpi-value { color: var(--good); }
.kpi.k-stalled .kpi-value { color: var(--stalled); }

/* ---- experiment-level group card (top-level, always visible) ---- */
.group-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px 22px 16px;
  margin-bottom: 18px;
}
.eyebrow {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin-bottom: 2px;
}
.group-titles h2 { font-size: 1.28rem; margin: 0; text-wrap: balance; }
.group-titles .purpose { margin-top: 6px; margin-bottom: 0; }
.purpose { color: var(--text-secondary); font-size: 0.9rem; line-height: 1.6; max-width: 65ch; }

.group-rollup {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 16px 0 4px;
  padding: 14px 16px;
  border-radius: 10px;
  background: var(--surface-1);
}
.rollup-meta { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.coverage { font-size: 0.76rem; color: var(--text-muted); white-space: nowrap; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.reg-chip { background: var(--muted-bg); color: var(--text-secondary); }

.methods-details { margin-top: 10px; }
.methods-details summary {
  cursor: pointer;
  font-size: 0.86rem;
  font-weight: 600;
  color: var(--accent);
  padding: 8px 2px;
}
.methods-list { display: flex; flex-direction: column; gap: 10px; margin-top: 6px; }

/* ---- method-level card (inside the experiment's <details>) ---- */
.method-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px 10px;
  background: var(--page-plane);
}
.method-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.method-head h3 { font-size: 0.98rem; margin: 0; text-wrap: balance; }
.reg-status {
  flex: none;
  font-size: 0.72rem;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  white-space: nowrap;
}
.reg-active { color: var(--accent); border-color: var(--accent); }
.method-purpose { color: var(--text-secondary); font-size: 0.84rem; line-height: 1.5; margin: 6px 0 4px; max-width: 65ch; }
.meta-line { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 10px; }

.machine-rows { display: flex; flex-direction: column; gap: 8px; }
.machine-row {
  display: grid;
  grid-template-columns: 108px minmax(90px, 160px) auto 1fr;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--surface-1);
}
.machine-row.muted-row { grid-template-columns: 108px 1fr; }
.machine-name { font-size: 0.82rem; font-weight: 600; }
.muted-note { font-size: 0.78rem; color: var(--text-muted); }

.meter { display: flex; align-items: center; gap: 8px; }
.meter-track { flex: 1; height: 6px; border-radius: 999px; background: var(--gridline); overflow: hidden; }
.meter-fill { height: 100%; background: var(--accent); border-radius: 999px; }
.meter-pct { font-size: 0.74rem; color: var(--text-secondary); width: 38px; flex: none; text-align: right; }
.meter-big .meter-track { height: 10px; }
.meter-big .meter-pct { font-size: 1rem; font-weight: 700; color: var(--text-primary); width: 52px; }

.chip {
  font-size: 0.7rem;
  padding: 2px 7px;
  border-radius: 999px;
  white-space: nowrap;
}
.st-active { background: var(--active-bg); color: var(--active-c); }
.st-done { background: var(--good-bg); color: var(--good); }
.st-stalled { background: var(--stalled-bg); color: var(--stalled); }
.st-queued, .st-muted { background: var(--muted-bg); color: var(--text-muted); }

.status-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: none;
}
.dot-active { background: var(--active-dot); box-shadow: 0 0 0 3px var(--active-bg); }
.dot-done   { background: var(--good-dot); box-shadow: 0 0 0 3px var(--good-bg); }
.dot-idle   { background: var(--text-muted); box-shadow: 0 0 0 3px var(--muted-bg); }
.state-line { display: flex; align-items: center; gap: 7px; font-size: 0.82rem; font-weight: 600; margin: 4px 0 2px; }
.state-line.state-active { color: var(--active-c); }
.state-line.state-done { color: var(--good); }
.state-line.state-idle { color: var(--text-muted); }

.cond-details { margin-top: 12px; }
.cond-details summary {
  cursor: pointer;
  font-size: 0.78rem;
  color: var(--text-secondary);
  padding: 6px 0;
}
.table-scroll { overflow-x: auto; margin-top: 6px; }
table.cond-table { border-collapse: collapse; width: 100%; font-size: 0.76rem; }
table.cond-table th {
  text-align: left;
  color: var(--text-muted);
  font-weight: 500;
  padding: 5px 10px;
  border-bottom: 1px solid var(--gridline);
  white-space: nowrap;
}
table.cond-table td {
  padding: 5px 10px;
  border-bottom: 1px solid var(--gridline);
  vertical-align: top;
}
.stem-cell { font-family: "JetBrains Mono", ui-monospace, monospace; color: var(--text-secondary); max-width: 260px; }
.cell-empty { color: var(--text-muted); }
.repeat-cell { color: var(--text-secondary); white-space: nowrap; }
.repeat-cell.repeat-met { color: var(--good); font-weight: 600; }
.repeat-cell.repeat-short { color: var(--stalled); }
.run-pill {
  display: inline-block;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.7rem;
  padding: 1px 6px;
  border-radius: 6px;
  margin: 1px 3px 1px 0;
  white-space: nowrap;
}

footer.page-foot { color: var(--text-muted); font-size: 0.76rem; text-align: center; padding-top: 20px; }
</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap">
"""


def render_page(vm: dict) -> str:
    machine_chips = "".join(render_machine_chip(mf) for mf in vm["machine_freshness"]) or (
        '<span class="machine-chip">no machine has synced yet</span>'
    )
    kpis = "".join(
        [
            render_kpi_tile(str(vm["global_counts"].get("active", 0)), "active runs", "k-active"),
            render_kpi_tile(str(vm["global_counts"].get("done", 0)), "done runs", "k-done"),
            render_kpi_tile(str(vm["global_counts"].get("stalled", 0)), "stalled runs", "k-stalled"),
            render_kpi_tile(str(vm["global_counts"].get("no_eval_yet", 0)), "queued runs"),
            render_kpi_tile(
                f'{vm["conditions_never_touched"]}/{vm["conditions_seen"]}',
                "conditions not started (all machines)",
            ),
        ]
    )
    cards = "".join(render_group_card(g, vm["machines"]) for g in vm["groups"])
    generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(vm["generated_at"]))

    return f"""{_STYLE}
<div class="wrap">
  <header class="page-head">
    <h1>Embodied Perceptron Runs</h1>
    <p class="sub">Progress board for embodied_perceptron training runs across 3 machines
    (MacOS / Desktop1 + Win+Docker / Desktop + Ubuntu), tracked per experiment. Open a card to
    drill down into its methods, then into per-condition x seed detail. Generated: {generated}</p>
    <div class="machine-chips">{machine_chips}</div>
  </header>

  <div class="kpi-row">{kpis}</div>

  {cards}

  <footer class="page-foot">
    Auto-generated from registry.yaml + status/*.json (build_dashboard.py).
    Edit registry.yaml to update the purpose notes.
  </footer>
</div>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", default=str(_REGISTRY_PATH))
    parser.add_argument("--status-dir", default=str(_STATUS_DIR))
    parser.add_argument("--out", default=str(_OUT_PATH))
    args = parser.parse_args()

    registry = load_registry(Path(args.registry))
    statuses = load_statuses(Path(args.status_dir))
    vm = build_view_model(registry, statuses)
    page = render_page(vm)

    out_path = Path(args.out)
    out_path.write_text(page, encoding="utf-8")
    print(f"wrote {out_path} ({len(statuses)} machine(s), {len(vm['groups'])} experiment group(s))")


if __name__ == "__main__":
    main()
