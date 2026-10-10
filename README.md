# release-action

A Python package build and release tool for SAIL environments. It uses a GitHub Actions reusable workflow to generate a build matrix across Python, operating system, HGGCRT Runtime, PyTorch, and SDK versions, then publishes wheels, bootstrap source distributions, and associated metadata.

This repository provides a **reusable workflow**, referenced through `jobs.<job_id>.uses` in the caller's workflow. It is neither a regular Action used in `steps` nor a directly installable Python package.

## Release Pipeline

Entry point: [`.github/workflows/matrix-release.yml`](.github/workflows/matrix-release.yml).

```text
Validate the package name, release version, and image mappings
  → Build bootstrap source distributions (deduplicated by release_version)
  → Matrix build → Collect wheels → Repack → Rename → Upload wheels
  → Publish source distributions after at least one matrix job succeeds
  → Update the package's index.json
  → Submit release data for the current run
```

- `prepare-matrix`: Normalizes the package name, applies version gates, and generates up to 32 matrix jobs.
- `build-sdist`: Generates `.tar.gz` files using this repository's `setup.py` template. These bootstrap packages match and download prebuilt wheels; they are **not archives of the caller's complete source code**.
- `matrix-job`: Builds the caller's repository in the specified image, updates the wheel version with `revpkg`, and uploads it to Artifactory.
- `publish-sdist`: Checks success markers for the current `run_id` / `run_attempt` and publishes all built source distribution versions through Twine only after at least one matrix job succeeds.
- `update-metadata`: Queries Artifactory for the package's wheels and generates an index.
- `post-release-data`: After the index is successfully updated, submits saved release records individually and generates installation commands and idempotency keys.

The matrix uses `fail-fast: false`. Failed jobs do not cancel other matrix jobs. Source distributions and metadata can still be published when the conditions above are met, but this does not mean the entire workflow succeeded, and previously uploaded artifacts are not rolled back.

## Quick Start

### 1. Prepare the Environment and Credentials

The caller's repository requires:

- GitHub Actions enabled, with permission to use this repository's reusable workflow.
- A build command capable of generating wheels in a Linux x86_64 container, along with the required build dependencies.
- Runner access to the container registry and Artifactory at `download.t-head.cn`.
- The required Secrets listed below. The publishing account needs permissions to upload wheels / source distributions, execute AQL queries, and read and write the package index.

| Workflow Secret | Required | Purpose |
| --- | --- | --- |
| `release_username` | Yes | Artifactory publishing username |
| `release_token` | Yes | Artifactory publishing credential |
| `data_token` | Yes | 客户端 RSA 私钥，Base64 编码的未加密 DER 格式（PKCS8）|
| `template_token` | No | Read-only token for checking out a private template repository; defaults to `github.token` when omitted |

下方示例将调用方的大写 Secrets 映射到对应的工作流参数。不要将凭据写入 YAML 或构建命令。私有模板仓库还需在 Actions 设置中允许跨仓库调用。

发布数据使用 `/open/sync/artifact/save`，通过 AES-256-GCM 加密业务数据、RSA-OAEP-SHA256 加密 AES 密钥，并使用 RSA-SHA256 签名。工作流保留 `data_token` Secret 名称，其值为客户端 RSA 私钥；工作流将其映射到 `CLIENT_PRIVATE_KEY_B64` 环境变量。直接运行脚本时也通过该环境变量传入私钥。

在 `.github/scripts/post_data.py` 顶部直接填写 `OPEN_API_BASE_URL`、`OPEN_API_CLIENT_ID` 和 `SERVER_PUBLIC_KEY_B64` 三个常量；同名环境变量不生效。地址仅含协议、主机及可选端口，生产环境使用 HTTPS；公钥为 Base64 编码的 DER 格式（SubjectPublicKeyInfo）。这三个值当前为空，使用前必须填写，并通过 `template_ref` 引用已配置的脚本版本。私钥不得写入脚本。

### 2. Add a Workflow to the Caller's Repository

Create `.github/workflows/release.yml` in the repository of the package to publish, and adjust the package name and build command for your project:

```yaml
name: Publish Python Package

on:
  push:
    tags:
      - 'v*'
      - 'V*'
      - '[0-9]*'
  workflow_dispatch:
    inputs:
      tag_version:
        description: 'Optional release version; leave empty to use a version tag on the current commit'
        required: false
        type: string
        default: ''

permissions:
  contents: read

jobs:
  release:
    uses: t-head/release-action/.github/workflows/matrix-release.yml@master
    with:
      package_name: demo-package
      framework: torch
      python_versions: '["312"]'
      os_versions: '["ubuntu2404"]'
      runtime_versions: '["3"]'
      torch_versions: '["2.13.0", "2.10.0"]'
      sdk_versions: '["2.2.0"]'
      template_repository: t-head/release-action
      template_ref: master
      tag_version: ${{ inputs.tag_version || '' }}
      build_command: |
        python3 -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
    secrets:
      release_username: ${{ secrets.RELEASE_USERNAME }}
      release_token: ${{ secrets.RELEASE_TOKEN }}
      data_token: ${{ secrets.DATA_TOKEN }}
      # Enable as needed for a private template repository.
      # template_token: ${{ secrets.TEMPLATE_TOKEN }}
```

This example uses the two image combinations currently configured. `pip wheel` is only a generic example; ensure your project's build dependencies are installed, or replace it with your project's own Bash build command.

For production use, the `@master` reference in `uses` and the value of `template_ref` should **both be replaced with the same full commit SHA**. The former determines the workflow version, while the latter determines the version of the checked-out utility scripts. For cross-repository calls, do not use the caller's `github.sha` as the template version.

`template_repository` 为可选参数，默认值为 `t-head/release-action`，也可像示例一样显式传入。本仓库仅提供 `workflow_call` 入口，没有内置 `build.yml` 或手动触发工作流；手动运行需由调用方提供 `workflow_dispatch`。

## Inputs

Arrays are passed as **JSON strings**, not YAML arrays. All version arrays must be non-empty; duplicate values are removed.

| Input | Required | Current Default | Description |
| --- | --- | --- | --- |
| `package_name` | Yes | None | Python package name; the entry step replaces `-` with `_`, e.g. `demo-package` → `demo_package` |
| `framework` | Yes | None | Framework module name detected by the installation template, such as `torch`; an explicit empty string is allowed; not included in matrix expansion |
| `python_versions` | Yes | `'["310"]'` | Allowed values: `"38"`, `"310"`, `"312"`; JSON integers are also accepted, e.g. `[38,310]` |
| `os_versions` | Yes | `'["ubuntu2204"]'` | Allowed values: `ubuntu2004`, `ubuntu2204`, `ubuntu2404`, `alios7u2` |
| `runtime_versions` | Yes | `'["3"]'` | Allowed values: `"2"`, `"3"`; elements must be strings |
| `torch_versions` | Yes | `'["2.8.0"]'` | Allowed values: `"2.8.0"`, `"2.10.0"`, `"2.13.0"`; `'[""]'` means no Torch version is included |
| `sdk_versions` | Yes | `'["2.1.0"]'` | Allowed values: `"2.1.0"`, `"2.2.0"` |
| `template_repository` | No | `t-head/release-action` | 保存模板工具脚本的仓库 |
| `template_ref` | Yes | None | Template repository version; must match the version referenced in `uses` |
| `build_command` | Yes | None | Bash command executed at the root of the caller's repository; supports multiple lines; must come from trusted configuration |
| `tag_version` | No | Empty string | Overrides the release version without changing the checked-out source version |

An allowed value does not imply that a corresponding image exists. The matrix expands into the Cartesian product of the five version axes, and **every combination must have an image mapping**. Combinations with missing mappings are not automatically filtered out.

### Current Image Configuration

Image mappings are maintained in `IMAGES_MAP` in [`prepare_matrix.py`](.github/scripts/prepare_matrix.py). Currently, only the following combinations are configured:

| OS | Python | Runtime | SDK | Torch |
| --- | --- | --- | --- | --- |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.13.0` |
| `ubuntu2404` | `312` | `3` | `2.2.0` | `2.10.0` |

The corresponding images are:

```text
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.13.0-ubuntu24.04-hggcrt3-py312-20261005
download.t-head.cn/docker_release/pytorch:sdk2.2.0-2.10.0-ubuntu24.04-hggcrt3-py312-20261005
```

The default version combination in the inputs currently has no matching mapping. Explicitly use the versions in the Quick Start example. To add a combination, update `IMAGES_MAP` using the following key format:

```text
{os_version}-py{python_version}-sdk{sdk_version}-hggcrt{runtime_version}-torch{torch_version}
```

The `BUILD_CONTAINER_IMAGES missing ...` error refers to this mapping table. The current implementation does not load image configuration from an environment variable of the same name. Although `torch_versions: '[""]'` is a valid input, no corresponding image is currently configured.

## Versions and Artifact Naming

The release version is selected in the following order of precedence:

1. A non-empty `tag_version`, which must conform to the script's tag format and PEP 440. Invalid values cause an error without falling back.
2. `github.ref_name` for tag-triggered runs.
3. For other triggers, only tags on the caller's current commit are checked. The first correctly formatted version without `rc` is selected in descending Git version order; historical commits are not searched.

For all sources, a single leading `v` / `V` is removed, while internal content such as `+v0.3.0` is preserved. All builds and publishing are skipped if the explicit version or triggering tag contains `rc` (case-insensitive), or if the current commit has no qualifying tag.

`tag_version` only determines the release version, artifact paths, and release data; it does not check out source code from the tag with that name. Manual runs build the triggering revision of the selected branch / ref.

For SDK `2.2.0`, for example:

| Input Version | Normalized Tag | `release_version` |
| --- | --- | --- |
| `v1.0.0` | `1.0.0` | `1.0.0+sail2.2.0` |
| `V1.0.0+v0.3.0` | `1.0.0+v0.3.0` | `1.0.0+v0.3.0.sail2.2.0` |

Source distributions and repacked wheels use `release_version` as their version. The final wheel filename uses the portion of the tag before `+` as its base version, combined with environment information, for example:

```text
demo_package-1.0.0+hggcrt3torch2.13.0ubuntu2404oe-cp312-cp312-linux_x86_64.whl
```

The final wheel rename does not modify its internal metadata again. The SDK version and the tag's original local version suffix are reflected in the release directory, rather than directly included in the wheel filename above.

## Build Conventions and Publishing Locations

The build step sources `envsetup.sh` before executing `build_command`. Build images must have a working SDK, compiler toolchain, and the corresponding Python / Torch environment preinstalled.

The build command can access:

```text
PYTHON_VERSION  OS_VERSION  RUNTIME_VERSION  TORCH_VERSION  SDK_VERSION
RELEASE_VERSION  WHL_NAME  CONTAINER_IMAGE
```

The workflow retains `TORCH_URL` and `SDK_URL` environment variable entries, but their lookup logic is not enabled, so no download URLs are currently available. The "Install SDK and Torch" step is also commented out and cannot be relied on to complete the environment automatically.

The caller's code is checked out with its full Git history and recursive submodules. After compilation, the workflow recursively scans the source workspace for regular `.whl` files. If multiple files are found, it filters them by package name prefix; exactly one must remain. The selected artifact is collected into `dist/`, which retains only that wheel. Avoid leaving multiple artifacts for the same package in the build directory.

These addresses are currently hardcoded in the workflow or scripts, rather than exposed as configurable inputs. Ensure upload paths do not conflict across matrix combinations.

GitHub Actions artifacts also retain source distributions, release data JSON files, and matrix success markers. An additional wheel artifact is saved only for tag-triggered runs or when `tag_version` is explicitly provided. A branch run whose current commit has a tag does not save this wheel artifact unless the version is explicitly overridden.

**The workflow currently does not create GitHub Releases or upload Release attachments.** The "Save GitHub Release wheel artifacts" step actually uses `actions/upload-artifact`.

## Bootstrap Package Behavior

[`setup.py`](.github/scripts/setup.py) is a source distribution template and should not be run directly as this repository's installation entry point. The workflow fills in the package name, framework, and version, and sets `__FAKE_BUILD=FAKE_WHEEL` to generate bootstrap packages. No wheels are downloaded at this stage.

When a user installs a bootstrap package, the template detects the Python, OS, Runtime, and framework versions. By default, it downloads and executes the remote `strategy_hggc_sail.py` to match a prebuilt wheel, then extracts its dependency information. This process depends on a trusted remote strategy service, an accessible artifact repository, and `wget`; it does not compile the caller's source code on the user's machine.

Relevant environment variables:

| Variable | Description |
| --- | --- |
| `HGGCRT_VERSION` | Explicit Runtime version, such as `hggcrt3`; if unset or incorrectly formatted, detection falls back to `hgcc --version` |
| `SAIL_SDK_MATCH_STRATEGY` | Set to `off` to construct the wheel URL directly using local naming rules |
| `SAIL_PYPI_SDK_VESION` | SDK version lookup entry provided by the template; `VESION` is the existing spelling, and its use depends on the matching strategy |

## Repository Structure

```text
.github/
├── workflows/
│   └── matrix-release.yml      # Reusable release workflow
├── scripts/
│   ├── prepare_matrix.py       # Version gates, input validation, image mappings, and artifact naming
│   ├── setup.py                # Bootstrap source distribution template
│   ├── get_metadata.py         # Artifactory queries, SDK configuration merging, and index uploads
│   └── post_data.py            # Release data conversion, idempotency keys, and API submission
└── tests/
    ├── test_prepare_matrix.py  # Matrix, version, and input validation
    ├── test_build_sdist.py     # Source distribution generation and installation template
    ├── test_upload_wheels.py   # Wheel collection, repacking, renaming, and uploading
    ├── test_publish_sdist.py   # Conditional publishing, source distribution validation, and index updates
    └── test_github_release.py  # Artifact storage, release data, and template dependency parsing
```

## Local Testing

Use **Python 3.12**, matching the workflow's utility jobs. The current `post_data.py` uses f-string syntax supported in Python 3.12, so Python 3.11 cannot run the full test suite. Git and Bash are also required.

Run the following from the repository root (if `.venv` already exists, check its Python version first):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input pytest PyYAML packaging setuptools wheel requests cryptography
PYTHONDONTWRITEBYTECODE=1 python -m pytest .github/tests -q -p no:cacheprovider
```

These tests use mocks / shell shims to simulate the SDK, `revpkg`, `curl`, Twine, and HTTP requests. They do not require real publishing credentials, hardware, or build images, and do not upload artifacts to publishing services. They do not replace build and release validation in real images.
