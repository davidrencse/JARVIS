"""Complex work: a lead planner splits a goal into specialist subtasks, then JARVIS
runs them in dependency order. Each subtask agent may spawn its own subagents
(third level) when its power tier allows. Plans need an explicit `run` to execute.

Not brain.py: that one splits everyday conversational requests across model levels.
"""

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .router import specialists
from .runner import POWERS, final_message, run_task
from .fsio import atomic_write
from .locking import exclusive_lock
from .vault import recall as vault_recall

READ_ONLY_SPECIALISTS = {"architect", "reviewer", "researcher", "security", "scout",
                         "prompt-engineer"}
MAX_SUBTASKS = 20


def planner_spec(goal, catalog, recall, workspace=".", lanes=1):
    roster = "\n".join(f"- {name} (default power {s['power']}): {s['description']}"
                       for name, s in catalog.items())
    lane_rule = (
        f"JARVIS runs up to {lanes} subtasks at once, so subtasks that do not depend on "
        "each other start together: leave depends_on empty wherever one step genuinely "
        "does not need another's output, and only chain steps that really are sequential."
        if lanes > 1 else
        "JARVIS runs one subtask at a time, in dependency order.")
    return {
        "objective": "Plan this goal as specialist subtasks. Return ONLY one JSON object.",
        "provider": "claude", "role": "researcher", "mode": "read-only", "power": "high",
        "workspace": workspace,
        "context": f"""Goal from the owner:
{goal}

Specialists you can assign:
{roster}

Return JSON exactly in this shape (no prose before or after):
{{"summary": "one paragraph",
  "subtasks": [{{"id": "s1", "specialist": "<name above>", "objective": "...",
                "provider": "claude" | "codex", "power": "low" | "mid" | "high",
                "mode": "read-only" | "workspace-write", "owned_paths": ["..."],
                "depends_on": ["s0"], "acceptance_criteria": ["..."]}}]}}

Rules: at most {MAX_SUBTASKS} subtasks; ids unique; depends_on only earlier ids.
{lane_rule}
Writers that can run together must own non-overlapping paths, and a folder counts as
overlapping every path inside it: "src" and "src/app.py" collide, "src/a.py" and
"src/b.py" do not. Use the LOWEST power that can do each subtask: low for lookups and
mechanical edits, mid for normal implementation, high only for architecture, hard
debugging, security and final review. Use codex for well-specified implementation and
claude for design, review and research.
End with a reviewer subtask that checks the whole result.""",
        "evidence": recall,
    }


def extract_json(text):
    if not text:
        raise ValueError("Planner returned no answer.")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except ValueError:
        raise ValueError("Planner answer is not valid JSON.") from None


def _owned_path(value, workspace=None):
    """Canonical comparison key for a writer's workspace-relative ownership."""
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        raise ValueError(f"Invalid owned path: {value!r}.")
    parts = value.replace("\\", "/").split("/")
    if (any(part == ".." or ":" in part or "\x00" in part for part in parts)
            or not any(part and part != "." for part in parts)):
        raise ValueError(f"Invalid owned path: {value!r}.")
    # Windows treats trailing dots/spaces and case as aliases. Reject ambiguous names
    # even on other hosts so plans retain their meaning when moved to Windows.
    if any(part.rstrip(" .") != part for part in parts if part not in ("", ".")):
        raise ValueError(f"Invalid owned path: {value!r}.")
    normalized = [part for part in parts if part not in ("", ".")]
    if workspace is not None:
        root = Path(workspace).resolve()
        target = root.joinpath(*normalized).resolve()
        if target == root or root not in target.parents:
            raise ValueError(f"Owned path escapes workspace: {value!r}.")
        normalized = list(target.relative_to(root).parts)
    return "/".join(normalized).casefold()


def validate_plan(plan, catalog, workspace=None):
    if not isinstance(plan, dict) or not isinstance(plan.get("subtasks"), list):
        raise ValueError("Plan needs a subtasks list.")
    subtasks = plan["subtasks"]
    if not 1 <= len(subtasks) <= MAX_SUBTASKS:
        raise ValueError(f"Plan must have 1–{MAX_SUBTASKS} subtasks.")
    seen, owners = [], {}
    for sub in subtasks:
        if not isinstance(sub, dict):
            raise ValueError("Every subtask must be a JSON object.")
        sid = sub.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sid) or sid in seen:
            raise ValueError(f"Bad or duplicate subtask id: {sid!r}")
        if sub.get("specialist") not in catalog:
            raise ValueError(f"{sid}: unknown specialist {sub.get('specialist')!r}")
        if not isinstance(sub.get("objective"), str) or not sub["objective"].strip():
            raise ValueError(f"{sid}: objective must be non-empty text.")
        for field in ("depends_on", "owned_paths", "acceptance_criteria"):
            if not isinstance(sub.get(field, []), list):
                raise ValueError(f"{sid}: {field} must be a list.")
        if sub.get("mode", "read-only") not in ("read-only", "workspace-write"):
            raise ValueError(f"{sid}: mode must be read-only or workspace-write.")
        for dep in sub.get("depends_on", []):
            if dep not in seen:
                raise ValueError(f"{sid}: depends_on {dep!r} must name an earlier subtask.")
        if sub.get("provider", "claude") not in ("claude", "codex"):
            raise ValueError(f"{sid}: provider must be claude or codex.")
        if sub.get("power", "mid") not in POWERS:
            raise ValueError(f"{sid}: power must be low, mid, or high.")
        if sub["specialist"] in READ_ONLY_SPECIALISTS:
            sub["mode"] = "read-only"
        if sub.get("mode", "read-only") == "workspace-write":
            if not sub.get("owned_paths"):
                raise ValueError(f"{sid}: workspace-write needs owned_paths.")
            for path in sub.get("owned_paths", []):
                key = _owned_path(path, workspace)
                clash = next((p for p in owners if _nested(p, key)), None)
                if clash is not None:
                    raise ValueError(f"{sid}: {path} overlaps {clash}, owned by {owners[clash]}.")
                owners[key] = sid
        seen.append(sid)
    return plan


def _nested(a, b):
    """Do two owned_paths cover any of the same files? Writers run side by side, so
    'src/' and 'src/app.py' are a collision even though the strings differ."""
    a = str(a).replace("\\", "/").strip("/")
    b = str(b).replace("\\", "/").strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def subtask_spec(sub, plan, catalog, prior, workspace):
    specialist = catalog[sub["specialist"]]
    role = ("reviewer" if sub["specialist"] in ("reviewer", "security") else
            "researcher" if sub["specialist"] in READ_ONLY_SPECIALISTS else "implementer")
    power = sub.get("power") or specialist["power"]
    return {
        "objective": sub["objective"], "provider": sub.get("provider", "claude"),
        "role": role, "mode": sub.get("mode", "read-only"), "power": power,
        "workspace": workspace, "owned_paths": sub.get("owned_paths", []),
        "acceptance_criteria": sub.get("acceptance_criteria", []),
        "allow_subagents": power != "low",
        "context": (f"You are the {sub['specialist']} specialist on a JARVIS plan.\n"
                    f"{specialist['instructions']}\n\nPlan summary: {plan.get('summary', '')}"),
        "evidence": [f"Result of {dep}: {text[:3000]}" for dep, text in prior.items()],
    }


class Orchestrator:
    def __init__(self, store, memory, config, base):
        self.store, self.memory, self.config, self.base = store, memory, config, Path(base)
        self.catalog = specialists(base)
        self.dir = store.directory / "plans"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, plan_id):
        if not re.fullmatch(r"plan-[0-9a-f]{8}", plan_id):
            raise ValueError("Plan IDs look like plan-1a2b3c4d.")
        return self.dir / f"{plan_id}.json"

    def _save(self, record):
        atomic_write(self._path(record["id"]), json.dumps(record, indent=2), fsync=True)

    def load(self, plan_id):
        path = self._path(plan_id)
        if not path.is_file():
            raise ValueError(f"Unknown plan {plan_id}.")
        return json.loads(path.read_text(encoding="utf-8"))

    def create(self, goal, workspace=".", plan=None, timeout=900):
        """Plan a goal (one high-power planner run) or adopt a hand-written plan."""
        planner_task = None
        if plan is None:
            evidence = vault_recall(self.config, self.memory.path.parent, goal, limit=6,
                                    max_chars=2500) + self.memory.context(goal)
            spec = planner_spec(goal, self.catalog, evidence, workspace, self.store.lanes)
            planner_task = self.store.add(spec)["id"]
            result = run_task(self.store, planner_task, self.config, self.base, timeout)
            if result["state"] != "finished":
                raise RuntimeError(f"Planner run {result['state']}; see {result['artifacts']}.")
            plan = extract_json(result.get("answer"))
        plan = validate_plan(plan, self.catalog)
        record = {"id": f"plan-{secrets.token_hex(4)}", "goal": goal, "state": "planned",
                  "workspace": workspace, "planner_task": planner_task,
                  "summary": plan.get("summary", ""), "subtasks": plan["subtasks"],
                  "results": {}, "tasks": {}}
        item = self.memory.add("plan", json.dumps(plan, indent=2), title=goal[:200],
                               tags=["plan", record["id"]], source="orchestrator",
                               importance=0.7, meta={"plan_id": record["id"]})
        record["memory_id"] = item["id"]
        self._save(record)
        return record

    def ready(self, record):
        done = {sid for sid, r in record["results"].items() if r["state"] == "finished"}
        return [s for s in record["subtasks"] if s["id"] not in record["results"]
                and all(d in done for d in s.get("depends_on", []))]

    def run(self, plan_id, timeout=1800, max_steps=MAX_SUBTASKS):
        """Run ready subtasks until done, blocked, or max_steps.

        Steps whose dependencies are all satisfied run together, up to the ledger's lane
        count; the planner is told to give concurrent writers non-overlapping owned_paths
        and validate_plan refuses a plan that does not."""
        if not 1 <= timeout <= 3600:
            raise ValueError("timeout must be between 1 and 3600 seconds.")
        if not 1 <= max_steps <= MAX_SUBTASKS:
            raise ValueError(f"max_steps must be between 1 and {MAX_SUBTASKS}.")
        with exclusive_lock(self._path(plan_id).with_suffix(".lock")):
            return self._run(plan_id, timeout, max_steps)

    def _run(self, plan_id, timeout, max_steps):
        record = self.load(plan_id)
        if record["state"] in ("finished", "blocked"):
            return record
        root = (self.base / record["workspace"]).resolve()
        validate_plan({"subtasks": record["subtasks"]}, self.catalog, root)
        uncertain = {sid for sid, task_id in record["tasks"].items()
                     if sid not in record["results"]
                     and record.get("resume_tasks", {}).get(sid) != task_id}
        if uncertain:
            raise ValueError("Plan has a previously dispatched step without a checkpoint: "
                             + ", ".join(sorted(uncertain))
                             + ". Inspect its recorded task and artifacts before recovery; "
                             "it will not be run again automatically.")
        if any(r["state"] != "finished" for r in record["results"].values()):
            record["state"] = "blocked"
            self._save(record)
            return record
        record["state"] = "running"
        steps = 0
        while steps < max_steps and (batch := self.ready(record)):
            wave = batch[:max(1, min(self.store.lanes, max_steps - steps))]
            # Every dispatch is checkpointed before any of them starts, so a crash mid-wave
            # leaves each step recoverable by its own recorded task id.
            dispatch = []
            for sub in wave:
                prior = {d: record["results"][d].get("answer") or ""
                         for d in sub.get("depends_on", [])}
                spec = subtask_spec(sub, record, self.catalog, prior, record["workspace"])
                task_id = record.get("resume_tasks", {}).pop(sub["id"], None)
                if task_id is None:
                    task_id = self.store.add(spec)["id"]
                record["tasks"][sub["id"]] = task_id
                dispatch.append((sub, task_id))
            self._save(record)

            results, error = self._execute_wave(dispatch, timeout)
            for sub, task_id in dispatch:  # wave order, so a replay records identically
                result = results.get(sub["id"])
                if result is None:
                    continue  # never came back; its dispatch stands, for recover() to judge
                record["results"][sub["id"]] = {"state": result["state"], "task": task_id,
                                                "answer": (result.get("answer") or "")[:20000]}
                if result["state"] != "finished":
                    record["state"] = "blocked"
            # Execution is authoritative; failure to index memory must not replay work.
            self._save(record)
            for sub, _ in dispatch:
                answer = (results.get(sub["id"]) or {}).get("answer")
                if answer:
                    self._index_result(record, sub, answer)
            if error is not None:  # raised only once the finished siblings are durable
                raise error
            steps += len(results)
            if record["state"] == "blocked":
                break
        if record["state"] == "running":
            record["state"] = ("finished" if len(record["results"]) == len(record["subtasks"])
                               else "planned")
        self._save(record)
        return record

    def _execute_wave(self, dispatch, timeout):
        """Run a wave; return ({step id: result}, first error or None).

        An ordinary step failure comes back from run_task as a non-finished state, so a
        raise means the bookkeeping itself is unsafe (a lane claim lost to another
        process, a disk error) and must reach the caller. It is handed back rather than
        thrown so the steps that did finish can be checkpointed first."""
        def one(task_id):
            return run_task(self.store, task_id, self.config, self.base, timeout)

        if len(dispatch) == 1:  # the serial path, unchanged
            sub, task_id = dispatch[0]
            try:
                return {sub["id"]: one(task_id)}, None
            except Exception as exc:
                return {}, exc
        results, error = {}, None
        with ThreadPoolExecutor(max_workers=len(dispatch),
                                thread_name_prefix="jarvis-step") as pool:
            futures = {sub["id"]: pool.submit(one, task_id) for sub, task_id in dispatch}
            for sid, fut in futures.items():
                try:
                    results[sid] = fut.result()
                except Exception as exc:
                    error = error or exc
        return results, error

    def _index_result(self, record, sub, answer):
        """Content-deduped indexing can be retried without executing the task."""
        item = self.memory.add("result", answer, title=f"{record['id']}/{sub['id']}: "
                               f"{sub['objective'][:150]}", tags=["result", record["id"],
                               sub["specialist"]], source=f"agent:{sub['specialist']}",
                               links=[("part-of", record["memory_id"])])
        record["results"][sub["id"]]["memory_id"] = item["id"]
        self._save(record)

    def recover(self, plan_id, apply=False):
        """Inspect recorded dispatches; explicitly reconcile without launching agents.

        Finished work is adopted, failed work stays blocked, and never-started work
        keeps its original task ID for a later explicit run. Running/missing tasks
        need inspection, not a guessed retry. Run and recovery share the same lock.
        """
        with exclusive_lock(self._path(plan_id).with_suffix(".lock")):
            record = self.load(plan_id)
            steps = []
            for sub in record["subtasks"]:
                sid, task_id = sub["id"], record["tasks"].get(sub["id"])
                step = {"id": sid, "task": task_id, "action": "not_dispatched"}
                if sid in record["results"]:
                    result = record["results"][sid]
                    step.update(action="checkpointed", state=result["state"],
                                memory_indexed=bool(result.get("memory_id")))
                elif task_id:
                    try:
                        task = self.store.get(task_id)
                    except ValueError:
                        step.update(action="inspect", state="missing")
                    else:
                        step["state"] = task["state"]
                        if task["state"] == "queued":
                            step["action"] = "resume_original_task"
                        elif task["state"] in ("finished", "failed", "timed_out", "interrupted"):
                            step["action"] = "restore_checkpoint"
                            step["artifacts"] = str(self.store.directory / "runs" / task_id)
                        else:
                            step["action"] = "inspect"
                steps.append(step)
            report = {"id": plan_id, "state": record["state"], "applied": False, "steps": steps}
            if not apply:
                return report
            unresolved = [s["id"] for s in steps if s["action"] == "inspect"]
            if unresolved:
                raise ValueError("Recovery needs task inspection for: " + ", ".join(unresolved)
                                 + ". Running or missing tasks will not be retried.")
            for step in steps:
                sid, task_id = step["id"], step["task"]
                if step["action"] == "resume_original_task":
                    # Do not clear dispatches or create replacement IDs: the standalone
                    # runner may claim this task concurrently. Its ledger CAS decides.
                    record.setdefault("resume_tasks", {})[sid] = task_id
                elif step["action"] == "restore_checkpoint":
                    task = self.store.get(task_id)
                    answer = final_message(task["spec"].get("provider", "codex"),
                                           self.store.directory / "runs" / task_id) or ""
                    record["results"][sid] = {"task": task_id, "state": task["state"],
                                               "answer": answer[:20000],
                                               "output_missing": not bool(answer)}
                    record.get("resume_tasks", {}).pop(sid, None)
            if any(r["state"] != "finished" for r in record["results"].values()):
                record["state"] = "blocked"
            else:
                record["state"] = ("finished" if len(record["results"]) == len(record["subtasks"])
                                   else "planned")
            self._save(record)
            for sub in record["subtasks"]:
                result = record["results"].get(sub["id"], {})
                if result.get("answer") and not result.get("memory_id"):
                    task = self.store.get(result["task"])
                    answer = final_message(task["spec"].get("provider", "codex"),
                                           self.store.directory / "runs" / result["task"])
                    self._index_result(record, sub, answer or result["answer"])
            report.update(applied=True, state=record["state"],
                          missing_output=[sid for sid, r in record["results"].items()
                                          if r.get("output_missing")])
            return report
