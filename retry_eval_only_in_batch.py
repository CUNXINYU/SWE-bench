#!/usr/bin/env python3
"""
Retry evaluation only for instances with eval_exit_code=1 in an existing batch.
No patch regeneration; reuse existing predictions.json per instance.
"""

import json
import re
import subprocess
import sys
from pathlib import Path


TIMEOUT_SECONDS = 600


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python retry_eval_only_in_batch.py <batch_dir_name>")
        return 2

    project_root = Path(__file__).resolve().parent
    batch_dir = project_root / "olmo3" / sys.argv[1]
    summary_path = batch_dir / "batch_summary.json"
    if not summary_path.exists():
        print(f"batch_summary not found: {summary_path}")
        return 2

    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    for rec in summary["instances"]:
        if rec.get("reason") != "eval_exit_code=1":
            continue

        instance_id = rec["instance_id"]
        inst_dir = batch_dir / instance_id
        predictions_path = inst_dir / "predictions.json"
        if not predictions_path.exists():
            rec["reason"] = "predictions missing for eval retry"
            continue

        run_id = f"olmo3_reval_{instance_id}"
        cmd = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            summary["dataset_name"],
            "--split",
            summary["split"],
            "--instance_ids",
            instance_id,
            "--predictions_path",
            str(predictions_path),
            "--max_workers",
            "1",
            "--timeout",
            str(TIMEOUT_SECONDS),
            "--run_id",
            run_id,
            "--namespace",
            "none",
            "--cache_level",
            "env",
            "--clean",
            "True",
        ]
        proc = subprocess.run(cmd, cwd=project_root, text=True, capture_output=True)

        retry_log = inst_dir / "evaluation_reval.log"
        retry_log.write_text(
            "=== STDOUT ===\n"
            + proc.stdout
            + "\n\n=== STDERR ===\n"
            + proc.stderr
            + f"\n\n=== EXIT CODE ===\n{proc.returncode}\n",
            encoding="utf-8",
        )

        rec["run_id"] = run_id
        rec["eval_log_path"] = str(retry_log).replace(str(project_root), "/app")
        rec["status"] = "evaluated"
        rec["reason"] = f"eval_exit_code={proc.returncode}"

        m = re.search(r"Report written to ([^\r\n]+)", proc.stdout)
        if m:
            report_name = m.group(1).strip()
            report_path = project_root / report_name
            if report_path.exists():
                alias = inst_dir / safe_filename(report_name)
                alias.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
                rec["report_path"] = str(alias).replace(str(project_root), "/app")

        (inst_dir / "run_summary.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"eval-retry done: {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
