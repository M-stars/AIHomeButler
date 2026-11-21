# Capacitor Android 壳工程（APK 打包）

把 uni-app H5 构建产物包成原生 Android 壳（docs/APP_DESIGN.md 的安卓路线：
Capacitor 签名 APK），不依赖 HBuilderX 云打包，可完全本地构建。

## 环境要求（本机已验证）

| 组件 | 版本 | 位置 |
|---|---|---|
| JDK | 21（Temurin，JAVA_HOME=D:\JAVA） | gradle 8.11.1 要求 |
| Android SDK | platforms;android-35 + build-tools;35.0.0 + platform-tools | D:\Android\android-sdk |
| Gradle | 8.11.1（手动解压 D:\gradle-8.11.1，或走 gradlew wrapper） | — |

SDK 缺组件时用 cmdline-tools 补（`sdkmanager --sdk_root=D:\Android\android-sdk "build-tools;35.0.0" "platform-tools"`）。

## 一次打包流程

```bash
# 1. 重建 H5 产物并同步进壳工程（uni-app 上游改动后都要重跑）
cd aihome-butler-mobile && npm run build:h5
rm -rf android-app/www && cp -r dist/build/h5 android-app/www

# 2. 同步到 Android 工程（新增插件/修改 capacitor.config.json 后需要）
cd android-app && npx cap sync android

# 3. 构建（签名配置见下；产物 android/app/build/outputs/apk/release/app-release.apk）
export JAVA_HOME="D:\JAVA"
export GRADLE_USER_HOME="D:\AIHomeButler\aihome-butler-mobile\android-app\.gradle-home"   # 独立缓存，绕开全局 init.d 镜像
cd android-app/android
D:/gradle-8.11.1/bin/gradle.bat assembleRelease --no-daemon

# 4. 归档
cp app/build/outputs/apk/release/app-release.apk ../apk/AIHomeButler-v1.0.0-release.apk
```

## 签名（release）

- `android/aihome-butler.keystore` + `android/keystore.properties`（storeFile/storePassword/
  keyAlias/keyPassword）**均不入 git**（android/.gitignore 已忽略）；当前为本地测试密钥
  （alias `aihome-butler`，有效期 10000 天），正式上架前应换正式密钥并妥善保管。
- 缺失这两个文件时 release 退化为未签名构建（`app/build.gradle` 有守卫）。
- 重新生成：`keytool -genkeypair -keystore aihome-butler.keystore -alias aihome-butler -keyalg RSA -keysize 2048 -validity 10000`

## 已知坑（踩过的）

1. **全局 GRADLE_USER_HOME（D:\dev\.gradle）的 init.d/mirrors.gradle 会把所有仓库
   前置镜像到 bmclapi**——那是 MinecraftForge maven 镜像，没有 Android 依赖，构建必挂
   `Could not find framework-10.1.1.jar`。解法：构建前 export 独立
   `GRADLE_USER_HOME=android-app/.gradle-home`（已被 .gitignore）。
2. `local.properties` 的 `sdk.dir` 必须用**正斜杠**（`D:/Android/android-sdk`）；
   properties 文件里单个反斜杠是转义符，会被吃掉导致"卷标语法不正确"。
3. Windows 上强杀 gradle 进程会在 caches/transforms 留下文件锁，下次构建报
   `AccessDeniedException ... transforms`——删掉 `.gradle-home/caches/8.11.1/transforms` 重跑即可。
4. WebView 需访问局域网 `http://192.168.x.x:8080`：Manifest 已加
   `android:usesCleartextTraffic="true"`，capacitor.config.json 设 `androidScheme: "http"`
   + `cleartext: true`。服务端 401 已带 CORS 头（2026-11-21 修复），跨源凭证失效收敛在
   APK 内与 H5 行为一致。

## 已验证（2026-11-21）

`AIHomeButler-v1.0.0-release.apk`（3.3MB，minSdk 23 / target 35，已随仓库交付于 `android-app/apk/`）：
apksigner 验签通过（CN=AI Home Butler, OU=GraduationProject）；badging 确认包名
`com.aihome.butler`、应用名「AI家庭管家」、INTERNET 权限齐全。真机安装体验待 Pi/HA 环境联调。
