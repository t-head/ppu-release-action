"""wheel 重命名、重打包与上传的离线测试：模拟 SDK、revpkg 和 curl，不编译或联网。"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import quote

import pytest
import yaml


CI_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTORY_URL = "https://download.t-head.cn/artifactory/pypi_generic/"
RELEASE_VERSION = "1.2.3+v0.3.0.sail2.2.0"
WHEEL_NAME = "demo_package-1.2.3+dev5-cp310-cp310-linux_x86_64.whl"
RENAMED_WHEEL = "demo_package-1.2.3+hggcrt3torch2.10.0ubuntu2404oe-cp310-cp310-linux_x86_64.whl"

CURL_SHIM = r'''
python3() {
  "$TEST_PYTHON" "$@"
}
curl() {
  "$TEST_PYTHON" -c '
import json
import os
import sys

with open(os.environ["TEST_CURL_CALLS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
print(os.environ.get("TEST_HTTP_STATUS", "201"), end="")
sys.exit(int(os.environ.get("TEST_CURL_EXIT", "0")))
' "$@"
}
'''


REVPKG_SHIM = r'''
revpkg() {
  "$TEST_PYTHON" -c '
import argparse
import json
import os
from pathlib import Path
import sys

with open(os.environ["TEST_REPACK_CALLS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--new-version", required=True)
args = parser.parse_args()
assert Path(args.input).read_bytes() == b"test wheel data"
mode = os.environ["TEST_REPACK_MODE"]
filename = os.environ.get("TEST_REPACK_NAME") or (
    "demo_package-" + args.new_version + "-cp310-cp310-manylinux_2_28_x86_64.whl"
)
target = Path(args.output) / filename
if mode == "symlink":
    target.symlink_to(args.input)
elif mode == "directory":
    target.mkdir()
elif mode != "missing":
    target.write_bytes(b"" if mode == "empty" else b"repacked wheel data")
    if mode == "multiple":
        (Path(args.output) / "another-1.0.0-py3-none-any.whl").write_bytes(b"extra wheel")
sys.exit(int(os.environ["TEST_REPACK_EXIT"]))
' "$@"
}
'''


@pytest.fixture
def workflow():
    return yaml.load((CI_ROOT / "workflows/matrix-release.yml").read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


@pytest.fixture
def upload_step(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "上传 wheel 到 Artifactory")


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory(prefix=".upload-test-", dir=CI_ROOT) as directory:
        root = Path(directory)
        (root / "dist").mkdir()
        yield root


@pytest.fixture
def build_step(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "编译")


@pytest.fixture
def run_build(build_step, workspace):
    def run(filenames=(WHEEL_NAME,), build_command=None, **overrides):
        if build_command is None:
            build_command = "\n".join(
                f"printf 'test wheel data' > {shlex.quote('dist/' + name)}" for name in filenames
            ) or ":"
        env = dict(os.environ, BUILD_COMMAND=build_command)
        env.update(overrides)
        env.pop("WHL_NAME", None)
        # 只模拟 SDK 初始化，编译步骤保留构建命令生成的原始文件名。
        shim = 'source() { [[ "$1" == "/usr/local/PPU_SDK/envsetup.sh" ]]; }\n'
        return subprocess.run(["bash"], input=shim + build_step["run"], cwd=workspace,
                              env=env, text=True, capture_output=True, timeout=30)

    return run


@pytest.fixture
def collect_step(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "收集 wheel 到 dist")


@pytest.fixture
def run_collect(collect_step, workspace):
    def run(filenames=(), package_name="demo_package"):
        for filename in filenames:
            path = workspace / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test wheel data")
        env = dict(os.environ, PACKAGE_NAME=package_name, TEST_PYTHON=sys.executable)
        shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
        return subprocess.run(["bash"], input=shim + collect_step["run"], cwd=workspace,
                              env=env, text=True, capture_output=True, timeout=30)

    return run


@pytest.fixture
def repack_step(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "Repack wheel")


@pytest.fixture
def rename_step(workflow):
    return next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step.get("name") == "重命名 wheel")


@pytest.fixture
def run_rename(rename_step, workspace):
    def run(filenames=(WHEEL_NAME,), target=RENAMED_WHEEL):
        for filename in filenames:
            (workspace / "dist" / filename).write_bytes(b"test wheel data")
        env = dict(os.environ)
        if target is None:
            env.pop("WHL_NAME", None)
        else:
            env["WHL_NAME"] = target
        return subprocess.run(["bash"], input=rename_step["run"], cwd=workspace,
                              env=env, text=True, capture_output=True, timeout=30)

    return run


@pytest.fixture
def run_repack(repack_step, workspace):
    def run(**overrides):
        calls_file = workspace / "revpkg-calls.jsonl"
        env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_REPACK_CALLS=str(calls_file),
                   TEST_REPACK_MODE="file", TEST_REPACK_EXIT="0", TEST_REPACK_NAME="",
                   WHL_NAME=None, RELEASE_VERSION=RELEASE_VERSION)
        env.update(overrides)
        env = {key: value for key, value in env.items() if value is not None}
        args = shlex.split(repack_step["with"]["args"])
        assert args[:-1] == ["-euo", "pipefail", "-c"]
        # 仅将容器内临时路径映射到隔离目录；真实执行步骤的参数解析、cp 和输出校验。
        script = args[-1].replace("/tmp/origin", str(workspace / "origin"))
        script = script.replace("/tmp/release", str(workspace / "release"))
        result = subprocess.run([repack_step["with"]["entrypoint"], *args[:-1], REVPKG_SHIM + script],
                                cwd=workspace, env=env, text=True, capture_output=True, timeout=30)
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        return result, calls

    return run


@pytest.fixture
def run_upload(upload_step, workspace):
    def run(filenames=(WHEEL_NAME,), **overrides):
        for filename in filenames:
            (workspace / "dist" / filename).write_bytes(b"test wheel data")
        calls_file = workspace / "curl-calls.jsonl"
        env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_CURL_CALLS=str(calls_file),
                   TEST_HTTP_STATUS="201", TEST_CURL_EXIT="0", PYTHONDONTWRITEBYTECODE="1",
                   ARTIFACTORY_URL=upload_step["env"]["ARTIFACTORY_URL"], PACKAGE_NAME="demo_package",
                   RELEASE_VERSION=RELEASE_VERSION, WHL_NAME=filenames[0] if filenames else WHEEL_NAME,
                   RELEASE_USERNAME="test-release-user", RELEASE_TOKEN="test-release-token")
        env.update(overrides)
        result = subprocess.run(["bash"], input=CURL_SHIM + upload_step["run"], cwd=workspace,
                                env=env, text=True, capture_output=True, timeout=30)
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
        return result, calls

    return run


@pytest.fixture
def run_release_data(workflow, workspace):
    step = next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step["name"] == "生成发布数据 JSON")

    def run(matrix=None, package_name="demo_package", index="0", **overrides):
        if matrix is None:
            matrix = {
                "sdk_version": "2.2.0", "os_version": "ubuntu2404", "runtime_version": "3",
                "torch_version": "2.10.0", "python_version": "312", "tag": "v1.2.3+v0.3.0",
                "release_version": "v1.2.3+v0.3.0.sail2.2.0",
            }
        env = dict(os.environ, TEST_PYTHON=sys.executable, RUNNER_TEMP=str(workspace),
                   PACKAGE_NAME=package_name, MATRIX_JSON=json.dumps(matrix), MATRIX_INDEX=index,
                   GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="feature/current-branch")
        env.update(overrides)
        shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
        return subprocess.run(["bash"], input=shim + step["run"], cwd=workspace, env=env,
                              text=True, capture_output=True, timeout=30)

    return run


def test_upload_follows_rename_and_credentials_are_step_scoped(
    workflow, upload_step, repack_step, rename_step, collect_step
):
    job = workflow["jobs"]["matrix-job"]
    steps = job["steps"]
    index = steps.index(repack_step)
    assert steps[index - 2]["name"] == "编译"
    assert steps[index - 1] is collect_step
    assert collect_step["shell"] == "bash"
    assert collect_step["env"] == {"PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}"}
    assert "if" not in collect_step
    assert "continue-on-error" not in collect_step
    assert steps[index + 1] is rename_step
    assert steps[index + 2] is upload_step
    assert rename_step["shell"] == "bash"
    assert "if" not in rename_step
    assert "continue-on-error" not in rename_step
    assert repack_step["uses"] == "docker://download.t-head.cn/docker_tool/manylinux_revpkg:04"
    assert repack_step["with"]["entrypoint"] == "/bin/bash"
    assert "if" not in repack_step
    assert "continue-on-error" not in repack_step
    assert "${{" not in repack_step["with"]["args"]
    assert upload_step["name"] == "上传 wheel 到 Artifactory"
    assert upload_step["shell"] == "bash"
    assert "if" not in upload_step
    assert "continue-on-error" not in upload_step
    assert job["env"]["RELEASE_VERSION"] == "${{ matrix.release_version }}"
    assert job["env"]["WHL_NAME"] == "${{ matrix.whl_name }}"
    assert upload_step["env"] == {
        "ARTIFACTORY_URL": ARTIFACTORY_URL,
        "PACKAGE_NAME": "${{ needs.prepare-matrix.outputs.package_name }}",
        "RELEASE_USERNAME": "${{ secrets.release_username }}",
        "RELEASE_TOKEN": "${{ secrets.release_token }}",
    }
    for name in ("RELEASE_USERNAME", "RELEASE_TOKEN"):
        assert name not in job.get("env", {})
        assert name not in workflow.get("env", {})
        assert all(name not in step.get("env", {}) for step in job["steps"] if step is not upload_step)


def test_upload_shell_syntax(upload_step):
    result = subprocess.run(["bash", "-n"], input=upload_step["run"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ["200", "201", "204"])
def test_authenticated_upload_uses_release_version_and_encodes_segments(run_upload, status):
    result, calls = run_upload(TEST_HTTP_STATUS=status)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    args = calls[0]
    assert args[-1] == ARTIFACTORY_URL + "demo_package/" + quote(RELEASE_VERSION, safe="") + "/" + quote(WHEEL_NAME, safe="")
    assert args[args.index("--upload-file") + 1] == "dist/" + WHEEL_NAME
    assert args[args.index("--user") + 1] == "test-release-user:test-release-token"
    assert args[args.index("--proto") + 1] == "=https"
    assert args[args.index("--retry") + 1] == "3"
    assert args[args.index("--output") + 1] == "/dev/null"
    assert "--fail" in args
    assert "--location" not in args
    assert "--location-trusted" not in args
    assert "--insecure" not in args
    assert "--verbose" not in args
    assert "test-release-user" not in result.stdout + result.stderr
    assert "test-release-token" not in result.stdout + result.stderr
    assert "已上传 " + WHEEL_NAME in result.stdout


@pytest.mark.parametrize("version", [
    "2.4.0", "2.4.1.dev3+gabcdef123", "2.4.1.dev3+gabcdef123.d20260101",
])
def test_wheel_version_does_not_select_release_directory(run_upload, version):
    filename = f"demo_package-{version}-cp310-cp310-linux_x86_64.whl"
    result, calls = run_upload((filename,))
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0][-1] == (
        ARTIFACTORY_URL + "demo_package/" + quote(RELEASE_VERSION, safe="") + "/" + quote(filename, safe=""))


@pytest.mark.parametrize("filename", [
    "demo_package-1.2.3-2abc-py3-none-any.whl",
    "demo_package-1.2.3-cp312-cp312-linux_x86_64.whl",
])
def test_optional_build_tag_and_python_tag_do_not_change_release_directory(run_upload, filename):
    result, calls = run_upload((filename,))
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0][-1] == ARTIFACTORY_URL + "demo_package/" + quote(RELEASE_VERSION, safe="") + "/" + filename


def test_multiple_wheels_cannot_share_one_expected_name(run_upload):
    result, calls = run_upload((WHEEL_NAME, RENAMED_WHEEL))
    assert result.returncode != 0
    assert "上传 wheel 文件名与 WHL_NAME 不一致" in result.stderr
    assert calls == []


def test_distribution_name_normalization_preserves_requested_directory(run_upload):
    result, calls = run_upload(PACKAGE_NAME="Demo-Package")
    assert result.returncode == 0, result.stderr
    assert calls[0][-1].startswith(ARTIFACTORY_URL + "Demo-Package/" + quote(RELEASE_VERSION, safe="") + "/")


def test_no_wheel_fails_without_uploading_other_artifacts(workspace, run_upload):
    (workspace / "dist/source.tar.gz").write_bytes(b"not a wheel")
    result, calls = run_upload(())
    assert result.returncode != 0
    assert "::error::dist 目录中未找到 wheel 文件" in result.stderr
    assert calls == []


@pytest.mark.parametrize("credential", ["RELEASE_USERNAME", "RELEASE_TOKEN"])
def test_missing_credentials_fail_before_network(run_upload, credential):
    result, calls = run_upload(**{credential: ""})
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize("filename", [
    "invalid.whl",
    "demo_package-notversion-py3-none-any.whl",
    "demo_package-1.2.3-py3-none-any%2F.whl",
    "demo_package-1.2.3-py3-none-any\n.whl",
    "other_package-1.2.3-py3-none-any.whl",
])
def test_invalid_wheel_names_fail_before_network(run_upload, filename):
    result, calls = run_upload((filename,))
    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert calls == []


def test_all_paths_are_validated_before_first_upload(run_upload):
    result, calls = run_upload((WHEEL_NAME, "zzz_other-1.2.3-py3-none-any.whl"))
    assert result.returncode != 0
    assert "包名与 package_name 不一致" in result.stderr
    assert calls == []


@pytest.mark.parametrize("package_name", ["", "../other", "name|other"])
def test_invalid_package_path_is_rejected(run_upload, package_name):
    result, calls = run_upload(PACKAGE_NAME=package_name)
    assert result.returncode != 0
    assert calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_non_regular_wheels_are_rejected(workspace, run_upload, kind):
    path = workspace / "dist" / WHEEL_NAME
    if kind == "symlink":
        target = workspace / "unrelated-file"
        target.write_bytes(b"not an artifact")
        path.symlink_to(target)
    else:
        path.mkdir()
    result, calls = run_upload(())
    assert result.returncode != 0
    assert "上传产物必须是普通 wheel 文件" in result.stderr
    assert calls == []


@pytest.mark.parametrize("status", ["301", "302", "307", "401", "403", "409", "500"])
def test_non_success_status_stops_uploads(run_upload, status):
    result, calls = run_upload(TEST_HTTP_STATUS=status)
    assert result.returncode != 0
    assert "HTTP 状态码：" + status in result.stderr
    assert len(calls) == 1
    assert "已上传" not in result.stdout


@pytest.mark.parametrize("exit_code", ["7", "22", "28"])
def test_curl_failure_propagates(run_upload, exit_code):
    result, calls = run_upload(TEST_CURL_EXIT=exit_code, TEST_HTTP_STATUS="000")
    assert result.returncode == int(exit_code)
    assert len(calls) == 1
    assert "已上传" not in result.stdout


@pytest.mark.parametrize("runtime_version", ["2", "3"])
def test_runtime_environment_reaches_build_command(workflow, run_build, runtime_version):
    runtime_env = workflow["jobs"]["matrix-job"]["env"]["RUNTIME_VERSION"].replace(
        "${{ matrix.runtime_version }}", runtime_version)
    result = run_build(build_command='printf "%s\\n" "$RUNTIME_VERSION"', RUNTIME_VERSION=runtime_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == runtime_version + "\n"


def test_build_keeps_original_wheel_name(run_build, workspace):
    result = run_build()
    assert result.returncode == 0, result.stderr
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()


def test_rename_wheel_without_changing_bytes(run_rename, workspace):
    archive = workspace / "dist/source.tar.gz"
    archive.write_bytes(b"source archive")
    result = run_rename()
    assert result.returncode == 0, result.stderr
    assert not (workspace / "dist" / WHEEL_NAME).exists()
    assert (workspace / "dist" / RENAMED_WHEEL).read_bytes() == b"test wheel data"
    assert archive.read_bytes() == b"source archive"
    assert f"wheel 产物：dist/{RENAMED_WHEEL}" in result.stdout


def test_repacked_wheel_is_uploaded_under_release_version(
    run_build, run_collect, run_repack, run_rename, run_upload, workspace
):
    result = run_build()
    assert result.returncode == 0, result.stderr
    result = run_collect()
    assert result.returncode == 0, result.stderr
    result, _ = run_repack()
    assert result.returncode == 0, result.stderr
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"repacked wheel data"
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()
    result = run_rename(())
    assert result.returncode == 0, result.stderr
    result, calls = run_upload((), WHL_NAME=RENAMED_WHEEL)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    args = calls[0]
    assert args[args.index("--upload-file") + 1] == f"dist/{RENAMED_WHEEL}"
    assert args[-1] == ARTIFACTORY_URL + "demo_package/" + quote(RELEASE_VERSION, safe="") + "/" + quote(RENAMED_WHEEL, safe="")
    assert (workspace / "dist" / RENAMED_WHEEL).read_bytes() == b"repacked wheel data"
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [RENAMED_WHEEL]


@pytest.mark.parametrize("torch_version", ["2.10.0", "2.13.0"])
@pytest.mark.parametrize("package_name", ["deep_gemm", "deep-gemm"])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag_version,ref_type", [("", "tag"), ("2.4.0", "branch"), ("2.4.0+v0.5.0", "tag")])
def test_real_matrix_output_drives_repack_and_upload(
    workflow, run_build, run_collect, run_repack, run_rename, run_upload, run_release_data, workspace,
    torch_version, package_name, tag_version, ref_type, prefix
):
    inputs = {
        "package_name": package_name,
        "python_versions": '["312"]',
        "os_versions": '["ubuntu2404"]',
        "runtime_versions": '["3"]',
        "torch_versions": json.dumps([torch_version]),
        "sdk_versions": '["2.2.0"]',
        "tag_version": prefix + tag_version if tag_version else "",
    }
    tag = tag_version or "1.0.0+v0.3.0"
    public_version = tag.split("+", 1)[0]
    release_version = tag + (".sail2.2.0" if "+" in tag else "+sail2.2.0")
    normalized_output = workspace / "normalized-output"
    env = dict(os.environ, INPUTS_JSON=json.dumps(inputs), GITHUB_OUTPUT=str(normalized_output),
               TEST_PYTHON=sys.executable, GITHUB_REF_TYPE=ref_type, GITHUB_REF_NAME=prefix + "1.0.0+v0.3.0",
               PYTHONDONTWRITEBYTECODE="1")
    step = workflow["jobs"]["prepare-matrix"]["steps"][0]
    shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in normalized_output.read_text().splitlines())
    assert values["package_name"] == "deep_gemm"
    output = workspace / "matrix-output"
    env.update(INPUTS_JSON=values["inputs_json"], GITHUB_OUTPUT=str(output))
    result = subprocess.run([sys.executable, str(CI_ROOT / "scripts/prepare_matrix.py")],
                            cwd=workspace, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    matrix_outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert matrix_outputs["should_release"] == "true"
    entries = json.loads(matrix_outputs["matrix"])["include"]
    assert len(entries) == 1
    entry = entries[0]
    expected_name = (
        f"deep_gemm-{public_version}+hggcrt3torch{torch_version}ubuntu2404oe-"
        "cp312-cp312-linux_x86_64.whl"
    )
    assert entry["whl_name"] == expected_name
    assert entry["release_version"] == release_version
    assert entry["tag"] == tag

    original_name = "deep_gemm-1.0.0+v0.3.0-cp312-cp312-linux_x86_64.whl"
    build_directory = workspace / "component/build output"
    build_directory.mkdir(parents=True)
    result = run_build(build_command=(
        f"printf 'test wheel data' > {shlex.quote('component/build output/' + original_name)}\n"
        "printf 'dependency wheel' > dist/dependency-1.0.0-py3-none-any.whl"
    ))
    assert result.returncode == 0, result.stderr
    result = run_collect(package_name=values["package_name"])
    assert result.returncode == 0, result.stderr
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [original_name]
    assert (build_directory / original_name).read_bytes() == b"test wheel data"
    result, repack_calls = run_repack(
        RELEASE_VERSION=entry["release_version"],
        TEST_REPACK_NAME=f"deep_gemm-{release_version}-cp312-cp312-linux_x86_64.whl",
    )
    assert result.returncode == 0, result.stderr
    assert repack_calls == [[
        "--input", str(workspace / "origin" / original_name),
        "--output", str(workspace / "release"), "--new-version", entry["release_version"],
    ]]
    assert (workspace / "dist" / original_name).read_bytes() == b"repacked wheel data"
    assert not (workspace / "dist" / expected_name).exists()
    result = run_rename((), target=entry["whl_name"])
    assert result.returncode == 0, result.stderr
    result, calls = run_upload((), PACKAGE_NAME=values["package_name"],
                               RELEASE_VERSION=entry["release_version"], WHL_NAME=entry["whl_name"])
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0][-1] == (
        ARTIFACTORY_URL + "deep_gemm/" + quote(release_version, safe="") + "/" + quote(expected_name, safe=""))
    assert calls[0][calls[0].index("--upload-file") + 1] == "dist/" + expected_name
    assert (workspace / "dist" / expected_name).read_bytes() == b"repacked wheel data"
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [expected_name]
    result = run_release_data(matrix=entry, package_name=values["package_name"])
    assert result.returncode == 0, result.stderr
    payload = json.loads((workspace / "release-data/release-0.json").read_text())
    assert payload == {
        "artifactName": "deep_gemm", "sdkVersion": "2.2.0", "item": "deep_gemm",
        "osTypeList": "ubuntu2404", "runtimeVersion": "3", "framework": torch_version,
        "python": "312", "tag": tag,
    }
    assert json.loads(result.stdout) == payload


@pytest.mark.parametrize("directory", ["", "dist", "component/build output", ".artifacts/nested"])
@pytest.mark.parametrize("package_name", ["demo_package", "unrelated_package"])
def test_collect_single_wheel_keeps_original_name_and_bytes(run_collect, workspace, directory, package_name):
    source = Path(directory) / WHEEL_NAME
    result = run_collect((str(source),), package_name=package_name)
    assert result.returncode == 0, result.stderr
    assert (workspace / source).read_bytes() == b"test wheel data"
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert list((workspace / "dist").iterdir()) == [workspace / "dist" / WHEEL_NAME]
    assert "已收集 wheel：" in result.stdout


def test_collect_creates_missing_dist(run_collect, workspace):
    (workspace / "dist").rename(workspace / "unused-directory")
    result = run_collect(("component/" + WHEEL_NAME,))
    assert result.returncode == 0, result.stderr
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"


@pytest.mark.parametrize("directory", ["dist", "component/build output"])
@pytest.mark.parametrize("filename,package_name", [
    (WHEEL_NAME, "demo_package"),
    ("Demo-Package-1.2.3-py3-none-any.whl", "demo_package"),
    ("DEMO_PACKAGE-1.2.3-py3-none-any.whl", "Demo_Package"),
    ("demo_package_extra-1.2.3-py3-none-any.whl", "demo_package"),
    ("Demo.Package-1.2.3-py3-none-any.whl", "Demo.Package"),
])
def test_collect_multiple_filters_normalized_prefix_and_keeps_only_selected_in_dist(
    run_collect, workspace, directory, filename, package_name
):
    other = "dependency-1.0.0-py3-none-any.whl"
    archive = workspace / "dist/source.tar.gz"
    archive.write_bytes(b"source archive")
    hidden = workspace / "dist/.build-info"
    hidden.write_bytes(b"build info")
    result = run_collect((f"{directory}/{filename}", f"dist/{other}", f"vendor/{other}"), package_name)
    assert result.returncode == 0, result.stderr
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [filename]
    assert (workspace / "dist" / filename).read_bytes() == b"test wheel data"
    assert (workspace / directory / filename).read_bytes() == b"test wheel data"
    assert (workspace / "vendor" / other).read_bytes() == b"test wheel data"
    assert archive.read_bytes() == b"source archive"
    assert hidden.read_bytes() == b"build info"


@pytest.mark.parametrize("filenames,error", [
    ((), "未找到 .whl 文件"),
    (("vendor/other.whl", "dist/dependency.whl"), "实际为 0 个"),
    (("build/" + WHEEL_NAME, "other/" + WHEEL_NAME), "实际为 2 个"),
    (("build/" + WHEEL_NAME, "dist/" + WHEEL_NAME), "实际为 2 个"),
    (("build/" + WHEEL_NAME, "dist/Demo-Package-2.0.0-py3-none-any.whl"), "实际为 2 个"),
    (("build/demo.package-1.0.0-py3-none-any.whl", "dist/dependency.whl"), "实际为 0 个"),
])
def test_collect_missing_or_ambiguous_wheels_fails_before_changes(run_collect, workspace, filenames, error):
    archive = workspace / "dist/source.tar.gz"
    archive.write_bytes(b"source archive")
    result = run_collect(filenames)
    assert result.returncode != 0
    assert error in result.stderr
    assert "已收集 wheel" not in result.stdout
    for filename in filenames:
        assert filename in result.stdout
        assert (workspace / filename).read_bytes() == b"test wheel data"
    assert {path.name for path in (workspace / "dist").glob("*.whl")} == {
        Path(name).name for name in filenames if Path(name).parent == Path("dist")
    }
    assert archive.read_bytes() == b"source archive"


def test_collect_does_not_follow_symlinks_or_treat_directories_as_wheels(run_collect, workspace):
    source = workspace / "build"
    source.mkdir()
    (source / WHEEL_NAME).write_bytes(b"test wheel data")
    (workspace / "linked-directory").symlink_to(source, target_is_directory=True)
    (workspace / "demo_package-linked.whl").symlink_to(source / WHEEL_NAME)
    (workspace / "fake.whl").mkdir()
    result = run_collect()
    assert result.returncode == 0, result.stderr
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert json.loads(result.stdout.splitlines()[0].split("：", 1)[1]) == ["build/" + WHEEL_NAME]


@pytest.mark.parametrize("kind", ["directory", "symlink", "broken_symlink"])
def test_collect_rejects_non_regular_dist_wheels_before_copying(run_collect, workspace, kind):
    invalid = workspace / "dist/other.whl"
    target = workspace / "unrelated-file"
    if kind == "directory":
        invalid.mkdir()
    else:
        if kind == "symlink":
            target.write_bytes(b"unrelated data")
        invalid.symlink_to(target)
    result = run_collect(("build/" + WHEEL_NAME,))
    assert result.returncode != 0
    assert "dist 中的 wheel 必须是普通文件" in result.stderr
    assert not (workspace / "dist" / WHEEL_NAME).exists()
    assert (workspace / "build" / WHEEL_NAME).read_bytes() == b"test wheel data"
    if kind == "symlink":
        assert target.read_bytes() == b"unrelated data"


@pytest.mark.parametrize("kind", ["file", "symlink", "broken_symlink"])
def test_collect_rejects_invalid_dist_directory(run_collect, workspace, kind):
    dist = workspace / "dist"
    unused = workspace / "unused-directory"
    dist.rename(unused)
    if kind == "file":
        dist.write_bytes(b"not a directory")
    else:
        dist.symlink_to(unused if kind == "symlink" else workspace / "missing", target_is_directory=True)
    result = run_collect(("build/" + WHEEL_NAME,))
    assert result.returncode != 0
    assert "dist 必须是普通目录" in result.stderr
    assert list(unused.iterdir()) == []
    assert (workspace / "build" / WHEEL_NAME).read_bytes() == b"test wheel data"


def test_collect_rejects_empty_package_name(run_collect, workspace):
    result = run_collect(("build/" + WHEEL_NAME,), package_name="")
    assert result.returncode != 0
    assert "package_name 不能为空" in result.stderr
    assert list((workspace / "dist").iterdir()) == []


def test_repack_shell_syntax(repack_step):
    args = shlex.split(repack_step["with"]["args"])
    assert args[:-1] == ["-euo", "pipefail", "-c"]
    assert 'revpkg --input "/tmp/origin/${WHEEL_FILE}" --output /tmp/release --new-version "${RELEASE_VERSION}"' in args[-1]
    result = subprocess.run(["bash", "-n"], input=args[-1], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("output_name", [None, WHEEL_NAME])
@pytest.mark.parametrize("version", [RELEASE_VERSION, "v1.0.0+v0.3.0.sail2.2.0"])
def test_repack_replaces_only_wheel_content(run_build, run_repack, workspace, output_name, version):
    result = run_build()
    assert result.returncode == 0, result.stderr
    archive = workspace / "dist/source.tar.gz"
    archive.write_bytes(b"source archive")
    result, calls = run_repack(TEST_REPACK_NAME=output_name, RELEASE_VERSION=version)
    assert result.returncode == 0, result.stderr
    assert calls == [[
        "--input", str(workspace / "origin" / WHEEL_NAME),
        "--output", str(workspace / "release"), "--new-version", version,
    ]]
    assert (workspace / "origin" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"repacked wheel data"
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [WHEEL_NAME]
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()
    assert archive.read_bytes() == b"source archive"


@pytest.mark.parametrize("exit_code", [7, 23, 127])
def test_repack_failure_preserves_original_and_propagates(run_build, run_repack, workspace, exit_code):
    result = run_build()
    assert result.returncode == 0, result.stderr
    result, calls = run_repack(TEST_REPACK_EXIT=str(exit_code))
    assert result.returncode == exit_code
    assert len(calls) == 1
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert "已重打包" not in result.stdout


@pytest.mark.parametrize("mode", ["missing", "multiple", "symlink", "directory", "empty"])
def test_invalid_repack_output_preserves_original(run_build, run_repack, workspace, mode):
    result = run_build()
    assert result.returncode == 0, result.stderr
    result, calls = run_repack(TEST_REPACK_MODE=mode)
    assert result.returncode != 0
    assert "::error::重打包" in result.stderr
    assert len(calls) == 1
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert "已重打包" not in result.stdout


def test_stale_repack_output_is_rejected(run_build, run_repack, workspace):
    result = run_build()
    assert result.returncode == 0, result.stderr
    (workspace / "release").mkdir()
    (workspace / "release" / WHEEL_NAME).write_bytes(b"stale wheel")
    result, calls = run_repack()
    assert result.returncode != 0
    assert "重打包输出目录已存在 wheel 文件" in result.stderr
    assert calls == []
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"


@pytest.mark.parametrize("filenames", [(), (RENAMED_WHEEL, WHEEL_NAME)])
@pytest.mark.parametrize("other_files", [(), ("source.tar.gz", ".hidden-file", "build log.txt")])
def test_repack_requires_exactly_one_input(run_repack, workspace, filenames, other_files):
    for name in (*filenames, *other_files):
        (workspace / "dist" / name).write_bytes(b"test artifact contents")
    result, calls = run_repack()
    assert result.returncode == 1
    assert "重打包前 dist 目录必须包含且仅包含一个 wheel 文件" in result.stderr
    assert "dist 目录文件列表（含隐藏文件）：" in result.stderr
    for name in (*filenames, *other_files):
        assert name in result.stderr
        assert (workspace / "dist" / name).read_bytes() == b"test artifact contents"
    assert "test artifact contents" not in result.stdout + result.stderr
    assert calls == []


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_repack_rejects_invalid_input(run_repack, workspace, kind):
    wheel = workspace / "dist" / WHEEL_NAME
    if kind == "symlink":
        target = workspace / "unrelated-file"
        target.write_bytes(b"unrelated data")
        wheel.symlink_to(target)
    else:
        wheel.mkdir()
    result, calls = run_repack()
    assert result.returncode != 0
    assert "待重打包的 wheel 必须是普通文件" in result.stderr
    assert calls == []


@pytest.mark.parametrize("version", [None, ""])
def test_repack_requires_release_version(run_build, run_repack, workspace, version):
    result = run_build()
    assert result.returncode == 0, result.stderr
    result, calls = run_repack(RELEASE_VERSION=version)
    assert result.returncode != 0
    assert "RELEASE_VERSION" in result.stderr
    assert calls == []
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"


def test_already_named_wheel_is_kept(run_rename, workspace):
    result = run_rename(filenames=(RENAMED_WHEEL,))
    assert result.returncode == 0, result.stderr
    assert [path.name for path in (workspace / "dist").glob("*.whl")] == [RENAMED_WHEEL]
    assert (workspace / "dist" / RENAMED_WHEEL).read_bytes() == b"test wheel data"


@pytest.mark.parametrize("filenames", [(), (WHEEL_NAME, "another-1.0.0-py3-none-any.whl")])
def test_rename_requires_exactly_one_wheel(run_rename, workspace, filenames):
    result = run_rename(filenames=filenames)
    assert result.returncode != 0
    assert "必须包含且仅包含一个 wheel 文件" in result.stderr
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()
    for name in filenames:
        assert (workspace / "dist" / name).read_bytes() == b"test wheel data"


@pytest.mark.parametrize("target", [
    None, "", "../escape.whl", "/tmp/escape.whl", "dist/nested.whl", "bad name.whl",
    "bad\nname.whl", "name.txt", "$(id).whl", "dir\\escape.whl",
])
def test_invalid_target_leaves_original_wheel(run_rename, workspace, target):
    result = run_rename(target=target)
    assert result.returncode != 0
    assert "WHL_NAME" in result.stderr
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_non_regular_source_cannot_be_renamed(run_rename, workspace, kind):
    source = workspace / "dist" / WHEEL_NAME
    if kind == "directory":
        source.mkdir()
    else:
        target = workspace / "unrelated"
        target.write_bytes(b"unrelated data")
        source.symlink_to(target)
    result = run_rename(filenames=())
    assert result.returncode != 0
    assert "待重命名的 wheel 必须是普通文件" in result.stderr
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()
    if kind == "symlink":
        assert target.read_bytes() == b"unrelated data"


@pytest.mark.parametrize("kind", ["file", "directory", "broken_symlink"])
def test_existing_destination_is_never_overwritten(run_rename, workspace, kind):
    target = workspace / "dist" / RENAMED_WHEEL
    if kind == "file":
        target.write_bytes(b"existing data")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(workspace / "missing-target")
    result = run_rename()
    assert result.returncode != 0
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"test wheel data"
    if kind == "file":
        assert target.read_bytes() == b"existing data"
    elif kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_symlink()


def test_failed_build_does_not_rename_output(run_build, workspace):
    command = f"printf 'partial build' > {shlex.quote('dist/' + WHEEL_NAME)}; exit 7"
    result = run_build(build_command=command)
    assert result.returncode == 7
    assert (workspace / "dist" / WHEEL_NAME).read_bytes() == b"partial build"
    assert not (workspace / "dist" / RENAMED_WHEEL).exists()
    assert "wheel 产物" not in result.stdout


def test_build_collect_and_rename_shell_syntax(build_step, collect_step, rename_step):
    for step in (build_step, collect_step, rename_step):
        result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_requested_exact_release_path(run_upload):
    filename = "deep_gemm-1.0.0+hggcrt3torch2.10.0ubuntu2404oe-cp310-cp310-linux_x86_64.whl"
    result, calls = run_upload((filename,), PACKAGE_NAME="deep_gemm",
                              RELEASE_VERSION="1.0.0+v0.3.0.sail2.2.0", WHL_NAME=filename)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0][-1] == (
        "https://download.t-head.cn/artifactory/pypi_generic/deep_gemm/"
        "1.0.0%2Bv0.3.0.sail2.2.0/"
        "deep_gemm-1.0.0%2Bhggcrt3torch2.10.0ubuntu2404oe-cp310-cp310-linux_x86_64.whl"
    )


@pytest.mark.parametrize("version", ["1.2.3+v0.3.0.sail2.1.0", "v1.2.3+v0.4.0.sail2.2.0"])
def test_release_directory_comes_from_matrix_value(run_upload, version):
    result, calls = run_upload(RELEASE_VERSION=version)
    assert result.returncode == 0, result.stderr
    assert calls[0][-1] == ARTIFACTORY_URL + "demo_package/" + quote(version, safe="") + "/" + quote(WHEEL_NAME, safe="")


@pytest.mark.parametrize("version", ["", ".", "..", "../outside", "a/b", "a\\b", "a%2Fb", "a b", "a\nb", "a?b"])
def test_invalid_release_directory_is_rejected_before_upload(run_upload, version):
    result, calls = run_upload(RELEASE_VERSION=version)
    assert result.returncode != 0
    assert "RELEASE_VERSION" in result.stderr
    assert calls == []


@pytest.mark.parametrize("name", ["", "different-1.0.0-py3-none-any.whl", "../escape.whl", "a\nb.whl"])
def test_missing_or_mismatched_expected_wheel_name_is_rejected(run_upload, name):
    result, calls = run_upload(WHL_NAME=name)
    assert result.returncode != 0
    assert "WHL_NAME" in result.stderr
    assert calls == []


@pytest.mark.parametrize("sdk,os_type,runtime,torch,python,tag", [
    ("2.2.0", "ubuntu2404", "3", "2.13.0", "312", "v1.0.0+v0.3.0"),
    ("2.1.0", "ubuntu2204", "2", "2.8.0", "310", "1.2.3"),
    ("2.1.0", "alios7u2", "2", "", "38", "1.0.0+v0.3.0"),
    ("2.2.0", "ubuntu2004", "3", "2.10.0", "310", "v2.0.0"),
])
def test_release_json_keeps_requested_matrix_values_and_original_tag(
    run_release_data, workspace, sdk, os_type, runtime, torch, python, tag
):
    matrix = {
        "sdk_version": sdk, "os_version": os_type, "runtime_version": runtime,
        "torch_version": torch, "python_version": python, "tag": tag,
        "release_version": f"{tag}.sail{sdk}", "image": "unused-image",
    }
    result = run_release_data(matrix, package_name="Deep__Gemm.ext",
                              RELEASE_USERNAME="test-release-user", RELEASE_TOKEN="test-release-token")
    assert result.returncode == 0, result.stderr
    output = workspace / "release-data/release-0.json"
    expected = {
        "artifactName": "Deep__Gemm.ext", "sdkVersion": sdk, "item": "Deep__Gemm.ext",
        "osTypeList": os_type, "runtimeVersion": runtime, "framework": torch,
        "python": python, "tag": tag,
    }
    assert json.loads(output.read_text()) == expected
    assert result.stdout == output.read_text()
    assert output.read_text().endswith("\n")
    assert all(isinstance(value, str) for value in json.loads(output.read_text()).values())
    assert "test-release-user" not in result.stdout + result.stderr
    assert "test-release-token" not in result.stdout + result.stderr
    assert "feature/current-branch" not in result.stdout


def test_release_json_filenames_do_not_collide_when_artifacts_are_merged(run_release_data, workspace):
    for index in ("0", "1", "255"):
        result = run_release_data(index=index)
        assert result.returncode == 0, result.stderr
    assert {path.name for path in (workspace / "release-data").glob("*.json")} == {
        "release-0.json", "release-1.json", "release-255.json",
    }


@pytest.mark.parametrize("index", ["", "../outside", "1/2", "-1", "0\n"])
def test_release_json_rejects_invalid_file_index(run_release_data, workspace, index):
    result = run_release_data(index=index)
    assert result.returncode != 0
    assert "矩阵索引必须是非负整数" in result.stderr
    assert not (workspace / "release-data").exists()


def test_release_json_refuses_to_overwrite_previous_record(run_release_data, workspace):
    result = run_release_data()
    assert result.returncode == 0, result.stderr
    output = workspace / "release-data/release-0.json"
    original = output.read_bytes()
    result = run_release_data(package_name="other_package")
    assert result.returncode != 0
    assert output.read_bytes() == original


def test_release_json_without_code_tag_fails_instead_of_using_branch_name(run_release_data, workspace):
    result = run_release_data(matrix={
        "sdk_version": "2.2.0", "os_version": "ubuntu2404", "runtime_version": "3",
        "torch_version": "2.10.0", "python_version": "312",
    })
    assert result.returncode != 0
    assert not (workspace / "release-data").exists()


def test_release_json_shell_syntax(workflow):
    step = next(step for step in workflow["jobs"]["matrix-job"]["steps"]
                if step["name"] == "生成发布数据 JSON")
    result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
