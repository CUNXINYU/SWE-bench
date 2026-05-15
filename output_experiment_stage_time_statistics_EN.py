# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


# This script extracts two types of timing data from local experiment artifacts:
# 1. Patch generation span: from the earliest input/patch file to the latest prediction file.
# 2. Docker evaluation time: the sum of per-instance evaluation times from log files.

PROJECT_ROOT = Path(__file__).resolve().parent
INFO_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - INFO")
TQDM_SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)s/it")


@dataclass
class PatchStageConfig:
    name: str
    start_patterns: list[str]
    end_patterns: list[str]
    method: str


@dataclass
class EvalStageConfig:
    name: str
    log_patterns: list[str]
    parser: str
    method: str


@dataclass
class ResultRecord:
    name: str
    minutes: float
    detail: str
    method: str


def list_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(PROJECT_ROOT.glob(pattern))
    return sorted(path for path in files if path.is_file())


def file_mtime(path: Path) -> float:
    return path.stat().st_mtime


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def short_path(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def calculate_patch_stage(config: PatchStageConfig) -> ResultRecord:
    """Calculate the observable patch generation span from file modification times."""
    start_files = list_files(config.start_patterns)
    end_files = list_files(config.end_patterns)

    if not start_files:
        raise FileNotFoundError(f"{config.name}: no start files found: {config.start_patterns}")
    if not end_files:
        raise FileNotFoundError(f"{config.name}: no end files found: {config.end_patterns}")

    first_file = min(start_files, key=file_mtime)
    last_file = max(end_files, key=file_mtime)
    minutes = (file_mtime(last_file) - file_mtime(first_file)) / 60
    detail = (
        f"{len(start_files)} start files, {len(end_files)} end files; "
        f"earliest {format_time(file_mtime(first_file))} {short_path(first_file)}; "
        f"latest {format_time(file_mtime(last_file))} {short_path(last_file)}"
    )
    return ResultRecord(config.name, minutes, detail, config.method)


def parse_info_span_seconds(log_path: Path) -> float:
    """Calculate the span between the first and last INFO timestamps in run_instance.log."""
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    timestamps = [
        datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
        for match in INFO_TIME_RE.finditer(text)
    ]
    if len(timestamps) < 2:
        return 0.0
    return (timestamps[-1] - timestamps[0]).total_seconds()


def parse_tqdm_seconds(log_path: Path) -> float:
    """Extract the final tqdm s/it value from evaluation*.log."""
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    values = [float(match.group(1)) for match in TQDM_SECONDS_RE.finditer(text)]
    return values[-1] if values else 0.0


def calculate_eval_stage(config: EvalStageConfig) -> ResultRecord:
    """Parse evaluation logs and sum the per-instance evaluation times."""
    log_files = list_files(config.log_patterns)
    if not log_files:
        raise FileNotFoundError(f"{config.name}: no evaluation logs found: {config.log_patterns}")

    seconds_by_file: list[tuple[Path, float]] = []
    for log_file in log_files:
        if config.parser == "info_span":
            seconds = parse_info_span_seconds(log_file)
        elif config.parser == "tqdm_seconds":
            seconds = parse_tqdm_seconds(log_file)
        else:
            raise ValueError(f"Unknown evaluation log parser: {config.parser}")
        seconds_by_file.append((log_file, seconds))

    total_seconds = sum(seconds for _, seconds in seconds_by_file)
    nonzero_count = sum(1 for _, seconds in seconds_by_file if seconds > 0)
    detail = (
        f"{len(log_files)} logs, {nonzero_count} non-zero timings; "
        f"total seconds {total_seconds:.2f} s"
    )
    return ResultRecord(config.name, total_seconds / 60, detail, config.method)


def make_bar(value: float, max_value: float, width: int = 36) -> str:
    if max_value <= 0:
        return ""
    filled = round(value / max_value * width)
    return "#" * filled + " " * (width - filled)


def print_chart(title: str, records: list[ResultRecord]) -> None:
    print()
    print(title)
    print("-" * len(title))
    max_value = max(record.minutes for record in records)
    for record in records:
        print(f"{record.name:<18} | {make_bar(record.minutes, max_value)} | {record.minutes:>7.2f} minutes")


def print_table(title: str, records: list[ResultRecord], time_header: str) -> None:
    print()
    print(title)
    print(f"| Batch / Model | {time_header} | Method | Extraction Details |")
    print("|---|---:|---|---|")
    for record in records:
        print(f"| {record.name} | {record.minutes:.2f} | {record.method} | {record.detail} |")


def build_patch_configs() -> list[PatchStageConfig]:
    """Configure patch generation timing rules for the five experiment groups."""
    return [
        PatchStageConfig(
            name="olmo-3 first round",
            start_patterns=["olmo3/batch_20260504_111710/*/prompt.txt"],
            end_patterns=["olmo3/batch_20260504_111710/*/predictions.json"],
            method="Earliest prompt.txt modification time -> latest predictions.json modification time",
        ),
        PatchStageConfig(
            name="olmo-3 second round",
            start_patterns=["olmo3/validated_artifacts_20260505/*/predictions.json"],
            end_patterns=["olmo3/validated_artifacts_20260505/*/predictions.json"],
            method="Earliest -> latest predictions.json write time in validated_artifacts_20260505",
        ),
        PatchStageConfig(
            name="Sonnet 4.6",
            start_patterns=["Sonnet4.6/patches/*.patch"],
            end_patterns=["Sonnet4.6/prediction_*.json"],
            method="Earliest patches/*.patch modification time -> latest prediction_*.json modification time",
        ),
        PatchStageConfig(
            name="Kimi K2.5",
            start_patterns=["Kimi K2.5/patches/*.patch"],
            end_patterns=["Kimi K2.5/prediction_*.json"],
            method="Earliest patches/*.patch modification time -> latest prediction_*.json modification time",
        ),
        PatchStageConfig(
            name="GPT-5.4",
            start_patterns=["GPT-5.4/patches/*.patch"],
            end_patterns=["GPT-5.4/prediction_*.json"],
            method="Earliest patches/*.patch modification time -> latest prediction_*.json modification time",
        ),
    ]


def build_eval_configs() -> list[EvalStageConfig]:
    """Configure local Docker evaluation timing rules for the five experiment groups."""
    return [
        EvalStageConfig(
            name="olmo-3 first round",
            log_patterns=["olmo3/batch_20260504_111710/*/evaluation*.log"],
            parser="tqdm_seconds",
            method="Sum the final tqdm s/it values from evaluation*.log",
        ),
        EvalStageConfig(
            name="olmo-3 second round",
            log_patterns=["olmo3/validated_artifacts_20260505/*/run_instance.log"],
            parser="info_span",
            method="Sum first-to-last INFO timestamp spans from run_instance.log",
        ),
        EvalStageConfig(
            name="Sonnet 4.6",
            log_patterns=["logs/run_evaluation/sonnet46_20260509_035653_*/*/*/run_instance.log"],
            parser="info_span",
            method="Sum first-to-last INFO timestamp spans from the sonnet46_20260509_035653 batch",
        ),
        EvalStageConfig(
            name="Kimi K2.5",
            log_patterns=["logs/run_evaluation/kimi_20260509_015857_*/*/*/run_instance.log"],
            parser="info_span",
            method="Sum first-to-last INFO timestamp spans from the kimi_20260509_015857 batch",
        ),
        EvalStageConfig(
            name="GPT-5.4",
            log_patterns=["logs/run_evaluation/gpt54_20260509_030631_*/*/*/run_instance.log"],
            parser="info_span",
            method="Sum first-to-last INFO timestamp spans from the gpt54_20260509_030631 batch",
        ),
    ]


def main() -> None:
    patch_records = [calculate_patch_stage(config) for config in build_patch_configs()]
    eval_records = [calculate_eval_stage(config) for config in build_eval_configs()]

    print("Extracted Experiment Stage Timing Results")
    print("=" * 41)
    print(f"Project root: {PROJECT_ROOT}")
    print("Note: patch generation time is based on file modification times; Docker evaluation time is parsed from logs.")

    print_chart("Figure 1 Observable Span from Prompt Reading to Patch Output", patch_records)
    print_table("Figure 1 Data Table", patch_records, "Approximate wall-clock time (minutes)")

    print_chart("Figure 2 Total Local Docker Evaluation Time", eval_records)
    print_table("Figure 2 Data Table", eval_records, "Total evaluation time (minutes)")


if __name__ == "__main__":
    main()
