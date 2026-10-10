"""矩阵准备脚本的 CPU 单元测试"""

import importlib.util
import itertools
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import create_autospec, mock_open

import pytest
import yaml


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/prepare_matrix.py"
DEFAULT_INPUTS = {
    "python_versions": '["310"]',
    "os_versions": '["ubuntu2204"]',
    "runtime_versions": '["3"]',
    "torch_versions": '["2.8.0"]',
    "sdk_versions": '["2.1.0"]',
}
ALL_VERSIONS = {
    "python_versions": ["38", "310", "312"],
    "os_versions": ["ubuntu2004", "ubuntu2204", "ubuntu2404", "alios7u2"],
    "runtime_versions": ["2", "3"],
    "torch_versions": ["2.8.0", "2.10.0", "2.13.0"],
    "sdk_versions": ["2.1.0", "2.2.0"],
}
SYNTHETIC_PAIRS = [
    ("ubuntu2004", "38"),
    ("ubuntu2204", "310"),
    ("ubuntu2404", "312"),
    ("alios7u2", "38"),
    ("alios7u2", "310"),
    ("alios7u2", "312"),
]


def image_key(entry):
    return (
        f"{entry['os_version']}-py{entry['python_version']}-sdk{entry['sdk_version']}"
        f"-hggcrt{entry['runtime_version']}-torch{entry['torch_version']}"
    )


def make_images(pairs):
    return {
        f"{os_version}-py{python_version}-sdk{sdk}-hggcrt{runtime}-torch{torch}":
            f"test.invalid/matrix:{os_version}-py{python_version}-sdk{sdk}-hggcrt{runtime}-torch{torch}"
        for (os_version, python_version), sdk, runtime, torch in itertools.product(
            pairs, ALL_VERSIONS["sdk_versions"], ALL_VERSIONS["runtime_versions"],
            ["", *ALL_VERSIONS["torch_versions"]],
        )
    }


@pytest.fixture
def script():
    # 按文件路径加载，避免导入 deep_gemm 及其硬件依赖。
    spec = importlib.util.spec_from_file_location("prepare_matrix_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def output_file(script, monkeypatch):
    output = mock_open()
    # 仅替换目标模块的文件接口，避免影响 pytest 自身的文件读取。
    monkeypatch.setattr(script, "open", output, raising=False)
    return output


@pytest.fixture
def run_matrix(script, output_file, monkeypatch):
    monkeypatch.delenv("BUILD_CONTAINER_IMAGES", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", "github-output")
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "1.0.0+v0.3.0")
    monkeypatch.setattr(script, "IMAGES_MAP", make_images(SYNTHETIC_PAIRS))

    def run(inputs=None, raw_inputs=None):
        raw = json.dumps({"package_name": "deep_gemm", **(DEFAULT_INPUTS if inputs is None else inputs)})
        monkeypatch.setenv("INPUTS_JSON", raw if raw_inputs is None else raw_inputs)
        script.main()
        output_file.assert_called_once_with("github-output", "a", encoding="utf-8")
        written = "".join(call.args[0] for call in output_file().write.call_args_list)
        assert written.endswith("\n")
        values = dict(line.split("=", 1) for line in written.splitlines())
        if values.get("should_release") == "false":
            assert values == {"should_release": "false"}
            return []
        assert set(values) == {"matrix", "should_release"}
        assert values["should_release"] == "true"
        matrix = json.loads(values["matrix"])
        assert set(matrix) == {"include"}
        return matrix["include"]

    return run


def test_fail_formats_github_annotation(script):
    with pytest.raises(SystemExit) as error:
        script.fail("测试错误")
    assert str(error.value) == "::error::测试错误"


@pytest.mark.parametrize("value", [["38", "310"], {"key": "value"}, None, 3, "text"])
def test_parse_json_accepts_valid_json(script, value):
    assert script.parse_json(json.dumps(value), "参数") == value


@pytest.mark.parametrize("raw", ["", "[", "[1,]", "not-json", None, []])
def test_parse_json_rejects_invalid_json(script, raw):
    with pytest.raises(SystemExit, match="::error::参数 必须是合法 JSON"):
        script.parse_json(raw, "参数")


@pytest.mark.parametrize("name,versions", [
    ("get_torch_url", {"python_version": "310", "os_version": "ubuntu2204",
                       "runtime_version": "3", "torch_version": "2.8.0", "sdk_version": "2.1.0"}),
    ("get_sdk_url", {"os_version": "ubuntu2204", "runtime_version": "3", "sdk_version": "2.1.0"}),
])
def test_url_stubs_return_empty_string(script, name, versions):
    assert getattr(script, name)(**versions) == ""


@pytest.mark.parametrize("os_version,python_version", SYNTHETIC_PAIRS)
def test_each_synthetic_image(script, run_matrix, capsys, os_version, python_version):
    inputs = dict(DEFAULT_INPUTS, os_versions=json.dumps([os_version]),
                  python_versions=json.dumps([python_version]))
    assert run_matrix(inputs) == [{
        "python_version": python_version,
        "os_version": os_version,
        "runtime_version": "3",
        "torch_version": "2.8.0",
        "sdk_version": "2.1.0",
        "image": script.IMAGES_MAP[f"{os_version}-py{python_version}-sdk2.1.0-hggcrt3-torch2.8.0"],
        "tag": "1.0.0+v0.3.0",
        "release_version": "1.0.0+v0.3.0.sail2.1.0",
        "whl_name": f"deep_gemm-1.0.0+hggcrt3torch2.8.0{os_version}oe-cp{python_version}-cp{python_version}-linux_x86_64.whl",
    }]
    assert capsys.readouterr().out == "已生成 1 个矩阵任务\n"


def test_package_name_changes_wheel_name_without_changing_axes(script, run_matrix):
    inputs = dict(DEFAULT_INPUTS, package_name="another_package", framework="torch")
    expected_versions = {
        name.removesuffix("s"): json.loads(value)[0]
        for name, value in DEFAULT_INPUTS.items()
    }
    assert run_matrix(inputs) == [{
        **expected_versions,
        "image": script.IMAGES_MAP["ubuntu2204-py310-sdk2.1.0-hggcrt3-torch2.8.0"],
        "tag": "1.0.0+v0.3.0",
        "release_version": "1.0.0+v0.3.0.sail2.1.0",
        "whl_name": "another_package-1.0.0+hggcrt3torch2.8.0ubuntu2204oe-cp310-cp310-linux_x86_64.whl",
    }]


def test_full_five_axis_cartesian_product(script, run_matrix, monkeypatch, capsys):
    # 当前映射没有覆盖所有 Python × OS 组合；本测试补齐虚拟映射以隔离展开逻辑。
    images = make_images(list(itertools.product(
        ALL_VERSIONS["os_versions"], ALL_VERSIONS["python_versions"]
    )))
    monkeypatch.setattr(script, "IMAGES_MAP", images)
    result = run_matrix({key: json.dumps(values) for key, values in ALL_VERSIONS.items()})
    fields = [key.removesuffix("s") for key in ALL_VERSIONS]
    assert [tuple(entry[key] for key in fields) for entry in result] == list(
        itertools.product(*ALL_VERSIONS.values())
    )
    assert len(result) == 144
    for entry in result:
        assert set(entry) == set(fields) | {"image", "tag", "release_version", "whl_name"}
        assert entry["tag"] == "1.0.0+v0.3.0"
        assert entry["image"] == images[image_key(entry)]
        assert entry["release_version"] == f"1.0.0+v0.3.0.sail{entry['sdk_version']}"
        assert entry["whl_name"] == (
            f"deep_gemm-1.0.0+hggcrt{entry['runtime_version']}torch{entry['torch_version']}"
            f"{entry['os_version']}oe-cp{entry['python_version']}-cp{entry['python_version']}-linux_x86_64.whl"
        )
    assert capsys.readouterr().out == "已生成 144 个矩阵任务\n"


def test_normalizes_numeric_python_and_deduplicates_all_axes(run_matrix):
    inputs = {
        "python_versions": '[312,"312",38,"38",310]',
        "os_versions": '["alios7u2","alios7u2"]',
        "runtime_versions": '["3","2","3"]',
        "torch_versions": '["2.13.0","2.8.0","2.13.0"]',
        "sdk_versions": '["2.2.0","2.1.0","2.2.0"]',
    }
    result = run_matrix(inputs)
    fields = [key.removesuffix("s") for key in inputs]
    expected = itertools.product(
        ["312", "38", "310"], ["alios7u2"], ["3", "2"],
        ["2.13.0", "2.8.0"], ["2.2.0", "2.1.0"],
    )
    assert [tuple(entry[key] for key in fields) for entry in result] == list(expected)
    assert len(result) == 24


def test_disabled_url_resolvers_are_not_called(script, run_matrix, monkeypatch):
    torch_resolver = create_autospec(script.get_torch_url)
    sdk_resolver = create_autospec(script.get_sdk_url)
    monkeypatch.setattr(script, "get_torch_url", torch_resolver)
    monkeypatch.setattr(script, "get_sdk_url", sdk_resolver)
    result = run_matrix(dict(DEFAULT_INPUTS, python_versions='[310,"310"]'))
    torch_resolver.assert_not_called()
    sdk_resolver.assert_not_called()
    assert result[0]["python_version"] == "310"
    assert "torch_url" not in result[0]
    assert "sdk_url" not in result[0]


def test_only_selected_image_mapping_is_required(script, run_matrix, monkeypatch):
    monkeypatch.setattr(script, "IMAGES_MAP", {
        "ubuntu2204-py310-sdk2.1.0-hggcrt3-torch2.8.0": "test.invalid/matrix:custom",
        "unused-py38": None,
    })
    assert run_matrix()[0]["image"] == "test.invalid/matrix:custom"


def test_rejects_malformed_outer_json(run_matrix, output_file):
    with pytest.raises(SystemExit, match="触发参数 必须是合法 JSON"):
        run_matrix(raw_inputs="{")
    output_file.assert_not_called()


@pytest.mark.parametrize("name", DEFAULT_INPUTS)
def test_rejects_missing_input(run_matrix, output_file, name):
    inputs = dict(DEFAULT_INPUTS)
    del inputs[name]
    with pytest.raises(SystemExit, match=f"{name} 必须是合法 JSON"):
        run_matrix(inputs)
    output_file.assert_not_called()


@pytest.mark.parametrize("name", DEFAULT_INPUTS)
def test_rejects_malformed_version_array(run_matrix, output_file, name):
    with pytest.raises(SystemExit, match=f"{name} 必须是合法 JSON"):
        run_matrix(dict(DEFAULT_INPUTS, **{name: "["}))
    output_file.assert_not_called()


@pytest.mark.parametrize("name", DEFAULT_INPUTS)
@pytest.mark.parametrize("raw", ["[]", "null", "{}", '"310"', "310"])
def test_requires_nonempty_array(run_matrix, output_file, name, raw):
    with pytest.raises(SystemExit, match=f"{name} 必须是非空 JSON 数组"):
        run_matrix(dict(DEFAULT_INPUTS, **{name: raw}))
    output_file.assert_not_called()


@pytest.mark.parametrize("name,value", [
    (name, value)
    for name in DEFAULT_INPUTS
    for value in [None, True, 3.1, {}, [], "", " ", "3\n", "$(id)"]
    if not (name == "torch_versions" and value == "")
])
def test_rejects_invalid_version_elements(run_matrix, output_file, name, value):
    with pytest.raises(SystemExit, match=f"{name} 的元素必须是非空版本字符串"):
        run_matrix(dict(DEFAULT_INPUTS, **{name: json.dumps([value])}))
    output_file.assert_not_called()


@pytest.mark.parametrize("name,value", [
    ("python_versions", "39"),
    ("os_versions", "ubuntu1804"),
    ("runtime_versions", "4"),
    ("torch_versions", "2.7.0"),
    ("sdk_versions", "2.3.0"),
])
def test_rejects_unsupported_candidates(run_matrix, output_file, name, value):
    with pytest.raises(SystemExit, match=f"{name} 包含不支持的值 {value}"):
        run_matrix(dict(DEFAULT_INPUTS, **{name: json.dumps([value])}))
    output_file.assert_not_called()


@pytest.mark.parametrize("name", [name for name in DEFAULT_INPUTS if name != "python_versions"])
def test_numeric_elements_are_only_supported_for_python(run_matrix, output_file, name):
    with pytest.raises(SystemExit, match=f"{name} 的元素必须是非空版本字符串"):
        run_matrix(dict(DEFAULT_INPUTS, **{name: "[2]"}))
    output_file.assert_not_called()


def test_rejects_unconfigured_python_os_pair(run_matrix, output_file):
    with pytest.raises(SystemExit, match="ubuntu2204-py38"):
        run_matrix(dict(DEFAULT_INPUTS, python_versions='["38"]'))
    output_file.assert_not_called()


@pytest.mark.parametrize("image", [None, "", " ", "test image", "test\nimage", 123, [], {}])
def test_rejects_invalid_selected_image(script, run_matrix, output_file, monkeypatch, image):
    monkeypatch.setattr(script, "IMAGES_MAP", {"ubuntu2204-py310-sdk2.1.0-hggcrt3-torch2.8.0": image})
    with pytest.raises(SystemExit, match="ubuntu2204-py310"):
        run_matrix()
    output_file.assert_not_called()


def test_checks_every_selected_image_before_writing_output(run_matrix, output_file):
    inputs = dict(DEFAULT_INPUTS, python_versions='["38","310"]',
                  os_versions='["ubuntu2004","alios7u2"]')
    with pytest.raises(SystemExit, match="ubuntu2004-py310"):
        run_matrix(inputs)
    output_file.assert_not_called()


def test_rejects_matrix_over_github_limit(script, run_matrix, output_file, monkeypatch):
    # 当前候选值最多生成 144 个任务，模拟乘积以覆盖超限保护分支。
    monkeypatch.setattr(script.math, "prod", lambda values: 257)
    with pytest.raises(SystemExit, match="257.*256"):
        run_matrix()
    output_file.assert_not_called()


@pytest.mark.parametrize("torch_version", ["2.10.0", "2.13.0"])
def test_script_entrypoint_uses_real_image_mapping(monkeypatch, torch_version):
    inputs = dict(DEFAULT_INPUTS, package_name="deep_gemm", python_versions='[312]',
                  os_versions='["ubuntu2404"]', torch_versions=json.dumps([torch_version]),
                  sdk_versions='["2.2.0"]')
    monkeypatch.setenv("INPUTS_JSON", json.dumps(inputs))
    monkeypatch.setenv("GITHUB_OUTPUT", "github-output")
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "1.0.0+v0.3.0")
    monkeypatch.delenv("BUILD_CONTAINER_IMAGES", raising=False)
    output = mock_open()
    namespace = runpy.run_path(str(SCRIPT_PATH), init_globals={"open": output}, run_name="__main__")
    output.assert_called_once_with("github-output", "a", encoding="utf-8")
    written = "".join(call.args[0] for call in output().write.call_args_list)
    values = dict(line.split("=", 1) for line in written.splitlines())
    assert values["should_release"] == "true"
    assert json.loads(values["matrix"]) == {"include": [{
        "python_version": "312",
        "os_version": "ubuntu2404",
        "runtime_version": "3",
        "torch_version": torch_version,
        "sdk_version": "2.2.0",
        "image": namespace["IMAGES_MAP"][f"ubuntu2404-py312-sdk2.2.0-hggcrt3-torch{torch_version}"],
        "tag": "1.0.0+v0.3.0",
        "release_version": "1.0.0+v0.3.0.sail2.2.0",
        "whl_name": (
            f"deep_gemm-1.0.0+hggcrt3torch{torch_version}ubuntu2404oe-"
            "cp312-cp312-linux_x86_64.whl"
        ),
    }]}


def test_requested_release_and_wheel_example(script, run_matrix, monkeypatch):
    monkeypatch.setattr(script, "IMAGES_MAP", make_images([("ubuntu2404", "310")]))
    entry = run_matrix(dict(DEFAULT_INPUTS, os_versions='["ubuntu2404"]',
                            torch_versions='["2.10.0"]', sdk_versions='["2.2.0"]'))[0]
    assert entry["release_version"] == "1.0.0+v0.3.0.sail2.2.0"
    assert entry["whl_name"] == (
        "deep_gemm-1.0.0+hggcrt3torch2.10.0ubuntu2404oe-cp310-cp310-linux_x86_64.whl"
    )


@pytest.mark.parametrize("python_version", ["38", "310", "312"])
@pytest.mark.parametrize("runtime_version", ["2", "3"])
def test_empty_torch_omits_entire_suffix(run_matrix, python_version, runtime_version):
    entries = run_matrix(dict(DEFAULT_INPUTS, python_versions=json.dumps([python_version]),
                              os_versions='["alios7u2"]', runtime_versions=json.dumps([runtime_version]),
                              torch_versions='["", ""]', sdk_versions='["2.1.0", "2.2.0"]'))
    assert len(entries) == 2
    for entry in entries:
        assert entry["torch_version"] == ""
        assert entry["whl_name"] == (
            f"deep_gemm-1.0.0+hggcrt{runtime_version}alios7u2oe-"
            f"cp{python_version}-cp{python_version}-linux_x86_64.whl"
        )
        assert entry["release_version"] == f"1.0.0+v0.3.0.sail{entry['sdk_version']}"


def test_empty_and_nonempty_torch_expand_independently(run_matrix):
    entries = run_matrix(dict(DEFAULT_INPUTS, torch_versions='["", "2.10.0"]'))
    assert len(entries) == 2
    assert "torch" not in entries[0]["whl_name"]
    assert "torch2.10.0" in entries[1]["whl_name"]
    assert entries[0]["release_version"] == entries[1]["release_version"]
    assert entries[0]["image"] != entries[1]["image"]


def test_empty_torch_still_requires_its_own_image(script, run_matrix, monkeypatch, output_file):
    monkeypatch.setattr(script, "IMAGES_MAP", {
        "ubuntu2204-py310-sdk2.1.0-hggcrt3-torch2.8.0": "test.invalid/torch:existing"
    })
    with pytest.raises(SystemExit, match="缺少 ubuntu2204-py310-sdk2.1.0-hggcrt3-torch 或"):
        run_matrix(dict(DEFAULT_INPUTS, torch_versions='[""]'))
    output_file.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag,version,release_version", [
    ("1.0.0+v0.3.0", "1.0.0", "1.0.0+v0.3.0.sail2.1.0"),
    ("1.0.0+V0.3.0", "1.0.0", "1.0.0+V0.3.0.sail2.1.0"),
    ("1.2.3", "1.2.3", "1.2.3+sail2.1.0"),
    ("1.0.0b1+v0.3.0", "1.0.0b1", "1.0.0b1+v0.3.0.sail2.1.0"),
])
def test_trigger_tag_normalized_and_wheel_uses_public_version(
    run_matrix, monkeypatch, prefix, tag, version, release_version
):
    monkeypatch.setenv("GITHUB_REF_NAME", prefix + tag)
    entry = run_matrix()[0]
    assert entry["release_version"] == release_version
    assert entry["tag"] == tag
    assert entry["whl_name"].startswith(f"deep_gemm-{version}+hggcrt3")
    assert entry["whl_name"].count("+") == 1


@pytest.mark.parametrize("tag", [
    "1.0.0rc1", "1.0.0RC1", "v1.2.3Rc2", "V1.2.3RC2", "1.0.0rC1+v0.3.0",
    "1.0.0+rc1", "1.0.0+V0.3.0.RC", "1.0.0+source",
])
def test_rc_tags_skip_before_matrix_input_and_image_validation(script, run_matrix, monkeypatch, capsys, tag):
    monkeypatch.setenv("GITHUB_REF_NAME", tag)
    monkeypatch.setattr(script, "IMAGES_MAP", {})
    runner = create_autospec(script.subprocess.run)
    monkeypatch.setattr(script.subprocess, "run", runner)
    assert run_matrix(inputs={"python_versions": "["}) == []
    assert "跳过全部构建和发布" in capsys.readouterr().out
    runner.assert_not_called()


def test_rc_script_entrypoint_exits_successfully_without_matrix(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".matrix-skip-test-", dir=SCRIPT_PATH.parents[1]) as directory:
        output = Path(directory) / "github-output"
        env = dict(os.environ, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="1.2.3RC1",
                   GITHUB_OUTPUT=str(output), INPUTS_JSON="{}", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT_PATH)], env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert output.read_text() == "should_release=false\n"
        assert "跳过全部构建和发布" in result.stdout


@pytest.mark.parametrize("ref_type,ref_name", [
    ("tag", "9.9.9"), ("tag", "9.9.9RC1"), ("tag", "not-a-version"), ("branch", "main"),
])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
@pytest.mark.parametrize("tag_version,public_version,release_version", [
    ("2.4.0", "2.4.0", "2.4.0+sail2.1.0"),
    ("2.4.0+v0.5.0", "2.4.0", "2.4.0+v0.5.0.sail2.1.0"),
    ("2.4.0+V0.5.0", "2.4.0", "2.4.0+V0.5.0.sail2.1.0"),
    ("2.4.0.post1", "2.4.0.post1", "2.4.0.post1+sail2.1.0"),
    ("2.4.0.dev1+build_2", "2.4.0.dev1", "2.4.0.dev1+build_2.sail2.1.0"),
])
def test_tag_version_overrides_repository_and_drives_all_matrix_versions(
    script, run_matrix, monkeypatch, ref_type, ref_name, prefix, tag_version, public_version, release_version
):
    monkeypatch.setenv("GITHUB_REF_TYPE", ref_type)
    monkeypatch.setenv("GITHUB_REF_NAME", ref_name)
    monkeypatch.delenv("SOURCE_DIR", raising=False)
    runner = create_autospec(script.subprocess.run, side_effect=AssertionError("不应读取仓库 tag"))
    monkeypatch.setattr(script.subprocess, "run", runner)
    entries = run_matrix(dict(DEFAULT_INPUTS, tag_version=prefix + tag_version, torch_versions='["", "2.8.0"]'))
    assert len(entries) == 2
    for entry in entries:
        assert entry["tag"] == tag_version
        assert entry["release_version"] == release_version
        assert entry["whl_name"].startswith(f"deep_gemm-{public_version}+hggcrt3")
        assert entry["whl_name"].count("+") == 1
        assert "tag_version" not in entry
    runner.assert_not_called()


@pytest.mark.parametrize("ref_type,ref_name", [("tag", "1.0.0"), ("branch", "main"), ("tag", "1.0.0RC1")])
@pytest.mark.parametrize("tag_version", [
    None, 123, False, [], {}, " ", " 1.0.0", "1.0.0\n", "../1.0.0", "release/1.0.0",
    "$(id)", "1.0.0+", "1.0.0+a+b", "1..2", "1.0garbage", "1.0.0+bad..value",
    "v", "V", "vv1.0.0", "VV1.0.0", "vV1.0.0", "Vv1.0.0", "V1.0garbage",
])
def test_invalid_tag_version_fails_without_falling_back_or_writing_output(
    script, run_matrix, output_file, monkeypatch, ref_type, ref_name, tag_version
):
    monkeypatch.setenv("GITHUB_REF_TYPE", ref_type)
    monkeypatch.setenv("GITHUB_REF_NAME", ref_name)
    runner = create_autospec(script.subprocess.run)
    monkeypatch.setattr(script.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="tag_version 必须是合法"):
        run_matrix(dict(DEFAULT_INPUTS, tag_version=tag_version))
    runner.assert_not_called()
    output_file.assert_not_called()


@pytest.mark.parametrize("tag_version", ["1.0.0rc1", "1.0.0RC1", "v2.0.0Rc2", "V2.0.0RC2", "1.0.0+source"])
@pytest.mark.parametrize("ref_type", ["tag", "branch"])
def test_rc_override_skips_before_matrix_validation(script, run_matrix, monkeypatch, ref_type, tag_version):
    monkeypatch.setenv("GITHUB_REF_TYPE", ref_type)
    monkeypatch.setenv("GITHUB_REF_NAME", "9.9.9")
    monkeypatch.setattr(script, "IMAGES_MAP", {})
    runner = create_autospec(script.subprocess.run)
    monkeypatch.setattr(script.subprocess, "run", runner)
    assert run_matrix(inputs={"tag_version": tag_version, "python_versions": "["}) == []
    runner.assert_not_called()


@pytest.mark.parametrize("ref_type", ["tag", "branch"])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_empty_tag_version_preserves_repository_selection(
    run_matrix, monkeypatch, caller_git_repository, ref_type, prefix
):
    directory, git = caller_git_repository
    git("tag", prefix + "3.0.0")
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", ref_type)
    monkeypatch.setenv("GITHUB_REF_NAME", prefix + "3.0.0" if ref_type == "tag" else "main")
    entry = run_matrix(dict(DEFAULT_INPUTS, tag_version=""))[0]
    assert entry["tag"] == "3.0.0"
    assert entry["release_version"] == "3.0.0+sail2.1.0"


def test_tag_trigger_does_not_read_git_or_template_ref(script, run_matrix, monkeypatch):
    runner = create_autospec(script.subprocess.run)
    monkeypatch.setattr(script.subprocess, "run", runner)
    monkeypatch.setenv("SOURCE_DIR", "template-repository-must-not-be-read")
    entry = run_matrix(dict(DEFAULT_INPUTS, template_ref="v9.9.9"))[0]
    assert entry["release_version"] == "1.0.0+v0.3.0.sail2.1.0"
    runner.assert_not_called()


@pytest.mark.parametrize("tag", ["", "main", "release/1.0.0", "1.0.0+", "1.0.0+a+b",
                                      "1.0.0\n", " 1.0.0", "$(id)", "v", "V", "vv1.0.0",
                                      "VV1.0.0", "vV1.0.0", "Vv1.0.0"])
def test_invalid_trigger_tag_fails_without_writing_output(run_matrix, monkeypatch, output_file, tag):
    monkeypatch.setenv("GITHUB_REF_NAME", tag)
    with pytest.raises(SystemExit, match="tag 必须是版本号"):
        run_matrix()
    output_file.assert_not_called()


def test_tag_trigger_without_name_does_not_fall_back_to_git(run_matrix, monkeypatch, output_file):
    monkeypatch.delenv("GITHUB_REF_NAME")
    with pytest.raises(SystemExit, match="tag 必须是版本号"):
        run_matrix()
    output_file.assert_not_called()


@pytest.mark.parametrize("package_name", ["", None, 123, "../deep_gemm", "a/b", "a\nb", "-pkg"])
def test_invalid_package_name(run_matrix, output_file, package_name):
    with pytest.raises(SystemExit, match="package_name 必须是合法的 Python 包名"):
        run_matrix(dict(DEFAULT_INPUTS, package_name=package_name))
    output_file.assert_not_called()


def test_missing_package_name(run_matrix, output_file):
    with pytest.raises(SystemExit, match="package_name 必须是合法的 Python 包名"):
        run_matrix(raw_inputs=json.dumps(DEFAULT_INPUTS))
    output_file.assert_not_called()


@pytest.mark.parametrize("package_name", ["deep-gemm", "deep.gemm", "deep_gemm"])
def test_wheel_package_name_normalizes_separators(run_matrix, package_name):
    entry = run_matrix(dict(DEFAULT_INPUTS, package_name=package_name))[0]
    assert entry["whl_name"].startswith("deep_gemm-1.0.0+")


@pytest.mark.parametrize("inputs", [[], None, "text", 1])
def test_outer_json_requires_an_object(run_matrix, output_file, inputs):
    with pytest.raises(SystemExit, match="触发参数 必须是 JSON 对象"):
        run_matrix(raw_inputs=json.dumps(inputs))
    output_file.assert_not_called()


def test_branch_requires_explicit_caller_source(script, run_matrix, monkeypatch, output_file):
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "feat/add-release-workflow")
    monkeypatch.delenv("SOURCE_DIR", raising=False)
    runner = create_autospec(script.subprocess.run)
    monkeypatch.setattr(script.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="SOURCE_DIR"):
        run_matrix()
    output_file.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize("error", [OSError("git 不可用"), subprocess.CalledProcessError(128, "git")])
def test_git_errors_fail_cleanly(script, run_matrix, monkeypatch, output_file, error):
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("SOURCE_DIR", "caller-source")
    runner = create_autospec(script.subprocess.run, side_effect=error)
    monkeypatch.setattr(script.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="无法读取调用方仓库当前提交的 tag"):
        run_matrix()
    output_file.assert_not_called()
    assert runner.call_args.args[0] == [
        "git", "-C", "caller-source", "tag", "--points-at", "HEAD", "--sort=-version:refname"
    ]


@pytest.fixture
def caller_git_repository():
    # 所有 Git 操作仅发生在临时测试仓库，不读取用户 Git 配置或修改工作仓库。
    with tempfile.TemporaryDirectory(prefix=".matrix-tag-test-", dir=SCRIPT_PATH.parents[1]) as directory:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="CI Test", GIT_AUTHOR_EMAIL="ci@example.invalid",
                   GIT_COMMITTER_NAME="CI Test", GIT_COMMITTER_EMAIL="ci@example.invalid")

        def git(*args):
            return subprocess.run(["git", "-C", directory, *args], env=env,
                                  check=True, capture_output=True, text=True)

        git("init", "--quiet")
        git("commit", "--allow-empty", "--quiet", "-m", "initial")
        yield directory, git


@pytest.mark.parametrize("annotated", [False, True])
def test_branch_uses_only_current_commit_tag(
    run_matrix, monkeypatch, caller_git_repository, annotated
):
    directory, git = caller_git_repository
    git("tag", "1.0.0+v0.2.0")
    git("commit", "--allow-empty", "--quiet", "-m", "next version")
    tag = "1.0.0+v0.3.0"
    if annotated:
        git("tag", "-a", tag, "-m", "version")
    else:
        git("tag", tag)
    git("tag", "unrelated-tag")
    git("commit", "--allow-empty", "--quiet", "-m", "unreachable future version")
    git("tag", "9.9.9")
    git("switch", "--detach", "HEAD~1")
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "feat/add-release-workflow")
    entry = run_matrix(dict(DEFAULT_INPUTS, template_ref="v8.8.8"))[0]
    assert entry["tag"] == tag
    assert entry["release_version"] == "1.0.0+v0.3.0.sail2.1.0"
    assert entry["whl_name"].startswith("deep_gemm-1.0.0+")


@pytest.mark.parametrize("current_tags", [
    [], ["not-a-version"], ["1.1.0rc1"], ["1.1.0RC1"], ["V1.1.0RC1"],
    ["vv1.0.0"], ["VV1.0.0"], ["vV1.0.0"], ["Vv1.0.0"],
    ["1.1.0Rc1", "1.2.0rC1", "other-tag"],
])
def test_branch_without_eligible_head_tag_skips_even_with_history(
    run_matrix, monkeypatch, caller_git_repository, current_tags, capsys
):
    directory, git = caller_git_repository
    git("tag", "1.0.0")
    git("commit", "--allow-empty", "--quiet", "-m", "当前提交")
    for tag in current_tags:
        git("tag", tag)
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    assert run_matrix(inputs={"python_versions": "["}) == []
    assert "跳过全部构建和发布" in capsys.readouterr().out


def test_branch_without_any_tags_skips(run_matrix, monkeypatch, caller_git_repository):
    directory, _ = caller_git_repository
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    assert run_matrix() == []


@pytest.mark.parametrize("annotated", [False, True])
@pytest.mark.parametrize("prefix", ["", "v", "V"])
def test_multiple_head_tags_select_highest_eligible_version(
    run_matrix, monkeypatch, caller_git_repository, annotated, prefix
):
    directory, git = caller_git_repository
    for tag in (prefix + "1.9.0", prefix + "1.10.0", prefix + "1.11.0RC1", "unrelated-tag"):
        if annotated:
            git("tag", "-a", tag, "-m", "版本 tag")
        else:
            git("tag", tag)
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    monkeypatch.setenv("GITHUB_REF_NAME", "feature/rc-fix")
    entry = run_matrix()[0]
    assert entry["tag"] == "1.10.0"
    assert entry["release_version"] == "1.10.0+sail2.1.0"
    assert entry["whl_name"].startswith("deep_gemm-1.10.0+")


def test_rc_trigger_does_not_use_other_stable_tag_on_same_commit(
    run_matrix, monkeypatch, caller_git_repository
):
    directory, git = caller_git_repository
    git("tag", "1.0.0")
    git("tag", "1.1.0RC1")
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_NAME", "1.1.0RC1")
    assert run_matrix() == []


@pytest.mark.parametrize("prefix", ["v", "V"])
@pytest.mark.parametrize("local_version", ["v0.4.0", "V0.4.0"])
def test_branch_version_tag_with_v_prefix(
    run_matrix, monkeypatch, caller_git_repository, prefix, local_version
):
    directory, git = caller_git_repository
    git("tag", f"{prefix}2.0.0+{local_version}")
    monkeypatch.setenv("SOURCE_DIR", directory)
    monkeypatch.setenv("GITHUB_REF_TYPE", "branch")
    entry = run_matrix()[0]
    assert entry["tag"] == f"2.0.0+{local_version}"
    assert entry["release_version"] == f"2.0.0+{local_version}.sail2.1.0"
    assert entry["whl_name"].startswith("deep_gemm-2.0.0+")


@pytest.fixture
def run_normalize():
    workflow = yaml.load((SCRIPT_PATH.parents[1] / "workflows/matrix-release.yml").read_text(),
                         Loader=yaml.BaseLoader)
    step = workflow["jobs"]["prepare-matrix"]["steps"][0]

    def run(inputs):
        with tempfile.TemporaryDirectory(prefix=".normalize-test-", dir=SCRIPT_PATH.parents[1]) as directory:
            output = Path(directory) / "github-output"
            env = dict(os.environ, INPUTS_JSON=json.dumps(inputs), GITHUB_OUTPUT=str(output),
                       TEST_PYTHON=sys.executable)
            shim = 'python3() { "$TEST_PYTHON" "$@"; }\n'
            result = subprocess.run(["bash"], input=shim + step["run"], cwd=directory,
                                    env=env, text=True, capture_output=True, timeout=30)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
            return result, values

    return run


@pytest.mark.parametrize("name,expected", [
    ("deep-gemm", "deep_gemm"), ("deep-gemm-tools", "deep_gemm_tools"),
    ("Deep--Gemm.ext", "Deep__Gemm.ext"), ("deep_gemm", "deep_gemm"),
    ("deep.gemm", "deep.gemm"), ("x", "x"),
])
def test_entry_normalizes_only_hyphens_and_preserves_other_inputs(run_normalize, name, expected):
    inputs = dict(DEFAULT_INPUTS, package_name=name, framework="torch",
                  build_command='printf "a=b\\n"\npython setup.py bdist_wheel')
    result, values = run_normalize(inputs)
    assert result.returncode == 0, result.stderr
    assert set(values) == {"package_name", "inputs_json"}
    assert values["package_name"] == expected
    assert json.loads(values["inputs_json"]) == {**inputs, "package_name": expected}


@pytest.mark.parametrize("name", ["", None, 123, "../pkg", "a/b", "a\\b", "a\nb", "-pkg", "pkg-", "$(id)"])
def test_entry_rejects_invalid_package_before_writing_outputs(run_normalize, name):
    result, values = run_normalize(dict(DEFAULT_INPUTS, package_name=name))
    assert result.returncode != 0
    assert "package_name 必须是合法的 Python 包名" in result.stderr
    assert values == {}


def test_entry_requires_package_name(run_normalize):
    result, values = run_normalize(DEFAULT_INPUTS)
    assert result.returncode != 0
    assert "package_name" in result.stderr
    assert values == {}


@pytest.mark.parametrize("tag_version,expected_tag", [
    ("", "1.0.0+v0.3.0"), ("v2.4.0+v0.5.0", "2.4.0+v0.5.0"),
    ("V2.4.0+V0.5.0", "2.4.0+V0.5.0"),
])
def test_normalized_inputs_drive_matrix(run_normalize, run_matrix, tag_version, expected_tag):
    result, values = run_normalize(dict(DEFAULT_INPUTS, package_name="deep-gemm", tag_version=tag_version))
    assert result.returncode == 0, result.stderr
    entry = run_matrix(raw_inputs=values["inputs_json"])[0]
    assert entry["whl_name"].startswith("deep_gemm-")
    assert entry["tag"] == expected_tag
    assert json.loads(values["inputs_json"])["tag_version"] == tag_version
    assert values["package_name"] == "deep_gemm"


def test_workflow_uses_caller_history_and_exposes_generated_fields():
    workflow = yaml.load((SCRIPT_PATH.parents[1] / "workflows/matrix-release.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["prepare-matrix"]["steps"]
    source = next(step for step in steps if step["name"] == "检出调用方版本历史")
    template = next(step for step in steps if step["name"] == "检出模板工具")
    generate = next(step for step in steps if step.get("id") == "matrix")
    setup = next(step for step in steps if step["name"] == "配置矩阵校验 Python")
    install = next(step for step in steps if step["name"] == "安装版本校验依赖")
    assert steps.index(source) < steps.index(template) < steps.index(setup) < steps.index(install) < steps.index(generate)
    assert setup["uses"] == "actions/setup-python@v5"
    assert setup["with"]["python-version"] == "3.12"
    assert install["run"] == "python3 -m pip install --no-input packaging"
    tag_input = workflow["on"]["workflow_call"]["inputs"]["tag_version"]
    assert tag_input["type"] == "string"
    assert tag_input["required"] == "false"
    assert tag_input["default"] == ""
    assert "tag_version" not in str(source["with"])
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if step.get("uses") == "actions/checkout@v4":
                assert "tag_version" not in str(step["with"])
    assert source["uses"] == "actions/checkout@v4"
    assert source["with"]["fetch-depth"] == "0"
    assert source["with"]["ref"] == "${{ github.sha }}"
    assert source["with"]["persist-credentials"] == "false"
    assert "repository" not in source["with"]
    assert source["with"]["path"] != template["with"]["path"]
    assert generate["env"]["SOURCE_DIR"] == source["with"]["path"]
    normalize = steps[0]
    assert normalize["id"] == "normalize-package"
    assert normalize["env"]["INPUTS_JSON"] == "${{ toJSON(inputs) }}"
    assert "if" not in normalize
    assert "continue-on-error" not in normalize
    assert generate["env"]["INPUTS_JSON"] == "${{ steps.normalize-package.outputs.inputs_json }}"
    assert workflow["jobs"]["prepare-matrix"]["outputs"]["package_name"] == (
        "${{ steps.normalize-package.outputs.package_name }}"
    )
    assert workflow["jobs"]["prepare-matrix"]["outputs"]["should_release"] == (
        "${{ steps.matrix.outputs.should_release }}"
    )
    for job_id in ("build-sdist", "matrix-job", "publish-sdist", "update-metadata", "post-release-data"):
        job = workflow["jobs"][job_id]
        assert "needs.prepare-matrix.outputs.should_release == 'true'" in job["if"]
        if job_id in ("build-sdist", "matrix-job"):
            assert job["if"] == "${{ needs.prepare-matrix.outputs.should_release == 'true' }}"
        needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
        assert "prepare-matrix" in needs
        assert "inputs.package_name" not in json.dumps(job)
        assert "toJSON(inputs)" not in json.dumps(job)
        assert "${{ needs.prepare-matrix.outputs.package_name }}" in json.dumps(job)
    matrix_job = workflow["jobs"]["matrix-job"]
    assert matrix_job["env"]["RELEASE_VERSION"] == "${{ matrix.release_version }}"
    assert matrix_job["env"]["WHL_NAME"] == "${{ matrix.whl_name }}"
    assert "RELEASE_VERSION=$RELEASE_VERSION" in matrix_job["steps"][0]["run"]
    assert "WHL_NAME=$WHL_NAME" in matrix_job["steps"][0]["run"]


def test_workflow_runtime_input_and_environment_use_new_names():
    workflow = yaml.load((SCRIPT_PATH.parents[1] / "workflows/matrix-release.yml").read_text(),
                         Loader=yaml.BaseLoader)
    inputs = workflow["on"]["workflow_call"]["inputs"]
    assert inputs["runtime_versions"]["type"] == "string"
    assert inputs["runtime_versions"]["required"] == "true"
    assert json.loads(inputs["runtime_versions"]["default"]) == ["3"]
    assert "hggcrt_versions" not in inputs
    job = workflow["jobs"]["matrix-job"]
    assert job["env"]["RUNTIME_VERSION"] == "${{ matrix.runtime_version }}"
    assert "HGGCRT_VERSION" not in job["env"]
    assert "runtime${{ matrix.runtime_version }}" in job["name"]
    assert "RUNTIME_VERSION=$RUNTIME_VERSION" in job["steps"][0]["run"]
    assert "hggcrt_version" not in json.dumps(job)
    assert "HGGCRT_VERSION" not in json.dumps(job)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
