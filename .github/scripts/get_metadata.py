import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import quote

import requests
from packaging.version import Version

logger = logging.getLogger(__name__)
AQL_URL = "https://download.t-head.cn/artifactory/api/search/aql"
GENERIC_URL = "https://download.t-head.cn/artifactory/pypi_generic"
REQUEST_TIMEOUT = (30, 300)


@dataclass
class ArtifactoryItem:
    repo: str = ""
    path: str = ""
    name: str = ""
    type: str = ""
    size: int = 0
    created: str = ""
    created_by: str = ""
    modified: str = ""
    modified_by: str = ""
    updated: str = ""
    depth: int = 0
    actual_md5: str = ""
    actual_sha1: str = ""
    original_md5: str = ""
    sha256: str = ""
    downloads: int = 0
    remote_downloads: int = 0
    target: str = ""
    version: str = ""
    sub_version: str = ""
    sail_version: str = ""
    product: str = ""
    clean_name: str = ""
    version_suffix_holmes: str = ""

    def to_dict(self):
        fields = (
            "repo", "path", "name", "size", "created", "modified", "updated",
            "downloads", "remote_downloads", "target", "version", "sub_version",
            "sail_version", "product", "clean_name",
        )
        return {f: getattr(self, f) for f in fields}

def parse_build_version(build_version: str):
    try:
        package_version = ""
        package_sub_version = ""
        package_sdk_version = ""
        version_suffix_holmes = ""
        build_version = build_version.lower()
        if len(build_version.split("+")) > 1:
            package_version = build_version.split("+")[0]
            version_suffix = build_version.split("+")[1]
            version_suffix = version_suffix.replace(".oe", "")
            version_suffix = version_suffix.replace(".ce", "")
            version_suffix_split_by_holmes = version_suffix.split(".holmes")
            if len(version_suffix_split_by_holmes) > 1:
                version_suffix_holmes = version_suffix_split_by_holmes[1]
                version_suffix_holmes = f".holmes{version_suffix_holmes}"
            version_suffix = version_suffix_split_by_holmes[0]
            version_suffix = version_suffix.replace(".sail", "sail")
            version_suffix_split_by_sail = version_suffix.split("sail")
            if len(version_suffix_split_by_sail) == 2:
                package_sub_version = version_suffix_split_by_sail[0].replace("v", "")
                package_sdk_version = version_suffix_split_by_sail[1]
        else:
            package_version = build_version
        return package_version, package_sub_version, package_sdk_version, version_suffix_holmes
    except Exception as e:
        logger.warning("Parse build version error: %s, build version: %s", e, build_version)
        return "", "", "", ""

def get_target_data(target, items):
    pre_deal_data = {}
    for item in items:
        try:
            version = item.get("version")
            sub_version = item.get("sub_version")
            whl_name = item.get("name", "").replace(target, "")
            os = ""
            if "alios7u2" in whl_name:
                os = "alios7u2"
            elif "ubuntu2004" in whl_name:
                os = "ubuntu2004"
            elif "ubuntu2204" in whl_name:
                os = "ubuntu2204"
            elif "ubuntu2404" in whl_name:
                os = "ubuntu2404"
            else:
                raise Exception("Os error")
            _whl_name = whl_name.split(os)
            if "torch" != target and "torch" in whl_name:
                hggcrt = _whl_name[0].split('+')[1].split("torch")[0]
                pytorch = _whl_name[0].split('+')[1].split("torch")[1]
            else:
                hggcrt = _whl_name[0].split('+')[1]
                pytorch = ""
            edition_type = _whl_name[1].split('-')[0]
            python = ""
            if "cp38" in _whl_name[1]:
                python = "cp38"
            elif "cp39" in _whl_name[1]:
                python = "cp39"
            elif "cp310" in _whl_name[1]:
                python = "cp310"
            elif "cp311" in _whl_name[1]:
                python = "cp311"
            elif "cp312" in _whl_name[1]:
                python = "cp312"
            elif "cp314" in _whl_name[1]:
                python = "cp314"
            else:
                raise Exception("Python error")
            sdk_version = item.get("sail_version", "")
            key = f"{version}_{sub_version}_{os}_{python}_{hggcrt}_{pytorch}_{edition_type}"
            item["metadata"] = {"os": os, "hggcrt": hggcrt, "python": python, "version": version, "sub_version": sub_version, "pytorch": pytorch, "edition_type": edition_type, "sdk_version": sdk_version, "key": key}
            pre_deal_data.setdefault(key, []).append(item)
        except Exception as e:
            logger.warning("Failed to parse whl item: %s, item=%s", e, item)
    return pre_deal_data

def parse_version_safe(v):
    if not v or v.strip() == "":
        return Version("0.0.0")
    try:
        return Version(v)
    except:
        return Version("0.0.0")

def extract_number(s, prefix=""):
    """从类似 'cp310', 'cu126' 中提取数字"""
    if not s:
        return 0
    try:
        if prefix and s.startswith(prefix):
            return int(s[len(prefix):])
        return int(s)
    except:
        return 0

def sort_key(item):
    """
    排序键函数：返回一个 tuple, Python 会按元组的第一元素、第二元素...依次比较
    """
    return (
        # 1. 主版本号（语义化版本比较）
        parse_version_safe(item.get("version")),
        # 2. 子版本号
        parse_version_safe(item.get("sub_version")),
        # 3. Python 版本（cp310 -> 310，数字比较）
        extract_number(item.get("python"), "cp"),
        # 4. HGGCRT 版本（hggcrt3 -> 3）
        extract_number(item.get("hggcrt"), "hggcrt"),
        # 5. 操作系统字符串
        item.get("os", ""),
        # 6. 版本类型（ce/oe 等）
        item.get("edition", ""),
        # 7. Torch 版本（如果有）
        parse_version_safe(item.get("torch")),
        # 8. SDK 版本（取第一个 key 的版本号，如果为空则为 0）
        parse_version_safe(list(item.get("sdk", {}).keys())[0] if item.get("sdk") else "0.0.0")
    )

def get_target_json_data(target, data):
    releases = []
    for _, items in data.items():
        sdk_versions = {}
        _t = {}
        for item in items:
            sdk_version = item.get("metadata").get("sdk_version").replace("sail", "").replace("sail", "")
            sdk_version = sdk_version.replace(".oe", "")
            sdk_version = sdk_version.replace(".ce", "")
            try:
                _ = Version(sdk_version)
            except Exception as e:
                logger.warning("Not a valid version, sdk_version: %s, item: %s", sdk_version, item)
                continue
            if sdk_version not in sdk_versions:
                sdk_versions[sdk_version] = {"supported": [], "unsupported": []}
                _t[sdk_version] = item
            else:
                logger.debug("Duplicate sdk_version %s: existing=%s, new=%s", sdk_version, _t[sdk_version], item)
        sorted_keys = sorted(sdk_versions.keys(), key=parse_version_safe)
        sorted_sdk_versions = {k: sdk_versions[k] for k in sorted_keys}
        releases.append({
            "version": items[0].get("metadata").get("version"),
            "sub_version": items[0].get("metadata").get("sub_version"),
            "torch": items[0].get("metadata").get("pytorch"),
            "os": items[0].get("metadata").get("os"),
            "hggcrt": items[0].get("metadata").get("hggcrt"),
            "python": items[0].get("metadata").get("python"),
            "edition": items[0].get("metadata").get("edition_type"),
            "sdk": sorted_sdk_versions,
        })
    sorted_data = sorted(releases, key=sort_key)
    res = {"name": target, "release": sorted_data}
    return res

def normalize_sdk_value(value):
    """归一化 sdk 版本对应的值为 {"supported": [], "unsupported": []} 格式。
    兼容旧格式: list 视为 supported 列表; dict 补齐缺失的 key。"""
    if isinstance(value, dict):
        return {
            "supported": value.get("supported", []),
            "unsupported": value.get("unsupported", []),
        }
    if isinstance(value, list):
        return {"supported": value, "unsupported": []}
    return {"supported": [], "unsupported": []}

def merge_sdk_data(old_data, new_data):
    """用旧数据中已有的 sdk supported/unsupported 值回填新数据,
    避免覆盖仓库中已定义或手动维护过的值。"""
    key_fields = ["version", "sub_version", "torch", "os", "hggcrt", "python", "edition"]
    old_releases = {}
    for item in old_data.get("release", []):
        unique_key = tuple(item.get(k, "") for k in key_fields)
        old_releases[unique_key] = item.get("sdk", {})
    for item in new_data.get("release", []):
        unique_key = tuple(item.get(k, "") for k in key_fields)
        old_sdk = old_releases.get(unique_key, {})
        for sdk_version in item.get("sdk", {}):
            if sdk_version in old_sdk:
                item["sdk"][sdk_version] = normalize_sdk_value(old_sdk[sdk_version])
    return new_data

def get_artifacts_info(data):
    artifacts = []
    for item in data.get("results", []):
        if item.get("type") != "file":
            continue
        if ".whl" not in item.get("name"):
            continue
        artifact = ArtifactoryItem()
        artifact.repo             = item.get("repo", "")
        artifact.path             = item.get("path", "")
        artifact.name             = item.get("name", "")
        artifact.type             = item.get("type", "")
        artifact.size             = item.get("size", 0)
        artifact.created          = item.get("created", "")
        artifact.created_by       = item.get("created_by", "")
        artifact.modified         = item.get("modified", "")
        artifact.modified_by      = item.get("modified_by", "")
        artifact.updated          = item.get("updated", "")
        artifact.depth            = item.get("depth", 0)
        artifact.actual_md5       = item.get("actual_md5", "")
        artifact.actual_sha1      = item.get("actual_sha1", "")
        artifact.original_md5     = item.get("original_md5", "")
        artifact.sha256           = item.get("sha256", "")
        stats = item.get('stats') or []
        first_stat = stats[0] if stats else {}
        artifact.downloads        = first_stat.get("downloads", 0)
        artifact.remote_downloads = first_stat.get("remote_downloads", 0)
        if "/" in item.get("path", ""):
            artifact.target = item.get("path").split('/')[0]
            build_version = item.get("path").split('/')[1]
            package_version, package_sub_version, package_sdk_version, version_suffix_holmes = parse_build_version(build_version)
            artifact.version = package_version
            artifact.sub_version = package_sub_version
            artifact.sail_version = package_sdk_version
            artifact.version_suffix_holmes = version_suffix_holmes
        artifacts.append(artifact.to_dict())
    return artifacts

def normalize_target(target):
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", target
    ):
        raise ValueError("package_name 必须是合法的 Python 包名")
    return target.replace("-", "_")


def index_url(target):
    return f"{GENERIC_URL}/{quote(normalize_target(target), safe='')}/index.json"


def check_response(response):
    # requests 的 raise_for_status 不拒绝 3xx；这里也阻止重定向被当作成功。
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError(f"Artifactory 请求失败，HTTP {response.status_code}")


def get_latest_whl_info(target, username, password):
    target = normalize_target(target)
    query = {
        "repo": "pypi_generic", "path": {"$match": f"{target}/*"},
        "name": {"$match": "*.whl"},
    }
    body = ('items.find(' + json.dumps(query) + ').include("repo","path","name",'
            '"created","modified","updated","created_by","modified_by","type",'
            '"depth","original_md5","actual_md5","original_sha1","actual_sha1",'
            '"sha256","size","stat")')
    resp = requests.post(
        url=AQL_URL, auth=(username, password), headers={"Content-Type": "text/plain"},
        data=body, timeout=REQUEST_TIMEOUT, allow_redirects=False,
    )
    check_response(resp)
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Artifactory AQL 响应缺少 results 数组")
    result_range = data.get("range", {})
    if result_range.get("total", len(data["results"])) > len(data["results"]):
        raise ValueError("Artifactory AQL 结果不完整，拒绝覆盖索引")
    return data


def get_old_index_json(target, username, password):
    """保留已有 SDK 配置；仅 404 视为索引不存在，其他异常阻断更新。"""
    target = normalize_target(target)
    resp = requests.get(
        index_url(target), auth=(username, password),
        timeout=REQUEST_TIMEOUT, allow_redirects=False,
    )
    if resp.status_code == 404:
        return {}
    check_response(resp)
    data = resp.json()
    if (not isinstance(data, dict) or not isinstance(data.get("release"), list)
            or data.get("name", target) != target):
        raise ValueError("远端 index.json 格式或包名不正确，拒绝覆盖")
    return data


def get_index_json(target, username, password):
    target = normalize_target(target)
    if not username or not password:
        raise ValueError("Artifactory 用户名和密码不能为空")
    raw_data = get_latest_whl_info(target, username, password)
    artifacts = get_artifacts_info(raw_data)
    data = get_target_data(target=target, items=artifacts)
    json_data = get_target_json_data(target=target, data=data)
    old_data = get_old_index_json(target=target, username=username, password=password)
    json_data = merge_sdk_data(old_data=old_data, new_data=json_data)
    return json_data


def main():
    target = normalize_target(os.environ.get("PACKAGE_NAME", ""))
    username = os.environ.get("RELEASE_USERNAME", "")
    password = os.environ.get("RELEASE_TOKEN", "")
    data = get_index_json(target, username, password)
    if not data["release"] or not any(item.get("sdk") for item in data["release"]):
        raise ValueError("未找到有效 wheel 元数据，拒绝上传空索引")
    output = Path("index.json")
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    url = index_url(target)
    with output.open("rb") as source:
        resp = requests.put(
            url, auth=(username, password), data=source,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT, allow_redirects=False,
        )
    check_response(resp)
    print(f"已生成 index.json 并上传：{url}")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError, OSError) as error:
        raise SystemExit(f"::error::更新元数据失败：{error}") from None
