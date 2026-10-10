"""源码包及安装模板的离线测试：验证打包、版本读取和运行时匹配，不编译或下载。"""

import ast
from email.parser import Parser
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import create_autospec

from packaging.utils import canonicalize_name, parse_sdist_filename
from packaging.version import Version
import pytest
import yaml


CI_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = CI_ROOT / "workflows/matrix-release.yml"
SETUP_TEMPLATE = CI_ROOT / "scripts/setup.py"

# tag 读取及源码包构建均使用真实代码；执行 setup.py 前断言 FAKE_WHEEL，防止下载。
PYTHON_SHIM = r'''
python() {
  if [[ "${1:-}" == "setup.py" ]]; then
    [[ "${__FAKE_BUILD:-}" == "FAKE_WHEEL" ]] || return 42
    [[ "${TEST_SDIST_FAIL:-}" != "1" ]] || return 23
    [[ "${PWD##*/}" != "${TEST_SDIST_FAIL_VERSION:-}" ]] || return 23
  fi
  "$TEST_PYTHON" "$@"
}
'''


@pytest.fixture
def workflow():
    # BaseLoader 保留 GitHub Actions 的 on 键，不按 YAML 1.1 转换为布尔值。
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.fixture
def build_step(workflow):
    return next(step for step in workflow["jobs"]["build-sdist"]["steps"]
                if step.get("name") == "填充模板并构建源码包")


@pytest.fixture
def workspace():
    # 临时构建只写入工作区，测试结束后自动清理。
    with tempfile.TemporaryDirectory(prefix=".sdist-test-", dir=CI_ROOT) as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        (source / "setup.py").write_text("# 调用方原始构建脚本\n", encoding="utf-8")
        template = root / "ci-template/.github/scripts/setup.py"
        template.parent.mkdir(parents=True)
        template.write_bytes(SETUP_TEMPLATE.read_bytes())
        (template.parent / "prepare_matrix.py").write_bytes((CI_ROOT / "scripts/prepare_matrix.py").read_bytes())
        yield root


@pytest.fixture
def run_build(build_step, workspace):
    def run(version="1.2.3+sail2.2.0", package_name="demo_package", framework="torch",
            versions=None, **overrides):
        versions = [version] if versions is None else versions
        env = dict(os.environ, TEST_PYTHON=sys.executable,
                   GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="9.9.9",
                   MATRIX_JSON=json.dumps({"include": [{"release_version": v} for v in versions]}),
                   pkg_name=package_name, framework=framework, PYTHONDONTWRITEBYTECODE="1")
        env.pop("__FAKE_BUILD", None)
        env.pop("TEST_SDIST_FAIL", None)
        env.pop("TEST_SDIST_FAIL_VERSION", None)
        env.update(overrides)
        return subprocess.run(["bash"], input=PYTHON_SHIM + build_step["run"],
                              cwd=workspace, env=env, text=True, capture_output=True,
                              timeout=60)

    return run


def test_sdist_follows_matrix_preparation_and_precedes_wheel_build(workflow):
    jobs = workflow["jobs"]
    assert jobs["build-sdist"]["needs"] == "prepare-matrix"
    assert set(jobs["matrix-job"]["needs"]) == {"prepare-matrix", "build-sdist"}
    assert jobs["matrix-job"]["strategy"]["matrix"] == "${{ fromJSON(needs.prepare-matrix.outputs.matrix) }}"
    assert jobs["build-sdist"]["if"] == "${{ needs.prepare-matrix.outputs.should_release == 'true' }}"
    assert "continue-on-error" not in jobs["build-sdist"]


def test_source_and_template_checkouts_are_separate(workflow):
    source, template = workflow["jobs"]["build-sdist"]["steps"][:2]
    assert source["uses"] == template["uses"] == "actions/checkout@v4"
    assert source["with"]["path"] == "source"
    assert source["with"]["fetch-depth"] == "0"
    assert "repository" not in source["with"]
    assert "ref" not in source["with"]
    assert template["with"]["path"] == "ci-template"
    assert template["with"]["repository"] == "${{ inputs.template_repository }}"
    assert template["with"]["ref"] == "${{ inputs.template_ref }}"
    assert template["with"]["token"] == "${{ secrets.template_token || github.token }}"
    assert source["with"]["persist-credentials"] == template["with"]["persist-credentials"] == "false"


def test_inputs_and_artifact_configuration(workflow, build_step):
    job = workflow["jobs"]["build-sdist"]
    assert "container" not in job
    assert build_step["env"] == {
        "pkg_name": "${{ needs.prepare-matrix.outputs.package_name }}",
        "framework": "${{ inputs.framework }}",
        "MATRIX_JSON": "${{ needs.prepare-matrix.outputs.matrix }}",
    }
    assert "release_token" not in str(job)
    upload = job["steps"][-1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == "${{ needs.prepare-matrix.outputs.package_name }}-sdist"
    assert upload["with"]["path"] == "sdist/dist/*.tar.gz"
    assert upload["with"]["if-no-files-found"] == "error"


def test_sdist_shell_syntax(workflow):
    for step in workflow["jobs"]["build-sdist"]["steps"]:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"],
                                    text=True, capture_output=True)
            assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version,framework,expected_version", [
    ("1.2.3+sail2.2.0", "torch", "1.2.3+sail2.2.0"),
    ("1.0.0+v0.3.0.sail2.2.0", "torch", "1.0.0+v0.3.0.sail2.2.0"),
    ("1.2.4.dev5+gabcdef.sail2.1.0", "torch", "1.2.4.dev5+gabcdef.sail2.1.0"),
    ("2.0.0rc1+old.sail2.2.0", "", "2.0.0rc1+old.sail2.2.0"),
    ("v1.0.0+V0.3.0.sail2.2.0", "torch", "1.0.0+v0.3.0.sail2.2.0"),
])
def test_builds_sdist_with_rendered_metadata(workspace, run_build, version, framework, expected_version):
    result = run_build(version=version, framework=framework)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "source/setup.py").read_text() == "# 调用方原始构建脚本\n"
    assert (workspace / "ci-template/.github/scripts/setup.py").read_bytes() == SETUP_TEMPLATE.read_bytes()

    rendered = (workspace / "sdist/builds" / version / "setup.py").read_text(encoding="utf-8")
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(rendered).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"PACKAGE_NAME", "framework", "build_version"}
    }
    assert assignments == {
        "PACKAGE_NAME": "demo_package",
        "framework": framework,
        "build_version": version,
    }
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        prefix = archive.getnames()[0].split("/")[0]
        metadata = Parser().parsestr(archive.extractfile(f"{prefix}/PKG-INFO").read().decode("utf-8"))
        assert canonicalize_name(metadata["Name"]) == "demo-package"
        assert metadata["Version"] == expected_version
        assert metadata["Requires-Python"] == ">=3.8"
        assert archive.extractfile(f"{prefix}/setup.py").read().decode("utf-8") == rendered
        assert not any("ci-template" in name or "/source/" in name for name in archive.getnames())


def test_hyphenated_input_uses_normalized_name_in_sdist(workflow, workspace, run_build):
    step = workflow["jobs"]["prepare-matrix"]["steps"][0]
    output = workspace / "normalized-output"
    env = dict(os.environ, INPUTS_JSON=json.dumps({"package_name": "demo-package"}),
               GITHUB_OUTPUT=str(output), TEST_PYTHON=sys.executable)
    shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["package_name"] == "demo_package"
    result = run_build(package_name=values["package_name"])
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = workspace / "sdist/builds/1.2.3+sail2.2.0/setup.py"
    assert 'PACKAGE_NAME = "demo_package"' in rendered.read_text()
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert [path.name for path in archives] == ["demo_package-1.2.3+sail2.2.0.tar.gz"]
    with tarfile.open(archives[0]) as archive:
        template = archive.extractfile("demo_package-1.2.3+sail2.2.0/setup.py").read().decode()
        assert 'PACKAGE_NAME = "demo_package"' in template


@pytest.mark.parametrize("field,value", [
    ("package_name", ""),
    ("package_name", "../outside"),
    ("package_name", "bad|name"),
    ("package_name", 'bad"name'),
    ("package_name", "$(printf unsafe)"),
    ("package_name", "name\nother"),
    ("framework", "torch|other"),
    ("framework", 'torch"'),
    ("framework", "$(printf unsafe)"),
    ("framework", "torch\nother"),
])
def test_rejects_unsafe_metadata_before_rendering(workspace, run_build, field, value):
    result = run_build(**{field: value})
    assert result.returncode != 0
    assert f"::error::{field}" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("field", ["PACKAGE_NAME", "framework", "build_version"])
def test_missing_placeholder_fails(workspace, run_build, field):
    template = workspace / "ci-template/.github/scripts/setup.py"
    template.write_text(template.read_text().replace(f'{field} = ""', f'{field} = "changed"'), encoding="utf-8")
    result = run_build()
    assert result.returncode != 0
    assert "::error::源码包模板缺少预期占位字段" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("version", [
    "", "not-a-version", "1..2", "1.0.0+v0.3.0|other", '1.0.0+bad"',
    "1.0.0+$(id)", "1.0.0+bad\nvalue", "../1.0.0",
])
def test_invalid_release_version_stops_build(workspace, run_build, version):
    result = run_build(version=version)
    assert result.returncode != 0
    assert not (workspace / "sdist").exists()


@pytest.mark.parametrize("matrix", [
    "", "[", "null", "[]", "{}", '{"include": []}', '{"include": {}}',
    '{"include": [null]}', '{"include": [{}]}',
    '{"include": [{"release_version": null}]}',
    '{"include": [{"release_version": 123}]}',
    '{"include": [{"release_version": "1.0.0+sail2.2.0"}, {}]}',
])
def test_invalid_matrix_fails_before_any_build(workspace, run_build, matrix):
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode != 0
    assert "release_version" in result.stderr
    assert not (workspace / "sdist").exists()


@pytest.fixture
def generate_matrix(workspace):
    def run(tag_version="", **overrides):
        inputs = {
            "tag_version": tag_version,
            "package_name": "demo_package", "python_versions": '["312"]',
            "os_versions": '["ubuntu2404"]', "runtime_versions": '["3"]',
            "torch_versions": '["2.10.0", "2.13.0"]', "sdk_versions": '["2.2.0"]',
        }
        output = workspace / "matrix-output"
        env = dict(os.environ, INPUTS_JSON=json.dumps(inputs), SOURCE_DIR="source",
                   GITHUB_OUTPUT=str(output), GITHUB_REF_TYPE="tag",
                   GITHUB_REF_NAME="1.0.0+v0.3.0", PYTHONDONTWRITEBYTECODE="1")
        env.update(overrides)
        result = subprocess.run(
            [sys.executable, "ci-template/.github/scripts/prepare_matrix.py"],
            cwd=workspace, env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in output.read_text().splitlines())["matrix"]
    return run


@pytest.fixture
def tag_repository(workspace, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Tag Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tag-test@example.invalid")

    def git(*args, directory="source"):
        return subprocess.run(["git", *args], cwd=workspace / directory, check=True,
                              text=True, capture_output=True)

    for directory in ("source", "ci-template"):
        git("init", "--quiet", directory=directory)
        git("add", ".", directory=directory)
        git("commit", "--quiet", "-m", "初始化测试仓库", directory=directory)
    git("tag", "9.9.9", directory="ci-template")
    return git


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_branch_sdist_uses_matrix_release_version(
    workspace, run_build, tag_repository, generate_matrix, annotated, prefix
):
    tag_repository("tag", "0.9.0")
    tag_repository("commit", "--allow-empty", "--quiet", "-m", "待发布提交")
    tag = prefix + "1.0.0+v0.3.0"
    if annotated:
        tag_repository("tag", "-a", tag, "-m", "版本 tag")
    else:
        tag_repository("tag", tag)
    matrix = generate_matrix(GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert len(json.loads(matrix)["include"]) == 2
    result = run_build(MATRIX_JSON=matrix, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert result.returncode == 0, result.stdout + result.stderr
    release_version = "1.0.0+v0.3.0.sail2.2.0"
    rendered = workspace / "sdist/builds" / release_version / "setup.py"
    assert f'build_version = "{release_version}"' in rendered.read_text()
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert [path.name for path in archives] == [f"demo_package-{release_version}.tar.gz"]
    assert result.stdout.count("已构建源码包版本：") == 1


@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_tag_sdist_uses_prepared_release_version(workspace, run_build, tag_repository, generate_matrix, prefix):
    tag = prefix + "1.0.0+v0.3.0"
    tag_repository("tag", tag)
    tag_repository("tag", "-a", "2.0.0", "-m", "同提交的其他 tag")
    matrix = generate_matrix(GITHUB_REF_NAME=tag)
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = workspace / "sdist/builds/1.0.0+v0.3.0.sail2.2.0/setup.py"
    assert 'build_version = "1.0.0+v0.3.0.sail2.2.0"' in rendered.read_text()


@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag_version,release_version", [
    ("2.4.0", "2.4.0+sail2.2.0"),
    ("2.4.0+v0.5.0", "2.4.0+v0.5.0.sail2.2.0"),
])
def test_tag_version_drives_sdist_build_and_publication(
    workflow, workspace, generate_matrix, run_build, tag_version, release_version, prefix
):
    # source 目录没有 Git 仓库；覆盖版本不依赖 tag 存在，两个 Torch 矩阵只构建一个源码包。
    matrix = generate_matrix(tag_version=prefix + tag_version, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    entries = json.loads(matrix)["include"]
    assert len(entries) == 2
    assert {entry["tag"] for entry in entries} == {tag_version}
    assert {entry["release_version"] for entry in entries} == {release_version}
    result = run_build(MATRIX_JSON=matrix)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("已构建源码包版本：") == 1
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == 1
    archive_path = archives[0]
    assert parse_sdist_filename(archive_path.name)[1] == Version(release_version)
    with tarfile.open(archive_path) as archive:
        root = archive.getnames()[0].split("/")[0]
        metadata = Parser().parsestr(archive.extractfile(f"{root}/PKG-INFO").read().decode())
        assert metadata["Version"] == str(Version(release_version))
        template = archive.extractfile(f"{root}/setup.py").read().decode()
        assert f'build_version = "{release_version}"' in template

    download = workspace / "sdist-dist"
    download.mkdir()
    shutil.copy2(archive_path, download / archive_path.name)
    step = workflow["jobs"]["publish-sdist"]["steps"][-1]
    calls = workspace / "upload-calls"
    shim = '''python3() { "$TEST_PYTHON" "$@"; }
    twine() { printf '%s\\n' "${@: -1}" >> "$TEST_UPLOAD_CALLS"; }
    '''
    env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_UPLOAD_CALLS=str(calls),
               MATRIX_JSON=matrix, PACKAGE_NAME="demo_package", TWINE_USERNAME="test-user",
               TWINE_PASSWORD="test-token", ARTIFACTORY_URL=step["env"]["ARTIFACTORY_URL"])
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["sdist-dist/" + archive_path.name]
    assert (download / archive_path.name).read_bytes() == archive_path.read_bytes()


def test_sdist_does_not_read_git_again(workspace, run_build):
    result = run_build(GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (workspace / "sdist/dist/demo_package-1.2.3+sail2.2.0.tar.gz").is_file()


def test_sdist_failure_propagates(workspace, run_build):
    result = run_build(TEST_SDIST_FAIL="1")
    assert result.returncode == 23
    assert not list((workspace / "sdist").glob("dist/*.tar.gz"))


def test_later_sdist_failure_stops_remaining_versions(workspace, run_build):
    versions = ["1.2.3+sail2.1.0", "1.2.3+sail2.2.0", "1.2.4+sail2.2.0"]
    result = run_build(versions=versions, TEST_SDIST_FAIL_VERSION=versions[1])
    assert result.returncode == 23
    assert result.stdout.count("已构建源码包版本：") == 1
    assert not (workspace / "sdist/builds" / versions[2]).exists()
    assert [p.name for p in (workspace / "sdist/dist").glob("*.tar.gz")] == [
        f"demo_package-{versions[0]}.tar.gz"
    ]


@pytest.mark.parametrize("versions", [
    ["1.0.0+v0.3.0.sail2.2.0", "1.0.0+v0.3.0.sail2.1.0", "1.0.0+v0.3.0.sail2.2.0"],
    ["1.2.3+sail2.1.0", "1.2.3+sail2.2.0", "1.2.3+sail2.1.0"],
    ["v1.0.0+V0.3.0.sail2.2.0", "v1.0.0+V0.3.0.sail2.2.0"],
])
def test_builds_and_publishes_each_distinct_release_version(workflow, workspace, run_build, versions):
    result = run_build(versions=versions)
    assert result.returncode == 0, result.stdout + result.stderr
    unique_versions = list(dict.fromkeys(versions))
    assert result.stdout.count("已构建源码包版本：") == len(unique_versions)
    assert {p.name for p in (workspace / "sdist/builds").iterdir()} == set(unique_versions)
    archives = list((workspace / "sdist/dist").glob("*.tar.gz"))
    assert len(archives) == len(unique_versions)
    expected = {Version(v): v for v in unique_versions}
    download = workspace / "sdist-dist"
    download.mkdir()
    original_bytes = {}
    for path in archives:
        name, version = parse_sdist_filename(path.name)
        assert name == "demo-package"
        with tarfile.open(path) as archive:
            root = archive.getnames()[0].split("/")[0]
            metadata = Parser().parsestr(archive.extractfile(f"{root}/PKG-INFO").read().decode())
            assert metadata["Version"] == str(version)
            setup = archive.extractfile(f"{root}/setup.py").read().decode()
            assert f'build_version = "{expected[version]}"' in setup
        shutil.copy2(path, download / path.name)
        original_bytes[path.name] = path.read_bytes()

    step = workflow["jobs"]["publish-sdist"]["steps"][-1]
    calls = workspace / "upload-calls"
    shim = '''python3() { "$TEST_PYTHON" "$@"; }
    twine() { printf '%s\\n' "${@: -1}" >> "$TEST_UPLOAD_CALLS"; }
    '''
    env = dict(os.environ, TEST_PYTHON=sys.executable, TEST_UPLOAD_CALLS=str(calls),
               MATRIX_JSON=json.dumps({"include": [{"release_version": v} for v in versions]}),
               PACKAGE_NAME="demo_package", TWINE_USERNAME="test-user", TWINE_PASSWORD="test-token",
               ARTIFACTORY_URL=step["env"]["ARTIFACTORY_URL"])
    result = subprocess.run(["bash"], input=shim + step["run"], cwd=workspace,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    uploaded = [workspace / path for path in calls.read_text().splitlines()]
    assert len(uploaded) == len(unique_versions)
    assert {path.name for path in uploaded} == set(original_bytes)
    for path in uploaded:
        assert path.parent == download
        assert path.read_bytes() == original_bytes[path.name]


def test_wheel_build_uses_caller_command_and_full_history(workflow):
    steps = workflow["jobs"]["matrix-job"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "检出待编译仓库")
    build = next(step for step in steps if step.get("name") == "编译")
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["submodules"] == "recursive"
    assert checkout["with"]["persist-credentials"] == "false"
    assert "repository" not in checkout["with"]
    assert steps.index(checkout) < steps.index(build)
    assert build["env"] == {"BUILD_COMMAND": "${{ inputs.build_command }}"}
    assert workflow["on"]["workflow_call"]["inputs"]["build_command"]["required"] == "true"


@pytest.fixture
def setup_template(monkeypatch):
    # 仅导入安装模板，阻断真实下载和外部命令；需要的环境探测由测试显式模拟。
    spec = importlib.util.spec_from_file_location("sdist_template_under_test", SETUP_TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    for name in ("download_file", "run_cmd"):
        monkeypatch.setattr(module, name, create_autospec(
            getattr(module, name), side_effect=AssertionError("测试禁止真实下载或外部命令")))
    monkeypatch.setattr(module.subprocess, "run", create_autospec(
        module.subprocess.run, side_effect=AssertionError("测试禁止真实执行外部命令")))
    return module


@pytest.mark.parametrize("version", ["2", "3"])
def test_runtime_environment_takes_precedence_over_hgcc(setup_template, monkeypatch, version):
    monkeypatch.setenv("HGGCRT_VERSION", f"hggcrt{version}")
    assert setup_template.get_runtime_version() == version
    setup_template.subprocess.run.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "3", "hggcrt3.0", "hggcrt3\n"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runtime_falls_back_to_hgcc_for_missing_or_invalid_env(setup_template, monkeypatch, value, stream):
    if value is None:
        monkeypatch.delenv("HGGCRT_VERSION", raising=False)
    else:
        monkeypatch.setenv("HGGCRT_VERSION", value)
    runner = setup_template.subprocess.run
    runner.side_effect = None
    output = {"stdout": "", "stderr": ""}
    output[stream] = "编译器版本信息\nRuntime API version v2\n"
    runner.return_value = subprocess.CompletedProcess(["hgcc", "--version"], 0, **output)
    assert setup_template.get_runtime_version() == "2"
    runner.assert_called_once_with(["hgcc", "--version"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, check=True)


@pytest.mark.parametrize("output", ["", "Runtime API version v3.0", "Runtime API version v3 extra"])
def test_hgcc_rejects_non_integer_runtime_versions(setup_template, output):
    runner = setup_template.subprocess.run
    runner.side_effect = None
    runner.return_value = subprocess.CompletedProcess(["hgcc", "--version"], 0, stdout=output, stderr="")
    assert setup_template.get_runtime_version_by_hgcc() is None
    runner.assert_called_once()


@pytest.mark.parametrize("error", [FileNotFoundError("hgcc"), subprocess.CalledProcessError(1, "hgcc")])
def test_runtime_detection_handles_hgcc_failure(setup_template, monkeypatch, error):
    monkeypatch.delenv("HGGCRT_VERSION", raising=False)
    setup_template.subprocess.run.side_effect = error
    assert setup_template.get_runtime_version() is None
    setup_template.subprocess.run.assert_called_once()


@pytest.mark.parametrize("framework,version,suffix", [("torch", "2.10.0", "torch2.10.0"), ("", "", "")])
def test_local_wheel_selection_uses_runtime_detection(setup_template, monkeypatch, framework, version, suffix):
    monkeypatch.setenv("SAIL_SDK_MATCH_STRATEGY", "off")
    monkeypatch.setenv("HGGCRT_VERSION", "hggcrt3")
    monkeypatch.setattr(setup_template, "get_os_name", lambda: "ubuntu2404")
    monkeypatch.setattr(setup_template, "get_platform", lambda: "linux_x86_64")
    monkeypatch.setattr(setup_template, "get_framework_version", lambda: version)
    monkeypatch.setattr(setup_template, "framework", framework)
    monkeypatch.setattr(setup_template, "PACKAGE_NAME", "demo_package")
    monkeypatch.setattr(setup_template, "build_version", "1.0.0+v0.3.0.sail2.2.0")
    monkeypatch.setattr(setup_template, "BASE_WHEEL_URL", "https://example.invalid/wheels")
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    filename = f"demo_package-1.0.0+hggcrt3{suffix}ubuntu2404oe-{python_tag}-{python_tag}-linux_x86_64.whl"
    url = f"https://example.invalid/wheels/demo_package/1.0.0+v0.3.0.sail2.2.0/{filename}"
    assert setup_template.get_wheel_url() == (url, filename)
    assert tuple(setup_template.g_wheel_result) == (url, filename, "1.0.0+v0.3.0.sail2.2.0")
    setup_template.download_file.assert_not_called()
    setup_template.subprocess.run.assert_not_called()
