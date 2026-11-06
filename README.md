# check_system

一个只读的系统检查小工具。运行后显示这台电脑的基本状态：系统、CPU、内存、磁盘、网络、端口、进程等，并提醒你需要注意的问题。

默认只读取信息，不修改任何文件或系统设置。只有加上 `--output` 时，才会把报告写入你指定的文件。

## 要求

- Python 3.8 或更高版本
- 不需要安装其它依赖，只使用 Python 标准库

## 快速开始

```bash
python check_system.py
```

## 常用例子

```bash
python check_system.py --all                  # 打开所有不需要指定目录的检查
python check_system.py --ports --processes    # 只看端口和占内存的程序
python check_system.py --folder-sizes D:\下载  # 看哪个子文件夹最占空间
python check_system.py --duplicates D:\下载    # 找出内容相同的重复文件
python check_system.py --json --output a.json # 存成 JSON 报告
python check_system.py --quiet               # 只显示需要注意的问题
```

## 检查项

默认会显示：系统信息、CPU、内存、磁盘、网络。

| 选项 | 检查什么 |
|---|---|
| `--dns` | 正在使用的 DNS 服务器 |
| `--firewall` | 防火墙是否开启（Windows 需要管理员） |
| `--reboot` | 是否有更新等待重启 |
| `--temp` | 临时文件夹占用的空间 |
| `--processes` | 占内存最多的程序 |
| `--ports` | 正在监听的端口 |
| `--connections` | 已经建立的外部连接 |
| `--startup` | 开机自动启动的项目 |
| `--users` | 系统上能登录的账户 |
| `--all` | 打开以上所有系统类检查 |
| `--large-files 目录` | 该目录下最大的文件 |
| `--recent-files 目录` | 该目录下最近改动的文件 |
| `--folder-sizes 目录` | 该目录下各子文件夹的占用 |
| `--duplicates 目录` | 该目录下内容相同的文件 |

其它选项：

- `--top 数量`：排行榜显示前几名，默认 10
- `--json`：输出 JSON，方便别的程序处理
- `--quiet`：文字输出只显示需要注意的问题
- `--output 文件`：把报告另外保存到文件
- `--version`：显示版本号

## 退出码

- `0`：检查完成，没有需要注意的问题
- `1`：检查完成，但有需要注意的问题
- `2`：指定的目录不存在

## 它不会做什么

- 不删除任何文件，重复文件只列出来，由你自己决定
- 不修改防火墙、服务、启动项、账户或密码
- 不连接外网，不上传任何信息
- 不读取文件内容（重复文件检测会计算指纹，但不会显示内容）

## 已知限制

- 有些检查需要管理员权限，比如防火墙状态。读不到时会在"没能完成的检查"里说明，而不是显示成没有问题。
- 普通用户看不到其它账户的程序名，端口列表里这些只显示 PID。
- 目录扫描最多检查 200,000 个文件，超过会提示。
- 目前主要在 Windows 上测试，Linux 和 macOS 的代码路径还没有在真实机器上完整验证。

## 测试

```bash
python -m unittest -v test_check_system.py
```

## 许可证

MIT License，见 [LICENSE](LICENSE)。
