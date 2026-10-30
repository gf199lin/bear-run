# 没装 Python？按这里做（5 分钟）

本包所有程序都是 Python 脚本，所以电脑上要有 **Python 3.8 或更高版本**。

---

## 一、先确认到底有没有

按 `Win + R`，输入 `cmd`，回车，在黑窗口里输入：

```
py -3 -V
```

- 显示 `Python 3.x.x` → 已经装好了，**关掉窗口，回去双击 `1-安装环境.cmd`**。
- 提示「不是内部或外部命令」 → 继续看第二节。

---

## 二、安装

1. 打开 https://www.python.org/downloads/windows/
2. 下载 **Windows installer (64-bit)** 的最新 3.x 版本。
3. 双击安装包，**第一屏底部一定要勾选**：

   ```
   [x] Add python.exe to PATH
   ```

   （这一步不勾，后面所有命令都会找不到 Python，是最常见的坑。）

4. 点 **Install Now**，等它装完。
5. **关掉所有已经打开的命令行窗口**（PATH 要重新加载才生效）。

---

## 三、验证

重新按 `Win + R` → `cmd` → 输入：

```
py -3 -V
```

看到版本号就成功了。回去双击 `1-安装环境.cmd`，它会自动装 `pyserial` 和 `numpy`。

---

## 四、常见问题

**Q：`py` 命令没有，但 `python` 有？**

本包的脚本会自动依次尝试 `py -3` 和 `python`，所以能用 `python` 也行。验证方式：

```
python -V
```

**Q：输入 `python` 却弹出了微软应用商店？**

说明系统里只有「应用执行别名」，没有真 Python。到「设置 → 应用 → 高级应用设置 → 应用执行别名」，把 `python.exe` / `python3.exe` 两个开关**关掉**，然后按第二节正常安装。

**Q：公司/学校电脑不让装软件？**

那就用**便携版**：下载 "Windows embeddable package (64-bit)"，解压到 `D:\python`，然后在本包目录里用完整路径运行，例如：

```
D:\python\python.exe radar_autoconnect.py check
```

便携版默认没有 pip，需要额外装；如果只是跑 `2-检查雷达.cmd`，也需要 pyserial，建议优先争取正常安装。

**Q：装完还是提示找不到？**

99% 是没勾 PATH。重装一遍，勾上 `Add python.exe to PATH`，装完关掉所有命令行窗口重开。
