## 语言约束（{{language}}）

- 版本：.NET 8 及以上；统一用 `dotnet` CLI（`dotnet build` / `dotnet test`）。
- 依赖：`.csproj` 为依赖清单；`bin/`、`obj/` 不入库。
- 测试：xUnit，测试项目放 `tests/`。
- 格式与检查：`dotnet format`；启用 nullable。
- 命名：公共成员 PascalCase，局部变量 camelCase。
