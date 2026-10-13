from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    env = os.environ.get("CODEX_PARALLEL_HOME")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "data"


@dataclass
class HubConfig:
    root: Path = field(default_factory=_default_data_dir)
    host: str = "0.0.0.0"
    port: int = 8787
    default_backend: str = "local"
    default_concurrency: int = 8
    item_timeout_s: int = 1800
    worker_heartbeat_s: int = 15
    max_item_retries: int = 2
    sandbox_env_timeout_s: int = 600
    login_probe_timeout_s: int = 90
    e2b_template: str = "codex-parallel-sandbox"
    # 全局代理（天津集群 Clash: http://10.120.0.246:7890）。
    # 优先级: CODEX_PARALLEL_PROXY 环境变量 > 默认值。设 "0" 或 "" 显式禁用。
    # 作用面：RT 兑换（token_exchange）、local backend 的 codex 子进程、
    # hub 自身所有出网请求。e2b 沙箱内网络由沙箱侧决定，不受此影响。
    _env_proxy = os.environ.get("CODEX_PARALLEL_PROXY", "").strip()
    if _env_proxy and _env_proxy != "0":
        proxy: str | None = _env_proxy
    elif _env_proxy == "0":
        proxy: str | None = None
    else:
        proxy: str | None = "http://10.120.0.246:7890"

    @property
    def db_path(self) -> Path:
        return self.root / "hub.db"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def workspace_root(self) -> Path:
        return self.root / "workspaces"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    def ensure_dirs(self) -> None:
        for p in (self.root, self.secrets_dir, self.workspace_root, self.results_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.chmod(0o700)


def load_config(**overrides) -> HubConfig:
    cfg = HubConfig()
    if os.environ.get("CODEX_PARALLEL_PORT"):
        cfg.port = int(os.environ["CODEX_PARALLEL_PORT"])
    if os.environ.get("CODEX_PARALLEL_HOST"):
        cfg.host = os.environ["CODEX_PARALLEL_HOST"]
    if os.environ.get("CODEX_PARALLEL_BACKEND"):
        cfg.default_backend = os.environ["CODEX_PARALLEL_BACKEND"]
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg
