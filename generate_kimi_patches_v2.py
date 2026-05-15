#!/usr/bin/env python3
"""
使用云端模型 Kimi K2.5 生成 git diff 格式的修复补丁 (版本2 - 交互式)。
Use Kimi K2.5 to prepare prompts, collect patches, and export SWE-bench files.

使用流程：
Steps:
1. 运行此脚本准备环境并生成提示词
1. Run this script to prepare repos and prompts.
2. 逐个查看提示词，使用 Kimi K2.5 生成修复
2. Use Kimi K2.5 to create each fix.
3. 将模型输出的 git diff 粘贴回脚本或使用辅助功能
3. Save the model git diff for this script to collect.
4. 脚本自动应用补丁并导出最终结果
4. The script applies patches and writes final files.

所有结果保存在: Kimi K2.5/
All results are saved in: Kimi K2.5/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from datasets import load_from_disk

# 配置 / Basic paths.
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "Kimi K2.5"
REPO_CACHE = ROOT / "repo_cache"
PATCHES_DIR = OUT_DIR / "patches"
WORKTREES_DIR = OUT_DIR / "worktrees"
PROMPTS_DIR = OUT_DIR / "prompts"
LOGS_DIR = OUT_DIR / "logs"
PREDICTIONS_FILE = OUT_DIR / "predictions.json"
PREDICTIONS_JSONL_FILE = OUT_DIR / "predictions.jsonl"
MANIFEST_FILE = OUT_DIR / "manifest.json"

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

# 本地数据集路径 / Local dataset path.
LOCAL_DATASET = ROOT / "dataset_local"


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
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


def load_dataset_instances() -> dict[str, dict[str, Any]]:
    """加载本地数据集并返回 instance_id 到数据的映射。/ Load local dataset by instance id."""
    ds = load_from_disk(str(LOCAL_DATASET))
    return {item["instance_id"]: dict(item) for item in ds}


def get_repo_url(repo: str) -> str:
    """获取仓库的 GitHub URL。/ Get the GitHub repo URL."""
    return f"https://github.com/{repo}.git"


def prepare_repo(instance_data: dict[str, Any]) -> Path:
    """
    准备代码仓库到指定的 base_commit。
    Prepare the repo at the base commit.
    使用 repo_cache 来避免重复克隆。
    Use repo_cache to avoid cloning many times.
    """
    repo = instance_data["repo"]
    base_commit = instance_data["base_commit"]
    instance_id = instance_data["instance_id"]

    # 缓存目录名 / Cache folder name.
    repo_folder_name = repo.replace("/", "__")
    cache_path = REPO_CACHE / repo_folder_name

    # 工作目录（每个实例独立）/ Work folder for this instance.
    work_path = WORKTREES_DIR / instance_id

    print(f"  准备仓库 / Prepare repo: {repo}@{base_commit[:8]}")

    # 确保缓存存在 / Make sure the cache exists.
    if not cache_path.exists():
        print(f"    克隆仓库到缓存 / Clone repo to cache...")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        result = run_cmd(["git", "clone", "--no-checkout", get_repo_url(repo), str(cache_path)], timeout=600)
        if result.returncode != 0:
            # 如果已经存在但不完整，删除重试 / Delete broken cache and retry.
            if cache_path.exists():
                shutil.rmtree(cache_path)
            result = run_cmd(["git", "clone", "--no-checkout", get_repo_url(repo), str(cache_path)], timeout=600)
            if result.returncode != 0:
                raise RuntimeError(f"克隆失败 / Clone failed: {result.stderr}")

    # 创建工作目录 / Create the work folder.
    if work_path.exists():
        shutil.rmtree(work_path)
    work_path.parent.mkdir(parents=True, exist_ok=True)

    # 复制缓存到工作目录 / Copy cache to the work folder.
    shutil.copytree(cache_path, work_path, ignore=shutil.ignore_patterns(".tox", "build", "*.egg-info", "__pycache__"))

    # 检出到 base_commit / Checkout the base commit.
    result = run_cmd(["git", "checkout", "-f", base_commit], cwd=work_path, timeout=60)
    if result.returncode != 0:
        # 尝试 fetch 然后 checkout / Fetch first, then checkout again.
        run_cmd(["git", "fetch", "origin", base_commit], cwd=work_path, timeout=120)
        result = run_cmd(["git", "checkout", "-f", base_commit], cwd=work_path, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"无法检出到 / Cannot checkout {base_commit}: {result.stderr}")

    # 清理 git 状态 / Clean git state.
    run_cmd(["git", "clean", "-fd"], cwd=work_path)
    run_cmd(["git", "reset", "--hard"], cwd=work_path)

    print(f"    仓库准备完成 / Repo ready: {work_path}")
    return work_path


def read_file_with_limit(file_path: Path, max_lines: int = 100) -> str:
    """读取文件，限制行数。/ Read a file with a line limit."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = content.split('\n')
        if len(lines) > max_lines * 2:
            # 保留开头和结尾 / Keep the start and end.
            return '\n'.join(lines[:max_lines] + ['\n... [truncated] ...\n'] + lines[-max_lines:])
        return content
    except Exception as e:
        return f"[Error reading file: {e}]"


def find_relevant_files(repo_path: Path, problem_statement: str, hint: str, max_files: int = 10) -> list[Path]:
    """
    根据问题描述和提示，找到可能相关的文件。
    Find files that may be related to the issue.
    """
    potential_files = []

    # 从问题描述和提示中提取文件路径 / Read file paths from the issue text.
    text = problem_statement + " " + hint

    # 匹配常见的文件路径模式 / Match common file path forms.
    patterns = [
        r'`([^`]+\.py)`',
        r'"([^"]+\.py)"',
        r"'([^']+\.py)'",
        r'/(\S+\.py)',
        r'(\w+(?:/\w+)+\.py)',
    ]

    found_paths = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path_str = match.group(1)
            if path_str not in found_paths:
                found_paths.add(path_str)
                full_path = repo_path / path_str
                if full_path.exists() and full_path.is_file():
                    potential_files.append(full_path)

    # 如果没有找到，尝试搜索关键词相关的文件 / If no file is found, search by keywords.
    if not potential_files:
        # 提取问题中的函数名、类名等 / Get function and class names from the issue.
        words = re.findall(r'\b[a-z_][a-z0-9_]*\b', problem_statement.lower())
        keywords = [w for w in words if len(w) > 4 and w not in {
            'return', 'def', 'class', 'import', 'from', 'raise', 'assert',
            'error', 'issue', 'problem', 'fix', 'should', 'would', 'could'
        }]

        # 搜索包含这些关键词的 Python 文件 / Search Python files with these words.
        for py_file in repo_path.rglob("*.py"):
            if "test" in py_file.name.lower():
                continue
            if "__pycache__" in str(py_file):
                continue
            if "migration" in str(py_file).lower():
                continue

            rel_path = py_file.relative_to(repo_path).as_posix()
            if any(kw in rel_path.lower() for kw in keywords[:3]):
                potential_files.append(py_file)
                if len(potential_files) >= max_files:
                    break

    return potential_files[:max_files]


def generate_prompt(instance_data: dict[str, Any], repo_path: Path) -> str:
    """生成给 Kimi K2.5 的提示词。/ Build the prompt for Kimi K2.5."""
    instance_id = instance_data["instance_id"]
    problem = instance_data.get("problem_statement", "")
    hint = instance_data.get("hints_text", "")
    repo = instance_data.get("repo", "")

    # 找到相关文件 / Find related files.
    relevant_files = find_relevant_files(repo_path, problem, hint)

    # Note: This Chinese prompt asks Kimi to analyze the issue and return a git diff.
    prompt = f"""你是一个专业的软件工程师。请修复以下开源项目中的问题。

## 项目信息

- 实例 ID: {instance_id}
- 仓库: {repo}

## 问题描述 (Problem Statement)

{problem}

"""

    if hint:
        prompt += f"""## 提示 (Hints)

{hint}

"""

    # 添加相关文件内容 / Add related file content.
    if relevant_files:
        prompt += """## 相关文件

"""
        for file_path in relevant_files[:5]:  # 限制文件数量
            rel_path = file_path.relative_to(repo_path).as_posix()
            content = read_file_with_limit(file_path, max_lines=80)
            prompt += f"""### {rel_path}

```python
{content}
```

"""

    prompt += """## 任务要求

请分析上述问题并提供修复方案：

1. **分析问题根本原因**: 解释为什么会出现这个问题
2. **确定修改方案**: 说明需要修改哪些文件以及如何修改
3. **生成补丁**: 提供可直接应用的 git diff 格式补丁

## 输出格式

请严格按以下格式输出：

### 分析

[你的问题分析]

### 修复方案

[你的修复思路]

### Git Diff 补丁

```diff
diff --git a/path/to/file.py b/path/to/file.py
index xxxxxxx..xxxxxxx 100644
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -10,7 +10,7 @@ def example():
     unchanged_line
-    old_line_to_remove
+    new_line_to_add
     another_unchanged_line
```

**重要提示**:
- 必须输出标准的 git diff 格式（以 `diff --git` 开头）
- 确保补丁包含完整的上下文（至少3行）
- 补丁应该可以直接应用：`git apply patch.diff` 或 `patch -p1 < patch.diff`
- 只修改必要的代码，保持最小改动原则
- 如果需要在多个文件中修改，请包含所有文件的 diff

请提供 git diff 格式的补丁：
"""

    return prompt


def apply_patch(repo_path: Path, patch_content: str) -> tuple[bool, str]:
    """
    尝试应用补丁。
    Try to apply the patch.
    返回 (成功, 错误信息)
    Return success flag and error message.
    """
    # 清理补丁内容 / Clean patch text.
    patch_content = patch_content.strip()

    # 如果补丁内容被包裹在代码块中，提取出来 / Remove markdown code fences.
    if "```diff" in patch_content:
        match = re.search(r'```diff\n(.*?)```', patch_content, re.DOTALL)
        if match:
            patch_content = match.group(1).strip()
    elif "```" in patch_content:
        match = re.search(r'```\n?(.*?)```', patch_content, re.DOTALL)
        if match:
            patch_content = match.group(1).strip()

    # 确保补丁以 diff --git 开头 / Make sure the patch starts with diff --git.
    if not patch_content.startswith("diff --git"):
        # 尝试找到 diff --git 的位置 / Try to find the diff start.
        if "diff --git" in patch_content:
            patch_content = patch_content[patch_content.index("diff --git"):]
        else:
            return False, "补丁内容不以 'diff --git' 开头 / Patch does not start with 'diff --git'"

    # 写入临时文件 / Write a temp patch file.
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False, encoding='utf-8') as f:
        f.write(patch_content)
        patch_file = Path(f.name)

    try:
        # 首先尝试 git apply --check / Check with git apply first.
        result = run_cmd(["git", "apply", "--check", str(patch_file)], cwd=repo_path, timeout=30)
        if result.returncode != 0:
            # 尝试使用 --3way 选项 / Try --3way mode.
            result = run_cmd(["git", "apply", "--check", "--3way", str(patch_file)], cwd=repo_path, timeout=30)

        if result.returncode == 0:
            # 应用补丁 / Apply the patch.
            result = run_cmd(["git", "apply", str(patch_file)], cwd=repo_path, timeout=30)
            if result.returncode == 0:
                return True, ""
            else:
                return False, f"git apply 失败 / git apply failed: {result.stderr}"
        else:
            # 尝试直接读取并手动修改 / Leave the error for manual checking.
            return False, f"补丁验证失败 / Patch check failed (git apply --check): {result.stderr}"
    finally:
        if patch_file.exists():
            patch_file.unlink()


def export_git_diff(repo_path: Path) -> str:
    """导出当前仓库的 git diff。/ Export git diff from the repo."""
    result = run_cmd(["git", "diff", "--binary"], cwd=repo_path)
    if result.returncode == 0:
        return result.stdout
    return ""


def process_instance(instance_id: str, instance_data: dict[str, Any], auto_mode: bool = False) -> dict[str, Any]:
    """
    处理单个实例。
    Process one instance.

    参数:
    Args:
        instance_id: 实例 ID
        instance_data: 实例数据
        auto_mode: 如果为 True，尝试自动查找已存在的补丁文件
    """
    print(f"\n{'='*70}")
    print(f"处理实例 / Process instance: {instance_id}")
    print(f"{'='*70}")

    result = {
        "instance_id": instance_id,
        "model_name_or_path": "kimi-k2.5-cloud",
        "model_patch": "",
        "status": "pending",
        "reason": "",
    }

    try:
        # 准备仓库 / Prepare the repo.
        repo_path = prepare_repo(instance_data)
        result["repo_path"] = str(repo_path)

        # 生成提示词 / Build the prompt.
        prompt = generate_prompt(instance_data, repo_path)

        # 保存提示词 / Save the prompt.
        prompt_file = PROMPTS_DIR / f"{instance_id}_prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        print(f"  提示词已保存 / Prompt saved: {prompt_file}")

        if auto_mode:
            # 尝试查找预生成的补丁 / Look for a saved patch.
            manual_patch_file = PROMPTS_DIR / f"{instance_id}_patch.txt"
            if manual_patch_file.exists():
                print(f"  找到预生成补丁 / Found patch file: {manual_patch_file}")
                patch_content = manual_patch_file.read_text(encoding="utf-8")

                # 应用补丁 / Apply the patch.
                success, error = apply_patch(repo_path, patch_content)
                if success:
                    print(f"  补丁应用成功 / Patch applied.")
                    git_diff = export_git_diff(repo_path)
                    result["model_patch"] = git_diff
                    result["status"] = "success"
                    result["reason"] = "使用预生成补丁并成功应用 / Used saved patch and applied it."

                    # 保存补丁 / Save the patch.
                    patch_file = PATCHES_DIR / f"{instance_id}.patch"
                    patch_file.write_text(git_diff, encoding="utf-8")
                    print(f"  补丁已保存 / Patch saved: {patch_file}")
                else:
                    print(f"  补丁应用失败 / Patch failed: {error}")
                    result["status"] = "patch_failed"
                    result["reason"] = error
                    result["attempted_patch"] = patch_content
            else:
                print(f"  未找到预生成补丁，等待手动处理 / No saved patch found.")
                result["status"] = "waiting_for_patch"
                result["reason"] = f"请将 Kimi K2.5 生成的补丁保存到 / Save Kimi patch to: {manual_patch_file}"
        else:
            # 交互模式 - 只是准备环境 / Interactive mode only prepares files.
            print(f"\n  请使用 Kimi K2.5 处理上述提示词 / Use Kimi K2.5 to answer the prompt.")
            print(f"  然后将生成的 git diff 保存到 / Save git diff to: {PROMPTS_DIR / f'{instance_id}_patch.txt'}")
            result["status"] = "ready_for_model"
            result["reason"] = "提示词已生成，等待模型处理 / Prompt is ready."

    except Exception as e:
        print(f"  错误 / Error: {e}")
        import traceback
        traceback.print_exc()
        result["status"] = "error"
        result["reason"] = str(e)

    return result


def collect_manual_patches() -> list[dict[str, Any]]:
    """
    收集用户手动放入的补丁文件。
    Collect patch files saved by the user.
    """
    results = []

    # 加载数据集 / Load dataset.
    all_instances = load_dataset_instances()
    target_instances = {k: v for k, v in all_instances.items() if k in INSTANCE_IDS}

    for instance_id in INSTANCE_IDS:
        if instance_id not in target_instances:
            continue

        instance_data = target_instances[instance_id]
        repo_path = WORKTREES_DIR / instance_id

        # 检查是否已准备 / Check if the repo is ready.
        if not repo_path.exists():
            try:
                repo_path = prepare_repo(instance_data)
            except Exception as e:
                print(f"[{instance_id}] 准备仓库失败 / Prepare repo failed: {e}")
                continue

        # 查找手动补丁 / Look for manual patch.
        manual_patch_file = PROMPTS_DIR / f"{instance_id}_patch.txt"

        result = {
            "instance_id": instance_id,
            "model_name_or_path": "kimi-k2.5-cloud",
            "model_patch": "",
            "status": "pending",
        }

        if manual_patch_file.exists():
            patch_content = manual_patch_file.read_text(encoding="utf-8")
            print(f"[{instance_id}] 找到手动补丁 / Found manual patch")

            # 重置仓库 / Reset repo.
            run_cmd(["git", "reset", "--hard"], cwd=repo_path)
            run_cmd(["git", "clean", "-fd"], cwd=repo_path)

            # 应用补丁 / Apply patch.
            success, error = apply_patch(repo_path, patch_content)
            if success:
                git_diff = export_git_diff(repo_path)
                result["model_patch"] = git_diff
                result["status"] = "success"
                result["reason"] = "手动补丁应用成功 / Manual patch applied."

                # 保存 / Save.
                patch_file = PATCHES_DIR / f"{instance_id}.patch"
                patch_file.write_text(git_diff, encoding="utf-8")
                print(f"  成功 / Success: {patch_file}")
            else:
                result["status"] = "failed"
                result["reason"] = f"补丁应用失败 / Patch failed: {error}"
                print(f"  失败 / Failed: {error}")
        else:
            result["status"] = "no_patch"
            result["reason"] = f"未找到补丁文件 / Patch file not found: {manual_patch_file}"
            print(f"[{instance_id}] 未找到补丁文件 / Patch file not found")

        results.append(result)

    return results


def main() -> int:
    """主函数。/ Main entry point."""
    import argparse
    parser = argparse.ArgumentParser(description="使用 Kimi K2.5 生成 SWE-bench 补丁 / Generate SWE-bench patches with Kimi K2.5")
    parser.add_argument("--prepare", action="store_true", help="仅准备环境和生成提示词 / Prepare prompts only")
    parser.add_argument("--collect", action="store_true", help="收集手动放入的补丁并应用 / Collect saved patches")
    parser.add_argument("--auto", action="store_true", help="自动模式（查找预生成补丁）/ Auto mode")
    parser.add_argument("--instance", type=str, help="仅处理指定实例 / One instance only")
    args = parser.parse_args()

    print("=" * 70)
    print("使用 Kimi K2.5 云端模型生成 SWE-bench 补丁 / Generate SWE-bench patches with Kimi K2.5")
    print("=" * 70)

    # 创建输出目录 / Create output folders.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 加载数据集 / Load dataset.
    print("\n加载本地数据集 / Loading local dataset...")
    all_instances = load_dataset_instances()
    target_instances = {k: v for k, v in all_instances.items() if k in INSTANCE_IDS}
    print(f"数据集 / Dataset: {len(all_instances)} instances")
    print(f"目标实例 / Target instances: {len(INSTANCE_IDS)}")
    print(f"找到 / Found: {len(target_instances)}")

    missing = set(INSTANCE_IDS) - set(target_instances.keys())
    if missing:
        print(f"缺失 / Missing: {missing}")

    if args.collect:
        # 收集模式 / Collect mode.
        print("\n" + "-" * 70)
        print("收集手动补丁 / Collect saved patches...")
        results = collect_manual_patches()
    else:
        # 正常处理模式 / Normal mode.
        instance_list = [args.instance] if args.instance else INSTANCE_IDS

        results = []
        for instance_id in instance_list:
            if instance_id not in target_instances:
                print(f"\n跳过 / Skip {instance_id} (不在数据集中 / not in dataset)")
                results.append({
                    "instance_id": instance_id,
                    "status": "skipped",
                    "reason": "Instance not found in dataset",
                })
                continue

            instance_data = target_instances[instance_id]
            result = process_instance(instance_id, instance_data, auto_mode=args.auto)
            results.append(result)

    # 保存结果 / Save results.
    predictions = [
        {
            "instance_id": r["instance_id"],
            "model_name_or_path": "kimi-k2.5-cloud",
            "model_patch": r.get("model_patch", ""),
        }
        for r in results
    ]

    # 保存 manifest / Save manifest.
    MANIFEST_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n清单已保存 / Manifest saved: {MANIFEST_FILE}")

    # 保存 predictions / Save predictions.
    PREDICTIONS_FILE.write_text(json.dumps(predictions, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"预测结果(JSON) / Predictions JSON: {PREDICTIONS_FILE}")

    with open(PREDICTIONS_JSONL_FILE, "w", encoding="utf-8") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")
    print(f"预测结果(JSONL) / Predictions JSONL: {PREDICTIONS_JSONL_FILE}")

    # 打印摘要 / Print summary.
    print("\n" + "=" * 70)
    print("摘要 / Summary")
    print("=" * 70)

    status_counts = {}
    for r in results:
        status = r.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    print("\n下一步 / Next steps:")
    if args.prepare:
        print("1. 查看 prompts/ 目录中的提示词文件 / Check prompt files")
        print("2. 使用 Kimi K2.5 处理每个提示词，生成 git diff 补丁 / Use Kimi to create git diff patches")
        print("3. 将补丁保存到 prompts/{instance_id}_patch.txt / Save each patch there")
        print("4. 运行: python generate_kimi_patches_v2.py --collect")
    elif args.collect or args.auto:
        print("补丁已生成，现在可以运行评估脚本 / Patches are ready. Run evaluation:")
        print("  python evaluate_kimi_patches.py")
    else:
        print("1. 查看 prompts/ 目录中的提示词 / Check prompts")
        print("2. 使用 Kimi K2.5 生成补丁 / Create patches with Kimi K2.5")
        print("3. 运行收集 / Collect patches: python generate_kimi_patches_v2.py --collect")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
