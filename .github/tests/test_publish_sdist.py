"""源码包条件发布的离线测试，模拟 artifact 传递和 Twine，验证多版本原样发布。"""

from fnmatch import fnmatchcase
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import Mock

import pytest
import yaml


CI_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTORY_URL = "https://download.t-head.cn/artifactory/api/pypi/pypi_srcsupport/"
RELEASE_VERSION = "1.0.0+v0.3.0.sail2.2.0"
SDIST_NAME = f"demo_package-{RELEASE_VERSION}.tar.gz"

SHELL_SHIM = r'''
python3() {
  "$TEST_PYTHON" "$@"
}
twine() {
  "$TEST_PYTHON" -c '
import json
import os
from pathlib import Path
import sys

assert os.environ["TWINE_USERNAME"] == "test-release-user"
assert os.environ["TWINE_PASSWORD"] == "test-release-token"
assert Path(sys.argv[-1]).is_file()
calls_file = Path(os.environ["TEST_TWINE_CALLS"])
count = len(calls_file.read_text().splitlines()) if calls_file.exists() else 0
with calls_file.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
exit_code = int(os.environ["TEST_TWINE_EXIT"]) if count + 1 == int(os.environ["TEST_TWINE_FAIL_AT"]) else 0
sys.exit(exit_code)
' "$@"
}
curl() {
  printf "%s\n" "源码包不应再使用 curl" >&2
  return 99
}
'''


@pytest.fixture
def workflow():
    return yaml.load((CI_ROOT / "workflows/matrix-release.yml").read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


@pytest.fixture
def publish_job(workflow):
    return workflow["jobs"]["publish-sdist"]


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".publish-sdist-test-", dir=CI_ROOT) as directory:
        yield Path(directory)


@pytest.fixture
def run_step(workspace):
    def run(step, **overrides):
        env = dict(os.environ, TEST_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1",
                   TEST_TWINE_CALLS=str(workspace / "twine-calls.jsonl"), TEST_TWINE_EXIT="0",
                   TEST_TWINE_FAIL_AT="1", GITHUB_OUTPUT=str(workspace / "github-output"),
                   ARTIFACTORY_URL=step.get("env", {}).get("ARTIFACTORY_URL", ""),
                   PACKAGE_NAME="demo_package",
                   MATRIX_JSON=json.dumps({"include": [{"release_version": RELEASE_VERSION}]}),
                   TWINE_USERNAME="test-release-user", TWINE_PASSWORD="test-release-token")
        env.update(overrides)
        return subprocess.run(["bash"], input=SHELL_SHIM + step["run"], cwd=workspace,
                              env=env, text=True, capture_output=True, timeout=30)

    return run


@pytest.fixture
def run_upload(publish_job, workspace, run_step):
    def run(filenames=(SDIST_NAME,), versions=(RELEASE_VERSION,), **overrides):
        overrides.setdefault("MATRIX_JSON", json.dumps({
            "include": [{"release_version": version} for version in versions]
        }))
        directory = workspace / "sdist-dist"
        directory.mkdir(exist_ok=True)
        for filename in filenames:
            (directory / filename).write_bytes(b"source archive test data")
        result = run_step(publish_job["steps"][-1], **overrides)
        calls_file = workspace / "twine-calls.jsonl"
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        return result, calls

    return run


def render_artifact_expression(expression, index="0", run="100", attempt="2"):
    values = {"needs.prepare-matrix.outputs.package_name": "demo_package", "github.run_id": run,
              "github.run_attempt": attempt, "strategy.job-index": index}
    for key, value in values.items():
        expression = expression.replace("${{ " + key + " }}", value)
    return expression


def test_publish_waits_for_all_matrix_jobs_and_handles_aggregate_failure(publish_job):
    assert set(publish_job["needs"]) == {"prepare-matrix", "build-sdist", "matrix-job"}
    assert publish_job["if"] == (
        "${{ !cancelled() && needs.prepare-matrix.outputs.should_release == 'true' && "
        "needs.build-sdist.result == 'success' && "
        "(needs.matrix-job.result == 'success' || needs.matrix-job.result == 'failure') }}"
    )
    assert "strategy" not in publish_job
    assert "continue-on-error" not in publish_job


def test_markers_only_follow_successful_wheel_upload(workflow, workspace, run_step):
    steps = workflow["jobs"]["matrix-job"]["steps"]
    upload = next(step for step in steps if step.get("name") == "上传 wheel 到 Artifactory")
    create, save = steps[-2:]
    assert steps.index(upload) < steps.index(create)
    assert create["name"] == "记录矩阵任务成功"
    assert save["uses"] == "actions/upload-artifact@v4"
    for step in (create, save):
        assert "if" not in step
        assert "continue-on-error" not in step
    result = run_step(create, RUNNER_TEMP=str(workspace))
    assert result.returncode == 0, result.stderr
    assert (workspace / "matrix-success.txt").read_text() == "success\n"
    assert save["with"]["path"] == "${{ runner.temp }}/matrix-success.txt"
    assert save["with"]["if-no-files-found"] == "error"
    assert "outputs" not in workflow["jobs"]["matrix-job"]
    assert workflow["jobs"]["matrix-job"]["strategy"]["fail-fast"] == "false"


def test_marker_names_are_unique_and_run_attempt_scoped(workflow, publish_job):
    name = workflow["jobs"]["matrix-job"]["steps"][-1]["with"]["name"]
    download = publish_job["steps"][0]
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"]["merge-multiple"] == "false"
    pattern = render_artifact_expression(download["with"]["pattern"])
    names = [render_artifact_expression(name, index=str(i)) for i in range(144)]
    assert len(set(names)) == 144
    assert all(fnmatchcase(value, pattern) for value in names)
    assert not fnmatchcase(render_artifact_expression(name, attempt="1"), pattern)
    assert not fnmatchcase(render_artifact_expression(name, run="99"), pattern)
    assert not fnmatchcase(names[0].replace("demo_package", "other_package"), pattern)


def test_source_download_and_upload_use_gate_and_matching_artifact(workflow, publish_job):
    gate = publish_job["steps"][1]
    assert gate["id"] == "matrix-success"
    download, setup_python, install, upload = publish_job["steps"][2:]
    condition = "${{ steps.matrix-success.outputs.any_success == 'true' }}"
    assert all(step["if"] == condition for step in (download, setup_python, install, upload))
    assert setup_python["uses"] == "actions/setup-python@v5"
    assert setup_python["with"]["python-version"] == "3.12"
    assert install["run"] == "python -m pip install --no-input twine"
    assert "continue-on-error" not in upload
    assert upload["id"] == "publish"
    assert publish_job["outputs"]["published"] == "${{ steps.publish.outcome == 'success' }}"
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"]["name"] == workflow["jobs"]["build-sdist"]["steps"][-1]["with"]["name"]
    assert download["with"]["path"] == "sdist-dist"
    assert "env" not in publish_job
    assert all("TWINE_PASSWORD" not in str(step) for step in publish_job["steps"][:-1])
    assert upload["env"] == {
        "ARTIFACTORY_URL": ARTIFACTORY_URL,
        "PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}",
        "MATRIX_JSON": "${{ needs.prepare-matrix.outputs.matrix }}",
        "TWINE_USERNAME": "${{ secrets.release_username }}",
        "TWINE_PASSWORD": "${{ secrets.release_token }}",
    }
    assert all(not step.get("uses", "").startswith("actions/checkout") for step in publish_job["steps"])


@pytest.mark.parametrize("outcomes,expected", [
    (["success", "failure"], True),
    (["failure", "success"], True),
    (["success", "success"], True),
    (["success"], True),
    (["failure", "failure"], False),
    (["failure", "skipped"], False),
    (["cancelled"], False),
    ([], False),
])
def test_any_success_gate_and_conditional_upload(workflow, publish_job, workspace, run_step,
                                               run_upload, outcomes, expected):
    save_name = workflow["jobs"]["matrix-job"]["steps"][-1]["with"]["name"]
    pattern = render_artifact_expression(publish_job["steps"][0]["with"]["pattern"])
    # 即使旧尝试有成功记录，本次全部失败也不能误触发发布。
    artifacts = [(render_artifact_expression(save_name, attempt="1"), "success\n")]
    for index, outcome in enumerate(outcomes):
        if outcome == "success":
            artifacts.append((render_artifact_expression(save_name, index=str(index)), "success\n"))
    for name, contents in artifacts:
        if fnmatchcase(name, pattern):
            directory = workspace / "matrix-results" / name
            directory.mkdir(parents=True)
            (directory / "matrix-success.txt").write_text(contents)
    result = run_step(publish_job["steps"][1])
    assert result.returncode == 0, result.stderr
    output = (workspace / "github-output").read_text()
    assert output == f"any_success={str(expected).lower()}\n"
    if output == "any_success=true\n":
        upload_result, calls = run_upload()
        assert upload_result.returncode == 0, upload_result.stderr
        assert len(calls) == 1
    else:
        assert "跳过源码包发布" in result.stdout
        assert not (workspace / "twine-calls.jsonl").exists()


@pytest.mark.parametrize("contents", ["", "failure\n", "false\n", "success-invalid\n"])
def test_invalid_marker_is_not_counted(publish_job, workspace, run_step, contents):
    directory = workspace / "matrix-results/marker"
    directory.mkdir(parents=True)
    (directory / "matrix-success.txt").write_text(contents)
    result = run_step(publish_job["steps"][1])
    assert result.returncode == 0
    assert (workspace / "github-output").read_text() == "any_success=false\n"


def test_publish_shell_syntax(publish_job):
    for step in publish_job["steps"]:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
            assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("versions", [
    [RELEASE_VERSION],
    ["1.0.0+v0.3.0.sail2.1.0", RELEASE_VERSION],
    [RELEASE_VERSION, "1.0.0+v0.3.0.sail2.1.0", RELEASE_VERSION],
])
def test_twine_uploads_each_built_release_once_without_renaming(run_upload, workspace, versions):
    filenames = [f"demo_package-{version}.tar.gz" for version in dict.fromkeys(versions)]
    result, calls = run_upload(filenames, versions=versions)
    assert result.returncode == 0, result.stderr
    assert len(calls) == len(filenames)
    assert [Path(args[-1]).name for args in calls] == sorted(filenames)
    for args in calls:
        assert args[:-1] == ["upload", "--repository-url", ARTIFACTORY_URL,
                            "--verbose", "--disable-progress-bar", "--non-interactive"]
        uploaded = workspace / args[-1]
        assert uploaded.parent == workspace / "sdist-dist"
        assert uploaded.read_bytes() == b"source archive test data"
        assert f"已上传源码包 {uploaded.name}" in result.stdout
    assert len(list((workspace / "sdist-dist").iterdir())) == len(filenames)
    assert "test-release-token" not in result.stdout + result.stderr + str(calls)
    assert "test-release-user" not in result.stdout + result.stderr + str(calls)


def test_prerelease_and_hyphenated_package_name(run_upload):
    version = "2.0.0rc1.dev5+v0.3.0.sail2.2.0"
    filename = f"demo-package-{version}.tar.gz"
    result, calls = run_upload((filename,), versions=[version], PACKAGE_NAME="Demo-Package")
    assert result.returncode == 0, result.stderr
    assert Path(calls[0][-1]).name == filename
    assert calls[0][2] == ARTIFACTORY_URL


@pytest.mark.parametrize("version", [
    "1.2.3+sail2.2.0", "1.2.3+other.sail2.2.0", "1.0.0+v0.3.0.sail2.2.0", "1.2.3+oe.sail2.2.0",
])
def test_sdist_version_does_not_require_oe_suffix(run_upload, version):
    filename = f"demo_package-{version}.tar.gz"
    result, calls = run_upload((filename,), versions=[version])
    assert result.returncode == 0, result.stderr
    assert Path(calls[0][-1]).name == filename


def test_normalized_sdist_version_matches_raw_matrix_version(run_upload):
    result, calls = run_upload(versions=["v1.0.0+V0.3.0.sail2.2.0"])
    assert result.returncode == 0, result.stderr
    assert Path(calls[0][-1]).name == SDIST_NAME


@pytest.mark.parametrize("filenames", [
    (),
    (SDIST_NAME, "demo_package-1.2.4+oe.tar.gz"),
    ("other_package-1.2.3+oe.tar.gz",),
    ("demo_package-notversion.tar.gz",),
    ("demo_package-1.2.3+v0.3.0+oe.tar.gz",),
    (f"demo_package-{RELEASE_VERSION}\n.tar.gz",),
    (f"demo_package-{RELEASE_VERSION} .tar.gz",),
    ("demo_package-1.2.3%2Boe.tar.gz",),
    ("demo_package-1.0.0+v0.3.0.tar.gz",),
])
def test_missing_or_invalid_sdist_never_uploads(run_upload, filenames):
    result, calls = run_upload(filenames)
    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert calls == []


@pytest.mark.parametrize("overrides", [
    {"TWINE_USERNAME": ""}, {"TWINE_PASSWORD": ""},
    {"PACKAGE_NAME": "../outside"}, {"PACKAGE_NAME": "other"},
])
def test_missing_credentials_and_wrong_package_block_upload(run_upload, overrides):
    result, calls = run_upload(**overrides)
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_non_regular_source_archive_is_rejected(run_upload, workspace, kind):
    directory = workspace / "sdist-dist"
    directory.mkdir()
    path = directory / SDIST_NAME
    if kind == "symlink":
        target = workspace / "unrelated"
        target.write_bytes(b"not an artifact")
        path.symlink_to(target)
    else:
        path.mkdir()
    result, calls = run_upload(())
    assert result.returncode != 0
    assert "源码包必须是普通文件" in result.stderr
    assert calls == []


@pytest.mark.parametrize("failure_at", [1, 2])
def test_twine_failure_stops_remaining_uploads(run_upload, failure_at):
    versions = ["1.0.0+v0.3.0.sail2.1.0", RELEASE_VERSION]
    filenames = [f"demo_package-{version}.tar.gz" for version in versions]
    result, calls = run_upload(filenames, versions=versions,
                              TEST_TWINE_FAIL_AT=str(failure_at), TEST_TWINE_EXIT="23")
    assert result.returncode == 23
    assert len(calls) == failure_at
    assert result.stdout.count("已上传源码包") == failure_at - 1


@pytest.mark.parametrize("exit_code", ["1", "2", "7"])
def test_upload_failure_propagates(run_upload, exit_code):
    result, calls = run_upload(TEST_TWINE_EXIT=exit_code)
    assert result.returncode == int(exit_code)
    assert len(calls) == 1
    assert "已上传源码包" not in result.stdout


@pytest.mark.parametrize("value", [
    "", "[", "{}", "null", "[]", '{"include": []}', '{"include": null}',
    '{"include": [{}]}', '{"include": [null]}', '{"include": [{"release_version": null}]}',
    '{"include": [{"release_version": 2.2}]}', '{"include": [{"release_version": true}]}',
    '{"include": [{"release_version": "../2.2.0"}]}',
    '{"include": [{"release_version": "1.2.3+sail2.2.0\\n"}]}',
])
def test_invalid_matrix_fails_before_upload(run_upload, workspace, value):
    result, calls = run_upload(MATRIX_JSON=value)
    assert result.returncode != 0
    assert "release_version" in result.stderr
    assert calls == []
    assert list((workspace / "sdist-dist").iterdir()) == [workspace / "sdist-dist" / SDIST_NAME]


def test_missing_release_archive_blocks_all_uploads(run_upload):
    result, calls = run_upload(versions=[RELEASE_VERSION, "1.0.0+v0.3.0.sail2.1.0"])
    assert result.returncode != 0
    assert "缺少矩阵中的 release_version" in result.stderr
    assert calls == []


def test_duplicate_release_archive_blocks_all_uploads(run_upload):
    result, calls = run_upload((SDIST_NAME, f"demo-package-{RELEASE_VERSION}.tar.gz"))
    assert result.returncode != 0
    assert "源码包版本不在矩阵中或重复" in result.stderr
    assert calls == []


def test_empty_archive_blocks_all_uploads(run_upload, workspace):
    directory = workspace / "sdist-dist"
    directory.mkdir()
    (directory / SDIST_NAME).touch()
    result, calls = run_upload(())
    assert result.returncode != 0
    assert "源码包不能为空" in result.stderr
    assert calls == []


def test_requested_exact_filename_and_repository(run_upload):
    version = "1.0.0+v0.1.0.sail2.2.0"
    filename = f"deep_gemm-{version}.tar.gz"
    result, calls = run_upload((filename,), versions=[version], PACKAGE_NAME="deep_gemm")
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0][2] == "https://download.t-head.cn/artifactory/api/pypi/pypi_srcsupport/"
    assert Path(calls[0][-1]).name == "deep_gemm-1.0.0+v0.1.0.sail2.2.0.tar.gz"


def test_real_archives_keep_their_distinct_metadata_and_setup(workspace, run_upload):
    directory = workspace / "sdist-dist"
    directory.mkdir()
    versions = ["1.0.0+v0.3.0.sail2.1.0", RELEASE_VERSION]
    snapshots = {}
    for version in versions:
        root = f"demo_package-{version}"
        path = directory / f"{root}.tar.gz"
        metadata = f"Metadata-Version: 2.1\nName: demo_package\nVersion: {version}\n".encode()
        members = {
            f"{root}/PKG-INFO": metadata,
            f"{root}/demo_package.egg-info/PKG-INFO": metadata,
            f"{root}/setup.py": f'build_version = "{version}"\n'.encode(),
        }
        with tarfile.open(path, "w:gz") as archive:
            for name, contents in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        snapshots[path.name] = (path.read_bytes(), members)
    result, calls = run_upload((), versions=versions)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    for args in calls:
        uploaded = workspace / args[-1]
        original_bytes, members = snapshots[uploaded.name]
        assert uploaded.parent == directory
        assert uploaded.read_bytes() == original_bytes
        with tarfile.open(uploaded) as archive:
            assert set(archive.getnames()) == set(members)
            for name, contents in members.items():
                assert archive.extractfile(name).read() == contents


def test_update_metadata_uses_template_and_step_scoped_credentials(workflow):
    job = workflow["jobs"]["update-metadata"]
    assert job["name"] == "Update metadata"
    assert set(job["needs"]) == {"prepare-matrix", "publish-sdist"}
    assert "strategy" not in job
    assert "env" not in job
    assert "continue-on-error" not in job
    checkout, setup, install, update = job["steps"]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {
        "repository": "${{ inputs.template_repository }}",
        "ref": "${{ inputs.template_ref }}",
        "path": "ci-template",
        "token": "${{ secrets.template_token || github.token }}",
        "persist-credentials": "false",
    }
    assert setup["uses"] == "actions/setup-python@v5"
    assert setup["with"]["python-version"] == "3.12"
    assert install["run"] == "python -m pip install --no-input requests packaging"
    assert update["env"] == {
        "PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}",
        "RELEASE_USERNAME": "${{ secrets.release_username }}",
        "RELEASE_TOKEN": "${{ secrets.release_token }}",
    }
    assert update["run"] == "python ci-template/.github/scripts/get_metadata.py"
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert "if" not in step
    for step in job["steps"][:-1]:
        assert "secrets.release_" not in str(step)
    assert "inputs.package_name" not in str(job)


@pytest.mark.parametrize("cancelled,should_release,result,published,expected", [
    (False, "true", "success", "true", True),
    (False, "true", "success", "false", False),
    (False, "true", "success", "", False),
    (False, "false", "success", "true", False),
    (False, "true", "failure", "true", False),
    (False, "true", "skipped", "", False),
    (False, "true", "cancelled", "true", False),
    (True, "true", "success", "true", False),
])
def test_update_metadata_gate_requires_actual_sdist_publication(
    workflow, cancelled, should_release, result, published, expected
):
    condition = workflow["jobs"]["update-metadata"]["if"]
    assert condition == (
        "${{ !cancelled() && needs.prepare-matrix.outputs.should_release == 'true' && "
        "needs.publish-sdist.result == 'success' && needs.publish-sdist.outputs.published == 'true' }}"
    )
    # 显式状态函数允许矩阵部分失败后继续，但不允许未上传、失败或取消时更新。
    values = {
        "!cancelled()": str(not cancelled),
        "needs.prepare-matrix.outputs.should_release": repr(should_release),
        "needs.publish-sdist.result": repr(result),
        "needs.publish-sdist.outputs.published": repr(published),
    }
    expression = condition.removeprefix("${{ ").removesuffix(" }}")
    for key, value in values.items():
        expression = expression.replace(key, value)
    assert eval(expression.replace("&&", "and"), {"__builtins__": {}}) is expected


def metadata_wheel(version=RELEASE_VERSION, torch="2.10.0", python="312"):
    return {
        "repo": "pypi_generic", "type": "file", "path": f"demo_package/{version}",
        "name": f"demo_package-1.0.0+hggcrt3torch{torch}ubuntu2404oe-cp{python}-cp{python}-linux_x86_64.whl",
        "stats": [{"downloads": 2}],
    }


def metadata_response(status, payload=None):
    return Mock(status_code=status, json=Mock(return_value=payload))


@pytest.fixture
def metadata_script(monkeypatch):
    spec = importlib.util.spec_from_file_location("metadata_under_test", CI_ROOT / "scripts/get_metadata.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.requests.sessions.Session, "request",
                        Mock(side_effect=AssertionError("测试禁止真实网络请求")))
    monkeypatch.setattr(module.requests, "post", Mock(return_value=metadata_response(
        200, {"results": [metadata_wheel()], "range": {"total": 1}}
    )))
    monkeypatch.setattr(module.requests, "get", Mock(return_value=metadata_response(404)))
    monkeypatch.setattr(module.requests, "put", Mock(return_value=metadata_response(201)))
    return module


@pytest.fixture
def metadata_env(monkeypatch, workspace):
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("PACKAGE_NAME", "demo-package")
    monkeypatch.setenv("RELEASE_USERNAME", "test-release-user")
    monkeypatch.setenv("RELEASE_TOKEN", "test-release-token")
    return workspace / "index.json"


def test_metadata_query_uses_normalized_target_and_publication_credentials(metadata_script):
    module = metadata_script
    result = module.get_index_json("demo-package", "test-release-user", "test-release-token")
    assert result == {"name": "demo_package", "release": [{
        "version": "1.0.0", "sub_version": "0.3.0", "torch": "2.10.0", "os": "ubuntu2404",
        "hggcrt": "hggcrt3", "python": "cp312", "edition": "oe",
        "sdk": {"2.2.0": {"supported": [], "unsupported": []}},
    }]}
    post = module.requests.post.call_args.kwargs
    assert post["url"] == "https://download.t-head.cn/artifactory/api/search/aql"
    query = json.loads(post["data"].removeprefix("items.find(").split(").include(")[0])
    assert query == {"repo": "pypi_generic", "path": {"$match": "demo_package/*"},
                     "name": {"$match": "*.whl"}}
    assert post["headers"] == {"Content-Type": "text/plain"}
    assert module.requests.get.call_args.args == (
        "https://download.t-head.cn/artifactory/pypi_generic/demo_package/index.json",
    )
    for request in (module.requests.post, module.requests.get):
        kwargs = request.call_args.kwargs
        assert kwargs["auth"] == ("test-release-user", "test-release-token")
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == (30, 300)
    module.requests.put.assert_not_called()


@pytest.mark.parametrize("sdk_value,expected", [
    (["2.3.0"], {"supported": ["2.3.0"], "unsupported": []}),
    ({"supported": ["2.3.0"], "unsupported": ["2.4.0"]},
     {"supported": ["2.3.0"], "unsupported": ["2.4.0"]}),
])
def test_metadata_preserves_existing_sdk_rules_and_adds_new_releases(metadata_script, sdk_value, expected):
    module = metadata_script
    old = module.get_index_json("demo_package", "user", "token")
    old["release"][0]["sdk"]["2.2.0"] = sdk_value
    module.requests.get.return_value = metadata_response(200, old)
    module.requests.post.return_value = metadata_response(200, {"results": [
        metadata_wheel(), metadata_wheel("1.0.0+v0.3.0.sail2.1.0"),
        metadata_wheel(torch="2.13.0"),
    ]})
    data = module.get_index_json("demo_package", "user", "token")
    assert len(data["release"]) == 2
    first, second = data["release"]
    assert first["torch"] == "2.10.0"
    assert list(first["sdk"]) == ["2.1.0", "2.2.0"]
    assert first["sdk"]["2.2.0"] == expected
    assert first["sdk"]["2.1.0"] == {"supported": [], "unsupported": []}
    assert second["torch"] == "2.13.0"
    assert second["sdk"]["2.2.0"] == {"supported": [], "unsupported": []}


@pytest.mark.parametrize("version,main,sub,sdk", [
    ("1.0.0+v0.3.0.sail2.2.0", "1.0.0", "0.3.0", "2.2.0"),
    ("1.0.0+sail2.1.0", "1.0.0", "", "2.1.0"),
])
def test_metadata_parses_both_release_version_formats(metadata_script, version, main, sub, sdk):
    assert metadata_script.parse_build_version(version) == (main, sub, sdk, "")


def test_metadata_cli_writes_index_and_uploads_exact_file_bytes(metadata_script, metadata_env, capsys):
    module = metadata_script
    uploaded = []

    def upload(url, **kwargs):
        uploaded.append(kwargs["data"].read())
        assert uploaded[-1] == metadata_env.read_bytes()
        return metadata_response(201)

    module.requests.put.side_effect = upload
    runpy.run_path(str(CI_ROOT / "scripts/get_metadata.py"), run_name="__main__")
    assert metadata_env.is_file()
    assert len(uploaded) == 1
    assert json.loads(uploaded[0])["name"] == "demo_package"
    assert json.loads(uploaded[0])["release"][0]["sdk"] == {
        "2.2.0": {"supported": [], "unsupported": []},
    }
    args, kwargs = module.requests.put.call_args
    assert args == ("https://download.t-head.cn/artifactory/pypi_generic/demo_package/index.json",)
    assert kwargs["auth"] == ("test-release-user", "test-release-token")
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (30, 300)
    output = capsys.readouterr()
    assert "已生成 index.json 并上传" in output.out
    assert "test-release-user" not in output.out + output.err
    assert "test-release-token" not in output.out + output.err


@pytest.mark.parametrize("stage", ["post", "get", "put"])
@pytest.mark.parametrize("status", [301, 302, 401, 403, 500])
def test_metadata_http_failure_blocks_success(metadata_script, metadata_env, capsys, stage, status):
    module = metadata_script
    getattr(module.requests, stage).return_value = metadata_response(status)
    with pytest.raises(SystemExit, match=f"更新元数据失败.*HTTP {status}"):
        runpy.run_path(str(CI_ROOT / "scripts/get_metadata.py"), run_name="__main__")
    if stage != "put":
        module.requests.put.assert_not_called()
        assert not metadata_env.exists()
    if stage == "post":
        module.requests.get.assert_not_called()
    assert "已生成 index.json 并上传" not in capsys.readouterr().out


@pytest.mark.parametrize("stage", ["post", "get", "put"])
def test_metadata_network_timeout_fails_job(metadata_script, metadata_env, stage):
    module = metadata_script
    getattr(module.requests, stage).side_effect = module.requests.Timeout("连接超时")
    with pytest.raises(SystemExit, match="更新元数据失败.*连接超时"):
        runpy.run_path(str(CI_ROOT / "scripts/get_metadata.py"), run_name="__main__")
    if stage != "put":
        module.requests.put.assert_not_called()
        assert not metadata_env.exists()


@pytest.mark.parametrize("stage", ["post", "get"])
def test_metadata_invalid_json_never_overwrites_index(metadata_script, metadata_env, stage):
    module = metadata_script
    response = metadata_response(200)
    response.json.side_effect = ValueError("JSON 解析失败")
    getattr(module.requests, stage).return_value = response
    with pytest.raises(ValueError, match="JSON 解析失败"):
        module.main()
    module.requests.put.assert_not_called()
    assert not metadata_env.exists()


@pytest.mark.parametrize("payload", [None, [], {}, {"results": None}, {"results": {}},
    {"results": [metadata_wheel()], "range": {"total": 2}},
])
def test_metadata_invalid_or_truncated_aql_never_uploads(metadata_script, metadata_env, payload):
    module = metadata_script
    module.requests.post.return_value = metadata_response(200, payload)
    with pytest.raises(ValueError):
        module.main()
    module.requests.get.assert_not_called()
    module.requests.put.assert_not_called()
    assert not metadata_env.exists()


@pytest.mark.parametrize("payload", [None, [], {}, {"release": None}, {"release": {}},
    {"name": "other_package", "release": []},
])
def test_metadata_invalid_old_index_never_uploads(metadata_script, metadata_env, payload):
    module = metadata_script
    module.requests.get.return_value = metadata_response(200, payload)
    with pytest.raises(ValueError, match="格式或包名不正确"):
        module.main()
    module.requests.put.assert_not_called()
    assert not metadata_env.exists()


@pytest.mark.parametrize("items", [[], [{**metadata_wheel(), "name": "invalid.whl"}],
    [metadata_wheel("1.0.0")],
])
def test_metadata_empty_or_unusable_wheels_never_replace_index(metadata_script, metadata_env, items):
    module = metadata_script
    module.requests.post.return_value = metadata_response(200, {"results": items})
    with pytest.raises(ValueError, match="拒绝上传空索引"):
        module.main()
    module.requests.put.assert_not_called()
    assert not metadata_env.exists()


@pytest.mark.parametrize("field,value", [
    ("PACKAGE_NAME", ""), ("PACKAGE_NAME", "../other"), ("PACKAGE_NAME", "x/y"),
    ("PACKAGE_NAME", "x\ny"), ("PACKAGE_NAME", "$(id)"),
    ("RELEASE_USERNAME", ""), ("RELEASE_TOKEN", ""),
])
def test_metadata_invalid_inputs_fail_before_network(metadata_script, metadata_env, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        metadata_script.main()
    for request in (metadata_script.requests.post, metadata_script.requests.get, metadata_script.requests.put):
        request.assert_not_called()
    assert not metadata_env.exists()


@pytest.mark.parametrize("target,expected", [
    ("deep-gemm-tools", "deep_gemm_tools"), ("Deep--Gemm.ext", "Deep__Gemm.ext"),
    ("deep.gemm", "deep.gemm"), ("deep_gemm", "deep_gemm"),
])
def test_metadata_target_normalization_matches_workflow(metadata_script, target, expected):
    assert metadata_script.normalize_target(target) == expected
    assert metadata_script.index_url(target) == (
        f"https://download.t-head.cn/artifactory/pypi_generic/{expected}/index.json"
    )


def test_metadata_local_write_failure_does_not_upload(metadata_script, metadata_env):
    metadata_env.mkdir()
    with pytest.raises(OSError):
        metadata_script.main()
    metadata_script.requests.put.assert_not_called()
