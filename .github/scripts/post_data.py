import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import hashlib
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

API_URL = "https://thead-developer-admin.t-head.cn/api/developer/artifact/saveSingle"
REQUEST_TIMEOUT = (30, 300)


@dataclass
class PypiData:
    artifactName: str = ""
    businessType: str = "PYPI"
    idempotentKey: str = ""
    sdkVersion: str = ""
    subVersion: str = ""
    item: str = ""
    artifactVersion: str = ""
    osTypeList: list[str] = field(default_factory=list)
    hggcrtVersion: str = ""
    framework: str = ""
    python: str = ""
    artifactType: str = ""
    cmdLine: str = ""
    specificLine: str = ""

    def to_dict(self):
        return asdict(self)


def normalize_target(target):
    if not isinstance(target, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", target
    ):
        raise ValueError("package_name 必须是合法的 Python 包名")
    return target.replace("-", "_")

def check_response(response):
    # requests 的 raise_for_status 不拒绝 3xx；这里也阻止重定向被当作成功。
    if not 200 <= response.status_code < 300:
        raise ValueError(f"发布数据接口请求失败，HTTP {response.status_code}")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if response.history and content_type in {"text/html", "application/xhtml+xml"}:
        raise ValueError("接口跳转后返回 HTML，可能进入登录页面，不能确认发布成功")


def diagnostic_url(url, token):
    """仅保留协议、主机和路径，移除认证信息、查询参数及片段。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        value = urlunsplit((parts.scheme, host, parts.path, "", ""))
        if token:
            for secret in (token, quote(token, safe="")):
                value = value.replace(secret, "[REDACTED]")
        return value
    except ValueError:
        return "[无法解析的 URL]"


def log_http_response(response, token):
    """每跳立即记录白名单字段，后续跳转超时也能保留已收到的响应。"""
    location = response.headers.get("Location", "")
    target = urljoin(response.url, location) if location else ""
    details = {
        "method": response.request.method,
        "status": response.status_code,
        "url": diagnostic_url(response.url, token),
        "location": diagnostic_url(target, token),
        "content_type": response.headers.get("Content-Type", "").split(";", 1)[0].strip(),
        "authorization_present": "Authorization" in response.request.headers,
    }
    # JSON 转义控制字符，禁止响应头注入新的日志行或工作流命令。
    text = json.dumps(details, ensure_ascii=True)
    if token:
        text = text.replace(token, "[REDACTED]")
    print(f"HTTP 诊断：{text}", flush=True)
    if response.request.method != "POST":
        print("::warning::自动跳转后的请求不再是 POST，请确认目标是否为登录页面或正确的 API", flush=True)
    if response.is_redirect and urlsplit(target).scheme.lower() != "https":
        raise ValueError("拒绝跳转到非 HTTPS 地址，请检查上方脱敏 Location")


def build_payload(record, target):
    """按接口字段组装数据；保留原始矩阵值，未提供的字符串字段保持为空。"""
    if not isinstance(record, dict):
        raise ValueError("发布数据 JSON 必须是对象")
    payload = PypiData().to_dict()
    if record["artifactName"] != target or record["item"] != target:
        raise ValueError("发布数据包名与 package_name 不一致")

    record_package_name = record["artifactName"]
    record_sdk_version = record["sdkVersion"]
    record_item = record["item"]
    record_os_type_list = record["osTypeList"]
    record_runtime_version = record["runtimeVersion"]
    record_framework = record["framework"]
    record_python = record["python"]
    record_tag = record["tag"]

    payload["artifactName"] = record_package_name
    payload["item"] = record_item
    payload["artifactType"] = ""
    payload["businessType"] = "PYPI"
    payload["sdkVersion"] = record_sdk_version.lower().replace("v", "").replace("sail", "").replace("ppu", "")

    # 处理 osTypeList
    _record_os = record_os_type_list.lower().replace(".", "")
    record_os = ""
    if "ubuntu24" in _record_os:
        record_os = "ubuntu24"
    elif "ubuntu22" in _record_os:
        record_os = "ubuntu22"
    elif "ubuntu20" in _record_os:
        record_os = "ubuntu20"
    elif "alios7u2" in _record_os:
        record_os = "alios7u2"
    else:
        raise ValueError("osTypeList 不在支持的列表中")
    payload["osTypeList"] = [record_os]

    # 使用tag获取 version 和 sub_version
    tag_list = record_tag.split("+")
    version = ""
    sub_version = ""
    if len(tag_list) == 2:
        version, sub_version = tag_list
    elif len(tag_list) == 1:
        version = tag_list[0]
    payload["subVersion"] = sub_version
    payload["artifactVersion"] = version

    # 处理 hggcrtVersion
    hggcrt_version = record_runtime_version.lower().replace("hggcrt", "").replace("hggc", "")
    payload["hggcrtVersion"] = f"hggcrt{hggcrt_version}"

    # 处理 framework
    framework_version = record_framework.lower().replace("torch", "")
    if framework_version:
        payload["framework"] = f"torch{framework_version}"
    else:
        payload["framework"] = ""

    # 处理 python版本
    _python_version = record_python.lower().replace("python", "").replace("py", "").replace("cp", "").replace(".", "")
    python_version_list = _python_version.split("3", 1)
    python_version = f"3.{python_version_list[1]}"
    payload["python"] = python_version

    # 处理 cmdLine 和 specificLine
    payload["cmdLine"] = f"pip install {payload["item"]}=={payload["artifactVersion"]} -i https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple"
    if payload["subVersion"]:
        payload["specificLine"] = f"pip install {payload["item"]}=={payload["artifactVersion"]}+{payload["subVersion"]}.sail{payload["sdkVersion"]} -i https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple"
    else:
        payload["specificLine"] = f"pip install {payload["item"]}=={payload["artifactVersion"]}+sail{payload["sdkVersion"]} -i https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple"

    # 处理 idempotentKey
    pypi_key = f"{payload['item']}-{payload['artifactVersion']}-{payload['hggcrtVersion']}-{payload['python']}-{record_os_type_list}-{payload['framework']}-{payload['sdkVersion']}-{payload['subVersion']}-{payload['artifactType']}"
    payload['idempotentKey'] = hashlib.md5(pypi_key.encode('utf-8')).hexdigest()

    print(f"pypi_key: {pypi_key}")
    print(f"payload: {payload}")
    return payload


def read_payload(path, target):
    if not path.is_file() or path.is_symlink():
        raise ValueError("发布数据必须是普通 JSON 文件，不能是符号链接")
    return build_payload(json.loads(path.read_text(encoding="utf-8")), target)


def post_payload(payload, token):
    # 自动跳转沿用 requests 的方法转换及跨域认证头移除规则，不额外重试 POST。
    print("开始推送：" + json.dumps({"method": "POST", "url": diagnostic_url(API_URL, token)},
                                    ensure_ascii=True), flush=True)
    try:
        response = requests.post(
            API_URL,
            headers={"Content-Type": "application/json", "maskValue": "true", "Authorization": token},
            json=payload, timeout=REQUEST_TIMEOUT, allow_redirects=True,
            hooks={"response": lambda current, *args, **kwargs: log_http_response(current, token)},
        )
    except requests.RequestException as error:
        # 某些请求异常会包含认证头原文，仅输出异常类型。
        raise ValueError(f"发布数据请求失败：{type(error).__name__}") from None
    print(f"自动跳转次数：{len(response.history)}；最终 HTTP {response.status_code}", flush=True)
    check_response(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description="逐个推送成功矩阵任务的发布数据")
    parser.add_argument("--input-dir", type=Path, required=True, help="发布 JSON 文件所在目录")
    args = parser.parse_args(argv)
    target = normalize_target(os.environ.get("PACKAGE_NAME", ""))
    token = os.environ.get("DATA_TOKEN", "")
    if not token or token != token.strip() or "\r" in token or "\n" in token:
        raise ValueError("data_token 不能为空或包含首尾空白、换行")
    if not args.input_dir.is_dir() or args.input_dir.is_symlink():
        raise ValueError("发布数据目录不存在或不是普通目录")
    files = sorted(args.input_dir.glob("*.json"))
    if not files:
        raise ValueError("未找到待推送的发布数据 JSON")
    # 全部文件先校验，再按文件逐条发送，避免坏文件导致本可避免的部分推送。
    records = [(path, read_payload(path, target)) for path in files]
    for path, payload in records:
        post_payload(payload, token)
        print(f"已推送发布数据：{path.name}")
    print(f"共推送 {len(records)} 条发布数据")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(f"::error::推送发布数据失败：{error}") from None
