# 本仓库约定（liiklin/DouyinLiveRecorder）

本仓库是上游开源项目 **ihmily/DouyinLiveRecorder** 的 fork（默认分支 `main`），
被父仓 `live-recorder-client` 作为子模块引用（`LiveRecorder\sidecars\DouyinLiveRecorder`），
为客户端提供抖音/快手直播地址解析的服务化包装（`sidecar.py`，常驻 `serve` 模式）。

## 一、改这里之前：先查上游有没有修（上游优先）

```bash
git remote add upstream https://github.com/ihmily/DouyinLiveRecorder.git   # 只需一次
git fetch upstream --tags
git log --oneline v4.0.7..upstream/main -- src/ sidecar.py      # 上游有哪些相关改动
git log -S "<报错文案 / 函数名 / 关键字>" upstream/main           # 上游有没有改过这段逻辑
# 上游 issue/PR：https://api.github.com/search/issues?q=repo:ihmily/DouyinLiveRecorder+<关键字>
```

- **上游已修 → 同步上游，不要自己另写一套**：`git merge upstream/main`（或 `git cherry-pick <上游提交>`），
  提交信息写明同步到的上游提交号。
- **上游没修 → 才自己实现**，并在提交信息里写明"上游未修"的查证结论（查了哪些提交/issue）。

父仓里有现成脚本，可直接用：`pwsh -File LiveRecorder\scripts\sync-upstream.ps1`（加 `-Merge -Push` 会同步并推回）。

## 二、尽量只改我们自己的文件

- 我们新增：`sidecar.py`（协议/常驻模式）、`test_sidecar.py`。**优先改这两个**。
- 已知被我们改过的上游文件：`src/spider.py`、`src/room.py`。动这两个文件前先看上游，
  能用"外层包装"解决的（比如在 `sidecar.py` 里判断）就不要改 `src/`，否则以后 `merge upstream/main` 一直冲突。
- 改完必跑：`python -m unittest test_sidecar`（当前 15 passed）。

## 三、提交与推送（独立仓库）

```bash
git add <文件> && git commit -m "fix(sidecar): ..."
git push git@github.com:liiklin/DouyinLiveRecorder.git main   # HTTPS 常握手失败，用 SSH
```

父仓随后更新指针：`git add LiveRecorder/sidecars/DouyinLiveRecorder` → 提交 → 推送。

## 四、别做的事

- 不要把客户端专有的东西塞进上游文件（例如中文报错文案、客户端 config 读取），放在 `sidecar.py` 里。
- 不要为了"顺手"重命名/格式化上游代码，会让同步上游变难。
