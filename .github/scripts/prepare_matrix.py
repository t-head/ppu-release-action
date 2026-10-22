import itertools
import json
import math
import os
import re
import subprocess

from packaging.version import InvalidVersion, Version

IMAGES_MAP = {
    "ubuntu2404-py312-sdk2.2.0-hggcrt3-torch2.13.0": "download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.13.0-ubuntu24.04-hggcrt3-py312-20261005",
    "ubuntu2404-py312-sdk2.2.0-hggcrt3-torch2.10.0": "download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.10.0-ubuntu24.04-hggcrt3-py312-20261005",
}

def fail(message):
    raise SystemExit(f"::error::{message}")


def parse_json(raw, label):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        fail(f"{label} 必须是合法 JSON")


def get_torch_url(
    python_version: str,
    os_version: str,
    runtime_version: str,
    torch_version: str,
    sdk_version: str,
) -> str:
    """根据五个版本获取 Torch 下载地址，暂返回空字符串作为占位。"""
    # TODO: 实现 Torch 下载地址查询逻辑。
    return ""


def get_sdk_url(os_version: str, runtime_version: str, sdk_version: str) -> str:
    """根据 OS、Runtime 和 SDK 版本获取 SDK 下载地址，暂返回空字符串作为占位。"""
    # TODO: 实现 SDK 下载地址查询逻辑。
    return ""


def get_build_tag(tag_version=""):
    """优先使用指定版本；所有来源均过滤 rc，并统一去除开头的 v/V。"""
    version_pattern = r"[vV]?[0-9][A-Za-z0-9.!_]*(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?"
    if tag_version != "":
        # 同时校验发布路径安全性与 PEP 440，非法覆盖值不回退到仓库版本。
        if not isinstance(tag_version, str) or not re.fullmatch(version_pattern, tag_version):
            fail("tag_version 必须是合法版本号且符合 tag 格式，例如 1.0.0 或 1.0.0+v0.3.0")
        try:
            Version(tag_version)
        except InvalidVersion:
            fail("tag_version 必须是合法的 PEP 440 版本号，例如 1.0.0 或 1.0.0+v0.3.0")
        if "rc" in tag_version.lower():
            return None
        tag = tag_version
    elif os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if "rc" in tag.lower():
            return None
    else:
        source_dir = os.environ.get("SOURCE_DIR")
        if not source_dir:
            fail("分支构建必须通过 SOURCE_DIR 指定调用方仓库目录")
        try:
            result = subprocess.run(
                ["git", "-C", source_dir, "tag", "--points-at", "HEAD", "--sort=-version:refname"],
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            fail("无法读取调用方仓库当前提交的 tag")
        # 多个 tag 按 Git 版本降序选择首个合格版本，不回退到历史提交。
        tag = next((tag for tag in result.stdout.splitlines()
                    if re.fullmatch(version_pattern, tag) and "rc" not in tag.lower()), None)
        if tag is None:
            return None
    if not re.fullmatch(version_pattern, tag):
        fail("tag 必须是版本号，例如 1.0.0+v0.3.0")
    # 仅去除整个版本开头的一个前缀，保留 +v0.3.0 等内部内容。
    return re.sub(r"^[vV]", "", tag)


def main():
    # 必须先读取覆盖参数，再按最终选定版本应用发布门禁。
    inputs = parse_json(os.environ["INPUTS_JSON"], "触发参数")
    if not isinstance(inputs, dict):
        fail("触发参数 必须是 JSON 对象")
    tag = get_build_tag(inputs.get("tag_version", ""))
    should_release = tag is not None
    if not should_release:
        print("tag 包含 rc（忽略大小写），或当前提交没有合格版本 tag，仅构建 wheel 并保存 Actions artifact，不发布")

    package_name = inputs.get("package_name", "")
    if not isinstance(package_name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", package_name
    ):
        fail("package_name 必须是合法的 Python 包名")
    # wheel 分发名称中的分隔符统一为下划线。
    wheel_package_name = re.sub(r"[-_.]+", "_", package_name)
    candidates = {
        "python_versions": {"38", "310", "312"},
        "os_versions": {"ubuntu2004", "ubuntu2204", "ubuntu2404", "alios7u2"},
        "runtime_versions": {"2", "3"},
        "torch_versions": {"", "2.8.0", "2.10.0", "2.13.0"},
        "sdk_versions": {"2.1.0", "2.2.0"},
    }
    axes = {}
    for name, allowed in candidates.items():
        values = parse_json(inputs.get(name, ""), name)
        if not isinstance(values, list) or not values:
            fail(f"{name} 必须是非空 JSON 数组")
        normalized = []
        for value in values:
            # Python 版本同时支持 [38,310] 和 ["38","310"]。
            if name == "python_versions" and type(value) is int:
                value = str(value)
            # 空 Torch 版本用 [""] 表示，其他轴仍要求非空版本。
            empty_torch = name == "torch_versions" and value == ""
            if not empty_torch and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", value)):
                fail(f"{name} 的元素必须是非空版本字符串，仅允许字母、数字及 . _ + -；torch_versions 可使用空字符串")
            if allowed is not None and value not in allowed:
                fail(f"{name} 包含不支持的值 {value}；候选值：{', '.join(sorted(allowed))}")
            if value not in normalized:
                normalized.append(value)
        axes[name.removesuffix("s")] = normalized

    count = math.prod(len(values) for values in axes.values())
    if count > 256:
        fail(f"矩阵包含 {count} 个任务，超过 GitHub Actions 的 256 个任务限制，请缩小版本范围")

    for os_version, python_version, sdk_version, runtime_version, torch_version in itertools.product(axes["os_version"], axes["python_version"], axes["sdk_version"], axes["runtime_version"], axes["torch_version"]):
        key = f"{os_version}-py{python_version}-sdk{sdk_version}-hggcrt{runtime_version}-torch{torch_version}"
        image = IMAGES_MAP.get(key)
        if not isinstance(image, str) or not image or re.search(r"\s", image):
            fail(f"BUILD_CONTAINER_IMAGES 缺少 {key} 或镜像地址为空/包含空白字符")

    # tag 已统一去除 v/V 前缀；release 保留本地版本，wheel 只取 + 前的版本。
    wheel_version = tag.split("+", 1)[0] if should_release else ""
    combinations = []
    for values in itertools.product(*axes.values()):
        entry = dict(zip(axes, values))
        key = f"{entry['os_version']}-py{entry['python_version']}-sdk{entry['sdk_version']}-hggcrt{entry['runtime_version']}-torch{entry['torch_version']}"
        entry["image"] = IMAGES_MAP.get(key)
        # 非发布构建不生成发布版本和目标文件名，保留构建命令产出的原始 wheel。
        entry.update(tag="", release_version="", whl_name="")
        if should_release:
            # 记录最终版本（指定的 tag_version 或仓库 tag），供发布数据汇总使用。
            entry["tag"] = tag
            entry["release_version"] = f"{tag}.sail{entry['sdk_version']}"
            if "+" not in tag:
                entry["release_version"] = f"{tag}+sail{entry['sdk_version']}"
            torch_suffix = f"torch{entry['torch_version']}" if entry["torch_version"] else ""
            python_tag = f"cp{entry['python_version']}"
            entry["whl_name"] = (
                f"{wheel_package_name}-{wheel_version}+hggcrt{entry['runtime_version']}"
                f"{torch_suffix}{entry['os_version']}oe-{python_tag}-{python_tag}-linux_x86_64.whl"
            )
        # entry["torch_url"] = get_torch_url(
        #     python_version=entry["python_version"],
        #     os_version=entry["os_version"],
        #     runtime_version=entry["runtime_version"],
        #     torch_version=entry["torch_version"],
        #     sdk_version=entry["sdk_version"],
        # )
        # entry["sdk_url"] = get_sdk_url(
        #     os_version=entry["os_version"],
        #     runtime_version=entry["runtime_version"],
        #     sdk_version=entry["sdk_version"],
        # )
        combinations.append(entry)

    matrix = json.dumps({"include": combinations}, separators=(",", ":"))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"matrix={matrix}\n")
        output.write(f"should_release={str(should_release).lower()}\n")
    print(f"已生成 {count} 个矩阵任务")


if __name__ == "__main__":
    main()
