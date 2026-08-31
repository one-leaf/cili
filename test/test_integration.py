"""集成测试 - 完整工作流"""

import os


class TestIntegration:
    """集成测试 - 完整工作流"""

    def test_file_workflow(self, tools, test_workspace):
        """测试完整文件工作流"""
        from core.tools import get_tool_by_name

        # 1. 创建文件
        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="workflow.py", content="def add(a, b):\n    return a + b")

        # 2. 读取文件
        read_tool = get_tool_by_name(tools, "read")
        result = read_tool.execute(file_path="workflow.py")
        assert "def add" in result.output

        # 3. 编辑文件
        edit_tool = get_tool_by_name(tools, "edit")
        edit_tool.execute(
            file_path="workflow.py",
            old_text="def add(a, b):",
            new_text="def add(a: int, b: int) -> int:"
        )

        # 4. 验证编辑
        result = read_tool.execute(file_path="workflow.py")
        assert "a: int" in result.output

        # 5. 搜索文件
        grep_tool = get_tool_by_name(tools, "grep")
        result = grep_tool.execute(pattern="def add", path="workflow.py")
        assert not result.error
        assert "def add" in result.output

    def test_project_creation_workflow(self, tools, test_workspace):
        """测试项目创建工作流"""
        from core.tools import get_tool_by_name

        # 1. 创建项目结构
        write_tool = get_tool_by_name(tools, "write")
        write_tool.execute(file_path="myproject/__init__.py", content="")
        write_tool.execute(file_path="myproject/main.py", content="def main():\n    print('Hello')")
        write_tool.execute(file_path="myproject/utils.py", content="def helper():\n    pass")

        # 2. 验证结构
        find_tool = get_tool_by_name(tools, "find")
        result = find_tool.execute(pattern="*.py", path="myproject")
        assert not result.error
        assert "__init__.py" in result.output
        assert "main.py" in result.output
        assert "utils.py" in result.output

        # 3. 使用 bash 执行代码
        bash_tool = get_tool_by_name(tools, "bash")
        result = bash_tool.execute(command="python -c 'import myproject.main'")
        # 应该能成功导入
