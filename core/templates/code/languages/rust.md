## 语言约束（{{language}}）

- 版本：稳定版 Rust，统一用 `cargo`（`cargo build` / `cargo test` / `cargo run`）。
- 依赖：看 `Cargo.toml`；`Cargo.lock` 入库。
- 格式与检查：`cargo fmt`、`cargo clippy -- -D warnings`。
- 错误处理：用 `Result` + `?`；库代码避免 `unwrap()` / `expect()`（测试除外）。
- 构建：`target/` 不入库。
