# 跑不可信代码的沙箱镜像。
#
# ★为什么必须**预先**把运行时装进镜像：容器是 `--network none` 起的，
# 里面装不了任何东西。所以「装依赖」和「跑不可信代码」必须是两个阶段 ——
# 构建时联网备好，运行时断网执行。
#
# 这也是容器化跑测试真正的难点：**容器里必须有目标仓库需要的运行时**。
# 现在只备了 pytest（够跑我们的评测集）。要接任意仓库，得在 clone 之后、
# 断网执行之前，插一个「按 requirements 装依赖」的联网构建步骤。
# 那一步本身也在跑别人的代码（setup.py / build hook），需要单独的隔离。
#
#   docker build -f docker/sandbox.Dockerfile -t repopilot-sandbox:py312 .
#
# 用 gcr 镜像源：这台机器上 Docker Hub 经常拉不动。

FROM mirror.gcr.io/library/python:3.12-slim

# 只装测试运行器本身，不装任何网络/系统工具 —— 镜像里没有的东西，
# 不可信代码就用不上。这是纵深防御的一层：`curl` 不存在就没法外传。
RUN pip install --no-cache-dir pytest==8.3.4

# 不设 USER：容器由 `--user $(id -u):$(id -g)` 以宿主机身份启动，
# 这样 bind mount 里新建的文件属主才是宿主机用户（否则是 root，
# 宿主机清理 workspace 会失败）。
WORKDIR /work
