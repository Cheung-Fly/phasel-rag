"""后端单元测试包。

加上这个文件是为了让测试可以作为包被发现与运行：

    python -m unittest discover -s tests -t .

在此之前 `backend/tests/` 没有 `__init__.py`，也没有 pytest 依赖，
800 行测试实际上处于「写好了但跑不起来」的状态。测试不进入 api 镜像
（Dockerfile 只 COPY app 与 scripts），运行时通过挂载或 docker cp 注入。
"""
