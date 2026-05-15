#!/usr/bin/env python3
"""
在本地 Docker 中评估 Sonnet 4.6 生成的补丁（SWE-bench harness）。
Run local Docker evaluation for Sonnet 4.6 patches.

评估结果保存在: Sonnet4.6/eval_logs/
Results are saved in: Sonnet4.6/eval_logs/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from datasets import load_from_disk

# 配置 / Basic paths.
ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "Sonnet4.6"
PATCHES_DIR = MODEL_DIR / "patches"
PREDICTIONS_FILE = MODEL_DIR / "predictions.json"
PREDICTIONS_JSONL_FILE = MODEL_DIR / "predictions.jsonl"
EVAL_LOGS_DIR = MODEL_DIR / "eval_logs"

LOCAL_DATASET_PATH = ROOT / "dataset_full"
DATASET_SPLIT = "test"
TIMEOUT_SECONDS = 3600  # Note: Timeout for each instance.
GITHUB_ACCESS_TEST_REPO = "https://github.com/astropy/astropy"
DOCKER_PROXY_HOST = "host.docker.internal"
DOCKER_NO_PROXY = "localhost,127.0.0.1,host.docker.internal"

# 目标实例列表 / Target instances.
INSTANCE_IDS = [
    "astropy__astropy-6938",
    "astropy__astropy-7746",
    "django__django-11964",
    "django__django-11999",
    "django__django-12113",
    "django__django-12125",
    "django__django-12184",
    "django__django-12284",
    "django__django-12286",
    "django__django-12308",
]


def safe_filename(name: str) -> str:
    """将字符串转换为安全的文件名。/ Make a safe file name."""
    return re.sub(r"[^A-Za-z0-9_.=-]+", "_", name)


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """运行命令并返回结果。/ Run a command and return the result."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def build_docker_proxy_env(proxy_url: str) -> list[str]:
    """生成 Docker 代理参数。/ Build Docker proxy env args."""
    return [
        "-e",
        f"HTTP_PROXY={proxy_url}",
        "-e",
        f"HTTPS_PROXY={proxy_url}",
        "-e",
        f"http_proxy={proxy_url}",
        "-e",
        f"https_proxy={proxy_url}",
        "-e",
        f"NO_PROXY={DOCKER_NO_PROXY}",
        "-e",
        f"no_proxy={DOCKER_NO_PROXY}",
    ]


def test_docker_github_access(proxy_url: str | None = None) -> bool:
    """测试 Docker 是否能访问 GitHub。/ Test if Docker can reach GitHub."""
    cmd = ["docker", "run", "--rm"]
    if proxy_url:
        cmd += build_docker_proxy_env(proxy_url)
    cmd += [
        "swebench-local:latest",
        "git",
        "ls-remote",
        GITHUB_ACCESS_TEST_REPO,
        "HEAD",
    ]
    try:
        result = run_cmd(cmd, cwd=ROOT, timeout=120)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def configure_docker_proxy_env() -> list[str]:
    """直连失败时让用户输入代理端口。/ Ask for proxy port only if direct access fails."""
    print("检查 Docker 是否可以直连 GitHub / Checking Docker direct GitHub access...")
    if test_docker_github_access():
        print("Docker 可以直连 GitHub，不使用代理 / Docker can reach GitHub directly. No proxy is used.")
        return []

    print("Docker 直连 GitHub 失败，需要代理 / Direct GitHub access failed. Proxy is needed.")
    env_port = os.environ.get("SWE_BENCH_DOCKER_PROXY_PORT", "").strip()
    for attempt in range(1, 4):
        port = env_port if attempt == 1 and env_port else input(
            "请输入代理端口，例如 7897 / Enter proxy port, e.g. 7897: "
        ).strip()
        if not port:
            print("端口不能为空 / Port cannot be empty.")
            continue
        proxy_url = f"http://{DOCKER_PROXY_HOST}:{port}"
        print(f"测试代理 / Testing proxy: {proxy_url}")
        if test_docker_github_access(proxy_url):
            print("Docker 通过代理可以访问 GitHub / Docker can reach GitHub through proxy.")
            return build_docker_proxy_env(proxy_url)
        print("代理测试失败 / Proxy test failed.")
        env_port = ""

    raise RuntimeError("Docker 无法访问 GitHub，请检查代理端口或 Docker Desktop 代理设置。")


def load_predictions() -> dict[str, dict[str, Any]]:
    """加载预测结果。/ Load prediction results."""
    if not PREDICTIONS_FILE.exists():
        print(f"警告 / Warning: prediction file not found: {PREDICTIONS_FILE}")
        return {}

    with open(PREDICTIONS_FILE, "r", encoding="utf-8") as f:
        predictions = json.load(f)

    return {p["instance_id"]: p for p in predictions}


def copy_report_if_present(stdout: str, dest_dir: Path) -> str:
    """从 stdout 中提取报告路径并复制到目标目录。/ Copy report path found in stdout."""
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
    """复制运行日志到目标目录。/ Copy run logs to the output folder."""
    run_root = ROOT / "logs" / "run_evaluation" / run_id
    if not run_root.exists():
        return

    archive_root = dest_dir / "run_evaluation" / run_id
    if archive_root.exists():
        shutil.rmtree(archive_root)

    shutil.copytree(run_root, archive_root)


def find_detailed_report(run_id: str, instance_id: str) -> Path | None:
    """查找详细的评估报告。/ Find the detailed evaluation report."""
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


def merge_report(
    instance_id: str,
    basic_report_path: str,
    detailed_report: Path | None,
    dest_dir: Path,
) -> str:
    """合并基本报告和详细报告。/ Merge basic and detailed reports."""
    merged: dict[str, Any] = {}

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


def run_docker_evaluation(
    instance_id: str,
    prediction: dict[str, Any],
    dataset_subset_path: Path,
    logs_dir: Path,
    stamp: str,
    index: int,
    total: int,
    docker_proxy_env: list[str],
) -> dict[str, Any]:
    """
    运行 Docker 评估。
    Run Docker evaluation.

    返回评估结果摘要。
    Return evaluation summary.
    """
    print(f"\n[{index}/{total}] 评估 / Evaluate: {instance_id}")
    print("-" * 50)

    inst_logs = logs_dir / instance_id
    inst_logs.mkdir(parents=True, exist_ok=True)

    pred_path = MODEL_DIR / f"prediction_{instance_id}.json"
    single_pred = {instance_id: prediction}
    pred_path.write_text(json.dumps(single_pred, ensure_ascii=False, indent=2), encoding="utf-8")

    run_id = f"sonnet46_{stamp}_{instance_id.replace('__', '_').replace('-', '_')}"

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        *docker_proxy_env,
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
        "/app/Sonnet4.6/dataset_subset.json",
        "--split",
        DATASET_SPLIT,
        "--instance_ids",
        instance_id,
        "--predictions_path",
        f"/app/Sonnet4.6/prediction_{instance_id}.json",
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

    print("  运行 Docker 评估 / Run Docker evaluation...")
    print(f"  Run ID: {run_id}")

    try:
        proc = run_cmd(docker_cmd, cwd=ROOT, timeout=TIMEOUT_SECONDS + 600)
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(
            docker_cmd,
            124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + "\n评估超时 / Evaluation timed out",
        )

    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""

    (inst_logs / "run_instance.log").write_text(
        "=== 命令 / COMMAND ===\n"
        + " ".join(docker_cmd)
        + "\n\n=== STDOUT ===\n"
        + stdout_text
        + "\n\n=== STDERR ===\n"
        + stderr_text
        + f"\n\n=== 退出码 / EXIT CODE ===\n{proc.returncode}\n",
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

    try:
        report_data = json.loads(Path(final_report).read_text(encoding="utf-8"))
        if instance_id in report_data:
            inst_summary["resolved"] = report_data[instance_id].get("resolved", False)
        else:
            inst_summary["resolved"] = False
    except Exception:
        inst_summary["resolved"] = False

    (inst_logs / "summary.json").write_text(
        json.dumps(inst_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    status = "通过 / passed" if inst_summary.get("resolved") else "未通过 / failed"
    print(f"  结果 / Result: {status} (退出码 / exit code: {proc.returncode})")

    return inst_summary


def main() -> int:
    """主函数。/ Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="评估 Sonnet 4.6 补丁 / Evaluate Sonnet 4.6 patches")
    parser.add_argument("--instance", type=str, help="仅评估指定实例 / Evaluate one instance")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有成功评估的实例 / Skip passed runs")
    args = parser.parse_args()

    print("=" * 70)
    print("评估 Sonnet 4.6 模型补丁 / Evaluate Sonnet 4.6 patches")
    print("=" * 70)

    EVAL_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    print("\n加载数据集 / Loading dataset...")
    try:
        dataset = load_from_disk(str(LOCAL_DATASET_PATH))
        dataset_by_id = {item["instance_id"]: dict(item) for item in dataset}
    except Exception as e:
        print(f"加载数据集失败 / Dataset load failed: {e}")
        return 1

    print("加载预测 / Loading predictions...")
    predictions = load_predictions()
    if not predictions:
        print("未找到预测文件，请先生成 Sonnet 4.6 补丁 / No predictions found. Create Sonnet 4.6 patches first.")
        return 1

    print(f"找到 / Found {len(predictions)} predictions")

    available_instances = [iid for iid in INSTANCE_IDS if iid in dataset_by_id and iid in predictions]
    if args.instance:
        if args.instance in available_instances:
            available_instances = [args.instance]
        else:
            print(f"实例不可用 / Instance not available: {args.instance}")
            return 1

    subset = [dataset_by_id[iid] for iid in available_instances]
    dataset_subset_path = MODEL_DIR / "dataset_subset.json"
    dataset_subset_path.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据集子集已保存 / Dataset subset saved: {dataset_subset_path}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = EVAL_LOGS_DIR / f"eval_{stamp}"
    logs_dir.mkdir(parents=True, exist_ok=True)
    print(f"本轮评估日志目录 / Log dir for this run: {logs_dir.resolve()}")
    docker_proxy_env = configure_docker_proxy_env()

    if args.skip_existing:
        existing_results = {}
        for eval_dir in EVAL_LOGS_DIR.iterdir():
            if eval_dir.is_dir() and eval_dir.name.startswith("eval_"):
                for instance_dir in eval_dir.iterdir():
                    if instance_dir.is_dir():
                        summary_file = instance_dir / "summary.json"
                        if summary_file.exists():
                            try:
                                data = json.loads(summary_file.read_text(encoding="utf-8"))
                                if data.get("resolved"):
                                    existing_results[data["instance_id"]] = data
                            except Exception:
                                pass

        if existing_results:
            print(f"\n找到 {len(existing_results)} 个已成功的评估，将跳过这些实例 / Skip {len(existing_results)} passed runs")
            available_instances = [iid for iid in available_instances if iid not in existing_results]

    summary = {
        "run_type": "sonnet_4_6_evaluation",
        "timestamp": stamp,
        "predictions_path": str(PREDICTIONS_FILE),
        "dataset_subset_path": str(dataset_subset_path),
        "logs_dir": str(logs_dir),
        "timeout_seconds": TIMEOUT_SECONDS,
        "instances": [],
    }

    total = len(available_instances)
    resolved_count = 0

    for index, instance_id in enumerate(available_instances, start=1):
        prediction = predictions[instance_id]

        if not prediction.get("model_patch", "").strip():
            print(f"\n[{index}/{total}] 跳过 / Skip {instance_id} (无补丁 / no patch)")
            summary["instances"].append({
                "instance_id": instance_id,
                "status": "skipped_no_patch",
            })
            continue

        inst_summary = run_docker_evaluation(
            instance_id,
            prediction,
            dataset_subset_path,
            logs_dir,
            stamp,
            index,
            total,
            docker_proxy_env,
        )

        summary["instances"].append(inst_summary)

        if inst_summary.get("resolved"):
            resolved_count += 1

        (logs_dir / "evaluation_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("\n" + "=" * 70)
    print("评估完成 / Evaluation finished")
    print("=" * 70)
    print(f"总实例数 / Total: {total}")
    print(f"通过 / Passed: {resolved_count}")
    print(f"未通过 / Failed: {total - resolved_count}")
    print(f"通过率 / Pass rate: {resolved_count / total * 100:.1f}%" if total > 0 else "N/A")
    print(f"\n日志目录 / Log dir: {logs_dir}")

    summary_path = logs_dir / "FINAL_SUMMARY.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"摘要文件 / Summary file: {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
