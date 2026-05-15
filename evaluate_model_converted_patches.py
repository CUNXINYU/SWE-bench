#!/usr/bin/env python3
"""Evaluate model-converted patches locally with SWE-bench Docker."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from datasets import load_from_disk


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "model_converted_gitdiff"
LOGS_ROOT = ROOT / "logs_model_converted"
LOCAL_DATASET_PATH = ROOT / "dataset_local"
DATASET_SPLIT = "test"
TIMEOUT_SECONDS = 3600
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


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.=-]+", "_", name)


def run_cmd(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=TIMEOUT_SECONDS + 600,
    )


def copy_report_if_present(stdout: str, dest_dir: Path) -> str:
    match = re.search(r"Report written to ([^\r\n]+)", stdout or "")
    if not match:
        return ""
    report_name = match.group(1).strip()
    report_path = ROOT / report_name
    if not report_path.exists():
        return ""
    dest_path = dest_dir / safe_filename(report_name)
    shutil.copy2(report_path, dest_path)
    return str(dest_path)


def copy_run_logs(run_id: str, dest_dir: Path) -> None:
    run_root = ROOT / "logs" / "run_evaluation" / run_id
    if not run_root.exists():
        return
    archive_root = dest_dir / "run_evaluation" / run_id
    if archive_root.exists():
        shutil.rmtree(archive_root)
    shutil.copytree(run_root, archive_root)


def find_detailed_report(run_id: str, instance_id: str) -> Path | None:
    run_root = ROOT / "logs" / "run_evaluation" / run_id
    if not run_root.exists():
        return None
    reports = sorted(run_root.rglob("report.json"), key=lambda p: len(str(p)))
    for report in reports:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except Exception:
            continue
        if instance_id in data:
            return report
    return reports[0] if reports else None


def merge_report(instance_id: str, basic_report_path: str, detailed_report: Path | None, dest_dir: Path) -> str:
    merged: dict = {}
    if basic_report_path:
        try:
            merged.update(json.loads(Path(basic_report_path).read_text(encoding="utf-8")))
        except Exception:
            pass
    if detailed_report and detailed_report.exists():
        try:
            detailed = json.loads(detailed_report.read_text(encoding="utf-8"))
            merged.update(detailed)
        except Exception:
            pass
    if not merged:
        merged = {instance_id: {"resolved": False, "error": "no report found"}}
    final_report = dest_dir / "report.json"
    final_report.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(final_report)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = LOGS_ROOT / f"olmo3_model_converted_{stamp}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    dataset = load_from_disk(str(LOCAL_DATASET_PATH))
    dataset_by_id = {item["instance_id"]: dict(item) for item in dataset}
    subset = [dataset_by_id[instance_id] for instance_id in INSTANCE_IDS]
    dataset_subset_path = OUT_DIR / "dataset_subset_model_converted.json"
    dataset_subset_path.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")

    all_predictions = json.loads((OUT_DIR / "preds_model_converted.json").read_text(encoding="utf-8"))
    pred_by_id = {item["instance_id"]: item for item in all_predictions}

    summary = {
        "run_type": "model_raw_output_converted_gitdiff",
        "predictions_path": str(OUT_DIR / "preds_model_converted.json"),
        "dataset_subset_path": str(dataset_subset_path),
        "logs_dir": str(logs_dir),
        "timeout_seconds": TIMEOUT_SECONDS,
        "instances": [],
    }

    for index, instance_id in enumerate(INSTANCE_IDS, start=1):
        print(f"[{index}/{len(INSTANCE_IDS)}] evaluate {instance_id}", flush=True)
        inst_logs = logs_dir / instance_id
        inst_logs.mkdir(parents=True, exist_ok=True)
        pred_path = OUT_DIR / f"prediction_{instance_id}.json"
        pred_path.write_text(
            json.dumps({instance_id: pred_by_id[instance_id]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        run_id = f"olmo3_model_converted_{stamp}_{instance_id.replace('__', '_').replace('-', '_')}"
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ROOT.as_posix()}:/app",
            "-w",
            "/app",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "swebench-local:latest",
            "python",
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            "/app/model_converted_gitdiff/dataset_subset_model_converted.json",
            "--split",
            DATASET_SPLIT,
            "--instance_ids",
            instance_id,
            "--predictions_path",
            f"/app/model_converted_gitdiff/prediction_{instance_id}.json",
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

        try:
            proc = run_cmd(docker_cmd, ROOT)
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(
                docker_cmd,
                124,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + "\nOuter evaluation timeout expired.",
            )

        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        (inst_logs / "run_instance.log").write_text(
            "=== COMMAND ===\n"
            + " ".join(docker_cmd)
            + "\n\n=== STDOUT ===\n"
            + stdout_text
            + "\n\n=== STDERR ===\n"
            + stderr_text
            + f"\n\n=== EXIT CODE ===\n{proc.returncode}\n",
            encoding="utf-8",
        )
        basic_report = copy_report_if_present(stdout_text, inst_logs)
        detailed_report = find_detailed_report(run_id, instance_id)
        copy_run_logs(run_id, inst_logs)
        final_report = merge_report(instance_id, basic_report, detailed_report, inst_logs)
        inst_summary = {
            "instance_id": instance_id,
            "run_id": run_id,
            "exit_code": proc.returncode,
            "prediction_path": str(pred_path),
            "report_json": final_report,
            "run_instance_log": str(inst_logs / "run_instance.log"),
        }
        summary["instances"].append(inst_summary)
        (inst_logs / "summary.json").write_text(
            json.dumps(inst_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (logs_dir / "evaluation_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[done] {instance_id} exit_code={proc.returncode}", flush=True)

    print(f"[all done] logs={logs_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
