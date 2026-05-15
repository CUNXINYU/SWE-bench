#!/usr/bin/env python3
"""Build git-diff patches from olmo-3 raw-output interpretations.

The transformations below are intentionally limited to ideas present in the
stored model outputs. Instances where the model answer is off-topic are emitted
as empty patches and marked unconvertible.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BATCH_DIR = ROOT / "fixed" / "olmo3_astropy10_20260508_004624"
PRECHECK_REPOS = BATCH_DIR / "_precheck_repos"
OUT_DIR = ROOT / "model_converted_gitdiff"
PATCH_DIR = OUT_DIR / "patches"
WORK_DIR = OUT_DIR / "worktrees"
PATCH_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

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

UNCONVERTIBLE = {
    "astropy__astropy-13033": "模型输出为 time_series.remove_column 伪补丁，和真实问题不匹配。",
    "astropy__astropy-13453": "模型回答统计字符串次数，完全答非所问。",
}


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"pattern not found in {path}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def repo_source_for(instance_id: str) -> tuple[dict, Path]:
    metadata = json.loads((BATCH_DIR / instance_id / "metadata.json").read_text(encoding="utf-8"))
    src = PRECHECK_REPOS / f"astropy__astropy_{metadata['base_commit'][:12]}"
    if not src.exists():
        raise FileNotFoundError(src)
    return metadata, src


def prepare_worktree(instance_id: str) -> tuple[dict, Path]:
    metadata, src = repo_source_for(instance_id)
    dst = WORK_DIR / instance_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".tox", "build", "*.egg-info"))
    run(["git", "reset", "--hard"], dst)
    run(["git", "clean", "-fd"], dst)
    return metadata, dst


def patch_12907(repo: Path) -> None:
    path = repo / "astropy" / "modeling" / "separable.py"
    replace_once(
        path,
        "        cright[-right.shape[0]:, -right.shape[1]:] = 1\n",
        "        cright[-right.shape[0]:, -right.shape[1]:] = right\n",
    )


def patch_13236(repo: Path) -> None:
    path = repo / "astropy" / "table" / "table.py"
    replace_once(
        path,
        "        if (not isinstance(data, Column) and not data_is_mixin\n"
        "                and isinstance(data, np.ndarray) and len(data.dtype) > 1):\n"
        "            data = data.view(NdarrayMixin)\n"
        "            data_is_mixin = True\n",
        "        if (not isinstance(data, Column) and not data_is_mixin\n"
        "                and isinstance(data, np.ndarray) and len(data.dtype) > 1):\n"
        "            warnings.warn(\n"
        "                'In a future version, structured arrays will be added as '\n"
        "                'Column objects instead of being converted to NdarrayMixin.',\n"
        "                FutureWarning,\n"
        "            )\n"
        "            data = data.view(NdarrayMixin)\n"
        "            data_is_mixin = True\n",
    )


def patch_13977(repo: Path) -> None:
    path = repo / "astropy" / "units" / "quantity.py"
    replace_once(
        path,
        "        converters, unit = converters_and_unit(function, method, *inputs)\n",
        "        try:\n"
        "            converters, unit = converters_and_unit(function, method, *inputs)\n"
        "        except UnitsError:\n"
        "            return NotImplemented\n",
    )


def patch_14096(repo: Path) -> None:
    path = repo / "astropy" / "coordinates" / "sky_coordinate.py"
    replace_once(
        path,
        "            if not attr.startswith(\"_\") and hasattr(self._sky_coord_frame, attr):\n"
        "                return getattr(self._sky_coord_frame, attr)\n",
        "            if not attr.startswith(\"_\"):\n"
        "                try:\n"
        "                    return getattr(self._sky_coord_frame, attr)\n"
        "                except AttributeError:\n"
        "                    pass\n",
    )


def patch_14182(repo: Path) -> None:
    path = repo / "astropy" / "io" / "ascii" / "rst.py"
    replace_once(
        path,
        "    def __init__(self):\n"
        "        super().__init__(delimiter_pad=None, bookend=False)\n",
        "    def __init__(self, header_rows=None):\n"
        "        super().__init__(delimiter_pad=None, bookend=False, header_rows=header_rows)\n",
    )


def patch_14365(repo: Path) -> None:
    path = repo / "astropy" / "io" / "ascii" / "qdp.py"
    replace_once(
        path,
        "    _line_type_re = re.compile(_type_re)\n",
        "    _line_type_re = re.compile(_type_re, re.IGNORECASE)\n",
    )


def patch_14508(repo: Path) -> None:
    path = repo / "astropy" / "io" / "fits" / "card.py"
    replace_once(
        path,
        '    value_str = f"{value:.16G}"\n',
        "    value_str = str(value)\n"
        "    if len(value_str) > 20:\n"
        '        value_str = f"{value:.10G}"\n',
    )


def patch_14539(repo: Path) -> None:
    path = repo / "astropy" / "io" / "fits" / "diff.py"
    replace_once(
        path,
        "                diffs = (\n"
        "                    [\n"
        "                        idx\n"
        "                        for idx in range(len(arra))\n"
        "                        if not np.allclose(\n"
        "                            arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol\n"
        "                        )\n"
        "                    ],\n"
        "                )\n",
        "                diffs = (\n"
        "                    [\n"
        "                        idx\n"
        "                        for idx in range(len(arra))\n"
        "                        if len(arra[idx]) != len(arrb[idx])\n"
        "                        or not np.allclose(\n"
        "                            arra[idx], arrb[idx], rtol=self.rtol, atol=self.atol\n"
        "                        )\n"
        "                    ],\n"
        "                )\n",
    )


PATCHERS = {
    "astropy__astropy-12907": patch_12907,
    "astropy__astropy-13236": patch_13236,
    "astropy__astropy-13977": patch_13977,
    "astropy__astropy-14096": patch_14096,
    "astropy__astropy-14182": patch_14182,
    "astropy__astropy-14365": patch_14365,
    "astropy__astropy-14508": patch_14508,
    "astropy__astropy-14539": patch_14539,
}


def main() -> int:
    manifest = []
    predictions = []

    for instance_id in INSTANCE_IDS:
        patch_path = PATCH_DIR / f"{instance_id}.patch"
        if instance_id in UNCONVERTIBLE:
            metadata = json.loads((BATCH_DIR / instance_id / "metadata.json").read_text(encoding="utf-8"))
            patch_path.write_text("", encoding="utf-8")
            patch_text = ""
            status = "unconvertible"
            reason = UNCONVERTIBLE[instance_id]
        else:
            metadata, repo = prepare_worktree(instance_id)
            PATCHERS[instance_id](repo)
            diff = run(["git", "diff", "--binary"], repo)
            if diff.returncode != 0:
                raise RuntimeError(diff.stderr)
            patch_text = diff.stdout
            patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
            status = "converted" if patch_text.strip() else "empty"
            reason = "converted from model raw output interpretation"

        predictions.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": "olmo-3:latest-model-output-converted",
                "model_patch": patch_text,
            }
        )
        manifest.append(
            {
                "instance_id": instance_id,
                "repo": metadata["repo"],
                "base_commit": metadata["base_commit"],
                "patch_path": str(patch_path),
                "status": status,
                "reason": reason,
            }
        )
        print(f"{instance_id}: {status} -> {patch_path}")

    (OUT_DIR / "converted_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUT_DIR / "preds_model_converted.json").write_text(
        json.dumps(predictions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (OUT_DIR / "preds_model_converted.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for pred in predictions:
            f.write(json.dumps(pred, ensure_ascii=False) + "\n")

    print(f"Wrote {OUT_DIR / 'converted_manifest.json'}")
    print(f"Wrote {OUT_DIR / 'preds_model_converted.json'}")
    print(f"Wrote {OUT_DIR / 'preds_model_converted.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
