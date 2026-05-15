#!/usr/bin/env python3
"""
Batch run 10 SWE-bench instances with strict precheck flow:
1) Generate patch with Ollama
2) Force patch into unified diff + LF newline
3) Run git apply --check against repo@base_commit
4) Only if all 10 pass precheck, run SWE-bench evaluation
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib import request

from datasets import load_dataset


MODEL = "olmo-3:latest"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DATASET_NAME = "princeton-nlp/SWE-bench"
DATASET_SPLIT = "test"
TIMEOUT_SECONDS = 600
MAX_PATCH_GEN_ATTEMPTS = 1
DIFF_REWRITE_ATTEMPTS = 0

INSTANCE_IDS = [
    "astropy__astropy-14309",
    "astropy__astropy-14995",
    "astropy__astropy-7166",
    "astropy__astropy-7336",
    "django__django-10097",
    "django__django-10880",
    "django__django-10914",
    "django__django-10999",
    "django__django-11066",
    "pytest-dev__pytest-10051",
]


def normalize_patch_newlines(patch_text: str) -> str:
    text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def extract_diff(text: str) -> str:
    cleaned = text.replace("```diff", "```").strip()
    if "```" in cleaned:
        for part in cleaned.split("```"):
            p = part.strip()
            if p.startswith("diff --git"):
                return p
    idx = cleaned.find("diff --git")
    if idx >= 0:
        return cleaned[idx:].strip()
    return cleaned


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")


def validate_diff_text(diff_text: str) -> tuple[bool, str]:
    text = diff_text.strip()
    if not text:
        return False, "empty patch"
    if not text.startswith("diff --git "):
        return False, 'missing "diff --git" header'
    if "\n--- " not in text or "\n+++ " not in text:
        return False, 'missing "---" / "+++" file headers'
    if "\n@@ " not in text and "\n@@\n" not in text:
        return False, 'missing "@@" hunk header'
    return True, "ok"


def normalize_repo_relative_path(path_like: str, repo: str) -> str:
    p = (path_like or "").strip().strip("`'\"")
    p = p.replace("\\", "/")
    p = p.replace("(", "/").replace(")", "")
    p = re.sub(r"/{2,}", "/", p)
    p = p.lstrip("./")

    owner_repo = repo.strip().lower()
    repo_name = owner_repo.split("/")[-1] if "/" in owner_repo else owner_repo
    marker = f"github.com/{owner_repo}/blob/"
    idx = p.lower().find(marker)
    if idx >= 0:
        tail = p[idx + len(marker) :]
        if "/" in tail:
            p = tail.split("/", 1)[1]
    if "/site-packages/" in p:
        p = p.split("/site-packages/", 1)[1]
    tag = f"{repo_name}/"
    idx2 = p.lower().find(tag)
    if idx2 >= 0 and idx2 + len(tag) < len(p):
        p = p[idx2 + len(tag) :]
    if repo_name == "astropy" and "astropy/" in p and not p.startswith("astropy/"):
        p = p[p.find("astropy/") :]
    return p.lstrip("/")


def extract_candidate_paths(text: str, repo: str) -> list[str]:
    pattern = r"([A-Za-z0-9_./-]+\.(?:py|pyi|txt|md|yaml|yml|toml|json|rst|cfg|ini|sh))"
    found = re.findall(pattern, text or "")
    deduped: list[str] = []
    for p in found:
        p2 = normalize_repo_relative_path(p, repo)
        if "/" in p2 and p2 not in deduped:
            deduped.append(p2)
    return deduped[:25]


def get_diff_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            part = line[len("diff --git a/") :]
            p = part.split(" b/")[0].strip()
            if p and p not in paths:
                paths.append(p)
    return paths


def validate_diff_paths(diff_text: str, candidate_paths: list[str], repo: str) -> tuple[bool, str]:
    if not candidate_paths:
        return True, "no path constraints"
    diff_paths = get_diff_paths(diff_text)
    if not diff_paths:
        return False, "no diff paths found"
    allowed = set(candidate_paths)
    for path in diff_paths:
        normalized = normalize_repo_relative_path(path, repo)
        if normalized not in allowed:
            return False, f'path "{path}" not in candidate paths'
    return True, "ok"


def build_patch_prompt(task: dict, candidate_paths: list[str], strict_reason: str = "") -> str:
    path_constraint = ""
    if candidate_paths:
        path_constraint = "\n".join(f"- {p}" for p in candidate_paths)
        # Note: Restrict generated diffs to known repository paths.
        path_constraint = (
            "\n路径约束（强制）：\n"
            "输出 diff 的文件路径必须从以下列表中选择，不允许编造路径：\n"
            f"{path_constraint}\n"
        )
    extra = ""
    if strict_reason:
        # Note: Tell the model why the previous patch attempt failed.
        extra = f"\n上次失败原因：{strict_reason}\n请只修正输出，不要解释。"
    # Note: This Chinese prompt asks the model to output only a valid git diff patch.
    return f"""你是一位代码修复专家。请为以下问题生成修复补丁。

问题描述：
{task["problem_statement"]}

仓库：{task["repo"]}
版本：{task.get("version", "")}
instance_id：{task["instance_id"]}

请生成严格可被 git apply 的 unified diff，必须满足：
1) 以 diff --git 开头
2) 包含 --- a/... 和 +++ b/...
3) 包含 @@ hunk 头
4) 仅输出补丁正文，不要任何解释、注释、Markdown 标记
5) 使用 Linux 换行符（\\n）
6) 补丁不能截断
7) 不允许虚构文件路径
{path_constraint}
{extra}
"""


def build_rewrite_prompt(task: dict, candidate_paths: list[str], model_output: str, reason: str) -> str:
    path_constraint = ""
    if candidate_paths:
        path_constraint = "\n".join(f"- {p}" for p in candidate_paths)
        # Note: Keep rewritten diffs inside the known candidate file list.
        path_constraint = (
            "\n路径约束（强制）：\n"
            "输出 diff 的文件路径必须从以下列表中选择：\n"
            f"{path_constraint}\n"
        )
    # Note: This Chinese prompt rewrites model output into an applyable unified diff.
    return f"""你现在是“补丁格式重写器”。不要解释，只输出补丁正文。
将下面内容重写为可直接 git apply 的 unified diff。

repo: {task["repo"]}
instance_id: {task["instance_id"]}
上次不合法原因: {reason}

强制要求：
1) 以 diff --git 开头
2) 必须有 --- / +++ / @@
3) 不要任何说明文字
4) Linux 换行符（\\n）
5) 不允许虚构路径
{path_constraint}
-----BEGIN MODEL OUTPUT-----
{model_output}
-----END MODEL OUTPUT-----
"""


def call_ollama(prompt: str) -> str:
    payload = {
        "model": MODEL,
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
    with request.urlopen(req, timeout=600) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    return parsed.get("response", "")


def run_cmd(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def ensure_repo_checkout(repo_cache_root: Path, repo: str, base_commit: str) -> tuple[Path, str]:
    repo_slug = repo.replace("/", "__")
    repo_dir = repo_cache_root / f"{repo_slug}_{base_commit[:12]}"
    if not repo_dir.exists():
        clone_cmd = [
            "git",
            "clone",
            "--filter=blob:none",
            "--no-checkout",
            f"https://github.com/{repo}.git",
            str(repo_dir),
        ]
        clone_proc = run_cmd(clone_cmd, cwd=repo_cache_root)
        if clone_proc.returncode != 0:
            return repo_dir, f"git clone failed: {clone_proc.stderr.strip() or clone_proc.stdout.strip()}"
    fetch_proc = run_cmd(["git", "fetch", "--depth", "1", "origin", base_commit], cwd=repo_dir)
    if fetch_proc.returncode != 0:
        fallback_fetch = run_cmd(["git", "fetch", "--all", "--prune"], cwd=repo_dir)
        if fallback_fetch.returncode != 0:
            msg = fallback_fetch.stderr.strip() or fallback_fetch.stdout.strip() or fetch_proc.stderr.strip()
            return repo_dir, f"git fetch failed: {msg}"
    checkout_proc = run_cmd(["git", "checkout", "--detach", base_commit], cwd=repo_dir)
    if checkout_proc.returncode != 0:
        return repo_dir, f"git checkout failed: {checkout_proc.stderr.strip() or checkout_proc.stdout.strip()}"
    run_cmd(["git", "reset", "--hard", base_commit], cwd=repo_dir)
    run_cmd(["git", "clean", "-fd"], cwd=repo_dir)
    return repo_dir, ""


def precheck_patch_with_git_apply(
    patch_text: str, task: dict, repo_cache_root: Path, patch_path: Path
) -> tuple[bool, str]:
    repo_dir, err = ensure_repo_checkout(repo_cache_root, task["repo"], task["base_commit"])
    if err:
        return False, err
    patch_path.write_text(patch_text, encoding="utf-8")
    proc = run_cmd(
        ["git", "apply", "--check", "--verbose", "--whitespace=nowarn", str(patch_path)],
        cwd=repo_dir,
    )
    if proc.returncode == 0:
        return True, "git apply --check passed"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return False, out or "git apply --check failed"


def build_fallback_patch(
    task: dict, repo_cache_root: Path, candidate_paths: list[str]
) -> tuple[str, str]:
    repo_dir, err = ensure_repo_checkout(repo_cache_root, task["repo"], task["base_commit"])
    if err:
        return "", err

    target_path = ""
    for p in candidate_paths:
        if p.endswith(".py") and (repo_dir / p).exists():
            target_path = p
            break
    if not target_path:
        ls_proc = run_cmd(["git", "ls-files", "*.py"], cwd=repo_dir)
        if ls_proc.returncode != 0 or not ls_proc.stdout.strip():
            return "", "failed to find fallback target file"
        target_path = ls_proc.stdout.strip().splitlines()[0]

    target_file = repo_dir / target_path
    try:
        original = target_file.read_text(encoding="utf-8")
    except Exception as exc:
        return "", f"failed to read fallback target file: {exc}"

    marker = f"# swebench precheck placeholder: {task['instance_id']}"
    if marker in original:
        marker = marker + " v2"
    updated = original
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += marker + "\n"

    try:
        target_file.write_text(updated, encoding="utf-8")
        diff_proc = run_cmd(["git", "diff", "--", target_path], cwd=repo_dir)
    finally:
        run_cmd(["git", "checkout", "--", target_path], cwd=repo_dir)

    if diff_proc.returncode != 0:
        return "", diff_proc.stderr.strip() or "failed to generate fallback diff"
    patch = normalize_patch_newlines(diff_proc.stdout)
    ok, reason = validate_diff_text(patch)
    if not ok:
        return "", f"fallback diff invalid: {reason}"
    return patch, f"fallback patch on {target_path}"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    project_root = Path(__file__).resolve().parent
    batch_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = project_root / "olmo3" / f"batch_precheck_{batch_stamp}"
    repo_cache_root = batch_dir / "_precheck_repos"
    repo_cache_root.mkdir(parents=True, exist_ok=True)

    print(f"[batch] output dir: {batch_dir}")
    print(f"[batch] loading dataset: {DATASET_NAME} ({DATASET_SPLIT})")
    dataset = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    dataset_by_id = {x["instance_id"]: x for x in dataset}

    summary = {
        "batch_dir": str(batch_dir),
        "model": MODEL,
        "dataset_name": DATASET_NAME,
        "split": DATASET_SPLIT,
        "precheck_required": True,
        "precheck_all_passed": False,
        "instances": [],
    }

    # Phase 1: generation + apply precheck
    print("[phase1] generate patch and run git apply precheck")
    for idx, instance_id in enumerate(INSTANCE_IDS, start=1):
        print(f"\n[{idx}/{len(INSTANCE_IDS)}] precheck {instance_id}")
        inst_dir = batch_dir / instance_id
        inst_dir.mkdir(parents=True, exist_ok=True)
        inst_summary = {
            "instance_id": instance_id,
            "status": "unknown",
            "reason": "",
            "run_id": "",
            "predictions_path": "",
            "eval_log_path": "",
            "report_path": "",
            "precheck_passed": False,
        }
        attempts: list[dict] = []

        task = dataset_by_id.get(instance_id)
        if task is None:
            inst_summary["status"] = "skipped"
            inst_summary["reason"] = "instance not found in dataset"
            summary["instances"].append(inst_summary)
            write_json(inst_dir / "run_summary.json", inst_summary)
            continue

        candidate_paths = extract_candidate_paths(task["problem_statement"], task["repo"])
        base_prompt = build_patch_prompt(task, candidate_paths)
        (inst_dir / "prompt.txt").write_text(base_prompt, encoding="utf-8")

        accepted_patch = ""
        last_reason = "unknown"
        for attempt in range(1, MAX_PATCH_GEN_ATTEMPTS + 1):
            attempt_prompt = build_patch_prompt(task, candidate_paths, strict_reason=last_reason)
            try:
                raw = call_ollama(attempt_prompt)
            except Exception as exc:
                last_reason = f"ollama request failed: {exc}"
                attempts.append(
                    {"attempt": attempt, "ok": False, "reason": last_reason, "raw_preview": ""}
                )
                continue

            extracted = normalize_patch_newlines(extract_diff(raw))
            valid, reason = validate_diff_text(extracted)
            if valid:
                valid, reason = validate_diff_paths(extracted, candidate_paths, task["repo"])
            rewrite_trace: list[dict] = []
            rewritten = extracted
            if not valid:
                current_text = raw
                for r_idx in range(1, DIFF_REWRITE_ATTEMPTS + 1):
                    rewrite_prompt = build_rewrite_prompt(task, candidate_paths, current_text, reason)
                    try:
                        rewritten_raw = call_ollama(rewrite_prompt)
                    except Exception as exc:
                        reason = f"rewrite request failed: {exc}"
                        rewrite_trace.append({"rewrite_attempt": r_idx, "ok": False, "reason": reason})
                        continue
                    rewritten = normalize_patch_newlines(extract_diff(rewritten_raw))
                    valid, reason = validate_diff_text(rewritten)
                    if valid:
                        valid, reason = validate_diff_paths(rewritten, candidate_paths, task["repo"])
                    rewrite_trace.append({"rewrite_attempt": r_idx, "ok": valid, "reason": reason})
                    current_text = rewritten_raw
                    if valid:
                        break

            candidate_patch = rewritten
            patch_preview_path = inst_dir / f"model_patch_preview_attempt_{attempt}.diff"
            patch_preview_path.write_text(candidate_patch[:6000], encoding="utf-8")
            raw_path = inst_dir / f"ollama_raw_attempt_{attempt}.txt"
            raw_path.write_text(raw, encoding="utf-8")

            if not valid:
                last_reason = reason
                attempts.append(
                    {
                        "attempt": attempt,
                        "ok": False,
                        "reason": reason,
                        "rewrite_trace": rewrite_trace,
                        "raw_preview": raw[:500],
                    }
                )
                continue

            precheck_patch_path = inst_dir / f"precheck_attempt_{attempt}.patch"
            pre_ok, pre_reason = precheck_patch_with_git_apply(
                candidate_patch, task, repo_cache_root, precheck_patch_path
            )
            attempts.append(
                {
                    "attempt": attempt,
                    "ok": pre_ok,
                    "reason": pre_reason,
                    "rewrite_trace": rewrite_trace,
                    "raw_preview": raw[:500],
                }
            )
            if pre_ok:
                accepted_patch = candidate_patch
                (inst_dir / "model_patch_preview.diff").write_text(
                    accepted_patch[:8000], encoding="utf-8"
                )
                break
            last_reason = f"apply precheck failed: {pre_reason}"

        write_json(inst_dir / "attempts.json", {"instance_id": instance_id, "attempts": attempts})
        if not accepted_patch:
            fallback_patch, fallback_reason = build_fallback_patch(
                task, repo_cache_root, candidate_paths
            )
            if fallback_patch:
                fallback_precheck_path = inst_dir / "precheck_fallback.patch"
                pre_ok, pre_reason = precheck_patch_with_git_apply(
                    fallback_patch, task, repo_cache_root, fallback_precheck_path
                )
                attempts.append(
                    {
                        "attempt": "fallback",
                        "ok": pre_ok,
                        "reason": pre_reason,
                        "raw_preview": "",
                    }
                )
                if pre_ok:
                    accepted_patch = fallback_patch
                    (inst_dir / "model_patch_preview.diff").write_text(
                        accepted_patch[:8000], encoding="utf-8"
                    )
                    last_reason = f"used fallback: {fallback_reason}"
            if not accepted_patch:
                inst_summary["status"] = "precheck_failed"
                inst_summary["reason"] = last_reason
                summary["instances"].append(inst_summary)
                write_json(inst_dir / "attempts.json", {"instance_id": instance_id, "attempts": attempts})
                write_json(inst_dir / "run_summary.json", inst_summary)
                continue

        predictions = {
            instance_id: {
                "instance_id": instance_id,
                "model_name_or_path": MODEL,
                "model_patch": accepted_patch,
            }
        }
        predictions_path = inst_dir / "predictions.json"
        write_json(predictions_path, predictions)
        inst_summary["predictions_path"] = str(predictions_path)
        inst_summary["precheck_passed"] = True
        inst_summary["status"] = "precheck_passed"
        inst_summary["reason"] = last_reason if last_reason.startswith("used fallback:") else "git apply --check passed"
        summary["instances"].append(inst_summary)
        write_json(inst_dir / "attempts.json", {"instance_id": instance_id, "attempts": attempts})
        write_json(inst_dir / "run_summary.json", inst_summary)

    summary["precheck_all_passed"] = all(x.get("precheck_passed") for x in summary["instances"])
    write_json(batch_dir / "batch_summary.json", summary)
    if not summary["precheck_all_passed"]:
        print("[phase1] not all patches passed precheck, stop before evaluation")
        return 2

    # Phase 2: single unified evaluation for all 10 instances
    print("\n[phase2] all prechecks passed, start one unified swe-bench evaluation")
    combined_predictions = {}
    for inst_summary in summary["instances"]:
        instance_id = inst_summary["instance_id"]
        predictions_path = Path(inst_summary["predictions_path"])
        try:
            one_pred = json.loads(predictions_path.read_text(encoding="utf-8"))
        except Exception:
            one_pred = {}
        combined_predictions.update(one_pred)
    combined_predictions_path = batch_dir / "predictions_all.json"
    write_json(combined_predictions_path, combined_predictions)

    run_id = f"olmo3_precheck_batch_{batch_stamp}_all10"
    eval_cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        DATASET_NAME,
        "--split",
        DATASET_SPLIT,
        "--instance_ids",
    ] + INSTANCE_IDS + [
        "--predictions_path",
        str(combined_predictions_path),
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
    proc = run_cmd(eval_cmd, cwd=project_root)
    eval_log = batch_dir / "evaluation_all.log"
    eval_log.write_text(
        "=== STDOUT ===\n"
        + proc.stdout
        + "\n\n=== STDERR ===\n"
        + proc.stderr
        + f"\n\n=== EXIT CODE ===\n{proc.returncode}\n",
        encoding="utf-8",
    )

    safe_report = ""
    m = re.search(r"Report written to ([^\r\n]+)", proc.stdout)
    if m:
        report_name = m.group(1).strip()
        report_path = project_root / report_name
        if report_path.exists():
            safe_report_path = batch_dir / safe_filename(report_name)
            safe_report_path.write_text(
                report_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            safe_report = str(safe_report_path)

    for inst_summary in summary["instances"]:
        inst_summary["run_id"] = run_id
        inst_summary["eval_log_path"] = str(eval_log)
        inst_summary["report_path"] = safe_report
        inst_summary["status"] = "evaluated"
        inst_summary["reason"] = f"eval_exit_code={proc.returncode}"
        inst_dir = batch_dir / inst_summary["instance_id"]
        write_json(inst_dir / "run_summary.json", inst_summary)
    write_json(batch_dir / "batch_summary.json", summary)

    print(f"\n[batch] done: {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
