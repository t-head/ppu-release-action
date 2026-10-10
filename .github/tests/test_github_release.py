"""GitHub Release wheel 产物保存测试；当前工作流不包含 Release API 发布任务。"""

from fnmatch import fnmatchcase
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
from unittest.mock import Mock

import pytest
import requests
import yaml
from packaging.requirements import Requirement
from packaging.version import Version


CI_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow():
    return yaml.load((CI_ROOT / "workflows/matrix-release.yml").read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


@pytest.fixture
def wheel_artifact(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "保存 GitHub Release wheel 产物")


def test_tag_artifact_follows_upload_and_precedes_success_marker(workflow, wheel_artifact):
    steps = workflow["jobs"]["matrix-job"]["steps"]
    index = steps.index(wheel_artifact)
    assert steps[index - 1]["name"] == "上传 wheel 到 Artifactory"
    assert [step["name"] for step in steps[index + 1:]] == [
        "生成发布数据 JSON", "保存发布数据 JSON", "记录矩阵任务成功", "保存矩阵任务成功标记",
    ]
    assert wheel_artifact["if"] == "${{ inputs.tag_version != '' || github.ref_type == 'tag' }}"
    assert wheel_artifact["uses"] == "actions/upload-artifact@v4"
    assert wheel_artifact["with"]["path"] == "dist/*.whl"
    assert wheel_artifact["with"]["if-no-files-found"] == "error"
    assert wheel_artifact["with"]["compression-level"] == "0"
    assert "continue-on-error" not in wheel_artifact
    assert "release_token" not in str(wheel_artifact)


@pytest.mark.parametrize("tag_version,ref_type,expected", [
    ("", "tag", True), ("", "branch", False),
    ("2.4.0", "branch", True), ("v2.4.0+v0.5.0", "tag", True),
])
def test_wheel_artifact_accepts_validated_override(wheel_artifact, tag_version, ref_type, expected):
    expression = wheel_artifact["if"].removeprefix("${{ ").removesuffix(" }}")
    expression = expression.replace("inputs.tag_version", repr(tag_version))
    expression = expression.replace("github.ref_type", repr(ref_type)).replace("||", "or")
    assert eval(expression, {"__builtins__": {}}) is expected


@pytest.mark.parametrize("field,other", [
    ("needs.prepare-matrix.outputs.package_name", "other_package"),
    ("github.run_id", "99"),
    ("github.run_attempt", "1"),
])
@pytest.mark.parametrize("step_name,label", [
    ("保存 GitHub Release wheel 产物", "release-wheels"),
    ("保存发布数据 JSON", "release-data"),
])
def test_artifact_names_are_isolated_by_package_run_attempt_and_matrix(workflow, field, other, step_name, label):
    artifact = next(step for step in workflow["jobs"]["matrix-job"]["steps"] if step["name"] == step_name)

    def render_names(overrides):
        values = {"needs.prepare-matrix.outputs.package_name": "demo_package",
                  "github.run_id": "100", "github.run_attempt": "2"}
        values.update(overrides)
        name = artifact["with"]["name"]
        for key, value in values.items():
            name = name.replace("${{ " + key + " }}", value)
        return {name.replace("${{ strategy.job-index }}", str(index)) for index in range(256)}

    names = render_names({})
    assert len(names) == 256
    assert f"demo_package-{label}-100-2-0" in names
    assert names.isdisjoint(render_names({field: other}))


def test_release_data_is_saved_after_publication_without_tag_only_gate(workflow):
    steps = workflow["jobs"]["matrix-job"]["steps"]
    generate = next(step for step in steps if step["name"] == "生成发布数据 JSON")
    save = steps[steps.index(generate) + 1]
    assert steps.index(generate) > next(i for i, step in enumerate(steps) if step["name"] == "上传 wheel 到 Artifactory")
    assert generate["shell"] == "bash"
    assert generate["env"] == {
        "PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}",
        "MATRIX_JSON": "${{ toJSON(matrix) }}",
        "MATRIX_INDEX": "${{ strategy.job-index }}",
    }
    assert save["name"] == "保存发布数据 JSON"
    assert save["uses"] == "actions/upload-artifact@v4"
    assert save["with"]["path"] == "${{ runner.temp }}/release-data/release-${{ strategy.job-index }}.json"
    assert save["with"]["if-no-files-found"] == "error"
    for step in (generate, save):
        # 默认 success() 保证编译或上传失败时跳过；合格分支与 tag 均可保存。
        assert "if" not in step
        assert "continue-on-error" not in step
        assert "secrets." not in str(step)
    assert "github.ref_name" not in str(generate)
    assert "${{" not in generate["run"]


def test_workflow_and_jobs_only_require_read_access(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert job.get("permissions", workflow["permissions"]) == {"contents": "read"}


def test_post_job_downloads_current_release_data_and_limits_secret_scope(workflow):
    job = workflow["jobs"]["post-release-data"]
    assert set(job["needs"]) == {"prepare-matrix", "update-metadata"}
    assert workflow["on"]["workflow_call"]["secrets"]["data_token"]["required"] == "true"
    download, checkout, setup, install, post = job["steps"]
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {
        "pattern": "${{ needs.prepare-matrix.outputs.package_name }}-release-data-${{ github.run_id }}-${{ github.run_attempt }}-*",
        "path": "release-data", "merge-multiple": "true",
    }
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}", "ref": "${{ inputs.template_ref }}",
        "path": "ci-template", "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["uses"] == "actions/setup-python@v5"
    assert setup["with"]["python-version"] == "3.12"
    assert install["run"] == "python -m pip install --no-input requests"
    assert post["run"] == "python ci-template/.github/scripts/post_data.py --input-dir release-data"
    assert post["env"] == {
        "PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}",
        "DATA_TOKEN": "${{ secrets.data_token }}",
    }
    assert "env" not in job
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "if" not in step
        assert "continue-on-error" not in step
    for job_data in workflow["jobs"].values():
        for step in job_data["steps"]:
            if step is not post:
                assert "secrets.data_token" not in str(step)


def test_post_download_pattern_matches_all_current_matrix_records(workflow):
    save = next(step for step in workflow["jobs"]["matrix-job"]["steps"] if step["name"] == "保存发布数据 JSON")
    download = workflow["jobs"]["post-release-data"]["steps"][0]

    def render(expression, **overrides):
        values = {"needs.prepare-matrix.outputs.package_name": "demo_package",
                  "github.run_id": "100", "github.run_attempt": "2", "strategy.job-index": "0"}
        values.update(overrides)
        for key, value in values.items():
            expression = expression.replace("${{ " + key + " }}", value)
        return expression

    pattern = render(download["with"]["pattern"])
    for index in range(256):
        assert fnmatchcase(render(save["with"]["name"], **{"strategy.job-index": str(index)}), pattern)
    for key, value in (("github.run_id", "99"), ("github.run_attempt", "1"),
                       ("needs.prepare-matrix.outputs.package_name", "other_package")):
        assert not fnmatchcase(render(save["with"]["name"], **{key: value}), pattern)


@pytest.mark.parametrize("cancelled,release,result,expected", [
    (False, "true", "success", True), (False, "false", "success", False),
    (False, "true", "failure", False), (False, "true", "skipped", False),
    (False, "true", "cancelled", False), (True, "true", "success", False),
])
def test_post_gate_requires_metadata_success(workflow, cancelled, release, result, expected):
    condition = workflow["jobs"]["post-release-data"]["if"]
    assert condition == (
        "${{ !cancelled() && needs.prepare-matrix.outputs.should_release == 'true' && "
        "needs.update-metadata.result == 'success' }}"
    )
    expression = condition.removeprefix("${{ ").removesuffix(" }}")
    for key, value in {
        "!cancelled()": str(not cancelled), "needs.prepare-matrix.outputs.should_release": repr(release),
        "needs.update-metadata.result": repr(result),
    }.items():
        expression = expression.replace(key, value)
    assert eval(expression.replace("&&", "and"), {"__builtins__": {}}) is expected


def post_response(status=200, url="https://thead-developer-admin.t-head.cn/api/developer/artifact/saveSingle",
                  method="POST", location=None, content_type="application/json"):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = content_type
    if location is not None:
        response.headers["Location"] = location
    response._content = b'{}'
    response.request = requests.Request(method, url, headers={"Authorization": "test-data-token"}).prepare()
    return response


@pytest.fixture
def post_script(monkeypatch):
    spec = importlib.util.spec_from_file_location("post_data_under_test", CI_ROOT / "scripts/post_data.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.requests.adapters.HTTPAdapter, "send",
                        Mock(side_effect=AssertionError("测试禁止真实网络请求")))
    monkeypatch.setattr(module.requests, "post", Mock(return_value=post_response()))
    return module


@pytest.fixture
def post_workspace(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".post-data-test-", dir=CI_ROOT) as directory:
        root = Path(directory)
        monkeypatch.chdir(root)
        monkeypatch.setenv("PACKAGE_NAME", "demo-package")
        monkeypatch.setenv("DATA_TOKEN", "test-data-token")
        (root / "release-data").mkdir()
        yield root


@pytest.fixture
def release_record():
    return {
        "artifactName": "demo_package", "sdkVersion": "2.2.0", "item": "demo_package",
        "osTypeList": "ubuntu2404", "runtimeVersion": "3", "framework": "2.10.0",
        "python": "312", "tag": "v1.0.0+v0.3.0",
    }


def test_post_payload_normalizes_fields_and_derives_versions_and_commands(post_script, release_record):
    original = json.dumps(release_record)
    payload = post_script.build_payload(release_record, "demo_package")
    assert payload == {
        "artifactName": "demo_package", "item": "demo_package", "businessType": "PYPI",
        "artifactType": "", "sdkVersion": "2.2.0", "artifactVersion": "v1.0.0",
        "subVersion": "v0.3.0", "osTypeList": ["ubuntu24"], "hggcrtVersion": "hggcrt3",
        "framework": "torch2.10.0", "python": "3.12",
        "idempotentKey": "9a1ec65e3f526b807f58e6b45a6da1c5",
        "cmdLine": "pip install demo_package==v1.0.0 -i "
                   "https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple",
        "specificLine": "pip install demo_package==v1.0.0+v0.3.0.sail2.2.0 -i "
                        "https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple",
    }
    assert json.dumps(release_record) == original


def test_post_derived_fields_cannot_be_overridden_by_extra_record_fields(post_script, release_record):
    expected = post_script.build_payload(release_record, "demo_package")
    release_record.update(idempotentKey="manual-key", subVersion="wrong", artifactVersion="wrong",
                          cmdLine=[], specificLine="wrong", artifactType="other",
                          businessType="other", hggcrtVersion="hggcrt2", unrelated="ignored")
    assert post_script.build_payload(release_record, "demo_package") == expected


@pytest.mark.parametrize("source_field,payload_field,raw,expected", [
    ("sdkVersion", "sdkVersion", "v2.2.0", "2.2.0"),
    ("sdkVersion", "sdkVersion", "SAIL2.1.0", "2.1.0"),
    ("sdkVersion", "sdkVersion", "PPUv2.2.0", "2.2.0"),
    ("osTypeList", "osTypeList", "ubuntu2404", ["ubuntu24"]),
    ("osTypeList", "osTypeList", "Ubuntu24.04", ["ubuntu24"]),
    ("osTypeList", "osTypeList", "ubuntu2204", ["ubuntu22"]),
    ("osTypeList", "osTypeList", "Ubuntu22.04", ["ubuntu22"]),
    ("osTypeList", "osTypeList", "ubuntu2004", ["ubuntu20"]),
    ("osTypeList", "osTypeList", "Ubuntu20.04", ["ubuntu20"]),
    ("osTypeList", "osTypeList", "AliOS7U2", ["alios7u2"]),
    ("runtimeVersion", "hggcrtVersion", "2", "hggcrt2"),
    ("runtimeVersion", "hggcrtVersion", "HGGCRT3", "hggcrt3"),
    ("runtimeVersion", "hggcrtVersion", "hggc2", "hggcrt2"),
    ("framework", "framework", "", ""),
    ("framework", "framework", "2.13.0", "torch2.13.0"),
    ("framework", "framework", "TORCH2.10.0", "torch2.10.0"),
    ("python", "python", "38", "3.8"), ("python", "python", "310", "3.10"),
    ("python", "python", "312", "3.12"), ("python", "python", "Python3.12", "3.12"),
    ("python", "python", "PY310", "3.10"), ("python", "python", "CP38", "3.8"),
    ("python", "python", "3.12", "3.12"),
])
def test_post_field_normalization(post_script, release_record, source_field, payload_field, raw, expected):
    payload = post_script.build_payload({**release_record, source_field: raw}, "demo_package")
    assert payload[payload_field] == expected
    assert "runtimeVersion" not in payload


def test_post_idempotent_key_is_stable_across_order_and_normalized_prefixes(post_script, release_record):
    expected = post_script.build_payload(release_record, "demo_package")["idempotentKey"]
    reordered = dict(reversed(list(release_record.items())))
    reordered.update(sdkVersion="SAILv2.2.0", runtimeVersion="HGGCRT3",
                     framework="TORCH2.10.0", python="CP312")
    assert post_script.build_payload(reordered, "demo_package")["idempotentKey"] == expected
    assert post_script.build_payload(release_record, "demo_package")["idempotentKey"] == expected


def test_post_idempotent_keys_distinguish_all_release_dimensions(post_script, release_record):
    keys = [post_script.build_payload(release_record, "demo_package")["idempotentKey"]]
    for overrides in (
        {"artifactName": "other_package", "item": "other_package"},
        {"sdkVersion": "2.1.0"}, {"osTypeList": "ubuntu2204"}, {"runtimeVersion": "2"},
        {"framework": "2.13.0"}, {"framework": ""}, {"python": "310"},
        {"tag": "v2.0.0+v0.3.0"}, {"tag": "v1.0.0+v0.4.0"}, {"tag": "v1.0.0"},
    ):
        record = {**release_record, **overrides}
        keys.append(post_script.build_payload(record, record["artifactName"])["idempotentKey"])
    assert len(set(keys)) == len(keys)
    assert all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in keys)


@pytest.mark.parametrize("status", [200, 201, 204])
def test_post_main_sends_one_request_per_json(post_script, post_workspace, release_record, capsys, status):
    post_script.requests.post.return_value.status_code = status
    files = []
    for index, sdk in enumerate(("2.1.0", "2.2.0")):
        path = post_workspace / f"release-data/release-{index}.json"
        path.write_text(json.dumps({**release_record, "sdkVersion": sdk}))
        files.append(path)
    originals = [path.read_bytes() for path in files]
    post_script.main(["--input-dir", "release-data"])
    calls = post_script.requests.post.call_args_list
    assert len(calls) == 2
    assert [call.kwargs["json"]["sdkVersion"] for call in calls] == ["2.1.0", "2.2.0"]
    for call in calls:
        assert call.args == ("https://thead-developer-admin.t-head.cn/api/developer/artifact/saveSingle",)
        assert call.kwargs["headers"] == {
            "Content-Type": "application/json", "maskValue": "true", "Authorization": "test-data-token",
        }
        assert call.kwargs["allow_redirects"] is True
        assert callable(call.kwargs["hooks"]["response"])
        assert call.kwargs["timeout"] == (30, 300)
        assert "auth" not in call.kwargs
    assert [path.read_bytes() for path in files] == originals
    output = capsys.readouterr()
    assert "共推送 2 条发布数据" in output.out
    assert "test-data-token" not in output.out + output.err


@pytest.mark.parametrize("version_source", ["repository", "override_branch", "override_rc_tag"])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag,version,sub_version,release_version", [
    ("1.0.0+v0.3.0", "1.0.0", "v0.3.0", "1.0.0+v0.3.0.sail2.2.0"),
    ("1.0.0+0.3.0", "1.0.0", "0.3.0", "1.0.0+0.3.0.sail2.2.0"),
    ("1.0.0+V0.3.0", "1.0.0", "V0.3.0", "1.0.0+V0.3.0.sail2.2.0"),
    ("1.2.3", "1.2.3", "", "1.2.3+sail2.2.0"),
    ("1.0.0+build_1", "1.0.0", "build_1", "1.0.0+build_1.sail2.2.0"),
])
def test_generated_matrix_json_reaches_post_cli(
    workflow, post_script, post_workspace, monkeypatch, tag, version, sub_version, release_version, version_source, prefix
):
    inputs = {
        "package_name": "demo_package", "sdk_versions": '["2.2.0"]',
        "os_versions": '["ubuntu2404"]', "runtime_versions": '["3"]',
        "torch_versions": '["2.10.0","2.13.0"]', "python_versions": '["312"]',
    }
    ref_type, ref_name = "tag", prefix + tag
    if version_source != "repository":
        inputs["tag_version"] = prefix + tag
        ref_type, ref_name = ("branch", "main") if version_source == "override_branch" else ("tag", "9.9.9RC1")
    output = post_workspace / "matrix-output"
    env = dict(os.environ, INPUTS_JSON=json.dumps(inputs), GITHUB_OUTPUT=str(output),
               GITHUB_REF_TYPE=ref_type, GITHUB_REF_NAME=ref_name, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, str(CI_ROOT / "scripts/prepare_matrix.py")],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["should_release"] == "true"
    matrices = json.loads(values["matrix"])["include"]
    assert len(matrices) == 2
    assert all(matrix["release_version"] == release_version for matrix in matrices)
    generate = next(step for step in workflow["jobs"]["matrix-job"]["steps"] if step["name"] == "生成发布数据 JSON")
    for index, matrix in enumerate(matrices):
        assert matrix["tag"] == tag
        assert matrix["runtime_version"] == "3"
        assert "hggcrt_version" not in matrix
        env = dict(os.environ, PACKAGE_NAME="demo_package", MATRIX_JSON=json.dumps(matrix),
                   MATRIX_INDEX=str(index), RUNNER_TEMP=str(post_workspace), TEST_PYTHON=sys.executable)
        result = subprocess.run(["bash"], input='python3() { "$TEST_PYTHON" "$@"; }\n' + generate["run"],
                                env=env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        record = json.loads((post_workspace / f"release-data/release-{index}.json").read_text())
        assert record["osTypeList"] == "ubuntu2404"
        assert record["runtimeVersion"] == matrix["runtime_version"]
        assert "hggcrtVersion" not in record
        assert record["python"] == "312"
        assert record["tag"] == tag
    monkeypatch.setattr(sys, "argv", ["post_data.py", "--input-dir", "release-data"])
    runpy.run_path(str(CI_ROOT / "scripts/post_data.py"), run_name="__main__")
    payloads = [call.kwargs["json"] for call in post_script.requests.post.call_args_list]
    assert len(payloads) == 2
    assert [data["framework"] for data in payloads] == ["torch2.10.0", "torch2.13.0"]
    assert len({data["idempotentKey"] for data in payloads}) == 2
    for data, matrix in zip(payloads, matrices):
        assert data["artifactName"] == data["item"] == "demo_package"
        assert data["python"] == "3.12" and data["osTypeList"] == ["ubuntu24"]
        assert data["hggcrtVersion"] == "hggcrt3" and data["sdkVersion"] == "2.2.0"
        assert "runtimeVersion" not in data
        assert data["artifactVersion"] == version and data["subVersion"] == sub_version
        for field, expected_version in (("cmdLine", version), ("specificLine", release_version)):
            args = shlex.split(data[field])
            assert args == ["pip", "install", f"demo_package=={expected_version}", "-i",
                            "https://download.t-head.cn/artifactory/api/pypi/pypi_index/simple"]
            # 安装约束必须能选中矩阵实际发布的版本，防止再次重复添加 v。
            assert Requirement(args[2]).specifier.contains(Version(matrix["release_version"]))


@pytest.mark.parametrize("status", [301, 302, 401, 403, 409, 500])
@pytest.mark.parametrize("failure_at", [1, 2])
def test_post_http_failure_stops_without_retry(post_script, post_workspace, release_record, status, failure_at, capsys):
    for index in range(3):
        (post_workspace / f"release-data/release-{index}.json").write_text(json.dumps(release_record))
    post_script.requests.post.side_effect = [post_response()] * (failure_at - 1) + [post_response(status=status)]
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        post_script.main(["--input-dir", "release-data"])
    assert post_script.requests.post.call_count == failure_at
    assert capsys.readouterr().out.count("已推送发布数据") == failure_at - 1


@pytest.mark.parametrize("error_type", ["Timeout", "ConnectionError", "InvalidHeader"])
def test_post_network_errors_do_not_expose_token(post_script, post_workspace, release_record, monkeypatch, capsys, error_type):
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    post_script.requests.post.side_effect = getattr(post_script.requests.exceptions, error_type)("test-data-token")
    monkeypatch.setattr(sys, "argv", ["post_data.py", "--input-dir", "release-data"])
    with pytest.raises(SystemExit, match=f"推送发布数据失败.*{error_type}") as error:
        runpy.run_path(str(CI_ROOT / "scripts/post_data.py"), run_name="__main__")
    output = capsys.readouterr()
    assert "test-data-token" not in str(error.value) + output.out + output.err
    post_script.requests.post.assert_called_once()


@pytest.fixture
def redirect_transport(post_script, monkeypatch, post_workspace):
    """只替换网络发送层，真实执行 requests 的跳转、方法转换和认证头处理。"""
    responses, sent = [], []

    def send(adapter, request, **kwargs):
        sent.append(request)
        assert responses, "出现未预期的网络请求"
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        response = post_response(**result)
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setenv("NETRC", str(post_workspace / "missing-netrc"))
    monkeypatch.setattr(post_script.requests, "post", post_script.requests.api.post)
    monkeypatch.setattr(post_script.requests.adapters.HTTPAdapter, "send", send)
    return responses, sent


@pytest.mark.parametrize("status,method", [(301, "GET"), (302, "GET"), (303, "GET"), (307, "POST"), (308, "POST")])
def test_redirects_follow_requests_method_rules_and_log_hops(post_script, redirect_transport, capsys, status, method):
    responses, sent = redirect_transport
    responses.extend([{"status": status, "location": "/api/final?ticket=hidden-query"}, {}])
    post_script.post_payload({"item": "demo_package"}, "test-data-token")
    assert [request.method for request in sent] == ["POST", method]
    assert sent[1].headers["Authorization"] == "test-data-token"
    if method == "POST":
        assert sent[1].body == sent[0].body
    else:
        assert sent[1].body is None
    output = capsys.readouterr().out
    assert output.count("HTTP 诊断：") == 2
    assert f'"status": {status}' in output
    assert f'"method": "{method}"' in output
    assert '"location": "https://thead-developer-admin.t-head.cn/api/final"' in output
    assert "自动跳转次数：1；最终 HTTP 200" in output
    assert ("请求不再是 POST" in output) == (method == "GET")
    assert "test-data-token" not in output and "hidden-query" not in output


def test_cross_host_redirect_does_not_forward_authorization(post_script, redirect_transport, capsys):
    responses, sent = redirect_transport
    responses.extend([{"status": 302, "location": "https://login.example.test/api?token=hidden-query"}, {}])
    post_script.post_payload({}, "test-data-token")
    assert sent[0].headers["Authorization"] == "test-data-token"
    assert "Authorization" not in sent[1].headers
    output = capsys.readouterr().out
    assert '"authorization_present": true' in output
    assert '"authorization_present": false' in output
    assert "https://login.example.test/api" in output
    assert "test-data-token" not in output and "hidden-query" not in output


@pytest.mark.parametrize("content_type", ["text/html; charset=UTF-8", "application/xhtml+xml"])
def test_redirect_to_html_is_not_reported_as_success(post_script, redirect_transport, capsys, content_type):
    responses, sent = redirect_transport
    responses.extend([{"status": 302, "location": "/login"}, {"content_type": content_type}])
    with pytest.raises(ValueError, match="可能进入登录页面"):
        post_script.post_payload({}, "test-data-token")
    assert len(sent) == 2
    assert '"status": 200' in capsys.readouterr().out


def test_redirect_to_http_is_blocked_before_sending(post_script, redirect_transport, capsys):
    responses, sent = redirect_transport
    responses.append({"status": 307, "location": "http://example.test/api?token=hidden-query"})
    with pytest.raises(ValueError, match="非 HTTPS"):
        post_script.post_payload({}, "test-data-token")
    assert len(sent) == 1
    output = capsys.readouterr().out
    assert "http://example.test/api" in output and "hidden-query" not in output


def test_final_redirect_error_logs_chain_before_failing(post_script, redirect_transport, capsys):
    responses, sent = redirect_transport
    responses.extend([{"status": 302, "location": "/login"}, {"status": 403}])
    with pytest.raises(ValueError, match="HTTP 403"):
        post_script.post_payload({}, "test-data-token")
    assert len(sent) == 2
    output = capsys.readouterr().out
    assert output.count("HTTP 诊断：") == 2
    assert "自动跳转次数：1；最终 HTTP 403" in output


def test_redirect_timeout_keeps_previous_hop_diagnostics(post_script, redirect_transport, capsys):
    responses, sent = redirect_transport
    responses.extend([{"status": 302, "location": "/login?token=hidden-query"},
                      requests.Timeout("test-data-token")])
    with pytest.raises(ValueError, match="Timeout") as error:
        post_script.post_payload({}, "test-data-token")
    assert len(sent) == 2
    output = capsys.readouterr().out
    assert '"status": 302' in output and "/login" in output
    assert "test-data-token" not in output + str(error.value)
    assert "hidden-query" not in output


def test_redirect_loop_has_a_limit_and_keeps_diagnostics(post_script, redirect_transport, capsys):
    responses, sent = redirect_transport
    responses.extend([{"status": 302, "location": "/loop?token=hidden-query"}] * 40)
    with pytest.raises(ValueError, match="TooManyRedirects"):
        post_script.post_payload({}, "test-data-token")
    assert len(sent) == 31
    output = capsys.readouterr().out
    assert output.count("HTTP 诊断：") == len(sent)
    assert "hidden-query" not in output and "test-data-token" not in output


def test_http_diagnostics_do_not_log_credentials_cookies_body_or_control_lines(post_script, capsys):
    response = post_response(status=302, location="https://user:password@login.example.test/login?ticket=hidden-query#hidden-fragment",
                             content_type="text/html\r\n::error::forged; charset=UTF-8")
    response.headers["Set-Cookie"] = "session=hidden-cookie"
    response._content = b"hidden-response-body"
    post_script.log_http_response(response, "test-data-token")
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert "https://login.example.test/login" in output
    assert "\\r\\n" in output
    for secret in ("test-data-token", "user:password", "hidden-query", "hidden-fragment",
                   "hidden-cookie", "hidden-response-body"):
        assert secret not in output


@pytest.mark.parametrize("url,expected", [
    ("https://user:password@example.test:8443/api?ticket=secret#fragment", "https://example.test:8443/api"),
    ("https://example.test/test-data-token/api", "https://example.test/[REDACTED]/api"),
    ("https://[::1]:8443/api?ticket=secret", "https://[::1]:8443/api"),
    ("https://[bad/api?ticket=secret", "[无法解析的 URL]"),
    ("", ""),
])
def test_diagnostic_url_removes_sensitive_parts(post_script, url, expected):
    assert post_script.diagnostic_url(url, "test-data-token") == expected


@pytest.mark.parametrize("bad_data,error", [
    ("", json.JSONDecodeError), ("{", json.JSONDecodeError), ("null", ValueError),
    ("[]", ValueError), ("{}", KeyError),
    ('{"artifactName": "other", "item": "other"}', ValueError),
])
def test_post_validates_all_files_before_sending(post_script, post_workspace, release_record, bad_data, error):
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    (post_workspace / "release-data/release-1.json").write_text(bad_data)
    with pytest.raises(error):
        post_script.main(["--input-dir", "release-data"])
    post_script.requests.post.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"osTypeList": ["ubuntu2404"]}, {"osTypeList": [None]}, {"python": 312},
    {"framework": None}, {"sdkVersion": {}}, {"runtimeVersion": 3}, {"tag": None},
])
def test_post_invalid_source_types_block_all_requests(post_script, post_workspace, release_record, overrides):
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    (post_workspace / "release-data/release-1.json").write_text(json.dumps({**release_record, **overrides}))
    # 当前脚本直接操作字符串；坏输入会抛出属性错误，但不得发送任何请求。
    with pytest.raises(AttributeError):
        post_script.main(["--input-dir", "release-data"])
    post_script.requests.post.assert_not_called()


@pytest.mark.parametrize("field", [
    "artifactName", "item", "sdkVersion", "osTypeList", "runtimeVersion", "framework", "python", "tag",
])
def test_post_missing_source_field_blocks_all_requests(post_script, post_workspace, release_record, field):
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    del release_record[field]
    (post_workspace / "release-data/release-1.json").write_text(json.dumps(release_record))
    with pytest.raises(KeyError, match=field):
        post_script.main(["--input-dir", "release-data"])
    post_script.requests.post.assert_not_called()


@pytest.mark.parametrize("os_type", ["", "debian12", "windows", "ubuntu1804"])
def test_post_unsupported_os_blocks_all_requests(post_script, post_workspace, release_record, os_type):
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    (post_workspace / "release-data/release-1.json").write_text(json.dumps({**release_record, "osTypeList": os_type}))
    with pytest.raises(ValueError, match="osTypeList 不在支持的列表中"):
        post_script.main(["--input-dir", "release-data"])
    post_script.requests.post.assert_not_called()


@pytest.mark.parametrize("token", ["", " ", "token\n", "token\rvalue", " token"])
def test_post_requires_valid_secret_before_requests(post_script, post_workspace, release_record, monkeypatch, token):
    monkeypatch.setenv("DATA_TOKEN", token)
    (post_workspace / "release-data/release-0.json").write_text(json.dumps(release_record))
    with pytest.raises(ValueError, match="data_token"):
        post_script.main(["--input-dir", "release-data"])
    post_script.requests.post.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "symlink", "directory_symlink"])
def test_post_requires_regular_json_files(post_script, post_workspace, release_record, kind):
    directory = post_workspace / "release-data"
    if kind == "missing":
        directory = post_workspace / "missing"
    elif kind == "directory":
        (directory / "release-0.json").mkdir()
    elif kind == "symlink":
        target = post_workspace / "other.json"
        target.write_text(json.dumps(release_record))
        (directory / "release-0.json").symlink_to(target)
    elif kind == "directory_symlink":
        link = post_workspace / "linked-data"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    with pytest.raises(ValueError):
        post_script.main(["--input-dir", str(directory)])
    post_script.requests.post.assert_not_called()
