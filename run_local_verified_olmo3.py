#!/usr/bin/env python3
"""
本地端到端流程：
1) 从 SWE-bench_Verified 读取指定 instance
2) 调用本地 Ollama 生成 diff 补丁
3) 写 predictions.json
4) 调用 SWE-bench Docker harness 本地评估
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Tuple
from urllib import request

from datasets import load_dataset


INSTANCE_ID = "astropy__astropy-14309"
DATASET_NAME = "princeton-nlp/SWE-bench_Verified"
DATASET_SPLIT = "test"
MODEL = "olmo-3:latest"
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", "http://host.docker.internal:11434/api/generate"
)
TIMEOUT_SECONDS = 600
MAX_PATCH_GEN_ATTEMPTS = 1
FALLBACK_MODELS = []
REQUIRE_VALID_DIFF_BEFORE_EVAL = True
DIFF_REWRITE_ATTEMPTS = 1


def extract_diff(text: str) -> str:
    cleaned = text.replace("```diff", "```").strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("diff --git"):
                return part
    idx = cleaned.find("diff --git")
    if idx >= 0:
        return cleaned[idx:].strip()
    return cleaned


def validate_diff_text(diff_text: str) -> Tuple[bool, str]:
    """
    只做“格式层面”的最小校验，确保能进入评估阶段。
    不做语义正确性判断。
    """
    text = diff_text.strip()
    if not text:
        return False, "empty patch"
    if not text.startswith("diff --git "):
        return False, 'missing "diff --git" header'
    if "\n--- " not in text or "\n+++ " not in text:
        return False, 'missing "---" / "+++" file headers'
    if "\n@@ " not in text and "\n@@\n" not in text:
        return False, 'missing "@@" hunk header'

    # 若出现多个文件变更，后续 diff header 也应合法 / If several files change, later diff headers must also be valid.
    if "diff --git " not in text:
        return False, "invalid diff structure"
    return True, "ok"


def normalize_patch_newlines(patch_text: str) -> str:
    """
    强制转为 Linux 换行，避免 CRLF 导致 patch 解析失败。
    Force Linux newlines to avoid patch parse errors from CRLF.
    """
    normalized = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized and not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def normalize_repo_relative_path(path_like: str, repo: str) -> str:
    """
    将各种来源的路径（URL、绝对路径、site-packages路径）归一为 repo 相对路径。
    Normalize URLs or absolute paths to repo-relative paths.
    """
    if not path_like:
        return ""
    p = path_like.strip().strip("`'\"")
    p = p.replace("\\", "/")
    p = p.replace("(", "/").replace(")", "")
    p = p.replace("..", "")
    p = re.sub(r"/{2,}", "/", p)
    p = p.lstrip("./")

    owner_repo = repo.strip().lower()
    repo_name = owner_repo.split("/")[-1] if "/" in owner_repo else owner_repo

    # GitHub blob/raw URL -> repo relative path
    marker = f"github.com/{owner_repo}/blob/"
    idx = p.lower().find(marker)
    if idx >= 0:
        tail = p[idx + len(marker) :]
        if "/" in tail:
            p = tail.split("/", 1)[1]  # strip branch

    # site-packages absolute path -> package relative path
    if "/site-packages/" in p:
        p = p.split("/site-packages/", 1)[1]

    # /<repo_name>/... -> ...
    tag = f"{repo_name}/"
    idx2 = p.lower().find(tag)
    if idx2 >= 0 and idx2 + len(tag) < len(p):
        p = p[idx2 + len(tag) :]

    # 保留 python 包相关路径 / Keep Python package paths.
    if repo_name == "astropy":
        if "astropy/" in p and not p.startswith("astropy/"):
            p = p[p.find("astropy/") :]

    p = p.lstrip("/")
    return p


def extract_candidate_paths(text: str, repo: str) -> list[str]:
    """
    从问题描述里提取看起来像 repo 内文件路径的候选项。
    """
    pattern = r"([A-Za-z0-9_./-]+\.(?:py|pyi|txt|md|yaml|yml|toml|json|rst|cfg|ini|sh))"
    found = re.findall(pattern, text or "")
    deduped = []
    for p in found:
        p2 = normalize_repo_relative_path(p, repo)
        if "/" in p2 and p2 not in deduped:
            deduped.append(p2)
    return deduped[:20]


def get_diff_paths(diff_text: str) -> list[str]:
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            part = line[len("diff --git a/") :]
            a_path = part.split(" b/")[0].strip()
            if a_path and a_path not in paths:
                paths.append(a_path)
    return paths


def validate_diff_paths(
    diff_text: str, candidate_paths: list[str], repo: str
) -> Tuple[bool, str]:
    """
    若已知候选路径，则要求 diff 命中的路径在候选集合中，降低幻觉路径概率。
    """
    if not candidate_paths:
        return True, "no path constraints"
    diff_paths = get_diff_paths(diff_text)
    if not diff_paths:
        return False, "no diff paths found"
    allowed = set(candidate_paths)
    for p in diff_paths:
        normalized = normalize_repo_relative_path(p, repo)
        if normalized not in allowed:
            return False, f'path "{p}" not in candidate paths'
    return True, "ok"


def build_patch_prompt(
    item: dict, candidate_paths: list[str], strict_reason: str = ""
) -> str:
    extra = ""
    if strict_reason:
        extra = f"\n上次输出不合格原因：{strict_reason}\n请只修正输出格式，不要解释。"
    path_constraint = ""
    if candidate_paths:
        lines = "\n".join(f"- {p}" for p in candidate_paths)
        path_constraint = (
            "\n路径约束（强制）：\n"
            "请只修改下列候选路径之一，不允许编造新文件名：\n"
            f"{lines}\n"
        )
    return f"""你是一位代码修复专家。请为以下问题生成修复补丁。

问题描述：
{item["problem_statement"]}

仓库：{item["repo"]}
版本：{item.get("version", "")}

请生成严格符合 Linux diff 格式的补丁，要求（必须全部满足）：
1) 必须以 diff --git 开头
2) 包含 --- a/文件路径 和 +++ b/文件路径
3) 包含 @@ 行号标记
4) 仅使用 + 和 - 标记修改行
5) 不添加任何解释文字、注释、标题、Markdown代码块标记
6) 使用 UTF-8 编码、Linux 换行符（\\n）
7) 补丁必须完整，不要中途截断
8) 输出最后一行必须是补丁内容，不要追加任何说明
9) 不允许使用占位路径或虚构文件（如 fit_fits.py、foo.py 等）
{path_constraint}
{extra}
"""


def build_rewrite_prompt(
    item: dict, candidate_paths: list[str], model_output: str, reason: str = ""
) -> str:
    reason_text = f"\n上次不合法原因：{reason}" if reason else ""
    path_constraint = ""
    if candidate_paths:
        lines = "\n".join(f"- {p}" for p in candidate_paths)
        path_constraint = (
            "\n路径约束（强制）：\n"
            "输出 diff 的文件路径必须从下面列表中选择：\n"
            f"{lines}\n"
        )
    return f"""你现在只做“补丁格式重写器”，不要解释，不要分析。
请将下面这段“模型输出内容”重写为可直接 `git apply` 的 unified diff。

任务背景：
- repo: {item["repo"]}
- version: {item.get("version", "")}
- instance_id: {item["instance_id"]}
{reason_text}

强制输出要求（必须全部满足）：
1) 必须以 diff --git 开头
2) 包含 --- a/文件路径 和 +++ b/文件路径
3) 包含 @@ 行号标记
4) 仅使用 + 和 - 标记修改行
5) 不添加任何解释文字、注释、标题、Markdown代码块标记
6) 使用 UTF-8 编码、Linux 换行符（\\n）
7) 补丁必须完整，不要中途截断
8) 不允许编造路径，必须使用真实 repo 相对路径
{path_constraint}

待重写内容如下：
-----BEGIN MODEL OUTPUT-----
{model_output}
-----END MODEL OUTPUT-----
"""


def call_ollama(prompt: str, model: str) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        OLLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        return parsed.get("response", "")


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")


def main() -> int:
    project_root = Path(__file__).resolve().parent
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = project_root / "local_runs" / f"olmo3_verified_{INSTANCE_ID}_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] 加载数据集: {DATASET_NAME} ({DATASET_SPLIT})")
    dataset = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    item = next((x for x in dataset if x["instance_id"] == INSTANCE_ID), None)
    if item is None:
        raise ValueError(f"未找到 instance_id: {INSTANCE_ID}")

    task_info = {
        "instance_id": item["instance_id"],
        "repo": item["repo"],
        "version": item.get("version", ""),
        "problem_statement": item["problem_statement"],
        "dataset_name": DATASET_NAME,
        "split": DATASET_SPLIT,
    }
    (run_dir / "task.json").write_text(
        json.dumps(task_info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    candidate_paths = extract_candidate_paths(item["problem_statement"], item["repo"])
    prompt = build_patch_prompt(item, candidate_paths)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    model_candidates = [MODEL]
    print(f"[2/5] 调用 Ollama，模型: {MODEL}")
    all_attempt_outputs = []
    model_patch = ""
    last_reason = "unknown"
    selected_model = MODEL

    for model_name in model_candidates:
        selected_model = model_name
        for attempt in range(1, MAX_PATCH_GEN_ATTEMPTS + 1):
            if attempt == 1:
                attempt_prompt = prompt
            else:
                attempt_prompt = build_patch_prompt(
                    item, candidate_paths, strict_reason=last_reason
                )
            try:
                raw_response = call_ollama(attempt_prompt, model_name)
            except Exception as exc:
                last_reason = f"ollama request failed: {exc}"
                all_attempt_outputs.append(
                    {
                        "model": model_name,
                        "attempt": attempt,
                        "is_valid_diff": False,
                        "reason": last_reason,
                        "raw_response": "",
                        "extracted_patch_preview": "",
                        "rewrite_trace": [],
                        "final_patch_preview": "",
                    }
                )
                continue
            extracted = extract_diff(raw_response)
            ok, reason = validate_diff_text(extracted)

            rewrite_trace = []
            rewritten_patch = extracted
            rewrite_ok = ok
            rewrite_reason = reason
            if not ok:
                current_text = raw_response
                for rewrite_idx in range(1, DIFF_REWRITE_ATTEMPTS + 1):
                    rewrite_prompt = build_rewrite_prompt(
                        item, candidate_paths, current_text, reason=rewrite_reason
                    )
                    try:
                        rewrite_raw = call_ollama(rewrite_prompt, model_name)
                    except Exception as exc:
                        rewrite_ok = False
                        rewrite_reason = f"ollama rewrite failed: {exc}"
                        rewrite_trace.append(
                            {
                                "rewrite_attempt": rewrite_idx,
                                "is_valid_diff": False,
                                "reason": rewrite_reason,
                                "rewrite_response_preview": "",
                            }
                        )
                        break
                    rewritten_patch = extract_diff(rewrite_raw)
                    rewrite_ok, rewrite_reason = validate_diff_text(rewritten_patch)
                    if rewrite_ok:
                        rewrite_ok, rewrite_reason = validate_diff_paths(
                            rewritten_patch, candidate_paths, item["repo"]
                        )
                    rewrite_trace.append(
                        {
                            "rewrite_attempt": rewrite_idx,
                            "is_valid_diff": rewrite_ok,
                            "reason": rewrite_reason,
                            "rewrite_response_preview": rewrite_raw[:1200],
                        }
                    )
                    if rewrite_ok:
                        break
                    current_text = rewrite_raw

            if ok:
                ok, reason = validate_diff_paths(
                    extracted, candidate_paths, item["repo"]
                )

            final_patch = rewritten_patch if rewrite_trace else extracted
            final_ok = rewrite_ok if rewrite_trace else ok
            final_reason = rewrite_reason if rewrite_trace else reason

            all_attempt_outputs.append(
                {
                    "model": model_name,
                    "attempt": attempt,
                    "is_valid_diff": final_ok,
                    "reason": final_reason,
                    "raw_response": raw_response,
                    "extracted_patch_preview": extracted[:1200],
                    "rewrite_trace": rewrite_trace,
                    "final_patch_preview": final_patch[:1200],
                }
            )
            if final_ok:
                model_patch = final_patch
                last_reason = "ok"
                break
            last_reason = final_reason
        if model_patch:
            break

    if not model_patch:
        # 所有尝试都失败时，保留最后一次提取结果给后续日志排查 / Keep last output for later debugging.
        model_patch = extract_diff(all_attempt_outputs[-1]["raw_response"])
    model_patch = normalize_patch_newlines(model_patch)

    (run_dir / "ollama_attempts.json").write_text(
        json.dumps(all_attempt_outputs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "ollama_response_raw.txt").write_text(
        all_attempt_outputs[-1]["raw_response"], encoding="utf-8"
    )

    predictions = {
        INSTANCE_ID: {
            "instance_id": INSTANCE_ID,
            "model_name_or_path": selected_model,
            "model_patch": model_patch,
        }
    }
    predictions_path = run_dir / "predictions.json"
    predictions_path.write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok_final, reason_final = validate_diff_text(model_patch)
    if not ok_final:
        print(f"警告：多轮后仍非合法 diff（{reason_final}），评估可能在 patch apply 阶段失败。")
        if REQUIRE_VALID_DIFF_BEFORE_EVAL:
            summary = {
                "run_dir": str(run_dir),
                "predictions_path": str(predictions_path),
                "evaluation_log_path": None,
                "evaluation_command": None,
                "evaluation_exit_code": None,
                "patch_format_valid_before_eval": ok_final,
                "patch_format_validation_reason": reason_final,
                "patch_generation_attempts_per_model": MAX_PATCH_GEN_ATTEMPTS,
                "model_candidates": model_candidates,
                "selected_model": selected_model,
                "candidate_paths": candidate_paths,
                "evaluation_report_original_path": None,
                "evaluation_report_safe_alias_path": None,
            }
            (run_dir / "run_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("已按策略停止：补丁格式不合法，未进入评估阶段。")
            return 2

    patch_preview = model_patch[:1200]
    (run_dir / "model_patch_preview.diff").write_text(patch_preview, encoding="utf-8")
    print(f"[3/5] 已写 predictions: {predictions_path}")

    print("[4/5] 开始本地 Docker 评估 (SWE-bench harness)")
    eval_cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        DATASET_NAME,
        "--split",
        DATASET_SPLIT,
        "--instance_ids",
        INSTANCE_ID,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        "1",
        "--timeout",
        str(TIMEOUT_SECONDS),
        "--run_id",
        f"olmo3_{INSTANCE_ID}_{run_stamp}",
        "--namespace",
        "none",
        "--cache_level",
        "env",
        "--clean",
        "True",
    ]

    eval_log = run_dir / "evaluation.log"
    proc = subprocess.run(
        eval_cmd,
        cwd=project_root,
        text=True,
        capture_output=True,
    )
    eval_log.write_text(
        "=== STDOUT ===\n"
        + proc.stdout
        + "\n\n=== STDERR ===\n"
        + proc.stderr
        + f"\n\n=== EXIT CODE ===\n{proc.returncode}\n",
        encoding="utf-8",
    )

    report_original_path = None
    report_safe_alias_path = None
    match = re.search(r"Report written to ([^\r\n]+)", proc.stdout)
    if match:
        report_name = match.group(1).strip()
        maybe_report = project_root / report_name
        if maybe_report.exists():
            report_original_path = str(maybe_report)
            safe_name = safe_filename(report_name) or f"report_{run_stamp}.json"
            report_safe_alias = run_dir / safe_name
            report_safe_alias.write_text(
                maybe_report.read_text(encoding="utf-8"), encoding="utf-8"
            )
            report_safe_alias_path = str(report_safe_alias)

    summary = {
        "run_dir": str(run_dir),
        "predictions_path": str(predictions_path),
        "evaluation_log_path": str(eval_log),
        "evaluation_command": eval_cmd,
        "evaluation_exit_code": proc.returncode,
        "patch_format_valid_before_eval": ok_final,
        "patch_format_validation_reason": reason_final,
        "patch_generation_attempts_per_model": MAX_PATCH_GEN_ATTEMPTS,
        "model_candidates": model_candidates,
        "selected_model": selected_model,
        "candidate_paths": candidate_paths,
        "evaluation_report_original_path": report_original_path,
        "evaluation_report_safe_alias_path": report_safe_alias_path,
    }
    (run_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[5/5] 流程结束，运行目录: {run_dir}")
    print(f"评估退出码: {proc.returncode}")
    print(f"评估日志: {eval_log}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
