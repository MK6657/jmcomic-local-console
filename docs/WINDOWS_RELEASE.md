# Windows 便携版

本分支先提供64位Windows便携包，包含Python运行时，使用现有浏览器界面。前端主题仍为eye-care。当前没有安装程序、自动更新、托盘或新的客户端界面。

## 使用

将ZIP完整解压到一个有写入权限的独立目录，双击 `JMComic.exe`。不要只复制EXE，旁边的 `_internal` 目录包含运行时、原生DLL、模板、静态资源和证书。程序打开本机页面；新启动的服务由控制台持有，按Enter或Ctrl+C停止。若复用了已有的当前版本服务，启动器只打开页面，不接管那个进程。

默认数据位于EXE同级的 `runtime/` 与 `downloads/`，不要放在Program Files等普通用户不可写的目录。`JMCONSOLE_HOME`、`JMCONSOLE_DOWNLOAD_DIR`、`JMCONSOLE_CACHE_DIR`、`JMCONSOLE_BACKUP_DIR` 沿用源码版的配置方式，启动前设置，运行中不能切换。环境变量只改变位置，不搬迁数据。

服务只监听127.0.0.1。源码版与便携版仍共用Windows单实例锁，不能同时启动两个正常服务。解压此包不会替换正在使用的版本，也不会自动导入个人数据。

自检命令不需要日常实例停机，使用临时目录与本机临时端口，不启动任务调度器：

```powershell
.\JMComic.exe self-check
```

退出码0且JSON中 `status` 为 `ok` 才表示自检通过。它检查打包后的页面、静态资源、设置API、WebP/GIF、PDF组件及SDK/libcurl本机取图，不证明真实上游搜索或下载可用。

维护命令沿用现有CLI，正常备份/恢复需先关闭本程序；恢复只写入不存在的新实例目录：

```powershell
.\JMComic.exe cli backup --output 'D:\PrivateBackups\jmcomic.zip'
.\JMComic.exe cli backup-info 'D:\PrivateBackups\jmcomic.zip'
.\JMComic.exe cli restore 'D:\PrivateBackups\jmcomic.zip' --to 'D:\JMComicRestored'
```

升级前先备份，关闭旧版本，再将新包解压到另一个目录验证。通过环境变量或恢复到全新实例决定使用的数据位置；不要将整包覆盖到个人实例，不自动降级数据库。旧版程序不能打开升级后的新版数据库。

## 从源码构建

在64位Windows创建独立构建环境，构建依赖不装到日常服务的虚拟环境：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-build\Scripts\python.exe -m pip check
.\.venv-build\Scripts\python.exe tools/build_windows.py
```

构建脚本生成清单和源码指纹，使用PyInstaller文件夹模式，包含SDK动态模块、图片/PDF与curl原生依赖。构建后从不含Python的PATH启动产物自检，成功才发布到新的 `releases/windows-x64-指纹/`；已存在的目标不覆盖。自定义 `--output` 必须与项目在同一盘，跨盘会在构建前拒绝；完成后可将ZIP复制到任意盘。输出包括可运行目录、ZIP、SHA-256与依赖版本清单。运行目录、数据库、下载、日志、Cookie和代理配置不在打包输入中。

需要验证实际ZIP，可显式运行以下脚本（普通pytest仍禁止网络）：

```powershell
.\.venv-build\Scripts\python.exe tests/check_windows_release.py 'releases\windows-x64-实际指纹' --check-launcher
```

它核实ZIP哈希/CRC，解压到含中文和空格的临时目录，再从无Python的PATH及无关工作目录运行EXE自检；检查继承的实例目录未被读取或创建，以及便携CLI能验证一个空数据库备份。`--check-launcher` 额外验证单实例锁被占用时，启动器的实际EXE子进程会退出，不接管或停止已有服务。

产物尚未签名。当前机器构建及自检不能替代干净Windows虚拟机的兼容性验收；真实上游、完整安装/卸载体验、Docker仍需分别验收。
