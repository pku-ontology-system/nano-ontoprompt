# 决策平台

本仓库的默认 `main` 是统一平台UI与逐步接入的模块。早期旧系统保存在 `legacy/…` 分支；入口存在不代表对应业务后台已接通。

## 启动平台

需要 Node.js 24 和 npm。在仓库根目录执行：

```sh
cd apps/platform-web
npm ci
npm run dev
```

打开 http://127.0.0.1:4317 。构建与检查：

```sh
npm run lint
npm run build
npm run test:registry
npx playwright install chromium
npm run test:ci
```

浏览器检查自行启动并关闭本次使用的临时服务，不需要先开开发服务器。Linux首次安装浏览器可用 `npx playwright install --with-deps chromium`。

## 从哪里继续

- [团队与AI协作规则及旧版本位置](CONTRIBUTING.md)
- [模块接入说明](docs/platform/MODULE_INTEGRATION.md)
- [演示计划](docs/platform/DEMO.md)
- [批准的导航设计正文](docs/platform/design/NAVIGATION_FULL_v0.2.md)

现阶段公共导航、搜索、收藏、最近使用、通知与账户交互使用浏览器本地数据。真实后台、登录与服务端权限尚待模块接入，不可用于生产业务数据。此说明不作为产品页面标签。

轻量CI只检查UI依赖、代码、构建与核心导航，不部署网站，也不调用旧后台或生产数据。
