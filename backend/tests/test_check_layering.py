"""分层 linter 的自测（PR-01）。

验证 linter 能：
1. 正确识别已知存量违规（image→writer、core→writer）。
2. 检出【新增】违规并 fail（exit 1）。
3. baseline 模式下存量违规不 fail。
4. strict 模式下存量违规也 fail。

不依赖业务代码导入；linter 是纯静态 AST 分析。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

# 直接从源文件加载 linter 模块（它不在 backend/app 包里，无法常规 import）
_LINTER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_layering.py"


def _load_linter():
    spec = importlib.util.spec_from_file_location("check_layering", _LINTER_PATH)
    assert spec and spec.loader, "无法加载 linter 模块"
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_layering"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def linter():
    return _load_linter()


def test_linter_scans_app_tree(linter):
    """scan() 应能扫描 backend/app/ 全树并返回结果（阶段 B 闭合后无 R5/R6 违规）。"""
    result = linter.scan()
    assert result.scanned > 0, "linter 至少应扫描到 backend/app/ 下的文件"

    # 阶段 B 闭合（PR-06）：image→writer / core→writer 反向依赖已全部切断，
    # scan() 不应再报告 R5（core→writer）或 R6（image→writer）违规。
    r5 = [v for v in result.violations if v.rule == "R5"]
    r6 = [v for v in result.violations if v.rule == "R6"]
    assert r5 == [], f"阶段 B 后不应有 core→writer 违规，实际：{r5}"
    assert r6 == [], f"阶段 B 后不应有 image→writer 违规，实际：{r6}"


def test_baseline_mode_passes(linter):
    """baseline 模式：当前无新增违规，应 exit 0。"""
    rc = linter.main([])
    assert rc == 0, "baseline 模式下不应有新增违规"


def test_strict_mode_fails_on_known_transitions(linter):
    """strict 模式：PR-11 后有 2 条已知过渡违规，应 exit 1。

    PR-12 修 R1（build_writer_model 下沉）后剩 1 条；create_type 归类后清零。
    """
    rc = linter.main(["--strict"])
    assert rc == 1, "strict 模式下应因存量过渡违规 fail"


def test_linter_detects_new_violation(linter, tmp_path, monkeypatch):
    """核心验收：新增一条 platform→domains 违规应被检出（即使 baseline 不含）。"""
    # 在临时 app 镜像里造一个 platform 文件违规 import domains
    fake_app = tmp_path / "app"
    (fake_app / "platform").mkdir(parents=True)
    (fake_app / "domains").mkdir(parents=True)
    (fake_app / "platform" / "__init__.py").write_text("")
    (fake_app / "domains" / "__init__.py").write_text("")
    (fake_app / "domains" / "leak.py").write_text("x = 1\n")
    (fake_app / "platform" / "bad.py").write_text(
        "from app.domains.leak import x\n"
    )

    monkeypatch.setattr(linter, "BACKEND_APP", fake_app)
    # 用空 baseline，确保这条被算作"新增"
    fake_baseline = tmp_path / "baseline.txt"
    monkeypatch.setattr(linter, "BASELINE_FILE", fake_baseline)

    result = linter.scan()
    r1 = [v for v in result.violations if v.rule == "R1"]
    assert len(r1) == 1, f"应检出 1 条 R1 (platform→domains) 违规，实际 {len(r1)}"
    assert "platform/bad.py" in r1[0].importer
    assert "app.domains.leak" in r1[0].imported


def test_same_domain_internal_import_not_flagged(linter, tmp_path, monkeypatch):
    """同 domain 内部子模块引用不应被误判（R3 误报防护）。"""
    fake_app = tmp_path / "app"
    (fake_app / "domains" / "image").mkdir(parents=True)
    (fake_app / "domains" / "__init__.py").write_text("")
    (fake_app / "domains" / "image" / "__init__.py").write_text("")
    (fake_app / "domains" / "image" / "store.py").write_text("x = 1\n")
    (fake_app / "domains" / "image" / "agent.py").write_text(
        "from app.domains.image.store import x\n"
    )

    monkeypatch.setattr(linter, "BACKEND_APP", fake_app)
    fake_baseline = tmp_path / "baseline.txt"
    monkeypatch.setattr(linter, "BASELINE_FILE", fake_baseline)

    result = linter.scan()
    # 不应有任何违规（同 domain 内部引用合法）
    assert result.violations == [], \
        f"同 domain 内部引用不应被误判，实际检出：{result.violations}"


def test_baseline_file_exists_and_tracks_transitions():
    """baseline 文件应存在；PR-12 后剩 2 条预先存在的跨域依赖。"""
    baseline = Path(__file__).resolve().parents[1] / "layering_baseline.txt"
    assert baseline.exists(), "backend/layering_baseline.txt 应存在"
    content = baseline.read_text(encoding="utf-8")
    violation_lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # PR-12 修 R1（build_writer_model 下沉）后剩 2 条：
    # - R2 image→writing.models（image 复用写作模型）
    # - R3 writing/meta→create_type.store
    assert len(violation_lines) == 2, f"PR-12 后 baseline 应有 2 条，实际 {len(violation_lines)}：{violation_lines}"
    assert any("image/agent.py|app.domains.writing.models" in l for l in violation_lines)
    assert any("meta/agent.py|app.create_type.store" in l for l in violation_lines)
    # R1 应已消除（build_writer_model 下沉到子类钩子）
    assert not any("base_service.py|app.domains.writing.models" in l for l in violation_lines), \
        "R1（platform→writing.models）应在 PR-12 消除"
