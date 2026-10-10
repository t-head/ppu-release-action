import argparse
import base64
import hashlib
import json
import os
import re
import requests
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REQUEST_TIMEOUT = (30, 300)
OPEN_API_BASE_URL = "https://developer.t-head.cn"
PATH_SAVE = "/open/artifact/saveSingle"
OPEN_API_CLIENT_ID = "sync-artifact-client"
SERVER_PUBLIC_KEY_B64 = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA34jHUC8n1EOOpc71hBu8vImFIv0qiuvCJAH2HPTW2GvFXGH//o8oRBfy0fWn24+Bk7GYvrS8BhPV7H8wwqBqba1/+b40leFFKDX1NYm+6cq4J7Rt+LzOSse5PghAZGiiGf+ImHoaActV7A9diQMdIBWZRqtHm4VEG/KJejq7fvAKm8geXoUoLzPQfFrpcj4bLcFmfUpFBmLARUHUUC9rlAjGDnQQcVWujveU0sBcTSTIRNwzkuDRkSRKbB0vnNIClt7tqB84FypWGwH2otF4WqeEC56pLX2esLzNh736tbR5rnXwGKjzUG3qx/CICwu9+GAFiIQZZH4jYce3E9CRUQIDAQAB"


@dataclass
class OpenApiConfig:
    base_url: str
    client_id: str
    server_public_key_b64: str = field(repr=False)
    client_private_key_b64: str = field(repr=False)
    timeout: tuple[int, int] = REQUEST_TIMEOUT

    @classmethod
    def from_env(cls):
        """公共配置使用脚本常量，仅客户端私钥从环境变量读取。"""
        return cls(
            base_url=OPEN_API_BASE_URL,
            client_id=OPEN_API_CLIENT_ID,
            server_public_key_b64=SERVER_PUBLIC_KEY_B64,
            client_private_key_b64=os.environ.get("CLIENT_PRIVATE_KEY_B64", ""),
        )

    def validate(self):
        for name, value in (
            ("OPEN_API_BASE_URL", self.base_url),
            ("OPEN_API_CLIENT_ID", self.client_id),
            ("SERVER_PUBLIC_KEY_B64", self.server_public_key_b64),
            ("CLIENT_PRIVATE_KEY_B64", self.client_private_key_b64),
        ):
            if (
                not isinstance(value, str) or not value or value != value.strip()
                or any(character.isspace() for character in value) or value.startswith("<")
            ):
                raise ValueError(f"{name} 不能为空、包含空白或使用占位值")
        if not self.client_id.isascii():
            raise ValueError("OPEN_API_CLIENT_ID 必须使用 ASCII 字符")
        try:
            parts = urlsplit(self.base_url)
            valid_url = (
                parts.scheme in {"http", "https"} and parts.hostname
                and parts.username is None and parts.password is None
                and parts.path in {"", "/"} and not parts.query and not parts.fragment
            )
            parts.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("OPEN_API_BASE_URL 必须是仅含协议、主机及可选端口的 HTTP(S) 地址")


class OpenApiClient:
    def __init__(self, config: OpenApiConfig):
        self.config = config
        self.config.validate()

        try:
            self._server_pub_key = serialization.load_der_public_key(
                base64.b64decode(self.config.server_public_key_b64, validate=True)
            )
            self._client_priv_key = serialization.load_der_private_key(
                base64.b64decode(self.config.client_private_key_b64, validate=True),
                password=None,
            )
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("密钥加载失败，请提供 Base64 编码的 DER 公钥和未加密的 DER 私钥") from None
        if not isinstance(self._server_pub_key, rsa.RSAPublicKey) or not isinstance(
            self._client_priv_key, rsa.RSAPrivateKey
        ):
            raise ValueError("服务端公钥和客户端私钥必须是 RSA 密钥")

    def _build_url(self, path: str) -> str:
        return self.config.base_url.rstrip("/") + path

    def _encrypt_payload(self, plain_bytes: bytes) -> dict[str, str]:
        aes_key = os.urandom(32)
        iv = os.urandom(12)

        cipher_blob = AESGCM(aes_key).encrypt(iv, plain_bytes, None)

        enc_key = self._server_pub_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        return {
            "encKey": base64.b64encode(enc_key).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8'),
            "cipher": base64.b64encode(cipher_blob).decode('utf-8'),
        }

    def _sign_request(self, body_bytes: bytes, timestamp: str) -> str:
        digest_hex = hashlib.sha256(body_bytes).hexdigest()
        string_to_sign = (
            f"{self.config.client_id}\nPOST\n{PATH_SAVE}\n{timestamp}\n{digest_hex}"
        ).encode("utf-8")

        signature = self._client_priv_key.sign(
            string_to_sign,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def save_artifact(self, business_data: dict[str, Any]) -> requests.Response:
        """加密业务数据，并对实际发送的请求体签名；失败时不自动重试。"""
        plain_bytes = json.dumps(
            business_data, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        envelope = self._encrypt_payload(plain_bytes)
        body_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign_request(body_bytes, timestamp_ms)

        headers = {
            "Content-Type": "application/json",
            "X-Open-Client": self.config.client_id,
            "X-Open-Timestamp": timestamp_ms,
            "X-Open-Sign": signature,
        }
        url = self._build_url(PATH_SAVE)
        try:
            response = requests.post(
                url,
                data=body_bytes,
                headers=headers,
                timeout=self.config.timeout,
                # 签名绑定 POST 和固定路径，禁止重定向改变请求或转发签名头。
                allow_redirects=False,
            )
        except requests.RequestException as error:
            # 请求异常可能包含认证信息，仅保留异常类型。
            raise ValueError(f"发布数据请求失败：{type(error).__name__}") from None
        print(f"发布数据接口响应：HTTP {response.status_code}", flush=True)
        check_response(response)
        return response


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
    if content_type in {"text/html", "application/xhtml+xml"}:
        raise ValueError("接口返回 HTML，可能进入登录页面，不能确认发布成功")


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
    payload["sdkVersion"] = record_sdk_version.lower().replace("v", "").replace("sail", "").replace("sail", "")

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
    pypi_key = f"{payload['item']}-{payload['artifactVersion']}-{payload['hggcrtVersion']}-{payload['python']}-{record_os}-{payload['framework']}-{payload['sdkVersion']}-{payload['subVersion']}-{payload['artifactType']}"
    payload['idempotentKey'] = hashlib.md5(pypi_key.encode('utf-8')).hexdigest()

    return payload


def read_payload(path, target):
    if not path.is_file() or path.is_symlink():
        raise ValueError("发布数据必须是普通 JSON 文件，不能是符号链接")
    return build_payload(json.loads(path.read_text(encoding="utf-8")), target)


def main(argv=None):
    parser = argparse.ArgumentParser(description="逐个推送成功矩阵任务的发布数据")
    parser.add_argument("--input-dir", type=Path, required=True, help="发布 JSON 文件所在目录")
    args = parser.parse_args(argv)
    target = normalize_target(os.environ.get("PACKAGE_NAME", ""))
    client = OpenApiClient(OpenApiConfig.from_env())
    if not args.input_dir.is_dir() or args.input_dir.is_symlink():
        raise ValueError("发布数据目录不存在或不是普通目录")
    files = sorted(args.input_dir.glob("*.json"))
    if not files:
        raise ValueError("未找到待推送的发布数据 JSON")
    # 全部文件先校验，再按文件逐条发送，避免坏文件导致本可避免的部分推送。
    records = [(path, read_payload(path, target)) for path in files]
    for path, payload in records:
        client.save_artifact(payload)
        print("------------------------")
        print(f"已推送发布数据：{json.dumps(path.name, ensure_ascii=True)}", flush=True)
        print(payload)
    print("------------------------")
    print(f"共推送 {len(records)} 条发布数据", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(f"::error::推送发布数据失败：{error}") from None
