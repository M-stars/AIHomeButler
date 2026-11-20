import { defineConfig } from 'vitest/config';
import path from 'path';

/**
 * 单元测试专用配置：不加载 uni-app 编译插件（纯 Node 环境），
 * 被测对象限定为 services/stores 的纯 TS 逻辑层；
 * 组件渲染与端上行为由 H5/小程序构建产物人工验收覆盖。
 */
export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts'],
  },
});
