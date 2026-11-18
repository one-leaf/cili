## 语言约束（{{language}}）

- 版本：C++17 及以上；构建用 CMake，构建目录 `build/` 不入库。
- 依赖：系统包优先；第三方库按项目现状（vcpkg / Conan / 源码引入）。
- 测试：GoogleTest（或项目既有框架）。
- 格式与检查：clang-format；编译警告（`-Wall -Wextra`）视为要处理的问题。
- 资源管理：RAII 与智能指针优先，不裸 `new` / `delete`。
