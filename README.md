# VS Code VSIX Downloader

Download VS Code extensions from Visual Studio Marketplace for offline installation.

从 Visual Studio Marketplace 下载 VS Code 扩展的 VSIX 离线安装包，支持按版本、平台筛选和列出可用版本。脚本只使用 Python 标准库。

## 用法

```sh
python vscode_ext_downloader.py --help
python vscode_ext_downloader.py ms-python.python --list
python vscode_ext_downloader.py ms-python.python --platform win32-x64 --output ./vsix
```

可使用扩展标识 `publisher.name`，也可直接粘贴市场页面链接。下载后，在 VS Code 命令面板选择 **Extensions: Install from VSIX…**，或使用 `code --install-extension 文件名.vsix`。

下载时需要访问 Marketplace。建议根据目标机器选择扩展版本和平台，下载的 VSIX 按各扩展自己的许可使用。
