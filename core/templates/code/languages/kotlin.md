## 语言约束（{{language}}）

- 版本：Kotlin 1.9 及以上；JVM 或 Android 按项目现状；构建用 Gradle（`./gradlew`）。
- 空安全：不用 `!!`，优先 `?.`、`?:` 或显式校验。
- 测试：JUnit 5 / kotlin.test；Android 另加 Robolectric 或 Espresso。
- 格式：ktlint；`build/`、`.gradle/` 不入库。
