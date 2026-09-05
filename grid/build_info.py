"""部署版本戳：让 Agent Card 对外暴露"现在服务的是哪个版本"。

deploy-to-wsl.sh 在部署时把 ``<git短hash>-<时间戳>`` 写进仓库根的 ``VERSION``
文件（该文件不进 git）；四个 server 启动时读它填进 AgentCard.version。
没有 VERSION 文件（例如直接在开发副本上手动起 server）时返回 "dev"。

这是 2026-09-05 收敛的核心可见性手段：此前两份副本漂移了两周而所有健康
检查全绿，因为卡片永远是 0.0.1 —— curl 一下卡片就能看出在跑谁、哪个版本。
"""
from pathlib import Path


def version_stamp() -> str:
    version_file = Path(__file__).resolve().parent / "VERSION"
    try:
        stamp = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        stamp = ""
    return stamp or "dev"
