"""Python 工具测试"""

import sys


class TestPythonTool:
    """Python 工具测试"""

    def test_python_execute_basic(self, tools):
        """测试 python 工具 - 基本执行"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        result = python_tool.execute(
            action="execute",
            code="print('Hello from Python')"
        )
        assert not result.error
        assert "Hello from Python" in result.output

    def test_python_execute_with_import(self, tools):
        """测试 python 工具 - 带导入的执行"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        code = """
import sys
print(f"Python {sys.version_info.major}.{sys.version_info.minor}")
"""
        result = python_tool.execute(action="execute", code=code)
        assert not result.error
        assert "Python" in result.output

    def test_python_info(self, tools):
        """测试 python 工具 - 环境信息"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        result = python_tool.execute(action="info")
        assert not result.error
        assert "Python" in result.output
        assert "Installed packages" in result.output

    def test_python_install_package(self, tools):
        """测试 python 工具 - 安装包"""
        from core.tools import get_tool_by_name

        python_tool = get_tool_by_name(tools, "python")
        # 安装一个小包用于测试
        result = python_tool.execute(action="install", packages="six")
        assert not result.error

        # 验证包已安装
        code = "import six; print(six.__version__)"
        result = python_tool.execute(action="execute", code=code)
        assert not result.error
