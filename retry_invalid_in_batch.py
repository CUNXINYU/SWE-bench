#!/usr/bin/env python3
"""
Retry only invalid_patch instances in an existing olmo3 batch directory.
It regenerates patch with stricter prompt + rewrite pass, then reruns evaluation.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib import request

MODEL = "olmo-3:latest"
OLLAMA_URL = "http://host.docker.internal:11434/api/generate"
MAX_ATTEMPTS = 3
REWRITE_ATTEMPTS = 2
TIMEOUT_SECONDS = 600


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
    with request.urlopen(req, timeout=420) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body).get("response", "")


def normalize_repo_relative_path(path_like: str, repo: str) -> str:
    p = (path_like or "").strip().strip("`'\"").replace("\\", "/")
    p = p.replace("(", "/").replace(")", "")
    p = re.sub(r"/{2,}", "/", p).lstrip("./")
    owner_repo = repo.strip().lower()
    repo_name = owner_repo.split("/")[-1]

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
    p = p.lstrip("/")
    return p


def extract_candidate_paths(text: str, repo: str) -> list[str]:
    pattern = r"([A-Za-z0-9_./-]+\.(?:py|pyi|txt|md|yaml|yml|toml|json|rst|cfg|ini|sh))"
    found = re.findall(pattern, text or "")
    out = []
    for p in found:
        n = normalize_repo_relative_path(p, repo)
        if "/" in n and n not in out:
            out.append(n)
    return out[:25]


def get_diff_paths(diff_text: str) -> list[str]:
    paths = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            p = line[len("diff --git a/") :].split(" b/")[0].strip()
            if p and p not in paths:
                paths.append(p)
    return paths


def validate_patch(diff_text: str, candidate_paths: list[str], repo: str) -> tuple[bool, str]:
    text = diff_text.strip()
    if not text.startswith("diff --git "):
        return False, 'missing "diff --git" header'
    if "\n--- " not in text or "\n+++ " not in text:
        return False, 'missing "---" / "+++" headers'
    if "\n@@ " not in text and "\n@@\n" not in text:
        return False, 'missing "@@" hunk header'
    if candidate_paths:
        allowed = set(candidate_paths)
        for p in get_diff_paths(text):
            if normalize_repo_relative_path(p, repo) not in allowed:
                return False, f'path "{p}" not in candidate paths'
    return True, "ok"


def build_prompt(task: dict, candidate_paths: list[str], reason: str = "") -> str:
    lines = "\n".join(f"- {p}" for p in candidate_paths) if candidate_paths else "- (none)"
    extra = f"\n上次失败原因：{reason}" if reason else ""
    return f"""你是补丁生成器。只输出可直接 git apply 的 unified diff，不要任何解释。
硬性要求：
1) 第一行必须是 diff --git a/... b/...
2) 必须包含 --- a/... 与 +++ b/...
3) 必须包含 @@ hunk
4) 只能输出补丁内容
5) Linux 换行符
6) 不允许占位符
7) 路径必须来自以下列表：
{lines}
{extra}

instance_id: {task["instance_id"]}
repo: {task["repo"]}
problem_statement:
{task["problem_statement"]}
"""


def build_rewrite_prompt(task: dict, candidate_paths: list[str], raw: str, reason: str) -> str:
    lines = "\n".join(f"- {p}" for p in candidate_paths) if candidate_paths else "- (none)"
    return f"""把下面文本重写成可直接 git apply 的 unified diff。只输出补丁，不要解释。
约束：
1) diff --git 开头
2) 包含 --- / +++ / @@
3) 仅用真实路径（必须来自列表）
{lines}
失败原因：{reason}

原始文本：
{raw}
"""


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python retry_invalid_in_batch.py <batch_dir_name>")
        return 2

    project_root = Path(__file__).resolve().parent
    batch_dir = project_root / "olmo3" / sys.argv[1]
    batch_summary_path = batch_dir / "batch_summary.json"
    if not batch_summary_path.exists():
        print(f"batch_summary not found: {batch_summary_path}")
        return 2

    batch_summary = json.loads(batch_summary_path.read_text(encoding="utf-8"))

    # Refresh current status from per-instance run_summary if present.
    for rec in batch_summary["instances"]:
        rs = batch_dir / rec["instance_id"] / "run_summary.json"
        if rs.exists():
            try:
                merged = json.loads(rs.read_text(encoding="utf-8"))
                rec.update(merged)
            except Exception:
                pass

    for rec in batch_summary["instances"]:
        if rec.get("status") != "invalid_patch":
            continue

        instance_id = rec["instance_id"]
        print(f"[retry] {instance_id}")
        inst_dir = batch_dir / instance_id
        inst_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = inst_dir / "prompt.txt"
        if prompt_path.exists():
            old_prompt = prompt_path.read_text(encoding="utf-8")
        else:
            old_prompt = ""

        # Recover task information from existing prompt artifact to avoid re-downloading dataset.
        m_repo = re.search(r"repo:\s*([^\r\n]+)", old_prompt)
        repo = m_repo.group(1).strip() if m_repo else instance_id.split("-")[0].replace("__", "/")
        m_ps = re.search(r"problem_statement:\s*(.*)$", old_prompt, flags=re.S)
        problem_statement = m_ps.group(1).strip() if m_ps else ""
        task = {
            "instance_id": instance_id,
            "repo": repo,
            "problem_statement": problem_statement,
        }

        candidate_paths = extract_candidate_paths(task["problem_statement"], task["repo"])

        patch = ""
        reason = "initial"
        raw = ""
        for _ in range(MAX_ATTEMPTS):
            prompt = build_prompt(task, candidate_paths, reason=reason)
            try:
                raw = call_ollama(prompt)
            except Exception as exc:
                reason = f"ollama request failed: {exc}"
                raw = ""
                continue
            candidate = normalize_patch_newlines(extract_diff(raw))
            ok, why = validate_patch(candidate, candidate_paths, task["repo"])
            if ok:
                patch = candidate
                reason = "ok"
                break
            reason = why
            for _ in range(REWRITE_ATTEMPTS):
                rewrite_prompt = build_rewrite_prompt(task, candidate_paths, raw, reason)
                try:
                    raw = call_ollama(rewrite_prompt)
                except Exception as exc:
                    reason = f"ollama rewrite failed: {exc}"
                    raw = ""
                    break
                candidate = normalize_patch_newlines(extract_diff(raw))
                ok, why = validate_patch(candidate, candidate_paths, task["repo"])
                if ok:
                    patch = candidate
                    reason = "ok"
                    break
                reason = why
            if patch:
                break

        (inst_dir / "ollama_response_raw_retry.txt").write_text(raw, encoding="utf-8")
        if not patch:
            rec["status"] = "invalid_patch"
            rec["reason"] = reason
            (inst_dir / "run_summary.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            continue

        predictions = {
            instance_id: {
                "instance_id": instance_id,
                "model_name_or_path": MODEL,
                "model_patch": patch,
            }
        }
        predictions_path = inst_dir / "predictions.json"
        predictions_path.write_text(
            json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        rec["predictions_path"] = str(predictions_path).replace(str(project_root), "/app")

        run_id = f"olmo3_retry_{instance_id}"
        rec["run_id"] = run_id
        cmd = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            batch_summary["dataset_name"],
            "--split",
            batch_summary["split"],
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
        eval_log = inst_dir / "evaluation_retry.log"
        eval_log.write_text(
            "=== STDOUT ===\n" + proc.stdout + "\n\n=== STDERR ===\n" + proc.stderr,
            encoding="utf-8",
        )
        rec["eval_log_path"] = str(eval_log).replace(str(project_root), "/app")
        rec["status"] = "evaluated"
        rec["reason"] = f"eval_exit_code={proc.returncode}"

        m = re.search(r"Report written to ([^\r\n]+)", proc.stdout)
        if m:
            rp = project_root / m.group(1).strip()
            if rp.exists():
                alias = inst_dir / safe_filename(m.group(1).strip())
                alias.write_text(rp.read_text(encoding="utf-8"), encoding="utf-8")
                rec["report_path"] = str(alias).replace(str(project_root), "/app")

        (inst_dir / "run_summary.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    batch_summary_path.write_text(
        json.dumps(batch_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"retry done: {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
