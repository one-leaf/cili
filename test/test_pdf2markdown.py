"""pdf2markdown 工具测试"""

import io
import os
import zipfile
from unittest.mock import Mock, patch

import pytest

from core.tools.pdf2markdown import (
    PDF2MarkdownTool,
    _AgentLimitError,
    _AuthError,
    _QuotaExceededError,
)


def _config_with_key(key: str):
    """构造带 mineru_api_key 的配置"""
    from core.config import Config, SystemConfig

    config = Config.__new__(Config)
    config.system = SystemConfig(mineru_api_key=key)
    return config


def _resp(json_data=None, content: bytes = b""):
    """构造 requests 响应 Mock"""
    r = Mock()
    r.status_code = 200
    r.content = content
    r.json.return_value = json_data or {}
    r.raise_for_status = Mock()
    return r


def _zip_bytes(md_name: str, md_content: str) -> bytes:
    """构造包含 full.md 的内存 ZIP"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(md_name, md_content)
    return buf.getvalue()


class TestPDF2MarkdownToolBasic:
    """基础测试"""

    def test_tool_name(self):
        """工具名称正确"""
        tool = PDF2MarkdownTool(cwd=".")
        assert tool.name == "pdf2markdown"

    def test_tool_description(self):
        """工具描述包含关键信息"""
        tool = PDF2MarkdownTool(cwd=".")
        assert "MinerU" in tool.description
        assert "PDF" in tool.description
        assert "Markdown" in tool.description
        assert "Agent API" in tool.description
        assert "Precision API" in tool.description

    def test_tool_parameters(self):
        """参数定义完整"""
        tool = PDF2MarkdownTool(cwd=".")
        params = tool.parameters
        assert params["type"] == "object"
        assert "file_path" in params["properties"]
        assert "timeout" in params["properties"]
        assert "output_path" in params["properties"]
        assert "model_version" in params["properties"]
        assert "file_path" in params["required"]

    def test_model_version_enum(self):
        """model_version 枚举值正确"""
        tool = PDF2MarkdownTool(cwd=".")
        enum = tool.parameters["properties"]["model_version"].get("enum")
        assert enum == ["pipeline", "vlm", "MinerU-HTML"]


class TestPDF2MarkdownToolValidation:
    """参数验证测试"""

    def test_missing_file_path(self):
        """缺少必填参数"""
        tool = PDF2MarkdownTool(cwd=".")
        result = tool.execute()
        assert result.is_error
        assert "file_path is required" in result.output

    def test_file_not_found(self):
        """文件不存在"""
        tool = PDF2MarkdownTool(cwd=".")
        result = tool.execute(file_path="nonexistent.pdf")
        assert result.is_error
        assert "file not found" in result.output

    def test_unsupported_extension(self, tmp_path):
        """不支持的文件格式（cwd 用 tmp_path，满足 workspace 边界）"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path))
        tmp = tmp_path / "test.txt"
        tmp.write_bytes(b"test")

        result = tool.execute(file_path=str(tmp))
        assert result.is_error
        assert "unsupported file type" in result.output
        assert ".txt" in result.output

    def test_supported_extensions(self):
        """支持的文件格式列表"""
        from core.tools.pdf2markdown import _PRECISION_SUPPORTED_EXT

        expected = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".html"}
        assert _PRECISION_SUPPORTED_EXT == expected


class TestPDF2MarkdownToolErrors:
    """错误处理测试"""

    def test_agent_limit_error(self):
        """Agent 限制异常"""
        try:
            raise _AgentLimitError("test limit")
        except _AgentLimitError as e:
            assert "test limit" in str(e)

    def test_auth_error(self):
        """认证异常"""
        try:
            raise _AuthError("token expired")
        except _AuthError as e:
            assert "token expired" in str(e)

    def test_quota_exceeded_error(self):
        """配额超限异常"""
        try:
            raise _QuotaExceededError("daily limit")
        except _QuotaExceededError as e:
            assert "daily limit" in str(e)


class TestPDF2MarkdownToolPrecisionErrors:
    """Precision API 错误码处理测试"""

    def test_handle_auth_error_a0202(self):
        """A0202 错误码"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(_AuthError) as exc_info:
            tool._handle_precision_error("A0202", "Token 错误")
        assert "A0202" in str(exc_info.value)

    def test_handle_auth_error_a0211(self):
        """A0211 错误码"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(_AuthError) as exc_info:
            tool._handle_precision_error("A0211", "Token 过期")
        assert "A0211" in str(exc_info.value)

    def test_handle_quota_error(self):
        """-60018 每日限额错误"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(_QuotaExceededError):
            tool._handle_precision_error(-60018, "每日解析任务数量已达上限")

    def test_handle_file_size_error(self):
        """-60005 文件大小超限"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(Exception) as exc_info:
            tool._handle_precision_error(-60005, "文件太大")
        assert "200MB" in str(exc_info.value)

    def test_handle_page_limit_error(self):
        """-60006 页数超限"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(Exception) as exc_info:
            tool._handle_precision_error(-60006, "页数太多")
        assert "200页" in str(exc_info.value)

    def test_handle_html_quota_error(self):
        """-60019 HTML 解析额度不足"""
        tool = PDF2MarkdownTool(cwd=".")
        with pytest.raises(Exception) as exc_info:
            tool._handle_precision_error(-60019, "HTML 额度不足")
        # 应该包含错误码和提示
        assert "-60019" in str(exc_info.value)


class TestPDF2MarkdownToolAgentErrors:
    """Agent API 错误码处理测试"""

    @patch("requests.post")
    def test_agent_file_size_limit(self, mock_post, tmp_path):
        """Agent API 文件超限 (-30001)"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path))

        mock_response = Mock()
        mock_response.json.return_value = {
            "code": -30001,
            "msg": "文件大小超出轻量接口限制",
        }
        mock_post.return_value = mock_response

        tmp = tmp_path / "test.pdf"
        tmp.write_bytes(b"test")

        result = tool.execute(file_path=str(tmp))
        assert result.is_error
        # 应该尝试 Agent，失败后如果没有 API key 会报错
        assert "未配置" in result.output or "MinerU API Key" in result.output

    @patch("requests.post")
    def test_agent_unsupported_type(self, mock_post, tmp_path):
        """Agent API 不支持的文件类型 (-30002)"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path))

        mock_response = Mock()
        mock_response.json.return_value = {
            "code": -30002,
            "msg": "轻量接口不支持该文件类型",
        }
        mock_post.return_value = mock_response

        # 用 .doc 格式（Agent 不支持但 Precision 支持）
        tmp = tmp_path / "test.doc"
        tmp.write_bytes(b"test")

        result = tool.execute(file_path=str(tmp))
        # Agent 不支持 .doc，会跳过，因为没有 API key 所以报错
        assert result.is_error


class TestPDF2MarkdownToolConfig:
    """配置测试"""

    def test_get_api_key_from_config(self):
        """从配置获取 API key"""
        from core.config import Config, SystemConfig

        config = Config.__new__(Config)
        config.system = SystemConfig(mineru_api_key="test-key-123")

        tool = PDF2MarkdownTool(cwd=".", config=config)
        key = tool._get_mineru_api_key()
        assert key == "test-key-123"

    def test_get_api_key_from_env(self):
        """从环境变量获取 API key"""
        with patch.dict(os.environ, {"MINERU_API_KEY": "env-key-456"}):
            tool = PDF2MarkdownTool(cwd=".")
            key = tool._get_mineru_api_key()
            assert key == "env-key-456"

    def test_get_api_key_empty(self):
        """无 API key"""
        tool = PDF2MarkdownTool(cwd=".")
        key = tool._get_mineru_api_key()
        # 可能为空字符串或环境变量中的值
        assert isinstance(key, str)


class TestPDF2MarkdownPrecisionFirst:
    """商业 API 优先策略测试（配置 apikey → Precision 优先，失败降级 Agent）"""

    @patch("requests.get")
    @patch("requests.put")
    @patch("requests.post")
    def test_api_key_tries_precision_first(self, mock_post, mock_put, mock_get, tmp_path):
        """配置了 API key → 优先走 Precision API（vlm 模型），不调用免费 Agent API"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path), config=_config_with_key("k-1"))
        tmp = tmp_path / "doc.pdf"
        tmp.write_bytes(b"%PDF-fake")

        zip_bytes = _zip_bytes("doc/full.md", "# 精析结果")
        post_urls = []

        def post_side(url, **kw):
            post_urls.append(url)
            if url.endswith("/api/v4/file-urls/batch"):
                assert kw["json"]["model_version"] == "vlm"
                assert kw["headers"]["Authorization"] == "Bearer k-1"
                return _resp({"code": 0, "data": {"batch_id": "b1", "file_urls": ["http://oss/upload"]}})
            raise AssertionError(f"Agent API 不应被调用: {url}")

        def get_side(url, **kw):
            if "/extract-results/batch/" in url:
                return _resp({"code": 0, "data": {"extract_result": [
                    {"state": "done", "full_zip_url": "http://zip/f"}]}})
            if url == "http://zip/f":
                return _resp(content=zip_bytes)
            raise AssertionError(f"unexpected GET: {url}")

        mock_post.side_effect = post_side
        mock_get.side_effect = get_side
        mock_put.return_value = _resp()

        result = tool.execute(file_path=str(tmp))
        assert not result.is_error
        assert "Precision" in result.output
        assert "api/v4" in post_urls[0]
        assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# 精析结果"

    @patch("requests.get")
    @patch("requests.put")
    @patch("requests.post")
    def test_quota_failure_falls_back_to_agent(self, mock_post, mock_put, mock_get, tmp_path):
        """Precision 每日配额超限 → 降级免费 Agent API 完成，结果标注降级原因"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path), config=_config_with_key("k-2"))
        tmp = tmp_path / "doc.pdf"
        tmp.write_bytes(b"%PDF-fake")

        def post_side(url, **kw):
            if "/api/v4/" in url:
                return _resp({"code": -60018, "msg": "每日解析任务数量已达上限"})
            if url.endswith("/api/v1/agent/parse/file"):
                return _resp({"code": 0, "data": {"task_id": "t1", "file_url": "http://oss/u"}})
            raise AssertionError(f"unexpected POST: {url}")

        def get_side(url, **kw):
            if url.endswith("/api/v1/agent/parse/t1"):
                return _resp({"code": 0, "data": {"state": "done", "markdown_url": "http://cdn/md"}})
            if url == "http://cdn/md":
                return _resp(content="# 免费结果".encode("utf-8"))
            raise AssertionError(f"unexpected GET: {url}")

        mock_post.side_effect = post_side
        mock_get.side_effect = get_side
        mock_put.return_value = _resp()

        result = tool.execute(file_path=str(tmp))
        assert not result.is_error
        assert "[Agent API]" in result.output
        assert "降级" in result.output
        assert "每日解析任务数量已达上限" in result.output
        assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# 免费结果"

    @patch("requests.get")
    @patch("requests.put")
    @patch("requests.post")
    def test_auth_failure_falls_back_to_agent(self, mock_post, mock_put, mock_get, tmp_path):
        """Precision 认证失败（A0202）→ 降级免费 Agent API"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path), config=_config_with_key("bad-key"))
        tmp = tmp_path / "doc.pdf"
        tmp.write_bytes(b"%PDF-fake")

        def post_side(url, **kw):
            if "/api/v4/" in url:
                return _resp({"code": "A0202", "msg": "authenticate failed"})
            if url.endswith("/api/v1/agent/parse/file"):
                return _resp({"code": 0, "data": {"task_id": "t2", "file_url": "http://oss/u"}})
            raise AssertionError(f"unexpected POST: {url}")

        def get_side(url, **kw):
            if url.endswith("/api/v1/agent/parse/t2"):
                return _resp({"code": 0, "data": {"state": "done", "markdown_url": "http://cdn/md"}})
            if url == "http://cdn/md":
                return _resp(content="# fallback".encode("utf-8"))
            raise AssertionError(f"unexpected GET: {url}")

        mock_post.side_effect = post_side
        mock_get.side_effect = get_side
        mock_put.return_value = _resp()

        result = tool.execute(file_path=str(tmp))
        assert not result.is_error
        assert "[Agent API]" in result.output
        assert "A0202" in result.output

    @patch("requests.post")
    def test_precision_fail_oversize_no_fallback(self, mock_post, tmp_path):
        """Precision 失败且文件超出 Agent 限制（>10MB）→ 不降级，直接报错"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path), config=_config_with_key("k-3"))
        tmp = tmp_path / "big.pdf"
        tmp.write_bytes(b"0" * (11 * 1024 * 1024))
        mock_post.return_value = _resp({"code": -60010, "msg": "解析失败"})

        result = tool.execute(file_path=str(tmp))
        assert result.is_error
        assert "无法降级" in result.output

    @patch("requests.post")
    def test_no_key_keeps_agent_first(self, mock_post, tmp_path):
        """未配置 API key → 仍走免费 Agent API（原逻辑不变）"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path))
        tmp = tmp_path / "doc.pdf"
        tmp.write_bytes(b"%PDF-fake")
        mock_post.return_value = _resp({"code": 0, "data": {"task_id": "t3", "file_url": "http://oss/u"}})

        with patch("requests.get") as mock_get, patch("requests.put") as mock_put:
            mock_get.side_effect = lambda url, **kw: (
                _resp({"code": 0, "data": {"state": "done", "markdown_url": "http://cdn/md"}})
                if url.endswith("/api/v1/agent/parse/t3") else _resp(content="# free".encode("utf-8"))
            )
            mock_put.return_value = _resp()
            result = tool.execute(file_path=str(tmp))

        assert not result.is_error
        assert "[Agent API]" in result.output
        # 不应触碰商业 API
        called = mock_post.call_args[0][0]
        assert "api/v1/agent" in called


class TestPDF2MarkdownToolModelVersion:
    """model_version 参数测试"""

    def test_html_auto_model_version(self, tmp_path):
        """HTML 文件自动选择 MinerU-HTML"""
        tool = PDF2MarkdownTool(cwd=str(tmp_path))

        tmp = tmp_path / "test.html"
        tmp.write_bytes(b"<html>test</html>")

        # 模拟执行，检查 model_version 被正确设置
        # 由于没有 API key，会报错，但可以验证逻辑
        result = tool.execute(file_path=str(tmp), model_version="")
        # HTML 应该自动设为 MinerU-HTML，但没有 API key 所以失败
        assert result.is_error

    def test_custom_model_version(self):
        """自定义 model_version"""
        tool = PDF2MarkdownTool(cwd=".")

        # 验证参数接受自定义 model_version
        params = tool.parameters["properties"]["model_version"]
        assert params["enum"] == ["pipeline", "vlm", "MinerU-HTML"]


class TestPDF2MarkdownToolIntegration:
    """集成测试（需要网络，标记为 slow）"""

    @pytest.mark.slow
    def test_agent_api_flow(self):
        """Agent API 完整流程（需要真实 API 调用）"""
        # 这个测试需要真实网络调用，标记为 slow
        # 在实际测试中可以跳过
        pass

    @pytest.mark.slow
    def test_precision_api_flow(self):
        """Precision API 完整流程（需要 API key）"""
        # 这个测试需要 API key，标记为 slow
        pass


# 工具注册测试
class TestToolRegistration:
    """工具注册测试"""

    def test_tool_in_factory(self):
        """工具在工厂中正确注册"""
        from core.tools import create_tools

        tools = create_tools(cwd=".test", workspace_uuid="test", session=None, config=None)
        tool_names = [t.name for t in tools]
        assert "pdf2markdown" in tool_names

    def test_tool_schema(self):
        """工具 schema 正确"""
        tool = PDF2MarkdownTool(cwd=".")
        schema = tool.to_schema()
        assert schema["name"] == "pdf2markdown"
        assert "input_schema" in schema
        assert schema["input_schema"]["type"] == "object"
