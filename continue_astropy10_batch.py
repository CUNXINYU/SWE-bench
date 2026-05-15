#!/usr/bin/env python3
"""Continue the astropy10 batch from instance 7 (14182) to complete all 10."""

import json
import sys
from pathlib import Path

# Import from existing script
from run_olmo3_astropy10_fixed_logs import (
    INSTANCE_IDS,
    call_ollama,
    get_valid_patch,
    evaluate_instance,
    load_from_disk,
    LOCAL_DATASET_PATH,
    DATASET_SPLIT,
    MODEL,
    write_json,
    extract_candidate_paths,
)


def main() -> int:
    # Use existing batch directory
    batch_dir = Path("c:/Users/BBKt/Projects/SWE-bench/fixed/olmo3_astropy10_20260508_004624")
    logs_dir = Path("c:/Users/BBKt/Projects/SWE-bench/logs/olmo3_astropy10_20260508_004624")
    repo_cache_root = batch_dir / "_precheck_repos"
    
    if not batch_dir.exists():
        print(f"[error] Batch directory not found: {batch_dir}")
        return 1
    
    print(f"[continue] batch dir: {batch_dir}")
    print(f"[continue] logs dir: {logs_dir}")
    
    # Load dataset
    local_dataset_path = Path("c:/Users/BBKt/Projects/SWE-bench") / LOCAL_DATASET_PATH
    dataset = load_from_disk(local_dataset_path)
    dataset_by_id = {item["instance_id"]: item for item in dataset}
    
    # Load existing summary
    summary_path = batch_dir / "batch_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    
    # Find completed instances
    completed_ids = {inst["instance_id"] for inst in summary["instances"]}
    print(f"[continue] completed: {len(completed_ids)}/10")
    
    # Continue from instance 7 (index 6)
    for idx, instance_id in enumerate(INSTANCE_IDS, start=1):
        if instance_id in completed_ids:
            print(f"\n[{idx}/10] {instance_id} already completed, skipping")
            continue
        
        print(f"\n[{idx}/10] generate/precheck/evaluate {instance_id}")
        inst_dir = batch_dir / instance_id
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
        
        # Save metadata
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
        
        # Get valid patch
        from run_olmo3_astropy10_fixed_logs import build_patch_prompt
        prompt = build_patch_prompt(task, candidate_paths)
        (inst_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        
        patch, attempts, reason = get_valid_patch(task, inst_dir, repo_cache_root, candidate_paths)
        write_json(inst_dir / "attempts.json", {"attempts": attempts})
        
        if not patch:
            inst_summary["status"] = "no_evaluable_patch"
            inst_summary["reason"] = reason
            summary["instances"].append(inst_summary)
            write_json(inst_dir / "run_summary.json", inst_summary)
            write_json(summary_path, summary)
            print(f"[skip] {instance_id} no evaluable patch: {reason}")
            continue
        
        (inst_dir / "patch_from_git.diff").write_text(patch, encoding="utf-8")
        
        # Save prediction
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
        
        # Evaluate
        stamp = "20260508_004624"
        from pathlib import Path as PathLib
        project_root = PathLib("c:/Users/BBKt/Projects/SWE-bench")
        eval_result = evaluate_instance(
            project_root, batch_dir, logs_dir, stamp, instance_id, predictions_path
        )
        inst_summary.update(eval_result)
        inst_summary["status"] = "evaluated"
        summary["instances"].append(inst_summary)
        write_json(inst_dir / "run_summary.json", inst_summary)
        write_json(summary_path, summary)
        print(f"[done] {instance_id} eval_exit_code={inst_summary['evaluation_exit_code']}")
    
    # Final summary
    summary["precheck_all_passed"] = all(
        item.get("precheck_passed") for item in summary["instances"]
    )
    summary["all_instances_processed"] = len(summary["instances"]) == len(INSTANCE_IDS)
    write_json(summary_path, summary)
    write_json(logs_dir / "evaluation_summary.json", summary)
    print(f"\n[done] all 10 instances processed")
    print(f"[done] fixed: {batch_dir}")
    print(f"[done] logs: {logs_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
