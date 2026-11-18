## 语言约束（{{language}}）

- 版本：Node.js 20 LTS 及以上；包管理用 npm（或项目既有的 pnpm / yarn，不混用）。
- 依赖：`package.json` 是唯一依赖清单；`node_modules/`、`dist/`、`build/` 不入库。
- 语言：新代码用 TypeScript，`strict` 打开；避免用 `any` 逃逸。
- 测试：Vitest（或项目既有 Jest）；测试文件 `*.test.ts`。
- 格式与检查：ESLint + Prettier，提交前跑 lint。
- 构建：`npm run build`，产物只从源码生成。
