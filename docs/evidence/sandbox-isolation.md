# 沙箱隔离对照实测

重跑：`make sandbox-check ARGS=--local` / `make sandbox-check`
生成于 2026-09-11 16:44 · 镜像 `repopilot-sandbox:py312` · Darwin 24.6.0

> 两组跑的是**同一份探针、同一批绝对路径**（路径在宿主机上算好后字面量塞进去，
> 探针里不写 `expanduser` —— 容器里 `HOME=/tmp` 会让它漂成 `/tmp/.ssh`，
> 那样对照就不成立了）。唯一的差别就是被测的那个变量：跑在哪种沙箱里。

```
$ make sandbox-check ARGS=--local          # 对照组：本地子进程
======================================================================
本地子进程（对照组）
======================================================================
  ESCAPED  联网成功
  ESCAPED  看得见: /Users
  ESCAPED  看得见: /Users/kayou/.ssh
  ESCAPED  看得见: /Users/kayou/Documents/py_agent/.env
  BLOCKED  读 /etc/shadow: FileNotFoundError
  BLOCKED  根目录只读: OSError
  INFO     workspace 可写（应该的）
  INFO     uid=501 cwd=/private/var/folders/9x/s69qtgvj1bs721mgq03gwtcm0000gn/T/sandbox-check-mlcledh1 python=/Users/kayou/Documents/py_agent/.venv/bin/python3
越狱成功 4 项，耗时 670ms

$ make sandbox-check                       # 容器沙箱
======================================================================
容器沙箱（repopilot-sandbox:py312）
======================================================================
  BLOCKED  联网被拦: OSError
  BLOCKED  看不见: /Users
  BLOCKED  看不见: /Users/kayou/.ssh
  BLOCKED  看不见: /Users/kayou/Documents/py_agent/.env
  BLOCKED  读 /etc/shadow: PermissionError
  BLOCKED  根目录只读: OSError
  INFO     workspace 可写（应该的）
  INFO     uid=501 cwd=/var/folders/9x/s69qtgvj1bs721mgq03gwtcm0000gn/T/sandbox-check-xkxiitt2 python=/usr/local/bin/python3
越狱成功 0 项，耗时 400ms
```
