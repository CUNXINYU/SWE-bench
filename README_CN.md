
## 本次实验流程

**环境依赖与前置条件**

复现下文流水线前，请在本机预先安装并配置好下列组件（按你要跑的实验组别取舍即可）：

1. **WSL 2 与 Ubuntu 发行版**：Modal 评估及需在 Linux 下跑的 harness 常用；Windows 原生 Python 缺少 `resource` 模块时，也建议在 WSL 内执行相关命令。
2. **Docker（Docker Desktop 或 Docker Engine）**：本地 SWE-bench Docker harness、`docker build` / `docker run`、镜像缓存与评测容器依赖此项。
3. **Python 虚拟环境**：在 Windows 或 WSL 中为该仓库创建 `venv` / `.venv-modal` 等，执行 `pip install -e .` 安装本仓库，并按各脚本补充依赖。
4. **sb-cli**：SWE-bench 官方云端评测命令行工具；需完成安装、账号与配置后方可提交与拉取报告。
5. **Modal**：云端评测备选路径；通常在 WSL 的虚拟环境中安装 `modal` 与 `swebench[modal]`，并执行 `modal setup` 完成登录。
6. **Ollama**：本地大模型推理服务；用于拉取并托管 **olmo-3（约 7B）** 等模型；监听地址与端口以你的安装为准（实验中可通过环境变量指向 API）。
7. **olmo-3（7B 量级）**：在 Ollama 中拉取可用的 olmo-3 变体（例如 `olmo-3:latest`，名称以官方/Ollama 库为准），保证磁盘与显存/内存满足推理要求。
8. **云端大模型 API（按需）**：若进行 Kimi K2.5、GPT-5.4、Sonnet 4.6 等补丁生成或评测，还需对应平台的 API 密钥、计费与网络可达性。

此外建议预先安装 **Git**，Python 版本满足仓库要求，并为 Docker 镜像与 `logs/`、`eval_logs/` 等预留**足够磁盘空间**。


本次实验流程如下。PowerShell 示例假定当前目录已为仓库根目录；WSL 下请先 `cd` 到同一克隆目录（挂载路径因机器而异），再执行 bash 示例。

实验分为五组：第一次本地大模型实验、第二次本地大模型实验、Kimi K2.5 云端 API 大模型实验、GPT-5.4 云端 API 大模型实验、Sonnet 4.6 云端 API 大模型实验。每组都包含「生成修复补丁」和「评估补丁」两个阶段。

**1. 第一次本地大模型实验：olmo-3 10 实例批量实验**

第一次本地大模型实验不是只完成 `astropy__astropy-14309` 一个实例，而是使用 `run_olmo3_batch10.py` 对以下 10 个实例进行批量补丁生成和本地 Docker 评估：

astropy__astropy-14309  
astropy__astropy-14995  
astropy__astropy-7166  
astropy__astropy-7336  
django__django-10097  
django__django-10880  
django__django-10914  
django__django-10999  
django__django-11066  
pytest-dev__pytest-10051  

该阶段使用本地 Ollama 中的 `olmo-3:latest` 生成补丁，并调用 SWE-bench Docker harness 做本地评估。正式批量运行脚本：

```powershell
python .\run_olmo3_batch10.py
```

这个脚本完成的事情是：

1. 从 `princeton-nlp/SWE-bench` 的 `test` split 读取上述 10 个实例。
2. 为每个实例构造 prompt，并调用本地 Ollama 接口生成 `git diff` 补丁。
3. 从模型输出中提取 `diff --git`，统一换行格式，并检查 `---`、`+++`、`@@` 等 diff 结构。
4. 使用对应仓库的 `base_commit` 执行 `git apply --check` 预检查。
5. 每个实例写出 `prompt.txt`、`ollama_raw_attempt_*.txt`、`attempts.json`、`model_patch_preview.diff`、`predictions.json` 和 `run_summary.json`。
6. 当 10 个实例都通过预检查后，合并为 `predictions_all.json`，再统一调用本地 Docker 版 SWE-bench harness 评估。

如果 Ollama 服务不是默认地址，可以先在 PowerShell 中设置环境变量：

```powershell
$env:OLLAMA_URL = "http://127.0.0.1:<OLLAMA_PORT>/api/generate"
python .\run_olmo3_batch10.py
```

（将 `<OLLAMA_PORT>` 换成你的 Ollama 监听端口，本机默认常为 `11434`。）

第一次本地大模型批量实验的结果文件主要保存在：`olmo3/batch_precheck_*`、`olmo3/batch_precheck_*/predictions_all.json`、`olmo3/batch_precheck_*/batch_summary.json`、`olmo3/batch_precheck_*/evaluation_all.log`、`olmo3/batch_precheck_*/evaluation_report.json`。

其中 `run_local_verified_olmo3.py` 是更早的单实例端到端验证脚本，主要用于验证本地 `olmo-3:latest`、Ollama 接口和 Docker 评估链路是否可用；正式的第一次本地大模型 10 实例实验以 `run_olmo3_batch10.py` 为准。

**本组报告文件位置（相对仓库根）：** 汇总类见 `olmo3/batch_precheck_*/evaluation_report.json`、`olmo3/batch_precheck_*/batch_summary.json`、`olmo3/batch_precheck_*/evaluation_all.log`；合并预测见 `olmo3/batch_precheck_*/predictions_all.json`。若 SWE-bench harness 同时在仓库根写出评测产物，还可能在 `logs/run_evaluation/`、`logs/build_images/`（具体随 `run_id` 与脚本配置而定）。

**2. 第二次本地大模型实验：olmo-3 Astropy 10 实例**

第二次本地大模型实验选择的是以下 10 个 Astropy 实例：

astropy__astropy-12907  
astropy__astropy-13033  
astropy__astropy-13236  
astropy__astropy-13453  
astropy__astropy-13977  
astropy__astropy-14096  
astropy__astropy-14182  
astropy__astropy-14365  
astropy__astropy-14508  
astropy__astropy-14539  

本组实验不是 `run_olmo3_batch10.py` 那组实例，而是 Astropy-only 的第二次实例选择。相关文件目录：`olom-3-v2/astropy10_second_instance_selection/`。

生成原始模型输出：

```powershell
python .\run_olmo3_astropy10_fixed_logs.py
```

若需从中断位置继续：

```powershell
python .\continue_astropy10_batch.py
```

这两个脚本会调用本地 `olmo-3:latest`，读取 10 个 Astropy 实例的题目描述，构造 prompt，让模型输出修复思路或补丁内容，并保存每个实例的原始输出、prompt、metadata、attempts 和 run summary。

由于这组实验中 olmo-3 的原始输出有些不是标准 `git diff`，后面再运行模型输出转换脚本：

```powershell
python .\olom-3-v2\astropy10_second_instance_selection\build_model_converted_patches.py
```

可转换的补丁输出目录：`olom-3-v2/astropy10_second_instance_selection/model_converted_gitdiff/`。核心文件：`model_converted_gitdiff/preds_model_converted.json`、`model_converted_gitdiff/preds_model_converted.jsonl`、`model_converted_gitdiff/converted_manifest.json`、`model_converted_gitdiff/patches/*.patch`。

本地 Docker 评估：

```powershell
python .\olom-3-v2\astropy10_second_instance_selection\evaluate_model_converted_patches.py
```

评估日志与报告：`olom-3-v2/astropy10_second_instance_selection/logs_model_converted/`。

`converted_manifest.json` 中记录了每个实例是否成功从模型原始输出转换为补丁。其中 `astropy__astropy-13033` 和 `astropy__astropy-13453` 被标记为 `unconvertible`，因为模型输出和真实问题不匹配。

**本组报告文件位置（相对仓库根）：** 评测日志与 harness 产物在 `olom-3-v2/astropy10_second_instance_selection/logs_model_converted/` 下按运行目录展开；典型路径含 `.../logs_model_converted/<run_dir>/<instance_id>/report.json`、`summary.json`，同一 `<run_dir>` 下还可能有 `evaluation_summary.json`（命名以你本地实际运行目录为准）。

**3. Kimi K2.5 云端 API 大模型实验**

补丁生成辅助脚本：

```powershell
python .\generate_kimi_patches_v2.py
```

输出目录与文件：`Kimi K2.5/patches/`、`Kimi K2.5/predictions.json`。

本地 Docker 评估：

```powershell
python .\evaluate_kimi_patches.py
```

评估结果：`Kimi K2.5/eval_logs/`。

如需直接调用 harness：

```powershell
python -m swebench.harness.run_evaluation `
  --dataset_name "Kimi K2.5/dataset_subset.json" `
  --split test `
  --predictions_path "Kimi K2.5/predictions.json" `
  --max_workers 1 `
  --timeout 3600 `
  --run_id kimi-k25-local-20260509 `
  --namespace none `
  --cache_level env `
  --clean True
```

sb-cli 报告：`Kimi K2.5/cloud_eval/`、仓库根目录 `sb-cli-reports/`。Modal 报告：`Modal_cloud_eval/kimi-k2.5-cloud.kimi-k25-modal-20260509.json`。

**本组报告文件位置（相对仓库根）：** 本地 Docker 评测：汇总见 `Kimi K2.5/eval_logs/eval_<时间戳>/FINAL_SUMMARY.json`、`evaluation_summary.json`（若存在）；逐实例 harness 报告见 `Kimi K2.5/eval_logs/eval_<时间戳>/<instance_id>/report.json`。sb-cli：`Kimi K2.5/cloud_eval/` 内 JSON 报告及仓库根 `sb-cli-reports/`。Modal：`Modal_cloud_eval/kimi-k2.5-cloud.kimi-k25-modal-20260509.json`。

**4. GPT-5.4 云端 API 大模型实验**

补丁与预测：`GPT-5.4/patches/`、`GPT-5.4/predictions.json`、`GPT-5.4/predictions.jsonl`。

本地 Docker 评估：

```powershell
python .\evaluate_gpt54_patches.py
```

评估结果：`GPT-5.4/eval_logs/`。

Harness 示例：

```powershell
python -m swebench.harness.run_evaluation `
  --dataset_name "GPT-5.4/dataset_subset.json" `
  --split test `
  --predictions_path "GPT-5.4/predictions.json" `
  --max_workers 1 `
  --timeout 3600 `
  --run_id gpt54-local-20260509 `
  --namespace none `
  --cache_level env `
  --clean True
```

sb-cli 报告：`GPT-5.4/cloud_eval/`。Modal 报告：`Modal_cloud_eval/gpt-5.4.gpt54-modal-20260509.json`。

**本组报告文件位置（相对仓库根）：** 本地 Docker 评测：汇总见 `GPT-5.4/eval_logs/eval_<时间戳>/FINAL_SUMMARY.json`、`evaluation_summary.json`（若存在）；逐实例见 `GPT-5.4/eval_logs/eval_<时间戳>/<instance_id>/report.json`。sb-cli：`GPT-5.4/cloud_eval/` 内 JSON 报告及仓库根 `sb-cli-reports/`。Modal：`Modal_cloud_eval/gpt-5.4.gpt54-modal-20260509.json`。

**5. Sonnet 4.6 云端 API 大模型实验**

补丁与预测：`Sonnet4.6/patches/`、`Sonnet4.6/predictions.json`、`Sonnet4.6/predictions.jsonl`。

本地 Docker 评估：

```powershell
python .\evaluate_sonnet46_patches.py
```

评估结果：`Sonnet4.6/eval_logs/`。

Harness 示例：

```powershell
python -m swebench.harness.run_evaluation `
  --dataset_name "Sonnet4.6/dataset_subset.json" `
  --split test `
  --predictions_path "Sonnet4.6/predictions.json" `
  --max_workers 1 `
  --timeout 3600 `
  --run_id sonnet46-local-20260509 `
  --namespace none `
  --cache_level env `
  --clean True
```

sb-cli 报告：`Sonnet4.6/cloud_eval/`。Modal 报告：`Modal_cloud_eval/sonnet-4.6.sonnet46-modal-20260509.json`。

**本组报告文件位置（相对仓库根）：** 本地 Docker 评测：汇总见 `Sonnet4.6/eval_logs/eval_<时间戳>/FINAL_SUMMARY.json`、`evaluation_summary.json`（若存在）；逐实例见 `Sonnet4.6/eval_logs/eval_<时间戳>/<instance_id>/report.json`。sb-cli：`Sonnet4.6/cloud_eval/` 内 JSON 报告及仓库根 `sb-cli-reports/`。Modal：`Modal_cloud_eval/sonnet-4.6.sonnet46-modal-20260509.json`。

**6. 云端 API 大模型的 sb-cli 与 Modal 评估指令**

前置：`sb-cli` 已安装且可在 PowerShell 中调用，例如在仓库根目录执行：

```powershell
sb-cli --help
```

Modal 因在 Windows 上缺少 `resource` 模块，通常在 WSL Ubuntu 中运行；在仓库根目录下：

```bash
source .venv-modal/bin/activate
modal setup
```

若尚未创建虚拟环境：

```bash
python3 -m venv .venv-modal
source .venv-modal/bin/activate
python -m pip install --upgrade pip
python -m pip install modal "swebench[modal]"
modal setup
```

**说明：** 第 6 节各小节（6.1–6.6）在命令示例之后均附有「本组报告文件位置」，便于对照下载或生成的 JSON。

**6.1 Kimi K2.5 的 sb-cli 云评估**

```powershell
sb-cli submit swe-bench_lite test --predictions_path "Kimi K2.5/predictions.json" --run_id "kimi-k25-lite-test-retry-20260509" --output_dir "Kimi K2.5/cloud_eval"
```

若轮询阶段因代理或 VPN 出现 `/poll-jobs` 相关 SSL 错误，可直接拉取报告：

```powershell
sb-cli get-report swe-bench_lite test kimi-k25-lite-test-retry-20260509 --output_dir "Kimi K2.5/cloud_eval" --overwrite 1
```

**本组报告文件位置：** `Kimi K2.5/cloud_eval/swe-bench_lite__test__kimi-k25-lite-test-retry-20260509.json`（亦可在仓库根 `sb-cli-reports/` 查找同名或相近命名的拉取结果）。

**6.2 GPT-5.4 的 sb-cli 云评估**

```powershell
sb-cli submit swe-bench_lite test --predictions_path "GPT-5.4/predictions.json" --run_id "gpt54-lite-20260509" --instance_ids "astropy__astropy-6938,astropy__astropy-7746,django__django-11964,django__django-11999,django__django-12113,django__django-12125,django__django-12184,django__django-12284,django__django-12286,django__django-12308" --output_dir "GPT-5.4/cloud_eval"
```

**本组报告文件位置：** `GPT-5.4/cloud_eval/swe-bench_lite__test__gpt54-lite-20260509.json`（亦可在仓库根 `sb-cli-reports/` 查找）。

**6.3 Sonnet 4.6 的 sb-cli 云评估**

```powershell
sb-cli submit swe-bench_lite test --predictions_path "Sonnet4.6/predictions.json" --run_id "sonnet46-lite-20260509" --instance_ids "astropy__astropy-6938,astropy__astropy-7746,django__django-11964,django__django-11999,django__django-12113,django__django-12125,django__django-12184,django__django-12284,django__django-12286,django__django-12308" --output_dir "Sonnet4.6/cloud_eval"
```

**本组报告文件位置：** `Sonnet4.6/cloud_eval/swe-bench_lite__test__sonnet46-lite-20260509.json`（亦可在仓库根 `sb-cli-reports/` 查找）。

**6.4 Kimi K2.5 的 Modal 云评估**

（当前目录为仓库根目录。）

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path "Kimi K2.5/predictions.json" \
  --run_id "kimi-k25-modal-20260509" \
  --parallelism 10 \
  --modal true
```

若不支持 `--parallelism`，改用：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path "Kimi K2.5/predictions.json" \
  --run_id "kimi-k25-modal-20260509" \
  --max_workers 10 \
  --modal true
```

复制报告到统一目录：

```bash
mkdir -p Modal_cloud_eval
cp kimi-k2.5-cloud.kimi-k25-modal-20260509.json Modal_cloud_eval/
```

**本组报告文件位置：** `Modal_cloud_eval/kimi-k2.5-cloud.kimi-k25-modal-20260509.json`（若未复制，可在仓库根查找同名 `kimi-k2.5-cloud*.json` 再移到 `Modal_cloud_eval/`）。

**6.5 GPT-5.4 的 Modal 云评估**

注意 `--max_workers 10 \` 行末反斜杠不可漏，否则 `--modal true` 不会传给 Python。

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path "GPT-5.4/predictions.json" \
  --run_id "gpt54-modal-20260509" \
  --max_workers 10 \
  --modal true
```

```bash
mkdir -p Modal_cloud_eval
cp gpt-5.4.gpt54-modal-20260509.json Modal_cloud_eval/
```

**本组报告文件位置：** `Modal_cloud_eval/gpt-5.4.gpt54-modal-20260509.json`（若未复制，可在仓库根查找同名 `gpt-5.4*.json` 再移到 `Modal_cloud_eval/`）。

**6.6 Sonnet 4.6 的 Modal 云评估**

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --split test \
  --predictions_path "Sonnet4.6/predictions.json" \
  --run_id "sonnet46-modal-20260509" \
  --max_workers 10 \
  --modal true
```

```bash
mkdir -p Modal_cloud_eval
cp sonnet-4.6.sonnet46-modal-20260509.json Modal_cloud_eval/
```

若复制失败，可先列出报告文件名：

```bash
ls -lh *sonnet*modal*.json
```

**本组报告文件位置：** `Modal_cloud_eval/sonnet-4.6.sonnet46-modal-20260509.json`（若未复制，可在仓库根用上述 `ls` 找到实际文件名后再复制到 `Modal_cloud_eval/`）。

**7. 三种评估方式的对应关系**

1. 本地 Docker 评估：通过 `evaluate_kimi_patches.py`、`evaluate_gpt54_patches.py`、`evaluate_sonnet46_patches.py`、`evaluate_model_converted_patches.py` 或 `python -m swebench.harness.run_evaluation` 执行。
2. sb-cli 云端评估：最终报告保存在各模型的 `cloud_eval/` 与仓库根的 `sb-cli-reports/`。
3. Modal 云端评估：最终报告统一在 `Modal_cloud_eval/`。

本实验中的 sb-cli 云端评估最终表现为 `0 completed / 10 failed runs`。在排除 API、环境与配额等因素后，该结果更适合记录 sb-cli 服务端或后端基础设施异常，不宜当作模型补丁语义失败统计。

复现时请先确认对应模型的 `predictions.json` 或 `preds_model_converted.json` 已生成，再运行本地 Docker 评估脚本；如需云端评估，用同一份预测提交 sb-cli 或 Modal，并将报告保存到上述相对路径。

**本小结报告索引：** 第 1–5 组实验的报告路径见各组末尾「本组报告文件位置」；sb-cli 与 Modal 的专项报告见第 6.1–6.6 节末尾同名条目。
