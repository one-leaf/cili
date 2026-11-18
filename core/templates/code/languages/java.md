## 语言约束（{{language}}）

- 版本：JDK 17 及以上；构建工具用项目已有的 Maven（`mvn`）或 Gradle（`./gradlew`），不混用。
- 依赖：只看 `pom.xml` / `build.gradle`；下载的依赖目录不入库。
- 测试：JUnit 5，测试放 `src/test/java`。
- 格式与检查：google-java-format 或 Checkstyle；公开 API 写 Javadoc。
- 构建：`mvn package` 或 `./gradlew build`；`target/`、`build/` 不入库。
