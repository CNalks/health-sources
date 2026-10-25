# hs-intake：GitHub Actions → 服务器的受限推送

安装（root，一次性）：

```bash
useradd --system --create-home --shell /bin/sh hs-intake          # 需要 shell 才能执行强制命令；登录被 restrict+command 锁死
install -m 0755 deploy/intake/hs-intake /usr/local/bin/hs-intake
install -d -o hs-intake -g health-sources -m 0755 /var/lib/health-sources/inbox
setfacl -m u:hs-intake:rwx /var/lib/ghs-pipeline/raw              # 只为原子替换 cdc_nndss_us.csv
install -d -m 700 -o hs-intake -g hs-intake ~hs-intake/.ssh
# authorized_keys 一行（公钥来自新生成的专用密钥，私钥只放 GitHub secret HS_INTAKE_SSH_KEY）：
#   restrict,command="/usr/local/bin/hs-intake" ssh-ed25519 AAAA... hs-intake@github-actions
```

GHS 自动建站（`.github/workflows/ghs-site.yml`）另需：

```bash
install -d -m 0750 -o hs-intake -g root /var/lib/ghs-site/incoming
install -m 0755 ops/aliyun/ghs-site-install /usr/local/sbin/ghs-site-install     # 来自 GHS 仓库
echo 'hs-intake ALL=(root) NOPASSWD: /usr/local/sbin/ghs-site-install ""' > /etc/sudoers.d/ghs-site   # 只允许无参数
chmod 0440 /etc/sudoers.d/ghs-site && visudo -c
```

目标一览：`paho`、`nndss`（投递数据）、`ghs-data`（读出本来就公开的 GHS 站点数据）、`ghs-site`（投递构建好的网站包，
由 root 的安装脚本校验、切换、冒烟测试、失败自动回滚）。

`restrict` = no-pty、no-port/agent/X11-forwarding、no-user-rc；`command=` 让这把钥匙只能运行接收脚本。
接收脚本只认 `paho`（→ `/var/lib/health-sources/inbox/paho.jsonl`）和 `nndss`（→ `/var/lib/ghs-pipeline/raw/cdc_nndss_us.csv`），
校验格式、限制大小，原子替换，旧版本留作 `*.prev`。日志：`journalctl -t hs-intake`（syslog）。

撤销：删 `~hs-intake/.ssh/authorized_keys` 那一行（或 `userdel -r hs-intake`），删 `/etc/sudoers.d/ghs-site`，删 GitHub secrets。
回滚某次推送：`mv <dest>.prev <dest>`。
