#!/usr/bin/env python3
"""Generate and evaluate a new 10-instance astropy batch with olmo-3."""

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib import request

from datasets import load_from_disk

import run_olmo3_batch10 as base_runner
from run_olmo3_batch10 import (
    DATASET_NAME,
    DATASET_SPLIT,
    MODEL,
    build_fallback_patch,
    build_patch_prompt,
    build_rewrite_prompt,
    ensure_repo_checkout,
    extract_candidate_paths,
    extract_diff,
    normalize_patch_newlines,
    precheck_patch_with_git_apply,
    safe_filename,
    validate_diff_paths,
    validate_diff_text,
    write_json,
)


base_runner.OLLAMA_URL = "http://localhost:11434/api/generate"
LOCAL_DATASET_PATH = "dataset_local"
ONE_HOUR_SECONDS = 3600
MAX_MODEL_ATTEMPTS = 3
MAX_REWRITE_ATTEMPTS = 2


INSTANCE_IDS = [
    "astropy__astropy-12907",
    "astropy__astropy-13033",
    "astropy__astropy-13236",
    "astropy__astropy-13453",
    "astropy__astropy-13977",
    "astropy__astropy-14096",
    "astropy__astropy-14182",
    "astropy__astropy-14365",
    "astropy__astropy-14508",
    "astropy__astropy-14539",
]


def call_ollama(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        base_runner.OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=ONE_HOUR_SECONDS) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    return parsed.get("response", "")


def run_cmd(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def copy_report_if_present(project_root: Path, stdout: str | None, dest_dir: Path) -> str:
    if stdout is None:
        return ""
    match = re.search(r"Report written to ([^\r\n]+)", stdout)
    if not match:
        return ""
    report_name = match.group(1).strip()
    report_path = project_root / report_name
    if not report_path.exists():
        return ""
    dest_path = dest_dir / safe_filename(report_name)
    dest_path.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
    return str(dest_path)


def copy_run_logs(project_root: Path, run_id: str, dest_dir: Path) -> None:
    run_root = project_root / "logs" / "run_evaluation" / run_id
    if not run_root.exists():
        return
    archive_root = dest_dir / "run_evaluation" / run_id
    archive_root.mkdir(parents=True, exist_ok=True)
    for path in run_root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(run_root)
            target = archive_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def evaluate_instance(
    project_root: Path,
    fixed_dir: Path,
    logs_dir: Path,
    stamp: str,
    instance_id: str,
    predictions_path: Path,
) -> dict:
    instance_logs_dir = logs_dir / instance_id
    instance_logs_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"olmo3_astropy10_{stamp}_{instance_id.replace('__', '_').replace('-', '_')}"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{project_root.as_posix()}:/app",
        "-w",
        "/app",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "swebench-local:latest",
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        f"/app/fixed/olmo3_astropy10_{stamp}/dataset_subset.json",
        "--split",
        DATASET_SPLIT,
        "--instance_ids",
        instance_id,
        "--predictions_path",
        f"/app/fixed/olmo3_astropy10_{stamp}/{instance_id}/predictions.json",
        "--max_workers",
        "1",
        "--timeout",
        str(ONE_HOUR_SECONDS),
        "--run_id",
        run_id,
        "--namespace",
        "none",
        "--cache_level",
        "env",
        "--clean",
        "True",
    ]
    print(f"[eval] {instance_id} starting docker evaluation")
    proc = run_cmd(docker_cmd, cwd=project_root)
    eval_log = instance_logs_dir / "run_instance.log"
    stdout_text = proc.stdout if proc.stdout is not None else ""
    stderr_text = proc.stderr if proc.stderr is not None else ""
    eval_log.write_text(
        "=== COMMAND ===\n"
        + " ".join(docker_cmd)
        + "\n\n=== STDOUT ===\n"
        + stdout_text
        + "\n\n=== STDERR ===\n"
        + stderr_text
        + f"\n\n=== EXIT CODE ===\n{proc.returncode}\n",
        encoding="utf-8",
    )
    report_path = copy_report_if_present(project_root, proc.stdout, instance_logs_dir)
    copy_run_logs(project_root, run_id, instance_logs_dir)
    return {
        "run_id": run_id,
        "evaluation_exit_code": proc.returncode,
        "evaluation_log": str(eval_log),
        "evaluation_report": report_path,
        "predictions_path": str(predictions_path),
    }


def get_valid_patch(
    task: dict,
    inst_dir: Path,
    repo_cache_root: Path,
    candidate_paths: list[str],
) -> tuple[str, list[dict], str]:
    attempts: list[dict] = []
    last_reason = "initial generation"
    base_prompt = build_patch_prompt(task, candidate_paths)
    (inst_dir / "prompt.txt").write_text(base_prompt, encoding="utf-8")

    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        prompt = build_patch_prompt(task, candidate_paths, strict_reason=last_reason)
        try:
            raw = call_ollama(prompt)
        except Exception as exc:
            last_reason = f"ollama request failed: {exc}"
            attempts.append({"attempt": attempt, "ok": False, "reason": last_reason})
            continue

        (inst_dir / f"ollama_raw_attempt_{attempt}.txt").write_text(raw, encoding="utf-8")
        patch = normalize_patch_newlines(extract_diff(raw))
        (inst_dir / f"model_patch_preview_attempt_{attempt}.diff").write_text(
            patch[:12000], encoding="utf-8"
        )

        valid, reason = validate_diff_text(patch)
        if valid:
            valid, reason = validate_diff_paths(patch, candidate_paths, task["repo"])

        rewrite_trace: list[dict] = []
        current_text = raw
        for rewrite_attempt in range(1, MAX_REWRITE_ATTEMPTS + 1):
            if valid:
                break
            rewrite_prompt = build_rewrite_prompt(task, candidate_paths, current_text, reason)
            try:
                rewritten_raw = call_ollama(rewrite_prompt)
            except Exception as exc:
                reason = f"rewrite request failed: {exc}"
                rewrite_trace.append(
                    {"rewrite_attempt": rewrite_attempt, "ok": False, "reason": reason}
                )
                continue
            (inst_dir / f"rewrite_raw_attempt_{attempt}_{rewrite_attempt}.txt").write_text(
                rewritten_raw, encoding="utf-8"
            )
            patch = normalize_patch_newlines(extract_diff(rewritten_raw))
            (inst_dir / f"rewrite_patch_attempt_{attempt}_{rewrite_attempt}.diff").write_text(
                patch[:12000], encoding="utf-8"
            )
            valid, reason = validate_diff_text(patch)
            if valid:
                valid, reason = validate_diff_paths(patch, candidate_paths, task["repo"])
            rewrite_trace.append(
                {"rewrite_attempt": rewrite_attempt, "ok": valid, "reason": reason}
            )
            current_text = rewritten_raw

        if not valid:
            last_reason = reason
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "reason": reason,
                    "rewrite_trace": rewrite_trace,
                }
            )
            continue

        patch_path = inst_dir / f"precheck_attempt_{attempt}.patch"
        pre_ok, pre_reason = precheck_patch_with_git_apply(
            patch, task, repo_cache_root, patch_path
        )
        attempts.append(
            {
                "attempt": attempt,
                "ok": pre_ok,
                "reason": pre_reason,
                "rewrite_trace": rewrite_trace,
            }
        )
        if pre_ok:
            return patch, attempts, "git apply --check passed"
        last_reason = f"apply precheck failed: {pre_reason}"

    fallback_patch, fallback_reason = build_fallback_patch(task, repo_cache_root, candidate_paths)
    if fallback_patch:
        fallback_path = inst_dir / "precheck_fallback.patch"
        pre_ok, pre_reason = precheck_patch_with_git_apply(
            fallback_patch, task, repo_cache_root, fallback_path
        )
        attempts.append(
            {
                "attempt": "fallback",
                "ok": pre_ok,
                "reason": pre_reason,
                "fallback_reason": fallback_reason,
            }
        )
        if pre_ok:
            return fallback_patch, attempts, f"used fallback: {fallback_reason}"
        last_reason = f"fallback apply precheck failed: {pre_reason}"
    else:
        attempts.append({"attempt": "fallback", "ok": False, "reason": fallback_reason})
        last_reason = fallback_reason

    return "", attempts, last_reason


def main() -> int:
    project_root = Path(__file__).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fixed_dir = project_root / "fixed" / f"olmo3_astropy10_{stamp}"
    logs_dir = project_root / "logs" / f"olmo3_astropy10_{stamp}"
    repo_cache_root = fixed_dir / "_precheck_repos"
    repo_cache_root.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[paths] fixed output: {fixed_dir}")
    print(f"[paths] logs output:  {logs_dir}")
    local_dataset_path = project_root / LOCAL_DATASET_PATH
    print(f"[dataset] local: {local_dataset_path} split={DATASET_SPLIT}")

    dataset = load_from_disk(local_dataset_path)
    dataset_by_id = {item["instance_id"]: item for item in dataset}
    dataset_subset_path = fixed_dir / "dataset_subset.json"
    subset = [dict(dataset_by_id[instance_id]) for instance_id in INSTANCE_IDS if instance_id in dataset_by_id]
    dataset_subset_path.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "model": MODEL,
        "dataset_name": str(dataset_subset_path),
        "split": DATASET_SPLIT,
        "local_dataset_path": str(local_dataset_path),
        "fixed_dir": str(fixed_dir),
        "logs_dir": str(logs_dir),
        "instances": [],
        "precheck_all_passed": False,
    }

    for index, instance_id in enumerate(INSTANCE_IDS, start=1):
        print(f"\n[{index}/{len(INSTANCE_IDS)}] generate/precheck/evaluate {instance_id}")
        inst_dir = fixed_dir / instance_id
        inst_dir.mkdir(parents=True, exist_ok=True)
        inst_summary = {
            "instance_id": instance_id,
            "status": "unknown",
            "precheck_passed": False,
            "reason": "",
            "predictions_path": "",
            "evaluation_exit_code": None,
            "evaluation_log": "",
            "evaluation_report": "",
            "run_id": "",
        }

        task = dataset_by_id.get(instance_id)
        if task is None:
            inst_summary["status"] = "missing_dataset_instance"
            inst_summary["reason"] = "instance_id not found in dataset"
            summary["instances"].append(inst_summary)
            write_json(inst_dir / "run_summary.json", inst_summary)
            continue

        candidate_paths = extract_candidate_paths(task["problem_statement"], task["repo"])
        (inst_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "repo": task["repo"],
                    "version": task.get("version", ""),
                    "base_commit": task["base_commit"],
                    "candidate_paths": candidate_paths,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        patch, attempts, reason = get_valid_patch(task, inst_dir, repo_cache_root, candidate_paths)
        write_json(inst_dir / "attempts.json", {"attempts": attempts})
        if not patch:
            inst_summary["status"] = "no_evaluable_patch"
            inst_summary["reason"] = reason
            summary["instances"].append(inst_summary)
            write_json(inst_dir / "run_summary.json", inst_summary)
            write_json(fixed_dir / "batch_summary.json", summary)
            print(f"[skip] {instance_id} no evaluable patch: {reason}")
            continue

        (inst_dir / "patch_from_git.diff").write_text(patch, encoding="utf-8")

        prediction = {
            instance_id: {
                "instance_id": instance_id,
                "model_name_or_path": MODEL,
                "model_patch": patch,
            }
        }
        predictions_path = inst_dir / "predictions.json"
        write_json(predictions_path, prediction)
        inst_summary["precheck_passed"] = True
        inst_summary["reason"] = reason
        inst_summary["predictions_path"] = str(predictions_path)
        inst_summary["status"] = "precheck_passed"
        write_json(inst_dir / "run_summary.json", inst_summary)
        print(f"[ok] {instance_id} precheck passed")

        eval_result = evaluate_instance(
            project_root, fixed_dir, logs_dir, stamp, instance_id, predictions_path
        )
        inst_summary.update(eval_result)
        inst_summary["status"] = "evaluated"
        summary["instances"].append(inst_summary)
        write_json(inst_dir / "run_summary.json", inst_summary)
        write_json(fixed_dir / "batch_summary.json", summary)
        write_json(logs_dir / "evaluation_summary.json", summary)
        print(
            f"[done] {instance_id} eval_exit_code={inst_summary['evaluation_exit_code']}"
        )

    summary["precheck_all_passed"] = all(
        item.get("precheck_passed") for item in summary["instances"]
    )
    summary["all_instances_processed"] = len(summary["instances"]) == len(INSTANCE_IDS)
    write_json(fixed_dir / "batch_summary.json", summary)
    write_json(logs_dir / "evaluation_summary.json", summary)
    print(f"[done] fixed: {fixed_dir}")
    print(f"[done] logs:  {logs_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
