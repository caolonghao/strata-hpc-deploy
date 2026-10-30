# Strata HPC deployment helpers

Git 同步脚本、配置模板和小型制品清单；Docker Hub 传输权重、源码归档等大文件。
本仓库仅提供文件传输工具，不包含权重，也不代表 Strata 已编译或推理验证成功。

需要 Python 3.12+、Docker CLI 和可用的 Docker daemon。Python 脚本仅用标准库，无 pip 依赖。
`config.example.json` 是路径和参数约定的示例，CLI 使用显式参数，不会自动加载该文件。

## 服务器同步脚本

```sh
git clone --depth 1 https://github.com/caolonghao/strata-hpc-deploy.git
cd strata-hpc-deploy
# 后续更新
git pull --ff-only
```

远端 Git 受限时可以使用 `timeout 120 git ...`，超时后保留日志并重试。

## 接收现有 Qwen MTP 权重

```sh
python scripts/receive.py manifests/qwen-mtp-rt-20261030.json \
  --dest ./assets/qwen-mtp-rt-20261030 --workers 2
```

脚本按清单中的 digest 拉取 6 组镜像，只创建停止的容器以复制文件，不启动容器。
已校验分片会跳过，Docker 会复用已完成的层；拉取失败会重试。
下载日志在目标目录的 `pull-bNN.log`。全部分片校验后合并到 `.uploading`，
核对完整 SHA-256 后重命名，再安全解压并核对包内 `SHA256SUMS`。
结果记录在 `TRANSFER-VERIFIED.json`，数据位于：

```text
assets/qwen-mtp-rt-20261030/extracted/strata-mtp-rebuild-20261030/mtp/rt/
```

压缩包大小 688,088,788 字节，SHA-256：
`a1040f6559bdb7ffc08cc1c52571c5dc7b3e50ce16082a21621c71dda85c01f6`。
它只含 Qwen MTP runtime 数据及 CJK 词表，不含主模型、引擎或 CUDA 库。
需把兼容引擎的 MTP 参数指向解压后的 `rt` 目录；不自动覆盖现有模型或启动推理。

若接收在解压阶段被中断，会保留 `extracted.uploading` 供检查；确认内容后将其移走再重试。
已存在的 `extracted` 会重新校验包内文件，校验不通过时停止。

## 本机发布新的大文件

登录自己的 Docker Hub，并确认归档适合上传到目标仓库；公开仓库中的文件可被任何人下载。
工作目录必须是新目录，且不能把实际分片加入 Git。

```sh
python scripts/publish.py /path/to/archive.tar.gz \
  --repository YOUR_DOCKERHUB_ACCOUNT/strata-transfer \
  --tag-prefix release-YYYYMMDD \
  --work-dir ./work/release-YYYYMMDD \
  --manifest-out ./manifests/release-YYYYMMDD.json \
  --checksum-file package-root/SHA256SUMS
```

默认每片 2 MiB，每组 60 个文件层，构建文件均从 `scratch` 开始。
发布成功后只把 `manifests/*.json` 和脚本更新提交 Git；远端 `git pull` 后接收对应清单即可。
若上传中断，可在对应 `work/.../batch-NN` 目录用 Docker 重推已构建的同名镜像；
必须收齐各组 `published.json` 的 digest 才能发布完整清单。

## 配置与验证

实际机器路径、账号凭证、网络配置和现场调查记录保存在各机器本地。
`.gitignore` 已排除本地配置、日志、权重和归档；提交前仍需检查 staged diff。

```sh
python -m unittest discover -s tests -v
```

测试验证损坏分片不能发布为完整归档、路径越界被拒绝、正常包可解压并重复校验。
