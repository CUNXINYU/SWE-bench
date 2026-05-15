from typing import Any

from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    END_TEST_OUTPUT,
    FAIL_ONLY_REPOS,
    FAIL_TO_FAIL,
    FAIL_TO_PASS,
    KEY_INSTANCE_ID,
    KEY_PREDICTION,
    MAP_REPO_VERSION_TO_SPECS,
    PASS_TO_FAIL,
    PASS_TO_PASS,
    RESET_FAILED,
    START_TEST_OUTPUT,
    TESTS_ERROR,
    TESTS_TIMEOUT,
    EvalType,
    ResolvedStatus,
    TestStatus,
)
from swebench.harness.test_spec.test_spec import TestSpec
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER


# 小节：工具函数 / Section: utility helpers
def test_passed(case: str, sm: dict[str, str]) -> bool:
    return case in sm and sm[case] in [TestStatus.PASSED.value, TestStatus.XFAIL.value]


def test_failed(case: str, sm: dict[str, str]) -> bool:
    return case not in sm or sm[case] in [
        TestStatus.FAILED.value,
        TestStatus.ERROR.value,
    ]


# 小节：评测报告 / Section: evaluation reports
def get_logs_eval(test_spec: TestSpec, log_fp: str) -> tuple[dict[str, str], bool]:
    """
    从对应日志文件中取出该任务实例的评测结果。
    Retrieve evaluation results for a task instance from its log file.

    参数 Args:
        log_fp (str): 日志文件路径 / path to log file
    返回 Returns:
        tuple: (测试名→状态映射, 是否已成功应用并可解析测试输出)
               (test-name → status map, whether patch applied and test section is parseable).

    各仓库 pytest 输出差异由 MAP_REPO_TO_PARSER 处理；接入新仓库或升级测试运行器（如 Modal 与本地 Docker）时请复核解析逻辑。
    Repo-specific pytest quirks live in MAP_REPO_TO_PARSER; re-validate parsers when adding repos or upgrading runners.
    """
    repo = test_spec.repo
    version = test_spec.version
    log_parser = MAP_REPO_TO_PARSER[repo]
    test_cmd = MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"]
    if isinstance(test_cmd, list):
        test_cmd = test_cmd[-1]

    with open(log_fp) as f:
        content = f.read()
        # 以下为 harness 写入日志的已知失败标记（补丁失败、重置失败、测试错误或超时）。
        # The following are sentinel substrings the harness writes on patch/reset failure or test abort.
        bad_codes = list(
            filter(
                lambda x: x in content,
                [
                    APPLY_PATCH_FAIL,
                    RESET_FAILED,
                    TESTS_ERROR,
                    TESTS_TIMEOUT,
                ],
            )
        )
        if bad_codes:
            return {}, False
        elif not (START_TEST_OUTPUT in content and END_TEST_OUTPUT in content):
            # 未检测到标准起止标记：通常表示测试段未产生或 patch 未生效（正常流程中应极少出现）。
            # No standard start/end markers—likely no test section or patch did not apply (rare in nominal runs).
            return {}, False

        # 截取标记之间的评测输出并解析为测试状态表。
        # Extract the bounded test output and parse into a status map.
        test_content = content.split(START_TEST_OUTPUT)[1].split(END_TEST_OUTPUT)[0]

        # 优先只解析标记区间内的内容，减少无关日志干扰。
        # Prefer parsing only the region between markers to ignore unrelated log noise.
        status_map = log_parser(test_content, test_spec)

        # 若在标记之间未解析出结果（常见于 Modal 等环境），回退为全文解析。
        # If markers yield nothing (e.g. Modal), fall back to parsing the full log.
        if not status_map:
            # stderr 上的 pytest 输出可能落在标记外，此时需全文匹配。
            # Pytest on stderr may appear outside markers; scan the whole log in that case.
            status_map = log_parser(content, test_spec)

        return status_map, True


def get_eval_tests_report(
    eval_status_map: dict[str, str],
    gold_results: dict[str, str],
    calculate_to_fail: bool = False,
    eval_type: EvalType = EvalType.PASS_AND_FAIL,
) -> dict[str, dict[str, list[str]]]:
    """
    依据金标（期望）与评测实际通过/失败的变化生成结构化报告。
    Build a structured report from gold vs. evaluated pass/fail transitions.

    Args:
        eval_status_map: 评测得到的用例名→状态字符串映射。/ Map from case name to harness status string.
        gold_results: 金标参考（含 FAIL_TO_PASS、PASS_TO_PASS 等键）。/ Gold reference with F2P/P2P buckets.
        calculate_to_fail: 是否计算 F2F / P2F 等扩展桶。/ Whether to fill F2F / P2F buckets.
        eval_type: PASS_AND_FAIL 或 FAIL_ONLY 枚举。/ PASS_AND_FAIL vs FAIL_ONLY mode.
    Returns:
        report: 各分组下 success / failure 列表。/ Dict of success/failure lists per bucket.

    指标释义（金标结果对 + 评测结果）Metric definitions (gold pair + eval):
    - Fail-Pass (F2P) + P: Success (Resolution)
    - Pass-Pass (P2P) + P: Success (Maintenance)
    - Fail-Pass (F2P) + F: Failure
    - Pass-Pass (P2P) + F: Failure

    其它 Miscellaneous:
    - Fail-Fail (F2F) + F: Failure Maintenance
    - Pass-Fail (P2F) + F: Not considered
    - Fail-Fail (F2F) + P: Success (Extra Credit)
    - Pass-Fail (P2F) + P: Not considered
    """

    def check_pass_and_fail(test_case, eval_status_map, success, failed):
        if test_passed(test_case, eval_status_map):
            # 当前未在评测状态表中出现则用 silent success 归入成功列表。
            # If absent from eval map, treat as silent success for this check.
            success.append(test_case)
        elif test_failed(test_case, eval_status_map):
            failed.append(test_case)

    def check_fail_only(test_case, eval_status_map, success, failed):
        if (
            test_case in eval_status_map
            and eval_status_map[test_case] == TestStatus.FAILED.value
        ):
            failed.append(test_case)
        else:
            success.append(test_case)

    check_test_case = (
        check_pass_and_fail if eval_type == EvalType.PASS_AND_FAIL else check_fail_only
    )

    # 计算修复指标（Fail→Pass）。
    # Resolution metrics (fail-to-pass).
    f2p_success = []
    f2p_failure = []
    for test_case in gold_results[FAIL_TO_PASS]:
        check_test_case(test_case, eval_status_map, f2p_success, f2p_failure)

    # 计算回归保持指标（Pass→Pass）。
    # Maintenance metrics (pass-to-pass).
    p2p_success = []
    p2p_failure = []
    for test_case in gold_results[PASS_TO_PASS]:
        check_test_case(test_case, eval_status_map, p2p_success, p2p_failure)

    results = {
        FAIL_TO_PASS: {
            "success": f2p_success,
            "failure": f2p_failure,
        },
        PASS_TO_PASS: {
            "success": p2p_success,
            "failure": p2p_failure,
        },
    }

    f2f_success = []
    f2f_failure = []
    p2f_success = []
    p2f_failure = []
    if calculate_to_fail:
        # “附加分”维度：仍为失败但评测行为发生变化的用例。
        # Extra-credit buckets: failures whose eval behavior still shifts.
        for test_case in gold_results[FAIL_TO_FAIL]:
            check_test_case(test_case, eval_status_map, f2f_success, f2f_failure)

        # 明确定义为不参与主指标的用例分组。
        # Groups explicitly excluded from primary scoring.
        for test_case in gold_results[PASS_TO_FAIL]:
            check_test_case(test_case, eval_status_map, p2f_success, p2f_failure)

    results.update(
        {
            FAIL_TO_FAIL: {
                "success": f2f_success,
                "failure": f2f_failure,
            },
            PASS_TO_FAIL: {
                "success": p2f_success,
                "failure": p2f_failure,
            },
        }
    )
    return results


def compute_fail_to_pass(report: dict[str, dict[str, Any]]) -> float:
    """
    计算 Fail→Pass（修复成功率）比例；若无相关用例则返回 1.0。
    Compute fail-to-pass ratio; returns 1.0 when no such tests apply.
    """
    total = len(report[FAIL_TO_PASS]["success"]) + len(report[FAIL_TO_PASS]["failure"])
    if total == 0:
        return 1
    return len(report[FAIL_TO_PASS]["success"]) / total


def compute_pass_to_pass(report: dict[str, dict[str, Any]]) -> float:
    """
    计算 Pass→Pass（无回归成功率）比例。
    Compute pass-to-pass (no-regression) ratio.

    当报告中没有 P2P 用例时返回 1.0，以免把「无维护测试」误判为维护失败（见 get_resolution_status）。
    When no P2P tests exist we return 1.0 so "no maintenance tests" does not break resolution (see get_resolution_status).
    """
    total = len(report[PASS_TO_PASS]["success"]) + len(report[PASS_TO_PASS]["failure"])
    if total == 0:
        # neutral：与本报告片中其它指标可比的中性得分。
        # Neutral score comparable to slices that do include maintenance tests.
        return 1
    return len(report[PASS_TO_PASS]["success"]) / total


def get_resolution_status(report: dict[str, dict[str, Any]]) -> str:
    """
    根据 F2P / P2P 比例判定该实例是否为 FULL / PARTIAL / NO。
    Decide FULL / PARTIAL / NO resolved status from F2P and P2P.

    判定准则 Criteria:
        - If fail-to-pass (Resolution) = 1 and pass-to-pass (Maintenance) = 1 -> FULL
        - If (fail-to-pass (Resolution) < 1 and > 0) and pass-to-pass (Maintenance) = 1 -> PARTIAL
        - Otherwise -> NO
    """
    f2p = compute_fail_to_pass(report)
    p2p = compute_pass_to_pass(report)

    if f2p == 1 and p2p == 1:
        return ResolvedStatus.FULL.value
    elif f2p < 1 and f2p > 0 and p2p == 1:
        return ResolvedStatus.PARTIAL.value
    else:
        return ResolvedStatus.NO.value


def get_eval_report(
    test_spec: TestSpec,
    prediction: dict[str, str],
    test_log_path: str,
    include_tests_status: bool,
) -> dict[str, Any]:
    """
    综合预测、任务规格与评测日志，生成模型在该实例上的结构化评测报告。
    Build a structured eval report from prediction, task spec, and harness log.

    参数 Args:
        test_spec:
            含 instance_id / FAIL_TO_PASS / PASS_TO_PASS 等字段的测试规格字典。
            Test spec dict including instance_id, FAIL_TO_PASS, PASS_TO_PASS, etc.
        prediction:
            含 instance_id、model_name_or_path、model_patch 的预测条目。
            Prediction dict including instance_id, model_name_or_path, model_patch.
        test_log_path: 评测日志路径 / Path to harness evaluation log.
        include_tests_status:
            为 True 时在报告中附带每项测试的明细状态。
            If True, attach per-test status breakdown to the returned report dict.
    返回 Returns:
        结构化指标字典；键为实例 ID。
        Structured metrics keyed by instance_id.
    """
    report_map = {}

    instance_id = prediction[KEY_INSTANCE_ID]
    report_map[instance_id] = {
        "patch_is_None": False,
        "patch_exists": False,
        "patch_successfully_applied": False,
        "resolved": False,
    }

    # 判断模型 patch 是否存在（可能为空的预测）。
    # Whether the submission contains a concrete patch chunk.
    if prediction[KEY_PREDICTION] is None:
        report_map[instance_id]["patch_is_None"] = True
        return report_map
    report_map[instance_id]["patch_exists"] = True

    # 解析评测日志，得到逐项测试状态映射。
    # Parse harness log into per-test statuses.
    eval_status_map, found = get_logs_eval(test_spec, test_log_path)

    if not found:
        return report_map
    report_map[instance_id]["patch_successfully_applied"] = True

    eval_ref = {
        KEY_INSTANCE_ID: test_spec.instance_id,
        FAIL_TO_PASS: test_spec.FAIL_TO_PASS,
        PASS_TO_PASS: test_spec.PASS_TO_PASS,
    }

    # FAIL_ONLY_REPOS 仅考察失败分支；其余仓库启用完整通过/失败统计。
    # FAIL_ONLY_REPOS skips pass maintenance checks; otherwise use PASS_AND_FAIL.
    eval_type = (
        EvalType.FAIL_ONLY
        if test_spec.repo in FAIL_ONLY_REPOS
        else EvalType.PASS_AND_FAIL
    )

    report = get_eval_tests_report(eval_status_map, eval_ref, eval_type=eval_type)
    if get_resolution_status(report) == ResolvedStatus.FULL.value:
        report_map[instance_id]["resolved"] = True

    if include_tests_status:
        # typing：动态附加明细字段时使用 ignore。
        # type: ignore keeps mypy permissive when attaching optional tests_status.
        report_map[instance_id]["tests_status"] = report  # type: ignore

    return report_map
